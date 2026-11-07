"""Webhook intake: provenance, dedupe, fail-closed, secure-mode, limits, auth on read routes."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from config.settings import settings
from security.auth import create_access_token
from services import paypal_client, webhook_service
from services.audit_service import verify_audit_chain

client = TestClient(main.app)
EVENT = {"id": "WH-100", "event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": "INV2-X"}}}


def _post(event=EVENT, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(event).encode()
    return client.post("/webhooks/paypal", content=body, headers={"content-type": "application/json", **(headers or {})})


def _auth():
    token = create_access_token({"sub": "cfo", "role": "CFO"}, expires_delta=timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    monkeypatch.setattr(settings, "paypal_client_id", "cid")
    monkeypatch.setattr(settings, "paypal_client_secret", "secret")
    monkeypatch.setattr(settings, "paypal_webhook_id", "WH")


def _stub_verify(monkeypatch, result):
    def fake(headers, raw_body):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(paypal_client, "verify_webhook_signature", fake)


# ---------- mock mode ----------
def test_mock_mode_labels_and_dedupes(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "mock")
    monkeypatch.delenv("RENDER", raising=False); monkeypatch.delenv("SECURE_MODE", raising=False)
    first, second = _post(), _post()
    assert first.status_code == 200 and first.json() == {"status": "received", "event_id": "WH-100", "provenance": "MOCK"}
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    events = client.get("/api/events", headers=_auth()).json()
    assert len(events) == 1 and events[0]["provenance"] == "MOCK" and events[0]["verified"] == 0


def test_mock_mode_is_refused_in_secure_mode(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "mock")
    monkeypatch.setenv("SECURE_MODE", "true")
    response = _post()
    assert response.status_code == 503 and "mock_webhooks_disabled" in response.json()["status"]
    assert client.get("/api/events", headers=_auth()).json() == []


# ---------- sandbox mode ----------
def test_verified_event_is_stored_and_audited(sandbox, monkeypatch):
    _stub_verify(monkeypatch, True)
    response = _post(headers={"paypal-transmission-id": "tx-9"})
    assert response.json()["provenance"] == "REAL_VERIFIED"
    row = client.get("/api/events", headers=_auth()).json()[0]
    assert row["verified"] == 1 and row["transmission_id"] == "tx-9" and row["payload"]["id"] == "WH-100"
    assert verify_audit_chain()["valid"] is True


def test_invalid_signature_is_rejected_and_not_trusted(sandbox, monkeypatch):
    _stub_verify(monkeypatch, False)
    response = _post()
    assert response.status_code == 400 and response.json()["status"] == "signature_invalid"
    row = client.get("/api/events", headers=_auth()).json()[0]
    assert row["provenance"] == "REAL_UNVERIFIED" and row["verified"] == 0


def test_forged_event_cannot_block_the_real_delivery(sandbox, monkeypatch):
    _stub_verify(monkeypatch, False)
    assert _post().status_code == 400                # attacker sends our event id first
    _stub_verify(monkeypatch, True)
    real = _post()
    assert real.status_code == 200 and real.json()["status"] == "received"
    assert _post().json()["status"] == "duplicate"   # the real one is now deduped


def test_paypal_outage_returns_503_and_stores_nothing(sandbox, monkeypatch):
    _stub_verify(monkeypatch, paypal_client.PayPalUnavailable("down"))
    assert _post().status_code == 503
    assert client.get("/api/events", headers=_auth()).json() == []


def test_missing_credentials_fail_closed(sandbox, monkeypatch):
    monkeypatch.setattr(settings, "paypal_webhook_id", "")
    response = _post()
    assert response.status_code == 503 and response.json()["status"] == "verification_not_configured"
    assert client.get("/api/events", headers=_auth()).json() == []


def test_unverified_rows_are_pruned(sandbox, monkeypatch):
    monkeypatch.setattr(webhook_service, "UNVERIFIED_KEEP", 3)
    _stub_verify(monkeypatch, False)
    for i in range(6):
        _post({"id": f"BAD-{i}", "event_type": "X"})
    rows = client.get("/api/events", headers=_auth()).json()
    assert len(rows) == 3 and rows[0]["event_id"] == "BAD-5"


# ---------- input limits and abuse controls ----------
def test_invalid_json_and_missing_fields(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "mock"); monkeypatch.delenv("RENDER", raising=False); monkeypatch.delenv("SECURE_MODE", raising=False)
    assert _post(raw=b"not json").status_code == 400
    assert _post({"event_type": "X"}).status_code == 400
    assert _post({"id": "A"}).status_code == 400
    assert _post(raw=b"[1,2,3]").status_code == 400


def test_oversize_body_rejected(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "mock"); monkeypatch.delenv("RENDER", raising=False); monkeypatch.delenv("SECURE_MODE", raising=False)
    big = json.dumps({"id": "BIG", "event_type": "X", "pad": "a" * (webhook_service.MAX_BODY_BYTES + 10)}).encode()
    assert _post(raw=big).status_code == 413


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "mock"); monkeypatch.delenv("RENDER", raising=False); monkeypatch.delenv("SECURE_MODE", raising=False)
    monkeypatch.setattr(webhook_service, "RATE_LIMIT_PER_MINUTE", 3)
    codes = [_post({"id": f"R-{i}", "event_type": "X"}).status_code for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


# ---------- read routes need a login ----------
def test_events_and_verify_routes_require_auth():
    assert client.get("/api/events").status_code == 401
    assert client.get("/api/audit/verify").status_code == 401
    body = client.get("/api/audit/verify", headers=_auth()).json()
    assert body["valid"] is True and "head_hash" in body


def test_live_mode_is_not_allowed():
    import config.settings as cs
    with pytest.raises(RuntimeError):
        cs.Settings(paypal_mode="live").validate_security()
