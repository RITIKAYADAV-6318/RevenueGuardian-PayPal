"""Tests for human-in-the-loop approval governance."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from security.auth import create_access_token
from services.approval_service import create_approval, decide_approval, list_approvals


def _token(username: str, role: str) -> str:
    return create_access_token({"sub": username, "role": role}, expires_delta=timedelta(minutes=5))


def test_cfo_can_approve_billing_action():
    record = create_approval(
        action_type="offer_discount",
        target="Acme Corp",
        payload={"discount_percent": 15, "account": "Acme Corp"},
        requested_by="recovery_agent",
        reason="Pricing dispute blocks renewal.",
    )

    decided = decide_approval(
        approval_id=record.approval_id,
        decision="approved",
        user={"username": "cfo", "role": "CFO"},
        note="Approved for renewal recovery.",
    )

    assert decided.status == "approved"
    assert decided.decided_by == "cfo"
    assert decided.execution_result["status"] == "success"


def test_sales_rep_cannot_approve_billing_action():
    record = create_approval(
        action_type="offer_discount",
        target="Acme Corp",
        payload={"discount_percent": 15},
        requested_by="recovery_agent",
    )

    with pytest.raises(PermissionError):
        decide_approval(
            approval_id=record.approval_id,
            decision="approved",
            user={"username": "sales", "role": "Sales_Rep"},
        )


def test_approval_api_requires_authorized_role():
    client = TestClient(main.app)
    created = client.post(
        "/api/approvals",
        headers={"Authorization": f"Bearer {_token('sales', 'Sales_Rep')}"},
        json={
            "action_type": "offer_discount",
            "target": "Acme Corp",
            "payload": {"discount_percent": 15},
            "requested_by": "test",
        },
    )
    assert created.status_code == 200
    approval_id = created.json()["approval_id"]

    sales_response = client.post(
        f"/api/approvals/{approval_id}/decision",
        headers={"Authorization": f"Bearer {_token('sales', 'Sales_Rep')}"},
        json={"decision": "approved"},
    )
    assert sales_response.status_code == 403

    cfo_response = client.post(
        f"/api/approvals/{approval_id}/decision",
        headers={"Authorization": f"Bearer {_token('cfo', 'CFO')}"},
        json={"decision": "approved"},
    )
    assert cfo_response.status_code == 200
    assert cfo_response.json()["status"] == "approved"


def test_list_approvals_filters_pending():
    create_approval(action_type="create_gmail_draft", target="TH_001", payload={"thread_id": "TH_001", "body": "Hello"})
    pending = list_approvals(status="pending")
    assert len(pending) == 1
    assert pending[0].action_type == "create_gmail_draft"
