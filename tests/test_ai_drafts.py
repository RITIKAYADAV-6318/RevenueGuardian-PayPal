"""AI-drafted reminders: Gemini output is validated, any failure falls back to a template, no PII is sent."""
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from config.settings import settings
from security.auth import create_access_token
from services import ai_drafts
from services import invoice_service as inv
from services.approval_service import create_approval, decide_approval

client = TestClient(main.app)
CFO = {"username": "cfo", "role": "CFO"}
ROW = {"customer_name": "Priya Sharma", "customer_email": "priya@secret-customer.test", "amount": 250.0, "currency": "USD",
       "due_date": (datetime.now(UTC).date() - timedelta(days=4)).isoformat(), "reminders_sent": 0}


def _h():
    return {"Authorization": "Bearer " + create_access_token({"sub": "cfo", "role": "CFO"}, expires_delta=timedelta(minutes=5))}


def _gemini(payload_text=None, status=200, calls=None, error=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if error:
            raise error
        text = payload_text if payload_text is not None else json.dumps({"note": "Hi Priya, gentle nudge about your open invoice. Thank you!", "tone": "friendly", "rationale": "First reminder, so a warm tone."})
        return httpx.Response(status, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
    return httpx.MockTransport(handler)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "model_name", "gemini-test")
    yield
    monkeypatch.setattr(ai_drafts, "_transport", None)


# ---------- fallback ----------
def test_without_a_key_the_template_is_used(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    friendly = ai_drafts.draft_reminder(ROW)
    firm = ai_drafts.draft_reminder({**ROW, "reminders_sent": 1})
    assert friendly["ai"] == {"used": False, "provider": "fallback", "model": None, "error": "GEMINI_API_KEY not set"}
    assert friendly["tone"] == "friendly" and "Priya" in friendly["note"] and "250.00" in friendly["note"]
    assert firm["tone"] == "firm" and "4 days overdue" in firm["note"]


def test_missing_customer_name_is_handled():
    draft = ai_drafts.fallback_draft({**ROW, "customer_name": None})
    assert draft["note"].startswith("Hi there,")


# ---------- Gemini path ----------
def test_gemini_draft_is_used_and_request_is_safe(key, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_drafts, "_transport", _gemini(calls=calls))
    draft = ai_drafts.draft_reminder(ROW)
    assert draft["note"].startswith("Hi Priya") and draft["tone"] == "friendly"
    assert draft["ai"] == {"used": True, "provider": "gemini", "model": "gemini-test", "error": None}
    request = calls[0]
    assert "gemini-test:generateContent" in str(request.url)
    assert request.headers["x-goog-api-key"] == "test-key" and "test-key" not in str(request.url)  # key in header, never in the URL
    body = request.content.decode()
    assert "secret-customer.test" not in body and "priya@" not in body            # no customer email goes to the model
    assert "250.00" in body and "Priya" in body                                      # only the facts it needs
    assert json.loads(body)["generationConfig"]["responseSchema"]["required"] == ["note", "tone", "rationale"]


@pytest.mark.parametrize("bad", [
    "not json at all",
    json.dumps({"note": "x", "tone": "friendly"}),                                          # missing rationale
    json.dumps({"note": "x", "tone": "angry", "rationale": "y"}),                            # tone outside the allowed set
    json.dumps({"note": "Pay now at http://evil.test", "tone": "firm", "rationale": "y"}),   # links are rejected
    json.dumps({"note": "Write to me at a@b.c", "tone": "firm", "rationale": "y"}),          # email addresses are rejected
    json.dumps({"note": "x" * 401, "tone": "firm", "rationale": "y"}),                       # too long
    json.dumps(["list", "not", "object"]),
    json.dumps({"note": "  ", "tone": "firm", "rationale": "y"}),                            # blank
])
def test_invalid_model_output_falls_back(key, monkeypatch, bad):
    monkeypatch.setattr(ai_drafts, "_transport", _gemini(payload_text=bad))
    draft = ai_drafts.draft_reminder(ROW)
    assert draft["ai"]["used"] is False and draft["ai"]["provider"] == "fallback" and draft["ai"]["error"]
    assert draft["note"].startswith("Hi Priya, a friendly reminder")


@pytest.mark.parametrize("kwargs", [{"status": 500}, {"status": 429}, {"status": 404}, {"error": httpx.ConnectError("down")}, {"error": httpx.ReadTimeout("slow")}])
def test_gemini_failures_fall_back(key, monkeypatch, kwargs):
    monkeypatch.setattr(ai_drafts, "_transport", _gemini(**kwargs))
    draft = ai_drafts.draft_reminder(ROW)
    assert draft["ai"]["used"] is False and draft["tone"] == "friendly"


def test_malformed_gemini_envelope_falls_back(key, monkeypatch):
    monkeypatch.setattr(ai_drafts, "_transport", httpx.MockTransport(lambda r: httpx.Response(200, json={"candidates": []})))
    assert ai_drafts.draft_reminder(ROW)["ai"]["used"] is False


def test_daily_cap_switches_to_fallback(key, monkeypatch):
    monkeypatch.setattr(settings, "ai_daily_call_cap", 2)
    calls = []
    monkeypatch.setattr(ai_drafts, "_transport", _gemini(calls=calls))
    results = [ai_drafts.draft_reminder(ROW) for _ in range(4)]
    assert [r["ai"]["used"] for r in results] == [True, True, False, False]
    assert len(calls) == 2 and results[2]["ai"]["error"] == "daily AI call cap reached"


# ---------- wired into the sweep ----------
def _overdue_mock_invoice(monkeypatch):
    monkeypatch.setattr(settings, "demo_overdue_minutes", 1)
    record = create_approval("create_paypal_invoice", "Acme", {"customer_email": "ap@acme.test", "customer_name": "Acme AP", "amount": 100, "description": "Overage", "due_days": 14}, requested_by="agent", reason="r")
    done = decide_approval(record.approval_id, "approved", CFO)
    inv._update(done.approval_id, sent_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat())
    return done


def test_sweep_uses_the_ai_draft_and_reports_it(key, monkeypatch):
    monkeypatch.setattr(ai_drafts, "_transport", _gemini())
    _overdue_mock_invoice(monkeypatch)
    out = client.post("/api/invoices/sweep", headers=_h()).json()
    assert out["proposed"] == 1 and out["ai_drafted"] == 1 and out["template_fallback"] == 0
    proposal = out["approvals"][0]
    assert proposal["payload"]["note"].startswith("Hi Priya") and proposal["payload"]["ai_draft"]["provider"] == "gemini"
    assert "Friendly reminder suggested (gemini)" in proposal["reason"] and proposal["status"] == "pending"   # still needs a human


def test_sweep_still_works_when_gemini_is_down(key, monkeypatch):
    monkeypatch.setattr(ai_drafts, "_transport", _gemini(status=503))
    _overdue_mock_invoice(monkeypatch)
    out = client.post("/api/invoices/sweep", headers=_h()).json()
    assert out["proposed"] == 1 and out["ai_drafted"] == 0 and out["template_fallback"] == 1
    assert out["approvals"][0]["payload"]["ai_draft"]["error"]


def test_ai_note_is_what_gets_sent_on_approval(key, monkeypatch):
    monkeypatch.setattr(ai_drafts, "_transport", _gemini())
    done = _overdue_mock_invoice(monkeypatch)
    proposal = client.post("/api/invoices/sweep", headers=_h()).json()["approvals"][0]
    result = decide_approval(proposal["approval_id"], "approved", CFO)
    assert result.status == "approved" and inv.get_invoice(done.approval_id)["reminders_sent"] == 1


def test_health_reports_gemini_without_leaking_the_key(key):
    body = client.get("/health").json()
    assert body["gemini_configured"] is True and "test-key" not in str(body)
