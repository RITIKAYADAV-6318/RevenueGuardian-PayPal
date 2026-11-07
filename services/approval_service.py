"""SQLite-backed human-in-the-loop approval queue."""
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from config.settings import settings
from models.domain import ApprovalRecord
from security.rbac import Permission, has_permission
from services.audit_service import log_audit_event

DB_PATH = settings.sqlite_db_path

ACTION_PERMISSION = {
    "send_email": Permission.APPROVE_EMAIL,
    "create_gmail_draft": Permission.APPROVE_EMAIL,
    "schedule_meeting": Permission.APPROVE_EMAIL,
    "update_crm_opportunity": Permission.UPDATE_CRM,
    "approve_billing": Permission.APPROVE_BILLING,
    "offer_discount": Permission.APPROVE_BILLING,
}


def init_approval_database() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            payload TEXT NOT NULL,
            requested_by TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            decided_by TEXT,
            decided_at TEXT,
            decision_note TEXT,
            execution_result TEXT
        )
    """)
    conn.commit()
    conn.close()


init_approval_database()


def _row_to_record(row: sqlite3.Row) -> ApprovalRecord:
    item = dict(row)
    item["payload"] = json.loads(item.get("payload") or "{}")
    execution_result = item.get("execution_result")
    item["execution_result"] = json.loads(execution_result) if execution_result else None
    item["reason"] = item.get("reason") or ""
    item["decision_note"] = item.get("decision_note") or ""
    return ApprovalRecord(**item)


def create_approval(action_type: str, target: str, payload: Dict[str, Any], requested_by: str = "api", reason: str = "") -> ApprovalRecord:
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO approvals (action_type, target, payload, requested_by, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (action_type, target, json.dumps(payload), requested_by, reason, "pending", now),
    )
    approval_id = cursor.lastrowid
    conn.commit()
    cursor.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
    record = _row_to_record(cursor.fetchone())
    conn.close()
    log_audit_event(requested_by, "approval_requested", "success", record.model_dump())
    return record


def list_approvals(status: Optional[str] = None, limit: int = 100) -> List[ApprovalRecord]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if status:
        cursor.execute("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
    else:
        cursor.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
    records = [_row_to_record(row) for row in cursor.fetchall()]
    conn.close()
    return records


def get_approval(approval_id: int) -> Optional[ApprovalRecord]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_record(row) if row else None


def required_permission(action_type: str) -> Permission:
    return ACTION_PERMISSION.get(action_type, Permission.BYPASS_APPROVALS)


def ensure_can_decide(user: Dict[str, Any], action_type: str) -> None:
    permission = required_permission(action_type)
    role = user.get("role", "")
    if not has_permission(role, permission):
        raise PermissionError(f"Role '{role}' cannot approve '{action_type}'. Requires '{permission.value}'.")


def _execute_action(action_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    from mcp import server as mcp_server

    if action_type == "send_email":
        return mcp_server.send_email(**payload)
    if action_type == "create_gmail_draft":
        return mcp_server.create_gmail_draft(**payload)
    if action_type == "schedule_meeting":
        return mcp_server.schedule_meeting(**payload)
    if action_type == "update_crm_opportunity":
        return mcp_server.update_crm_opportunity(**payload)
    if action_type in {"approve_billing", "offer_discount"}:
        return {"status": "success", "mode": "approval_recorded", "message": "Billing approval recorded for downstream execution.", "payload": payload}
    return {"status": "error", "message": f"Unsupported action type: {action_type}"}


def _claim_pending(approval_id: int, status: str, decided_by: str, decided_at: str, note: str) -> bool:
    """Atomically move a pending approval to `status`. Returns False if it was no longer pending.

    The conditional UPDATE is the lock: only one caller can win, so an approval can never be
    executed twice (e.g. double click, retried request, two reviewers at once).
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
            "WHERE approval_id = ? AND status = 'pending'",
            (status, decided_by, decided_at, note, approval_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def decide_approval(approval_id: int, decision: str, user: Dict[str, Any], note: str = "") -> ApprovalRecord:
    record = get_approval(approval_id)
    if not record:
        raise LookupError(f"Approval {approval_id} not found.")
    if record.status != "pending":
        raise ValueError(f"Approval {approval_id} is already {record.status}.")

    normalized = decision.lower().strip()
    if normalized not in {"approved", "rejected"}:
        raise ValueError("decision must be 'approved' or 'rejected'.")
    ensure_can_decide(user, record.action_type)

    decided_by = user.get("username") or user.get("sub") or "unknown"
    now = datetime.now(UTC).isoformat()

    if normalized == "rejected":
        if not _claim_pending(approval_id, "rejected", decided_by, now, note):
            raise ValueError(f"Approval {approval_id} is already decided.")
        updated = get_approval(approval_id)
        log_audit_event(decided_by, "approval_decided", "success", updated.model_dump())
        return updated

    # Approved: claim first ("executing"), run the action exactly once, then record the outcome.
    if not _claim_pending(approval_id, "executing", decided_by, now, note):
        raise ValueError(f"Approval {approval_id} is already being decided or was decided.")
    try:
        execution_result = _execute_action(record.action_type, record.payload)
    except Exception as exc:  # never leave an approval stuck in 'executing' or silently retryable
        execution_result = {"status": "error", "message": f"Execution raised {type(exc).__name__}: {exc}"}
    final_status = "failed" if execution_result.get("status") == "error" else "approved"

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE approvals SET status = ?, execution_result = ? WHERE approval_id = ?",
            (final_status, json.dumps(execution_result), approval_id),
        )
        conn.commit()
    finally:
        conn.close()
    updated = get_approval(approval_id)
    log_audit_event(decided_by, "approval_decided", "success" if final_status == "approved" else "failed", updated.model_dump())
    return updated
