"""Invoices created by Revenue Guardian after a human approval, plus the 'paid' confirmation loop.

Flow:  approval (create_paypal_invoice) -> invoice created + sent -> (optional) reminders ->
       INVOICING.INVOICE.PAID webhook -> invoice marked paid -> KPI.

Modes (settings.paypal_mode):
  mock     nothing leaves the process; ids look like MOCK-INV-...; payment can be simulated by an admin.
  sandbox  real calls to the PayPal sandbox Invoicing v2 API.

Provenance of a payment (why it counts or not):
  REAL_VERIFIED  a webhook whose signature PayPal itself confirmed   -> counts in the headline KPI
  MOCK / SIMULATED  mock-mode webhook or the 'simulate buyer payment' button -> shown, never counted

Honesty note on the KPI: 'paid after Guardian action' is a correlation (the customer paid after we
sent the invoice), not proof that Guardian caused the payment.

Sandbox request/response shapes follow PayPal's Invoicing v2 docs but have NOT yet been exercised
against the live sandbox from this codebase; the reminder endpoint in particular needs a live check.
"""
import sqlite3
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Dict, List, Optional

from config.settings import settings
from services import paypal_client
from services.audit_service import log_audit_event

DB_PATH = settings.sqlite_db_path

INVOICE_ACTIONS = {"create_paypal_invoice", "send_invoice_reminder"}
PAID_EVENT = "INVOICING.INVOICE.PAID"
CANCELLED_EVENT = "INVOICING.INVOICE.CANCELLED"
MAX_AMOUNT = 10_000.0          # sandbox demo ceiling
CFO_REQUIRED_AT = 500.0       # invoices at or above this can only be approved by the CFO
MAX_REMINDERS = 2             # per invoice
MIN_REMINDER_GAP_DAYS = 3

PENDING, SENT, PAID, CANCELLED, FAILED = "PENDING", "SENT", "PAID", "CANCELLED", "FAILED"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_invoice_database() -> None:
    conn = _connect()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS invoices (
            invoice_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL UNIQUE,
            request_id TEXT NOT NULL,
            paypal_invoice_id TEXT,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            customer_name TEXT,
            customer_email TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            description TEXT NOT NULL,
            due_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            paid_at TEXT,
            paid_provenance TEXT,
            paid_event_id TEXT,
            reminders_sent INTEGER NOT NULL DEFAULT 0,
            last_reminder_at TEXT,
            last_error TEXT)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_paypal_id ON invoices(paypal_invoice_id) WHERE paypal_invoice_id IS NOT NULL")
        if "payer_link" not in {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}:
            conn.execute("ALTER TABLE invoices ADD COLUMN payer_link TEXT")  # where the buyer pays; lets a judge finish the loop
        conn.commit()
    finally:
        conn.close()


init_invoice_database()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        found = conn.execute(sql, params).fetchone()
        return dict(found) if found else None
    finally:
        conn.close()


def _update(proposal_id: int, **fields: Any) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    conn = _connect()
    try:
        conn.execute(f"UPDATE invoices SET {columns} WHERE proposal_id = ?", (*fields.values(), proposal_id))
        conn.commit()
    finally:
        conn.close()


def get_invoice(proposal_id: int) -> Optional[Dict[str, Any]]:
    return _row("SELECT * FROM invoices WHERE proposal_id = ?", (proposal_id,))


def get_invoice_by_paypal_id(paypal_invoice_id: str) -> Optional[Dict[str, Any]]:
    return _row("SELECT * FROM invoices WHERE paypal_invoice_id = ?", (paypal_invoice_id,))


