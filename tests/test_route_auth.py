"""Every API route except /health and /auth/login must require a valid token."""
import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from security.auth import create_access_token

client = TestClient(main.app)


def _h(username, role):
    token = create_access_token({"sub": username, "role": role}, expires_delta=timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


PROTECTED_GET = ["/api/approvals", "/api/audit/logs", "/api/crm", "/api/emails", "/api/calendar"]


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_get_routes_reject_anonymous(path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", PROTECTED_GET)
def test_get_routes_accept_signed_in_user(path):
    assert client.get(path, headers=_h("sales", "Sales_Rep")).status_code == 200


def test_health_and_login_stay_public():
    assert client.get("/health").status_code == 200
    assert client.post("/auth/login", json={"username": "cfo", "password": "nope"}).status_code == 401


def test_create_approval_requires_token_and_ignores_body_requested_by():
    body = {"action_type": "offer_discount", "target": "Acme", "payload": {"d": 1}, "requested_by": "forged"}
    assert client.post("/api/approvals", json=body).status_code == 401
    ok = client.post("/api/approvals", json=body, headers=_h("sales", "Sales_Rep"))
    assert ok.status_code == 200
    assert ok.json()["requested_by"] == "sales"


def test_create_approval_rejects_unknown_action_type():
    body = {"action_type": "wire_money", "target": "X", "payload": {}}
    assert client.post("/api/approvals", json=body, headers=_h("cfo", "CFO")).status_code == 400


def test_run_audit_is_revops_manager_only():
    assert client.post("/api/audit/run").status_code == 401
    assert client.post("/api/audit/run", headers=_h("cfo", "CFO")).status_code == 403
    assert client.post("/api/audit/run", headers=_h("admin", "RevOps_Manager")).status_code == 200


def test_secure_mode_refuses_default_jwt_secret(monkeypatch):
    import config.settings as cs
    monkeypatch.setenv("SECURE_MODE", "true")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        cs.get_settings()


def test_secure_mode_accepts_custom_secret(monkeypatch):
    import config.settings as cs
    monkeypatch.setenv("SECURE_MODE", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 48)
    assert cs.get_settings().secure_mode is True


def test_health_reports_paypal_mode_without_leaking_secrets(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "paypal_client_secret", "super-secret")
    body = client.get("/health").json()
    assert body["paypal_mode"] in {"mock", "sandbox"}
    assert isinstance(body["webhook_verification_configured"], bool)
    assert "super-secret" not in str(body)
