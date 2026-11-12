"""/docs must offer an Authorize button, and bearer-token auth must still behave exactly as before."""
from datetime import timedelta

from fastapi.testclient import TestClient

import main
from security.auth import create_access_token

client = TestClient(main.app)


def _h(role="CFO", user="cfo"):
    return {"Authorization": "Bearer " + create_access_token({"sub": user, "role": role}, expires_delta=timedelta(minutes=5))}


def test_openapi_declares_a_bearer_scheme_for_protected_routes():
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert any(s.get("type") == "http" and s.get("scheme") == "bearer" for s in schemes.values())
    for path, method in [("/api/kpis", "get"), ("/api/approvals", "post"), ("/api/approvals/{approval_id}/decision", "post"), ("/api/invoices", "get")]:
        assert spec["paths"][path][method].get("security"), f"{method.upper()} {path} is not marked as protected"


def test_public_routes_stay_unprotected_in_the_spec():
    spec = client.get("/openapi.json").json()
    for path, method in [("/health", "get"), ("/auth/login", "post"), ("/webhooks/paypal", "post")]:
        assert not spec["paths"][path][method].get("security")


def test_missing_and_malformed_tokens_are_still_401():
    assert client.get("/api/kpis").status_code == 401
    assert client.get("/api/kpis", headers={"Authorization": "Token abc"}).status_code == 401
    assert client.get("/api/kpis", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401


def test_valid_token_works_on_the_decision_route_too():
    created = client.post("/api/approvals", json={"action_type": "offer_discount", "target": "Acme", "payload": {"d": 1}}, headers=_h("Sales_Rep", "sales")).json()
    denied = client.post(f"/api/approvals/{created['approval_id']}/decision", json={"decision": "approved"}, headers=_h("Sales_Rep", "sales"))
    assert denied.status_code == 403
    assert client.post(f"/api/approvals/{created['approval_id']}/decision", json={"decision": "approved"}).status_code == 401
    assert client.post(f"/api/approvals/{created['approval_id']}/decision", json={"decision": "approved"}, headers=_h()).status_code == 200
