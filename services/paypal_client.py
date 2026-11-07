"""Minimal PayPal REST client: cached OAuth token and webhook signature verification.

Sandbox only. Verification sends PayPal the *exact raw bytes* of the webhook body (spliced into
the request text, never re-serialized), because PayPal checks a CRC of the original payload.
"""
import base64
import json
import threading
import time
from typing import Any, Mapping, Optional

import httpx

from config.settings import settings

_transport: Optional[httpx.BaseTransport] = None  # tests inject httpx.MockTransport here
_token_lock = threading.Lock()
_token_cache = {"value": None, "expires_at": 0.0}


class PayPalNotConfigured(RuntimeError):
    """Client id / secret / webhook id missing. Verification must fail closed."""


class PayPalUnavailable(RuntimeError):
    """PayPal could not be reached or answered with an error. Not proof that an event is forged."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=10.0, transport=_transport)


def _require_config(need_webhook_id: bool = False) -> None:
    missing = [name for name, value in (("PAYPAL_CLIENT_ID", settings.paypal_client_id), ("PAYPAL_CLIENT_SECRET", settings.paypal_client_secret)) if not value]
    if need_webhook_id and not settings.paypal_webhook_id:
        missing.append("PAYPAL_WEBHOOK_ID")
    if missing:
        raise PayPalNotConfigured("Missing " + ", ".join(missing))


def reset_token_cache() -> None:
    with _token_lock:
        _token_cache.update(value=None, expires_at=0.0)


def get_access_token() -> str:
    _require_config()
    with _token_lock:
        if _token_cache["value"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["value"]
        basic = base64.b64encode(f"{settings.paypal_client_id}:{settings.paypal_client_secret}".encode()).decode()
        try:
            with _client() as client:
                response = client.post(
                    f"{settings.paypal_api_base}/v1/oauth2/token",
                    headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                    data={"grant_type": "client_credentials"},
                )
        except httpx.HTTPError as exc:
            raise PayPalUnavailable(f"OAuth request failed: {exc}") from exc
        if response.status_code != 200:
            raise PayPalUnavailable(f"OAuth returned HTTP {response.status_code}")
        body = response.json()
        _token_cache["value"] = body["access_token"]
        _token_cache["expires_at"] = time.time() + float(body.get("expires_in", 300))
        return _token_cache["value"]


def build_verify_request_text(headers: Mapping[str, str], raw_body: bytes) -> str:
    """JSON text for POST /v1/notifications/verify-webhook-signature with the raw event spliced in verbatim."""
    fields = {
        "auth_algo": headers.get("paypal-auth-algo", ""),
        "cert_url": headers.get("paypal-cert-url", ""),
        "transmission_id": headers.get("paypal-transmission-id", ""),
        "transmission_sig": headers.get("paypal-transmission-sig", ""),
        "transmission_time": headers.get("paypal-transmission-time", ""),
        "webhook_id": settings.paypal_webhook_id,
    }
    head = json.dumps(fields)[:-1]  # drop the closing brace
    return head + ', "webhook_event": ' + raw_body.decode("utf-8") + "}"


def verify_webhook_signature(headers: Mapping[str, str], raw_body: bytes) -> bool:
    """True if PayPal confirms the signature, False if PayPal says it is invalid or headers are missing.

    Raises PayPalNotConfigured (fail closed) or PayPalUnavailable (caller should return 5xx so PayPal retries).
    """
    _require_config(need_webhook_id=True)
    lowered = {key.lower(): value for key, value in headers.items()}
    required = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time")
    if any(not lowered.get(name) for name in required):
        return False
    token = get_access_token()
    try:
        text = build_verify_request_text(lowered, raw_body)
    except UnicodeDecodeError:
        return False
    try:
        with _client() as client:
            response = client.post(
                f"{settings.paypal_api_base}/v1/notifications/verify-webhook-signature",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                content=text.encode("utf-8"),
            )
    except httpx.HTTPError as exc:
        raise PayPalUnavailable(f"Verify request failed: {exc}") from exc
    if response.status_code == 401:
        reset_token_cache()
        raise PayPalUnavailable("PayPal rejected our access token")
    if response.status_code >= 500 or response.status_code == 429:
        raise PayPalUnavailable(f"PayPal verify returned HTTP {response.status_code}")
    if response.status_code != 200:
        return False
    return response.json().get("verification_status") == "SUCCESS"
