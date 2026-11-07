"""PayPal client: raw-body verification, token caching, fail-closed behaviour. No network used."""
import json

import httpx
import pytest

from config.settings import settings
from services import paypal_client as pc

HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "paypal-transmission-id": "tx-1",
    "paypal-transmission-sig": "sig==",
    "paypal-transmission-time": "2026-11-05T10:00:00Z",
}
# Deliberately odd spacing, key order and a non-ASCII character: re-serializing would change these bytes.
RAW = '{"id":  "WH-1",   "event_type": "INVOICING.INVOICE.PAID",\n "resource": {"note": "caf\u00e9", "z": 1, "a": 2}}'.encode("utf-8")


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "paypal_mode", "sandbox")
    monkeypatch.setattr(settings, "paypal_client_id", "cid")
    monkeypatch.setattr(settings, "paypal_client_secret", "secret")
    monkeypatch.setattr(settings, "paypal_webhook_id", "WEBHOOK-ID")
    pc.reset_token_cache()
    yield
    monkeypatch.setattr(pc, "_transport", None)
    pc.reset_token_cache()


def _transport(verify_status="SUCCESS", verify_http=200, token_http=200, calls=None):
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.content))
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(token_http, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(verify_http, json={"verification_status": verify_status})
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def test_raw_body_is_spliced_in_verbatim(monkeypatch):
    transport, calls = _transport()
    monkeypatch.setattr(pc, "_transport", transport)
    assert pc.verify_webhook_signature(HEADERS, RAW) is True
    sent = next(content for path, content in calls if path.endswith("verify-webhook-signature")).decode("utf-8")
    assert RAW.decode("utf-8") in sent                      # byte-for-byte, not re-serialized
    body = json.loads(sent)                                 # and still valid JSON
    assert body["webhook_id"] == "WEBHOOK-ID" and body["transmission_id"] == "tx-1"
    assert body["webhook_event"]["id"] == "WH-1"


def test_failure_status_returns_false(monkeypatch):
    monkeypatch.setattr(pc, "_transport", _transport(verify_status="FAILURE")[0])
    assert pc.verify_webhook_signature(HEADERS, RAW) is False


def test_missing_headers_return_false_without_calling_paypal(monkeypatch):
    transport, calls = _transport()
    monkeypatch.setattr(pc, "_transport", transport)
    assert pc.verify_webhook_signature({"paypal-transmission-id": "tx-1"}, RAW) is False
    assert calls == []


def test_token_is_cached_across_verifications(monkeypatch):
    transport, calls = _transport()
    monkeypatch.setattr(pc, "_transport", transport)
    pc.verify_webhook_signature(HEADERS, RAW)
    pc.verify_webhook_signature(HEADERS, RAW)
    assert sum(1 for path, _ in calls if path == "/v1/oauth2/token") == 1


@pytest.mark.parametrize("missing", ["paypal_client_id", "paypal_client_secret", "paypal_webhook_id"])
def test_missing_config_fails_closed(monkeypatch, missing):
    monkeypatch.setattr(settings, missing, "")
    with pytest.raises(pc.PayPalNotConfigured):
        pc.verify_webhook_signature(HEADERS, RAW)


def test_paypal_5xx_is_unavailable_not_forged(monkeypatch):
    monkeypatch.setattr(pc, "_transport", _transport(verify_http=503)[0])
    with pytest.raises(pc.PayPalUnavailable):
        pc.verify_webhook_signature(HEADERS, RAW)


def test_oauth_failure_is_unavailable(monkeypatch):
    monkeypatch.setattr(pc, "_transport", _transport(token_http=401)[0])
    with pytest.raises(pc.PayPalUnavailable):
        pc.verify_webhook_signature(HEADERS, RAW)


def test_network_error_is_unavailable(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(pc, "_transport", httpx.MockTransport(boom))
    with pytest.raises(pc.PayPalUnavailable):
        pc.verify_webhook_signature(HEADERS, RAW)


def test_api_request_sends_bearer_and_request_id_and_maps_outages(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        seen.append((request.method, request.url.path, request.headers.get("authorization"), request.headers.get("paypal-request-id"), request.content))
        return httpx.Response(503) if request.url.path.endswith("/down") else httpx.Response(201, json={"href": "x/invoices/INV2-1"})

    monkeypatch.setattr(pc, "_transport", httpx.MockTransport(handler))
    status, body = pc.api_request("POST", "/v2/invoicing/invoices", json_body={"a": 1}, request_id="rid-1")
    assert status == 201 and body["href"].endswith("INV2-1")
    assert seen[0][:4] == ("POST", "/v2/invoicing/invoices", "Bearer tok", "rid-1") and json.loads(seen[0][4]) == {"a": 1}
    with pytest.raises(pc.PayPalUnavailable):
        pc.api_request("POST", "/v2/down")
