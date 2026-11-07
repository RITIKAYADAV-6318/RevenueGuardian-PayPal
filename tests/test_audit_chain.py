"""Hash-chained audit log: tamper evidence, concurrency, legacy migration."""
import sqlite3
import threading

import services.audit_service as audit
from services.audit_service import GENESIS_HASH, log_audit_event, verify_audit_chain


def _db():
    return sqlite3.connect(audit.DB_PATH)


def test_empty_log_is_valid():
    result = verify_audit_chain()
    assert result["valid"] is True and result["checked"] == 0 and result["head_hash"] == GENESIS_HASH


def test_chain_links_rows_and_verifies():
    for i in range(5):
        log_audit_event("tester", "step", "success", {"i": i})
    result = verify_audit_chain()
    assert result["valid"] is True and result["checked"] == 5
    conn = _db()
    rows = conn.execute("SELECT prev_hash, record_hash FROM audit_logs ORDER BY log_id").fetchall()
    conn.close()
    assert rows[0][0] == GENESIS_HASH
    for earlier, later in zip(rows, rows[1:]):
        assert later[0] == earlier[1]
    assert result["head_hash"] == rows[-1][1]


def test_editing_a_middle_row_is_detected():
    ids = [log_audit_event("tester", "step", "success", {"i": i}) for i in range(5)]
    conn = _db()
    conn.execute("UPDATE audit_logs SET details = '{\"i\": 999}' WHERE log_id = ?", (ids[2],))
    conn.commit(); conn.close()
    result = verify_audit_chain()
    assert result["valid"] is False and result["first_broken_log_id"] == ids[2]


def test_deleting_a_middle_row_is_detected():
    ids = [log_audit_event("tester", "step", "success", {"i": i}) for i in range(5)]
    conn = _db()
    conn.execute("DELETE FROM audit_logs WHERE log_id = ?", (ids[2],))
    conn.commit(); conn.close()
    result = verify_audit_chain()
    assert result["valid"] is False and result["first_broken_log_id"] == ids[3]


def test_truncating_newest_rows_is_not_detectable_but_head_hash_changes():
    """Documented limit: pin head_hash externally to catch this."""
    ids = [log_audit_event("tester", "step", "success", {"i": i}) for i in range(4)]
    before = verify_audit_chain()["head_hash"]
    conn = _db()
    conn.execute("DELETE FROM audit_logs WHERE log_id = ?", (ids[-1],))
    conn.commit(); conn.close()
    after = verify_audit_chain()
    assert after["valid"] is True and after["head_hash"] != before


def test_parallel_writes_keep_the_chain_valid():
    errors = []

    def worker(n):
        try:
            for i in range(10):
                assert log_audit_event(f"w{n}", "parallel", "success", {"i": i}) > 0
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors
    result = verify_audit_chain()
    assert result["valid"] is True and result["checked"] == 80


def test_legacy_rows_are_migrated_and_backfilled():
    conn = _db()
    conn.execute("DROP TABLE audit_logs")
    conn.execute("CREATE TABLE audit_logs (log_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, details TEXT)")
    for i in range(3):
        conn.execute("INSERT INTO audit_logs (timestamp, actor, action, status, details) VALUES (?,?,?,?,?)", (f"2026-08-0{i+1}T00:00:00+00:00", "old", "legacy", "success", f"row {i}"))
    conn.commit(); conn.close()
    audit.init_audit_log_database()
    assert verify_audit_chain()["valid"] is True
    log_audit_event("new", "after_migration", "success", None)
    result = verify_audit_chain()
    assert result["valid"] is True and result["checked"] == 4


def test_fetch_logs_include_hashes_newest_first():
    log_audit_event("a", "first", "success", None)
    log_audit_event("b", "second", "success", None)
    logs = audit.fetch_audit_logs(limit=2)
    assert [row["action"] for row in logs] == ["second", "first"]
    assert logs[0]["record_hash"] and logs[0]["prev_hash"] == logs[1]["record_hash"]
