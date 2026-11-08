"""FastAPI backend for Revenue Guardian."""
from datetime import UTC, datetime, timedelta
from typing import Any, Dict
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from agents.deterministic_workflow import RevOpsOrchestrator
from config.settings import settings
from models.domain import ApprovalCreateRequest, ApprovalDecisionRequest, LoginRequest, TokenResponse
from mcp.server import read_calendar, read_crm, read_emails
from security.auth import authenticate_user, create_access_token, verify_access_token
from security.rbac import Permission, has_permission
from services import invoice_service, webhook_service
from services.audit_service import fetch_audit_logs, log_audit_event, verify_audit_chain
from services.approval_service import ACTION_PERMISSION, create_approval, decide_approval, list_approvals

app = FastAPI(title="Revenue Guardian API", version="1.0.0")


def current_user_from_authorization(authorization: str = Header(default="")) -> Dict[str, Any]:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    payload = verify_access_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"username": payload.get("sub", "unknown"), "role": payload.get("role", "")}


def require_user(authorization: str = Header(default="")) -> Dict[str, Any]:
    """Any signed-in user."""
    return current_user_from_authorization(authorization)


def require_permission(permission: Permission):
    """Signed-in user whose role holds `permission` (403 otherwise)."""
    def dependency(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
        if not has_permission(user.get("role", ""), permission):
            raise HTTPException(status_code=403, detail=f"Role '{user.get('role', '')}' lacks permission '{permission.value}'.")
        return user
    return dependency


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "mode": "live_adk" if settings.use_live_adk else "deterministic_mock",
        "paypal_mode": settings.paypal_mode,
        "webhook_verification_configured": bool(settings.paypal_client_id and settings.paypal_client_secret and settings.paypal_webhook_id),
    }

@app.post("/auth/login", response_model=TokenResponse)
def login(request: LoginRequest) -> TokenResponse:
    user = authenticate_user(request.username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token({"sub": user["username"], "role": user["role"]}, expires_delta=timedelta(minutes=settings.access_token_expire_minutes))
    public_user = {k: v for k, v in user.items() if k != "hashed_password"}
    log_audit_event(public_user["username"], "login", "success", {"role": public_user["role"]})
    return TokenResponse(access_token=token, user=public_user)

@app.post("/api/audit/run")
async def run_audit(user: Dict[str, Any] = Depends(require_permission(Permission.RUN_AUDIT))) -> Dict[str, Any]:
    run_id = f"api_run_{int(datetime.now(UTC).timestamp())}"
    result = await RevOpsOrchestrator().execute_workflow(run_id)
    log_audit_event(user["username"], "run_audit", result.status, {"run_id": run_id, "mode": result.mode})
    return result.model_dump()

@app.get("/api/crm")
def crm(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    return read_crm()

@app.get("/api/emails")
def emails(max_results: int = 10, user: Dict[str, Any] = Depends(require_user)):
    return read_emails(max_results=max_results)

@app.get("/api/calendar")
def calendar(max_results: int = 10, user: Dict[str, Any] = Depends(require_user)):
    return read_calendar(max_results=max_results)

@app.get("/api/audit/logs")
def audit_logs(limit: int = 100, user: Dict[str, Any] = Depends(require_user)):
    return fetch_audit_logs(limit=limit)


@app.post("/api/approvals")
def request_approval(request: ApprovalCreateRequest, user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    if request.action_type not in ACTION_PERMISSION:
        raise HTTPException(status_code=400, detail=f"Unknown action_type '{request.action_type}'. Allowed: {sorted(ACTION_PERMISSION)}")
    problem = invoice_service.validate_action_payload(request.action_type, request.payload)
    if problem:
        raise HTTPException(status_code=400, detail=f"Policy check failed: {problem}")
    record = create_approval(
        action_type=request.action_type,
        target=request.target,
        payload=request.payload,
        requested_by=user["username"],  # taken from the token, never from the request body
        reason=request.reason,
    )
    return record.model_dump()

@app.get("/api/approvals")
def approvals(status: str = "pending", limit: int = 100, user: Dict[str, Any] = Depends(require_user)):
    normalized_status = status if status else None
    return [record.model_dump() for record in list_approvals(status=normalized_status, limit=limit)]

@app.post("/api/approvals/{approval_id}/decision")
def approval_decision(approval_id: int, request: ApprovalDecisionRequest, authorization: str = Header(default="")) -> Dict[str, Any]:
    user = current_user_from_authorization(authorization)
    try:
        record = decide_approval(approval_id=approval_id, decision=request.decision, user=user, note=request.note)
        return record.model_dump()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request) -> JSONResponse:
    """Public by design: authenticity comes from PayPal's signature, verified against the raw body."""
    forwarded = request.headers.get("x-forwarded-for", "")
    client_key = forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    if webhook_service.rate_limited(client_key):
        return JSONResponse(status_code=429, content={"status": "rate_limited"})
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > webhook_service.MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"status": "too_large"})
    raw_body = await request.body()
    status_code, body = await run_in_threadpool(webhook_service.process_webhook, dict(request.headers), raw_body)
    return JSONResponse(status_code=status_code, content=body)


@app.get("/api/events")
def webhook_events(limit: int = 100, provenance: str = "", user: Dict[str, Any] = Depends(require_user)):
    return webhook_service.list_events(limit=limit, provenance=provenance or None)


@app.get("/api/audit/verify")
def audit_verify(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    return verify_audit_chain()


@app.get("/api/invoices")
def invoices(status: str = "", user: Dict[str, Any] = Depends(require_user)):
    return invoice_service.list_invoices(status=status or None)


@app.get("/api/kpis")
def kpis(user: Dict[str, Any] = Depends(require_user)) -> Dict[str, Any]:
    return invoice_service.kpis()


@app.post("/api/invoices/sweep")
def invoice_sweep(user: Dict[str, Any] = Depends(require_permission(Permission.APPROVE_BILLING))) -> Dict[str, Any]:
    """Propose reminders for overdue invoices. Creates pending approvals only; nothing is sent."""
    created = invoice_service.propose_overdue_reminders(requested_by=user["username"])
    return {"proposed": len(created), "approvals": created, "demo_threshold_minutes": settings.demo_overdue_minutes or None}


@app.post("/api/invoices/{proposal_id}/simulate-payment")
def simulate_payment(proposal_id: int, user: Dict[str, Any] = Depends(require_permission(Permission.APPROVE_BILLING))) -> Dict[str, Any]:
    result = invoice_service.simulate_payment(proposal_id, actor=user["username"])
    if result["status"] != "success":
        raise HTTPException(status_code=409, detail=result["message"])
    return result