def list_invoices(status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        if status:
            rows = conn.execute("SELECT * FROM invoices WHERE status = ? ORDER BY invoice_row_id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM invoices ORDER BY invoice_row_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------- validation ----------

def validate_invoice_payload(payload: Dict[str, Any]) -> Optional[str]:
    """Return an error message, or None if the payload is acceptable."""
    email = payload.get("customer_email")
    if not isinstance(email, str) or "@" not in email or len(email) > 254 or any(c.isspace() for c in email):
        return "customer_email must be a valid email address"
    try:
        amount = float(payload.get("amount"))
    except (TypeError, ValueError):
        return "amount must be a number"
    if not (0 < amount <= MAX_AMOUNT):
        return f"amount must be greater than 0 and at most {MAX_AMOUNT:,.0f}"
    currency = payload.get("currency", "USD")
    if not (isinstance(currency, str) and len(currency) == 3 and currency.isalpha()):
        return "currency must be a 3-letter code"
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 200:
        return "description is required (max 200 characters)"
    try:
        days = int(payload.get("due_days", 14))
    except (TypeError, ValueError):
        return "due_days must be a whole number"
    if not (1 <= days <= 90):
        return "due_days must be between 1 and 90"
    return None


def validate_action_payload(action_type: str, payload: Dict[str, Any]) -> Optional[str]:
    """Checked when a proposal is created, so a bad request never reaches the approval queue. None means acceptable."""
    if action_type == "create_paypal_invoice":
        return validate_invoice_payload(payload)
    if action_type == "send_invoice_reminder":
        target = payload.get("invoice_proposal_id")
        row = get_invoice(target) if isinstance(target, int) else None
        if not row:
            return "invoice_proposal_id must refer to an invoice Revenue Guardian created"
        if row["status"] != SENT:
            return f"invoice is {row['status']}; reminders are only for unpaid, sent invoices"
    return None


def requires_cfo(action_type: str, payload: Dict[str, Any]) -> bool:
    """Large invoices need the CFO specifically; an unreadable amount is never trusted."""
    if action_type != "create_paypal_invoice":
        return False
    try:
        return float(payload.get("amount")) >= CFO_REQUIRED_AT
    except (TypeError, ValueError):
        return True


# ---------- PayPal calls ----------

def _invoice_id_from_create_response(body: Any) -> Optional[str]:
    """Create returns a link ({'href': '.../v2/invoicing/invoices/INV2-XXXX'}); the id is the last path segment."""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("id"), str) and body["id"]:
        return body["id"]
    href = body.get("href")
    if isinstance(href, str) and "/invoices/" in href:
        return href.rstrip("/").rsplit("/", 1)[-1] or None
    return None


def _recipient_name(row: Dict[str, Any]) -> Dict[str, str]:
    parts = (row["customer_name"] or row["customer_email"]).strip().split(" ", 1)
    return {"given_name": parts[0], "surname": parts[1] if len(parts) > 1 else "."}


def _build_invoice_body(row: Dict[str, Any]) -> Dict[str, Any]:
    reference = f"RG-{row['proposal_id']}"
    return {
        "detail": {
            "reference": reference,
            "currency_code": row["currency"],
            "note": f"{row['description']} (Revenue Guardian proposal {reference})",
            "invoice_date": date.today().isoformat(),
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": row["due_date"]},
        },
        "primary_recipients": [{"billing_info": {"name": _recipient_name(row), "email_address": row["customer_email"]}}],
        "items": [{
            "name": row["description"],
            "quantity": "1",
            "unit_amount": {"currency_code": row["currency"], "value": f"{row['amount']:.2f}"},
        }],
    }


def create_and_send_invoice(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Executor for an approved 'create_paypal_invoice'. Safe to call twice for the same proposal."""
    proposal_id = payload.get("proposal_id")
    if not isinstance(proposal_id, int):
        return {"status": "error", "message": "proposal_id is required"}
    problem = validate_invoice_payload(payload)
    if problem:
        return {"status": "error", "message": problem}

    existing = get_invoice(proposal_id)
    if existing and existing["status"] in (SENT, PAID):
        return {"status": "success", "mode": existing["mode"], "message": "Invoice already sent for this proposal.", "invoice": existing}

    mode = "sandbox" if settings.paypal_mode == "sandbox" else "mock"
    if not existing:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO invoices (proposal_id, request_id, mode, status, customer_name, customer_email, amount, currency,
                   description, due_date, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (proposal_id, str(uuid.uuid4()), mode, PENDING, payload.get("customer_name") or "", payload["customer_email"].strip(),
                 round(float(payload["amount"]), 2), payload.get("currency", "USD").upper(), payload["description"].strip(),
                 (date.today() + timedelta(days=int(payload.get("due_days", 14)))).isoformat(), _now()),
            )
            conn.commit()
        finally:
            conn.close()
    row = get_invoice(proposal_id)

    if mode == "mock":
        paypal_id = row["paypal_invoice_id"] or f"MOCK-INV-{proposal_id}-{row['request_id'][:6]}"
        _update(proposal_id, paypal_invoice_id=paypal_id, status=SENT, sent_at=_now(), last_error=None)
        log_audit_event("invoice_service", "invoice_sent", "success", {"proposal_id": proposal_id, "invoice_id": paypal_id, "mode": "mock"})
        return {"status": "success", "mode": "mock", "message": "Mock invoice created and 'sent' (nothing left this process).", "invoice": get_invoice(proposal_id)}

    try:
        paypal_id = row["paypal_invoice_id"]
        if not paypal_id:
            code, body = paypal_client.api_request("POST", "/v2/invoicing/invoices", json_body=_build_invoice_body(row), request_id=f"create-{row['request_id']}")
            paypal_id = _invoice_id_from_create_response(body) if code in (200, 201) else None
            if not paypal_id:
                raise paypal_client.PayPalUnavailable(f"Create invoice returned HTTP {code}: {str(body)[:300]}")
            _update(proposal_id, paypal_invoice_id=paypal_id)  # saved before sending, so a retry resumes instead of duplicating
        code, body = paypal_client.api_request(
            "POST", f"/v2/invoicing/invoices/{paypal_id}/send",
            json_body={"send_to_recipient": True, "send_to_invoicer": False}, request_id=f"send-{row['request_id']}")
        if code not in (200, 202):
            raise paypal_client.PayPalUnavailable(f"Send invoice returned HTTP {code}: {str(body)[:300]}")
    except (paypal_client.PayPalNotConfigured, paypal_client.PayPalUnavailable) as exc:
        _update(proposal_id, status=FAILED, last_error=str(exc)[:500])
        log_audit_event("invoice_service", "invoice_send_failed", "failed", {"proposal_id": proposal_id, "reason": str(exc)[:300]})
        return {"status": "error", "message": f"PayPal invoice not sent: {exc}"}

    payer_link = None
    try:  # convenience only: the invoice is already sent, so a failed lookup must not fail the approval
        code, invoice = paypal_client.api_request("GET", f"/v2/invoicing/invoices/{paypal_id}")
        if code == 200 and isinstance(invoice, dict):
            payer_link = ((invoice.get("detail") or {}).get("metadata") or {}).get("recipient_view_url")
    except (paypal_client.PayPalNotConfigured, paypal_client.PayPalUnavailable):
        pass
    _update(proposal_id, status=SENT, sent_at=_now(), last_error=None, payer_link=payer_link)
    log_audit_event("invoice_service", "invoice_sent", "success", {"proposal_id": proposal_id, "invoice_id": paypal_id, "mode": "sandbox"})
    return {"status": "success", "mode": "sandbox", "message": "PayPal sandbox invoice created and sent.", "invoice": get_invoice(proposal_id)}


def send_reminder(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Executor for an approved 'send_invoice_reminder' (payload: invoice_proposal_id, optional note)."""
    target_id = payload.get("invoice_proposal_id")
    row = get_invoice(target_id) if isinstance(target_id, int) else None
    if not row:
        return {"status": "error", "message": "Unknown invoice for this reminder."}
    if row["status"] != SENT:
        return {"status": "error", "message": f"Invoice is {row['status']}; reminders are only sent for unpaid, sent invoices."}
    if row["reminders_sent"] >= MAX_REMINDERS:
        return {"status": "error", "message": f"Reminder limit reached ({MAX_REMINDERS} per invoice)."}
    note = str(payload.get("note") or "Friendly reminder: this invoice is still open.")[:500]
    if row["mode"] == "sandbox":
        try:
            code, body = paypal_client.api_request(
                "POST", f"/v2/invoicing/invoices/{row['paypal_invoice_id']}/remind",
                json_body={"subject": "Payment reminder", "note": note, "send_to_invoicer": False})
        except (paypal_client.PayPalNotConfigured, paypal_client.PayPalUnavailable) as exc:
            return {"status": "error", "message": f"Reminder not sent: {exc}"}
        if code not in (200, 202, 204):
            return {"status": "error", "message": f"Reminder not sent: PayPal returned HTTP {code}"}
    _update(row["proposal_id"], reminders_sent=row["reminders_sent"] + 1, last_reminder_at=_now())
    log_audit_event("invoice_service", "invoice_reminder_sent", "success", {"invoice_proposal_id": row["proposal_id"], "mode": row["mode"]})
    return {"status": "success", "mode": row["mode"], "message": "Reminder sent.", "invoice": get_invoice(row["proposal_id"])}


# ---------- the confirmation loop ----------

def _event_invoice_ids(event: Dict[str, Any]) -> tuple:
    resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
    invoice = resource.get("invoice") if isinstance(resource.get("invoice"), dict) else {}
    detail = invoice.get("detail") if isinstance(invoice.get("detail"), dict) else {}
    paypal_id = invoice.get("id") or resource.get("id")
    reference = detail.get("reference")
    return (str(paypal_id) if paypal_id else None, str(reference) if reference else None)


def handle_event(event: Dict[str, Any], provenance: str) -> str:
    """React to a stored, accepted webhook. Returns a short outcome label. Never raises."""
    try:
        if event.get("event_type") not in (PAID_EVENT, CANCELLED_EVENT):
            return "ignored"
        paypal_id, _reference = _event_invoice_ids(event)
        row = get_invoice_by_paypal_id(paypal_id) if paypal_id else None
        # We match on PayPal's invoice id only. The RG-<n> reference restarts after a database reset, so it
        # is shown in audit details but never trusted to pick a row.
        if not row:
            log_audit_event("invoice_service", "event_unknown_invoice", "ignored", {"event_id": event.get("id"), "invoice_id": paypal_id})
            return "unknown_invoice"
        if event["event_type"] == CANCELLED_EVENT:
            if row["status"] == SENT:
                _update(row["proposal_id"], status=CANCELLED)
            return "cancelled"
        if row["status"] == PAID:
            return "already_paid"
        if row["status"] != SENT:
            log_audit_event("invoice_service", "event_invoice_not_sent", "ignored", {"event_id": event.get("id"), "status": row["status"]})
            return "not_sent"
        _update(row["proposal_id"], status=PAID, paid_at=_now(), paid_provenance=provenance, paid_event_id=str(event.get("id")))
        log_audit_event("invoice_service", "invoice_paid", "success", {"proposal_id": row["proposal_id"], "invoice_id": paypal_id, "provenance": provenance, "event_id": event.get("id")})
        return "paid"
    except Exception as exc:  # a bug here must never turn a stored webhook into a 500 (PayPal would retry forever)
        log_audit_event("invoice_service", "event_handler_error", "error", {"event_id": event.get("id"), "error": f"{type(exc).__name__}: {exc}"})
        return "error"


def simulate_payment(proposal_id: int, actor: str) -> Dict[str, Any]:
    """Mock-mode demo button. Marked SIMULATED, so it never counts toward the real KPI."""
    if settings.paypal_mode != "mock":
        return {"status": "error", "message": "Simulated payments are only available in mock mode."}
    row = get_invoice(proposal_id)
    if not row or row["mode"] != "mock":
        return {"status": "error", "message": "No mock invoice for that proposal."}
    if row["status"] != SENT:
        return {"status": "error", "message": f"Invoice is {row['status']}, not SENT."}
    _update(proposal_id, status=PAID, paid_at=_now(), paid_provenance="SIMULATED", paid_event_id=f"SIM-{uuid.uuid4().hex[:8]}")
    log_audit_event(actor, "invoice_payment_simulated", "success", {"proposal_id": proposal_id})
    return {"status": "success", "invoice": get_invoice(proposal_id)}


# ---------- overdue sweep + KPIs ----------

def _reminder_gap() -> timedelta:
    minutes = settings.demo_overdue_minutes
    return timedelta(minutes=minutes) if minutes > 0 else timedelta(days=MIN_REMINDER_GAP_DAYS)


def overdue_invoices(today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Unpaid invoices past their due date. With DEMO_OVERDUE_MINUTES > 0, invoices unpaid for that long also count
    (a labelled demo threshold, so a sweep can be shown without waiting for a real due date)."""
    today = today or date.today()
    now = datetime.now(UTC)
    gap_cutoff = (now - _reminder_gap()).isoformat()
    demo_minutes = settings.demo_overdue_minutes
    demo_cutoff = (now - timedelta(minutes=demo_minutes)).isoformat() if demo_minutes > 0 else None
    result = []
    for r in list_invoices(status=SENT):
        past_due = r["due_date"] < today.isoformat() or bool(demo_cutoff and r["sent_at"] and r["sent_at"] < demo_cutoff)
        reminder_ok = not r["last_reminder_at"] or r["last_reminder_at"] < gap_cutoff
        if past_due and reminder_ok and r["reminders_sent"] < MAX_REMINDERS:
            result.append(r)
    return result


def propose_overdue_reminders(requested_by: str) -> List[Dict[str, Any]]:
    """Create one pending approval per overdue invoice that has none yet. Humans still decide."""
    from services.approval_service import create_approval, list_approvals

    already = {r.payload.get("invoice_proposal_id") for r in list_approvals(status="pending", limit=500) if r.action_type == "send_invoice_reminder"}
    created = []
    for row in overdue_invoices():
        if row["proposal_id"] in already:
            continue
        record = create_approval(
            action_type="send_invoice_reminder",
            target=row["customer_email"],
            payload={"invoice_proposal_id": row["proposal_id"], "note": f"Reminder: invoice for {row['currency']} {row['amount']:.2f} was due {row['due_date']}."},
            requested_by=requested_by,
            reason=f"Invoice overdue since {row['due_date']}.",
        )
        created.append(record.model_dump())
    return created


def kpis() -> Dict[str, Any]:
    rows = list_invoices(limit=10000)
    sent = [r for r in rows if r["status"] in (SENT, PAID)]
    paid = [r for r in rows if r["status"] == PAID]
    verified = [r for r in paid if r["paid_provenance"] == "REAL_VERIFIED"]
    return {
        "invoices_sent": len(sent),
        "invoices_paid": len(paid),
        "paid_after_guardian_action": len(verified),  # real, PayPal-verified only; correlation, not causation
        "amount_paid_verified": round(sum(r["amount"] for r in verified), 2),
        "paid_unverified_or_simulated": len(paid) - len(verified),
        "outstanding_count": len(sent) - len(paid),
        "outstanding_amount": round(sum(r["amount"] for r in sent if r["status"] == SENT), 2),
        "reminders_sent": sum(r["reminders_sent"] for r in rows),
        "note": "Paid after Guardian action counts only PayPal-verified payments and shows correlation, not causation.",
    }
