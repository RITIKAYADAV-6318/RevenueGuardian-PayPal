"""SQLite audit logging service."""
import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional
from config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AuditService")
DB_PATH = settings.sqlite_db_path

def init_audit_log_database() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS audit_logs (log_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, details TEXT)""")
    conn.commit()
    conn.close()

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
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO audit_logs (timestamp, actor, action, status, details) VALUES (?, ?, ?, ?, ?)", (timestamp, actor, action, status, details_str))
        conn.commit()
        log_id = cursor.lastrowid
        conn.close()
        return int(log_id)
    except Exception as exc:
        logger.error("Failed to persist audit log: %s", exc)
        return -1

def fetch_audit_logs(limit: int = 100, actor: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
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
    query += " ORDER BY timestamp DESC LIMIT ?"
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
