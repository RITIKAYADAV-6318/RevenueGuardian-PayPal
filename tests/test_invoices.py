"""Invoice flow: approval -> create+send -> reminders -> PAID webhook -> KPI. No network."""
import json
from datetime import date, timedelta

import pytest

from config.settings import settings
from services import invoice_service as inv
from services import paypal_client, webhook_service
from services.approval_service import create_approval, decide_approval
from services.audit_service import verify_audit_chain

CFO = {"username": "cfo", "role": "CFO"}
PAYLOAD = {"customer_email": "ap@acme.test", "customer_name": "Acme AP", "amount": 1250.5, "currency": "USD", "description": "Overdue Q3 overage", "due_days": 14}


class FakePayPal:
    def __init__(self):
        self.calls = []
        self.send_status = 202
        self.create_status = 201

    def __call__(self, method, path, *, json_body=None, request_id=None):
        self.calls.append((method, path, json_body, request_id))
        if path == "/v2/invoicing/invoices":
            return self.create_status, {"rel": "self", "href": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-ABC", "method": "GET"}
        if path.endswith("/send"):
            return self.send_status, {}
        if path.endswith("/remind"):
            return 204, {}
        return 404, {}


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    fake = FakePayPal()
    monkeypatch.setattr(paypal_client, "api_request", fake)
    return fake


def _approve_invoice(payload=None):
    record = create_approval("create_paypal_invoice", "Acme", payload or dict(PAYLOAD), requested_by="agent", reason="overdue")
    return decide_approval(record.approval_id, "approved", CFO)


def _paid_event(invoice_id="INV2-ABC", event_id="WH-PAID-1"):
    return json.dumps({"id": event_id, "event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": invoice_id, "detail": {"reference": "RG-1"}}}}).encode()


# ---------- creating and sending ----------

def test_mock_mode_invoice_is_created_without_any_network_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("mock mode must not call PayPal")
    monkeypatch.setattr(paypal_client, "api_request", boom)
    done = _approve_invoice()
    assert done.status == "approved" and done.execution_result["mode"] == "mock"
    row = inv.get_invoice(done.approval_id)
    assert row["status"] == "SENT" and row["paypal_invoice_id"].startswith("MOCK-INV-")


