"""Review hardening: proposal-time validation, CFO threshold, reminder cap, payer link, demo overdue threshold."""
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import main
from config.settings import settings
from security.auth import create_access_token
from services import invoice_service as inv
from services import paypal_client
from services.approval_service import create_approval, decide_approval

client = TestClient(main.app)
CFO = {"username": "cfo", "role": "CFO"}
REVOPS = {"username": "admin", "role": "RevOps_Manager"}
PAYLOAD = {"customer_email": "ap@acme.test", "customer_name": "Acme AP", "amount": 100, "currency": "USD", "description": "Overdue overage", "due_days": 14}


def _h(username, role):
    token = create_access_token({"sub": username, "role": role}, expires_delta=timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def _propose(payload, action="create_paypal_invoice", role=("sales", "Sales_Rep")):
    return client.post("/api/approvals", json={"action_type": action, "target": "Acme", "payload": payload}, headers=_h(*role))


def _decide(approval_id, role):
    return client.post(f"/api/approvals/{approval_id}/decision", json={"decision": "approved"}, headers=_h(*role))


def _sent_mock_invoice(amount=100):
    record = create_approval("create_paypal_invoice", "Acme", {**PAYLOAD, "amount": amount}, requested_by="agent", reason="r")
    return decide_approval(record.approval_id, "approved", CFO)


# ---------- validation when the proposal is created ----------
@pytest.mark.parametrize("change", [{"amount": 10_000.01}, {"amount": 0}, {"customer_email": "nope"}, {"due_days": 0}, {"description": ""}])
def test_bad_invoice_is_rejected_before_it_reaches_the_queue(change):
    response = _propose({**PAYLOAD, **change})
    assert response.status_code == 400 and "Policy check failed" in response.json()["detail"]
    assert client.get("/api/approvals", headers=_h("cfo", "CFO")).json() == []


def test_reminder_proposal_must_point_at_a_sent_invoice():
    assert _propose({"invoice_proposal_id": 999}, "send_invoice_reminder").status_code == 400
    done = _sent_mock_invoice()
    assert _propose({"invoice_proposal_id": done.approval_id}, "send_invoice_reminder").status_code == 200
    inv.simulate_payment(done.approval_id, "tester")
    assert _propose({"invoice_proposal_id": done.approval_id}, "send_invoice_reminder").status_code == 400  # now PAID


# ---------- CFO threshold ----------
def test_invoice_under_threshold_can_be_approved_by_revops():
    created = _propose({**PAYLOAD, "amount": 499.99}).json()
    assert _decide(created["approval_id"], ("admin", "RevOps_Manager")).status_code == 200


def test_invoice_at_threshold_needs_the_cfo():
    created = _propose({**PAYLOAD, "amount": 500}).json()
    denied = _decide(created["approval_id"], ("admin", "RevOps_Manager"))
    assert denied.status_code == 403 and "CFO" in denied.json()["detail"]
    assert client.get("/api/approvals?status=pending", headers=_h("cfo", "CFO")).json()[0]["approval_id"] == created["approval_id"]
    assert _decide(created["approval_id"], ("cfo", "CFO")).status_code == 200


def test_unreadable_amount_is_treated_as_cfo_only():
    assert inv.requires_cfo("create_paypal_invoice", {"amount": "abc"}) is True
    assert inv.requires_cfo("send_invoice_reminder", {}) is False


# ---------- reminder cap ----------
def test_third_reminder_is_refused():
    done = _sent_mock_invoice()
    results = []
    for _ in range(3):
        record = create_approval("send_invoice_reminder", "Acme", {"invoice_proposal_id": done.approval_id}, requested_by="agent", reason="r")
        results.append(decide_approval(record.approval_id, "approved", CFO))
    assert [r.status for r in results] == ["approved", "approved", "failed"]
    assert "limit" in results[2].execution_result["message"].lower()
    assert inv.get_invoice(done.approval_id)["reminders_sent"] == 2


# ---------- payer link + request shape (sandbox, no network) ----------
class FakePayPal:
    def __init__(self, link="https://www.sandbox.paypal.com/invoice/p/#ABC", get_error=None):
        self.calls, self.link, self.get_error = [], link, get_error

    def __call__(self, method, path, *, json_body=None, request_id=None):
        self.calls.append((method, path, json_body, request_id))
        if path == "/v2/invoicing/invoices" and method == "POST":
            return 201, {"href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-LINK"}
        if path.endswith("/send"):
            return 202, {}
        if method == "GET":
            if self.get_error:
                raise self.get_error
            return 200, {"detail": {"metadata": {"recipient_view_url": self.link}}}
        return 404, {}


def test_payer_link_is_stored_and_listed(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    monkeypatch.setattr(paypal_client, "api_request", FakePayPal())
    done = _sent_mock_invoice()
    assert done.status == "approved"
    assert inv.get_invoice(done.approval_id)["payer_link"] == "https://www.sandbox.paypal.com/invoice/p/#ABC"
    assert client.get("/api/invoices", headers=_h("cfo", "CFO")).json()[0]["payer_link"].endswith("#ABC")


def test_failed_link_lookup_does_not_fail_an_already_sent_invoice(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    monkeypatch.setattr(paypal_client, "api_request", FakePayPal(get_error=paypal_client.PayPalUnavailable("slow")))
    done = _sent_mock_invoice()
    row = inv.get_invoice(done.approval_id)
    assert done.status == "approved" and row["status"] == "SENT" and row["payer_link"] is None


def test_request_body_has_term_type_and_split_name(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    fake = FakePayPal()
    monkeypatch.setattr(paypal_client, "api_request", fake)
    _sent_mock_invoice()
    body = fake.calls[0][2]
    assert body["detail"]["payment_term"]["term_type"] == "DUE_ON_DATE_SPECIFIED"
    assert body["primary_recipients"][0]["billing_info"]["name"] == {"given_name": "Acme", "surname": "AP"}


# ---------- demo overdue threshold ----------
def _age(proposal_id, field, minutes):
    inv._update(proposal_id, **{field: (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()})


def test_demo_threshold_makes_a_fresh_invoice_overdue_only_when_enabled(monkeypatch):
    done = _sent_mock_invoice()
    _age(done.approval_id, "sent_at", 5)
    assert inv.overdue_invoices() == []                       # due date is 14 days away, threshold off
    monkeypatch.setattr(settings, "demo_overdue_minutes", 1)
    assert [r["proposal_id"] for r in inv.overdue_invoices()] == [done.approval_id]
    sweep = client.post("/api/invoices/sweep", headers=_h("cfo", "CFO")).json()
    assert sweep["proposed"] == 1 and sweep["demo_threshold_minutes"] == 1


def test_demo_threshold_respects_short_reminder_gap(monkeypatch):
    monkeypatch.setattr(settings, "demo_overdue_minutes", 1)
    done = _sent_mock_invoice()
    _age(done.approval_id, "sent_at", 10)
    inv._update(done.approval_id, reminders_sent=1, last_reminder_at=datetime.now(UTC).isoformat())
    assert inv.overdue_invoices() == []                       # reminded just now
    _age(done.approval_id, "last_reminder_at", 5)
    assert len(inv.overdue_invoices()) == 1
    inv._update(done.approval_id, reminders_sent=2)
    assert inv.overdue_invoices() == []                       # cap reached


def test_old_database_without_payer_link_is_migrated():
    conn = sqlite3.connect(inv.DB_PATH)
    conn.execute("DROP TABLE invoices")
    conn.execute("""CREATE TABLE invoices (invoice_row_id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL UNIQUE, request_id TEXT NOT NULL,
        paypal_invoice_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL, customer_name TEXT, customer_email TEXT NOT NULL, amount REAL NOT NULL,
        currency TEXT NOT NULL, description TEXT NOT NULL, due_date TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, paid_at TEXT,
        paid_provenance TEXT, paid_event_id TEXT, reminders_sent INTEGER NOT NULL DEFAULT 0, last_reminder_at TEXT, last_error TEXT)""")
    conn.commit(); conn.close()
    inv.init_invoice_database()
    conn = sqlite3.connect(inv.DB_PATH)
    assert "payer_link" in {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
    conn.close()


def test_demo_minutes_out_of_range_is_refused(monkeypatch):
    import config.settings as cs
    monkeypatch.setenv("DEMO_OVERDUE_MINUTES", "99999")
    with pytest.raises(RuntimeError):
        cs.get_settings()
