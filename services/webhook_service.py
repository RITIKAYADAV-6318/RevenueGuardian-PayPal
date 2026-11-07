"""PayPal webhook intake: size limit, verification, provenance label, dedupe, storage.

Provenance values:
  REAL_VERIFIED    sent by PayPal and confirmed through PayPal's verify API
  REAL_UNVERIFIED  signature check failed; stored (truncated) for visibility, never acted on
  MOCK             PAYPAL_MODE=mock, no verification; refused entirely in secure mode
"""
import json
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from config.settings import settings
from services import paypal_client
from services.audit_service import log_audit_event

DB_PATH = settings.sqlite_db_path
MAX_BODY_BYTES = 256 * 1024
UNVERIFIED_KEEP = 500
UNVERIFIED_RAW_CHARS = 4000
RATE_LIMIT_PER_MINUTE = 120

_rate_lock = threading.Lock()
_rate_hits: Dict[str, deque] = defaultdict(deque)


def init_webhook_database() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            provenance TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL,
            transmission_id TEXT,
            raw_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'received')""")
        # Dedupe only trusted events, so a forged copy of an event id can never block the real delivery.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_dedupe ON webhook_events(event_id) WHERE provenance IN ('REAL_VERIFIED','MOCK')")
        conn.commit()
    finally:
        conn.close()


init_webhook_database()


def rate_limited(client_key: str) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits[client_key]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_PER_MINUTE:
            return True
        hits.append(now)
        return False


def reset_rate_limits() -> None:
    with _rate_lock:
        _rate_hits.clear()


def _insert(event_id: str, event_type: str, provenance: str, verified: bool, transmission_id: str, raw_text: str) -> bool:
    """Insert one row. Returns False if a trusted event with this id already exists."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            "INSERT INTO webhook_events (event_id, event_type, provenance, verified, received_at, transmission_id, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, event_type, provenance, 1 if verified else 0, datetime.now(UTC).isoformat(), transmission_id, raw_text),
        )
        if not verified:
            conn.execute(
                "DELETE FROM webhook_events WHERE provenance = 'REAL_UNVERIFIED' AND id NOT IN "
                "(SELECT id FROM webhook_events WHERE provenance = 'REAL_UNVERIFIED' ORDER BY id DESC LIMIT ?)",
                (UNVERIFIED_KEEP,),
            )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def process_webhook(headers: Mapping[str, str], raw_body: bytes) -> Tuple[int, Dict[str, Any]]:
    """Handle one webhook delivery. Returns (http_status, json_body)."""
    if len(raw_body) > MAX_BODY_BYTES:
        return 413, {"status": "too_large"}
    try:
        event = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return 400, {"status": "invalid_json"}
    event_id, event_type = (event.get("id") if isinstance(event, dict) else None), (event.get("event_type") if isinstance(event, dict) else None)
    if not isinstance(event_id, str) or not isinstance(event_type, str) or not event_id or not event_type:
        return 400, {"status": "missing_id_or_event_type"}

    lowered = {key.lower(): value for key, value in headers.items()}
    transmission_id = lowered.get("paypal-transmission-id", "")
    raw_text = raw_body.decode("utf-8")

    if settings.paypal_mode == "mock":
        if settings.secure_mode:
            return 503, {"status": "mock_webhooks_disabled_in_secure_mode"}
        provenance, verified = "MOCK", False
    else:
        try:
            verified = paypal_client.verify_webhook_signature(lowered, raw_body)
        except paypal_client.PayPalNotConfigured as exc:
            log_audit_event("paypal", "webhook_verification_unavailable", "failed", {"event_id": event_id, "reason": str(exc)})
            return 503, {"status": "verification_not_configured"}
        except paypal_client.PayPalUnavailable as exc:
            log_audit_event("paypal", "webhook_verification_unavailable", "failed", {"event_id": event_id, "reason": str(exc)})
            return 503, {"status": "verification_unavailable"}  # 5xx => PayPal retries later
        if not verified:
            _insert(event_id, event_type, "REAL_UNVERIFIED", False, transmission_id, raw_text[:UNVERIFIED_RAW_CHARS])
            log_audit_event("paypal", "webhook_rejected", "failed", {"event_id": event_id, "event_type": event_type})
            return 400, {"status": "signature_invalid"}
        provenance = "REAL_VERIFIED"

    if not _insert(event_id, event_type, provenance, verified, transmission_id, raw_text):
        return 200, {"status": "duplicate", "event_id": event_id}
    log_audit_event("paypal", "webhook_received", "success", {"event_id": event_id, "event_type": event_type, "provenance": provenance})
    from services import invoice_service  # late import: invoice_service must not be needed to store an event
    invoice_service.handle_event(event, provenance)
    return 200, {"status": "received", "event_id": event_id, "provenance": provenance}


def list_events(limit: int = 100, provenance: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if provenance:
            rows = conn.execute("SELECT * FROM webhook_events WHERE provenance = ? ORDER BY id DESC LIMIT ?", (provenance, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    events = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("raw_json"))
        except ValueError:
            item["payload"] = None
        events.append(item)
    return events