def test_sandbox_invoice_request_shape_and_idempotency_key(sandbox):
    done = _approve_invoice()
    assert done.status == "approved"
    assert [c[:2] for c in sandbox.calls[2:]] == [("GET", "/v2/invoicing/invoices/INV2-ABC")]  # payer-link lookup after sending
    (m1, p1, body, rid1), (m2, p2, _, rid2) = sandbox.calls[:2]
    assert (m1, p1) == ("POST", "/v2/invoicing/invoices") and p2 == "/v2/invoicing/invoices/INV2-ABC/send"
    assert body["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "1250.50"}
    assert body["detail"]["reference"] == f"RG-{done.approval_id}"
    assert body["detail"]["payment_term"]["due_date"] == (date.today() + timedelta(days=14)).isoformat()
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "ap@acme.test"
    assert rid1 and rid2 and rid1 != rid2
    assert inv.get_invoice(done.approval_id)["status"] == "SENT"


def test_calling_the_executor_twice_does_not_create_a_second_invoice(sandbox):
    done = _approve_invoice()
    again = inv.create_and_send_invoice({**PAYLOAD, "proposal_id": done.approval_id})
    assert again["status"] == "success" and "already" in again["message"]
    assert len([c for c in sandbox.calls if c[1] == "/v2/invoicing/invoices"]) == 1


def test_failed_send_is_resumed_without_creating_a_duplicate(sandbox):
    sandbox.send_status = 400
    failed = _approve_invoice()
    assert failed.status == "failed" and inv.get_invoice(failed.approval_id)["status"] == "FAILED"
    sandbox.send_status = 202
    result = inv.create_and_send_invoice({**PAYLOAD, "proposal_id": failed.approval_id})
    assert result["status"] == "success"
    assert len([c for c in sandbox.calls if c[1] == "/v2/invoicing/invoices"]) == 1  # create ran once, send ran twice


def test_paypal_outage_marks_approval_failed_not_stuck(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")

    def down(*a, **k):
        raise paypal_client.PayPalUnavailable("network down")
    monkeypatch.setattr(paypal_client, "api_request", down)
    done = _approve_invoice()
    assert done.status == "failed" and "network down" in done.execution_result["message"]


@pytest.mark.parametrize("change", [
    {"customer_email": "not-an-email"}, {"amount": 0}, {"amount": -5}, {"amount": "abc"}, {"amount": 5_000_000},
    {"currency": "DOLLARS"}, {"description": ""}, {"due_days": 0}, {"due_days": 500},
])
def test_invalid_payload_is_refused_before_anything_is_stored(change):
    done = _approve_invoice({**PAYLOAD, **change})
    assert done.status == "failed"
    assert inv.list_invoices() == []


def test_sales_rep_cannot_approve_an_invoice():
    record = create_approval("create_paypal_invoice", "Acme", dict(PAYLOAD), requested_by="agent")
    with pytest.raises(PermissionError):
        decide_approval(record.approval_id, "approved", {"username": "sales", "role": "Sales_Rep"})
    assert inv.list_invoices() == []


# ---------- reminders ----------

def test_reminder_for_sent_invoice_updates_counters(sandbox):
    done = _approve_invoice()
    rem = create_approval("send_invoice_reminder", "ap@acme.test", {"invoice_proposal_id": done.approval_id}, requested_by="agent")
    out = decide_approval(rem.approval_id, "approved", CFO)
    assert out.status == "approved"
    assert sandbox.calls[-1][1].endswith("/INV2-ABC/remind")
    row = inv.get_invoice(done.approval_id)
    assert row["reminders_sent"] == 1 and row["last_reminder_at"]


def test_reminder_refused_for_paid_or_unknown_invoice():
    assert inv.send_reminder({"invoice_proposal_id": 999})["status"] == "error"
    done = _approve_invoice()
    inv.simulate_payment(done.approval_id, "admin")
    assert inv.send_reminder({"invoice_proposal_id": done.approval_id})["status"] == "error"


def test_overdue_sweep_proposes_once_and_respects_reminder_gap():
    done = _approve_invoice()
    inv._update(done.approval_id, due_date=(date.today() - timedelta(days=5)).isoformat())
    first = inv.propose_overdue_reminders("guardian")
    assert len(first) == 1 and first[0]["status"] == "pending"
    assert inv.propose_overdue_reminders("guardian") == []  # already has a pending proposal
    not_due = _approve_invoice()
    assert all(r["proposal_id"] != not_due.approval_id for r in inv.overdue_invoices())


# ---------- the paid loop ----------

def test_verified_paid_webhook_marks_invoice_paid_and_counts_in_kpi(sandbox, monkeypatch):
    done = _approve_invoice()
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: True)
    status, body = webhook_service.process_webhook({"paypal-transmission-id": "t"}, _paid_event())
    assert status == 200 and body["provenance"] == "REAL_VERIFIED"
    row = inv.get_invoice(done.approval_id)
    assert row["status"] == "PAID" and row["paid_provenance"] == "REAL_VERIFIED"
    k = inv.kpis()
    assert k["paid_after_guardian_action"] == 1 and k["amount_paid_verified"] == 1250.5 and k["outstanding_count"] == 0
    assert verify_audit_chain()["valid"] is True


def test_duplicate_delivery_does_not_double_count(sandbox, monkeypatch):
    _approve_invoice()
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: True)
    webhook_service.process_webhook({}, _paid_event())
    assert webhook_service.process_webhook({}, _paid_event())[1]["status"] == "duplicate"
    assert inv.kpis()["paid_after_guardian_action"] == 1


def test_unverified_paid_event_never_marks_anything_paid(sandbox, monkeypatch):
    done = _approve_invoice()
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: False)
    status, _ = webhook_service.process_webhook({}, _paid_event())
    assert status == 400
    assert inv.get_invoice(done.approval_id)["status"] == "SENT"
    assert inv.kpis()["paid_after_guardian_action"] == 0


