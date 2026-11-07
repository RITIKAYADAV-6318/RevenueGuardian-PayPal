"""SQLite audit logging service with a SHA-256 hash chain.

Every row stores `prev_hash` (the previous row's `record_hash`) and its own `record_hash`.
Editing or deleting a row in the middle of the log breaks the chain and `verify_audit_chain()`
reports the first broken row. Truncating the *newest* rows is not detectable from inside the
database; publish `head_hash` (shown in the UI) to pin the current end of the log.
"""
import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuditService")
DB_PATH = settings.sqlite_db_path
GENESIS_HASH = "0" * 64
_write_lock = threading.Lock()


def _compute_hash(prev_hash: str, timestamp: str, actor: str, action: str, status: str, details: str) -> str:
    payload = json.dumps([prev_hash, timestamp, actor, action, status, details], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def init_audit_log_database() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS audit_logs (log_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, details TEXT, prev_hash TEXT, record_hash TEXT)""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")}
        for column in ("prev_hash", "record_hash"):  # migrate databases created before the hash chain existed
            if column not in columns:
                conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {column} TEXT")
        conn.commit()
        _backfill_legacy_rows(conn)
    finally:
        conn.close()


def _backfill_legacy_rows(conn: sqlite3.Connection) -> None:
    """Hash rows written before the chain existed, but only if NO row is hashed yet (never repair a live chain)."""
    hashed = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE record_hash IS NOT NULL").fetchone()[0]
    if hashed:
        return
    prev = GENESIS_HASH
    rows = conn.execute("SELECT log_id, timestamp, actor, action, status, details FROM audit_logs ORDER BY log_id ASC").fetchall()
    for log_id, timestamp, actor, action, status, details in rows:
        record_hash = _compute_hash(prev, timestamp, actor, action, status, details or "")
        conn.execute("UPDATE audit_logs SET prev_hash = ?, record_hash = ? WHERE log_id = ?", (prev, record_hash, log_id))
        prev = record_hash
    conn.commit()


init_audit_log_database()


def log_audit_event(actor: str, action: str, status: str, details: Any = None) -> int:
    timestamp = datetime.now(UTC).isoformat()
    if details is None:
        details_str = ""
    elif isinstance(details, (dict, list)):
        details_str = json.dumps(details)
    else:
        details_str = str(details)
    logger.info("Audit Log | Actor: %s | Action: %s | Status: %s | Details: %s", actor, action, status, details_str)
    # In-process lock + BEGIN IMMEDIATE: concurrent writers can never read the same "last hash".
    with _write_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT record_hash FROM audit_logs ORDER BY log_id DESC LIMIT 1").fetchone()
            prev_hash = (row[0] if row and row[0] else GENESIS_HASH)
            record_hash = _compute_hash(prev_hash, timestamp, actor, action, status, details_str)
            cursor = conn.execute(
                "INSERT INTO audit_logs (timestamp, actor, action, status, details, prev_hash, record_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (timestamp, actor, action, status, details_str, prev_hash, record_hash),
            )
            conn.execute("COMMIT")
            return int(cursor.lastrowid)
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            logger.error("Failed to persist audit log: %s", exc)
            return -1
        finally:
            conn.close()


def verify_audit_chain() -> Dict[str, Any]:
    """Recompute the whole chain. Returns validity, rows checked, head hash and the first broken row id."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = conn.execute("SELECT log_id, timestamp, actor, action, status, details, prev_hash, record_hash FROM audit_logs ORDER BY log_id ASC").fetchall()
    finally:
        conn.close()
    prev = GENESIS_HASH
    for index, (log_id, timestamp, actor, action, status, details, prev_hash, record_hash) in enumerate(rows):
        expected = _compute_hash(prev, timestamp, actor, action, status, details or "")
        if prev_hash != prev or record_hash != expected:
            return {"valid": False, "checked": index, "total": len(rows), "head_hash": prev, "first_broken_log_id": log_id}
        prev = record_hash
    return {"valid": True, "checked": len(rows), "total": len(rows), "head_hash": prev, "first_broken_log_id": None}


def fetch_audit_logs(limit: int = 100, actor: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    query = "SELECT * FROM audit_logs"
    params: List[Any] = []
    conditions = []
    if actor:
        conditions.append("actor = ?")
        params.append(actor)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY log_id DESC LIMIT ?"
    params.append(limit)
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    logs = []
    for row in rows:
        item = dict(row)
        details = item.get("details") or ""
        if details.startswith("{") or details.startswith("["):
            try:
                item["details"] = json.loads(details)
            except json.JSONDecodeError:
                pass
        logs.append(item)
    return logs
