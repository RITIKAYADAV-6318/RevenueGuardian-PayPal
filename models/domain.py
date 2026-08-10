"""Shared Pydantic schemas used by the app, API, and deterministic workflow."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class ExecutiveRevenueSummary(BaseModel):
    total_arr: float
    revenue_at_risk: float
    forecasted_revenue_next_30_days: float
    unbilled_overages: float

class HighPriorityDealSummary(BaseModel):
    opportunity_id: str
    name: str
    value: float
    win_probability: float
    primary_risk: str
    recommended_action: str

class TodayRecommendation(BaseModel):
    action_type: str
    target: str
    description: str
    impact_value: float
    urgency: str

class ExecutiveSummaryResult(BaseModel):
    timestamp: str
    business_health_score: int
    morning_briefing: str
    executive_report: str
    revenue_summary: ExecutiveRevenueSummary
    high_priority_deals: List[HighPriorityDealSummary]
    todays_recommendations: List[TodayRecommendation]

class OrchestratorFailureLog(BaseModel):
    agent_name: str
    error_message: str
    timestamp: str

class OrchestrationRunResult(BaseModel):
    run_id: str
    status: str = Field(..., description="success, partial_success, or failed")
    timestamp: str
    crm_analysis: Optional[Any] = None
    email_analysis: Optional[Any] = None
    calendar_analysis: Optional[Any] = None
    prediction_analysis: Optional[Any] = None
    recovery_strategy: Optional[Any] = None
    executive_summary: Optional[ExecutiveSummaryResult] = None
    failures: List[OrchestratorFailureLog] = Field(default_factory=list)
    mode: str = "deterministic_mock"

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]


class ApprovalCreateRequest(BaseModel):
    action_type: str
    target: str
    payload: Dict[str, Any]
    requested_by: str = "api"
    reason: str = ""

class ApprovalDecisionRequest(BaseModel):
    decision: str
    note: str = ""

class ApprovalRecord(BaseModel):
    approval_id: int
    action_type: str
    target: str
    payload: Dict[str, Any]
    requested_by: str
    reason: str
    status: str
    created_at: str
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decision_note: str = ""
    execution_result: Optional[Dict[str, Any]] = None