def test_paid_event_for_unknown_invoice_is_ignored_safely(sandbox, monkeypatch):
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: True)
    status, _ = webhook_service.process_webhook({}, _paid_event(invoice_id="INV2-NOT-OURS"))
    assert status == 200
    assert inv.kpis()["invoices_paid"] == 0


def test_reference_alone_never_picks_an_invoice(sandbox, monkeypatch):
    done = _approve_invoice()
    event = json.dumps({"id": "WH-X", "event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": "INV2-OTHER", "detail": {"reference": f"RG-{done.approval_id}"}}}}).encode()
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: True)
    webhook_service.process_webhook({}, event)
    assert inv.get_invoice(done.approval_id)["status"] == "SENT"


def test_handler_bug_cannot_break_webhook_response(sandbox, monkeypatch):
    _approve_invoice()
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: True)
    monkeypatch.setattr(inv, "get_invoice_by_paypal_id", lambda _id: (_ for _ in ()).throw(RuntimeError("db exploded")))
    assert webhook_service.process_webhook({}, _paid_event())[0] == 200


def test_simulated_payment_is_shown_but_not_counted_as_real():
    done = _approve_invoice()
    assert inv.simulate_payment(done.approval_id, "admin")["status"] == "success"
    k = inv.kpis()
    assert k["invoices_paid"] == 1 and k["paid_after_guardian_action"] == 0 and k["paid_unverified_or_simulated"] == 1
    assert inv.simulate_payment(done.approval_id, "admin")["status"] == "error"  # not SENT any more


def test_simulate_is_refused_in_sandbox_mode(sandbox):
    done = _approve_invoice()
    assert inv.simulate_payment(done.approval_id, "admin")["status"] == "error"
    assert inv.get_invoice(done.approval_id)["status"] == "SENT"


def test_cancelled_event_marks_sent_invoice_cancelled(sandbox, monkeypatch):
    done = _approve_invoice()
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", lambda headers, raw: True)
    event = json.dumps({"id": "WH-C", "event_type": "INVOICING.INVOICE.CANCELLED", "resource": {"invoice": {"id": "INV2-ABC"}}}).encode()
    webhook_service.process_webhook({}, event)
    assert inv.get_invoice(done.approval_id)["status"] == "CANCELLED"


# ---------- routes (need fastapi; skipped where it is not installed) ----------

def _client():
    pytest.importorskip("fastapi")
    from datetime import timedelta as td
    from fastapi.testclient import TestClient
    import main
    from security.auth import create_access_token
    def h(user, role):
        return {"Authorization": "Bearer " + create_access_token({"sub": user, "role": role}, expires_delta=td(minutes=5))}
    return TestClient(main.app), h


def test_invoice_routes_require_tokens_and_roles():
    client, h = _client()
    for path in ("/api/invoices", "/api/kpis"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=h("sales", "Sales_Rep")).status_code == 200
    assert client.post("/api/invoices/sweep").status_code == 401
    assert client.post("/api/invoices/sweep", headers=h("sales", "Sales_Rep")).status_code == 403
    assert client.post("/api/invoices/1/simulate-payment", headers=h("sales", "Sales_Rep")).status_code == 403
    assert client.post("/api/invoices/999/simulate-payment", headers=h("cfo", "CFO")).status_code == 409


def test_invoice_approval_end_to_end_through_the_api():
    client, h = _client()
    body = {"action_type": "create_paypal_invoice", "target": "Acme", "payload": dict(PAYLOAD)}
    created = client.post("/api/approvals", json=body, headers=h("sales", "Sales_Rep")).json()
    assert client.post(f"/api/approvals/{created['approval_id']}/decision", json={"decision": "approved"}, headers=h("sales", "Sales_Rep")).status_code == 403
    ok = client.post(f"/api/approvals/{created['approval_id']}/decision", json={"decision": "approved"}, headers=h("cfo", "CFO"))
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert client.post(f"/api/invoices/{created['approval_id']}/simulate-payment", headers=h("cfo", "CFO")).status_code == 200
    assert client.get("/api/kpis", headers=h("cfo", "CFO")).json()["invoices_paid"] == 1
