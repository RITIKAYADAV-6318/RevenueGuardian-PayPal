"""An approval must execute at most once, even under concurrent approvals."""
import threading

import pytest

from services import approval_service as svc

CFO = {"username": "cfo", "role": "CFO"}


def test_concurrent_approvals_execute_once(monkeypatch):
    calls = []

    def slow_execute(action_type, payload):
        import time
        time.sleep(0.2)
        calls.append(1)
        return {"status": "success"}

    monkeypatch.setattr(svc, "_execute_action", slow_execute)
    rec = svc.create_approval("offer_discount", "Acme", {"d": 1})
    results = []

    def go():
        try:
            svc.decide_approval(rec.approval_id, "approved", CFO)
            results.append("ok")
        except ValueError:
            results.append("rejected_duplicate")

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1
    assert results.count("ok") == 1
    assert svc.get_approval(rec.approval_id).status == "approved"


def test_executor_exception_marks_failed_and_is_not_retryable(monkeypatch):
    def boom(action_type, payload):
        raise RuntimeError("network down")

    monkeypatch.setattr(svc, "_execute_action", boom)
    rec = svc.create_approval("offer_discount", "Acme", {"d": 1})
    out = svc.decide_approval(rec.approval_id, "approved", CFO)
    assert out.status == "failed"
    assert "network down" in out.execution_result["message"]
    with pytest.raises(ValueError):
        svc.decide_approval(rec.approval_id, "approved", CFO)


def test_reject_then_approve_is_blocked():
    rec = svc.create_approval("offer_discount", "Acme", {"d": 1})
    svc.decide_approval(rec.approval_id, "rejected", CFO)
    with pytest.raises(ValueError):
        svc.decide_approval(rec.approval_id, "approved", CFO)
