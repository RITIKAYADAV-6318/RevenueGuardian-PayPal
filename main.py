"""FastAPI backend for Revenue Guardian."""
from datetime import datetime, timedelta
from typing import Any, Dict
from fastapi import FastAPI, HTTPException
from agents.deterministic_workflow import RevOpsOrchestrator
from config.settings import settings
from models.domain import LoginRequest, TokenResponse
from mcp.server import read_calendar, read_crm, read_emails
from security.auth import authenticate_user, create_access_token
from services.audit_service import fetch_audit_logs, log_audit_event

app = FastAPI(title="Revenue Guardian API", version="1.0.0")

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "mode": "live_adk" if settings.use_live_adk else "deterministic_mock"}

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
async def run_audit() -> Dict[str, Any]:
    run_id = f"api_run_{int(datetime.utcnow().timestamp())}"
    result = await RevOpsOrchestrator().execute_workflow(run_id)
    log_audit_event("api", "run_audit", result.status, {"run_id": run_id, "mode": result.mode})
    return result.model_dump()

@app.get("/api/crm")
def crm() -> Dict[str, Any]:
    return read_crm()

@app.get("/api/emails")
def emails(max_results: int = 10):
    return read_emails(max_results=max_results)

@app.get("/api/calendar")
def calendar(max_results: int = 10):
    return read_calendar(max_results=max_results)

@app.get("/api/audit/logs")
def audit_logs(limit: int = 100):
    return fetch_audit_logs(limit=limit)
