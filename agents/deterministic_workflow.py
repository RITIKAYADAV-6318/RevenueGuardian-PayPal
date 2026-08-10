"""Deterministic mock workflow for no-key local demos and tests."""
from datetime import datetime
from typing import Optional
from config.settings import settings
from models.domain import ExecutiveRevenueSummary, ExecutiveSummaryResult, HighPriorityDealSummary, OrchestrationRunResult, OrchestratorFailureLog, TodayRecommendation

def build_demo_summary() -> ExecutiveSummaryResult:
    timestamp = datetime.utcnow().isoformat()
    revenue_summary = ExecutiveRevenueSummary(total_arr=228000.0, revenue_at_risk=93000.0, forecasted_revenue_next_30_days=184800.0, unbilled_overages=500.0)
    high_priority_deals = [
        HighPriorityDealSummary(opportunity_id="OPP_201", name="Acme Corp - Enterprise Expansion", value=75000.0, win_probability=0.35, primary_risk="Pricing dispute and missed renewal meeting", recommended_action="Offer overage concession and schedule executive call"),
        HighPriorityDealSummary(opportunity_id="OPP_202", name="Globex - API Platform Migration", value=120000.0, win_probability=0.55, primary_risk="Technical API blocker affecting migration", recommended_action="Escalate to engineering and send migration checklist"),
        HighPriorityDealSummary(opportunity_id="OPP_203", name="Initech - Security Module Add-on", value=15000.0, win_probability=0.15, primary_risk="Customer ghosted the sales rep for 20 days", recommended_action="Reassign opportunity and send re-engagement email"),
    ]
    recommendations = [
        TodayRecommendation(action_type="offer_discount", target="Acme Corp", description="Offer a targeted overage concession and schedule a decision call before the billing-cycle deadline.", impact_value=75000.0, urgency="high"),
        TodayRecommendation(action_type="escalate_manager", target="Globex", description="Escalate the batch API 500 errors to engineering leadership and keep the customer updated today.", impact_value=120000.0, urgency="high"),
        TodayRecommendation(action_type="reassign_opportunity", target="Initech", description="Move the stalled security add-on to a senior account executive and send a concise re-engagement note.", impact_value=15000.0, urgency="medium"),
    ]
    report = """## Executive Overview
Business health is 65/100, with $228,000 ARR under management and $184,800 expected in the next 30 days.

## High-Risk Accounts
- Acme Corp: pricing objection, missed renewal meeting, and $21,000 expected churn exposure.
- Globex: technical blocker on the batch API creates the largest account risk at $64,800 expected exposure.
- Initech: 20 days of non-response has dropped expansion probability to 15%.

## Pipeline & Forecast Analysis
The active pipeline totals $210,000. Stalled opportunities represent roughly 43% of pipeline value, so the forecast depends on fast recovery action.

## Recommended Immediate Actions
- Approve the Acme pricing recovery plan.
- Escalate the Globex blocker to engineering leadership.
- Reassign Initech and send a re-engagement email.
"""
    return ExecutiveSummaryResult(
        timestamp=timestamp,
        business_health_score=65,
        morning_briefing="Business health is 65/100 with a $210,000 active pipeline and $228,000 ARR under management. Globex is the highest-risk account because a technical blocker threatens a $120,000 migration opportunity. Today should focus on engineering escalation, Acme pricing recovery, and Initech re-engagement.",
        executive_report=report,
        revenue_summary=revenue_summary,
        high_priority_deals=high_priority_deals,
        todays_recommendations=recommendations,
    )

class RevOpsOrchestrator:
    """Wrapper that uses deterministic output unless live ADK is explicitly enabled."""
    def __init__(self, model_name: Optional[str] = None, use_live_adk: Optional[bool] = None):
        self.model_name = model_name or settings.model_name
        self.use_live_adk = settings.use_live_adk if use_live_adk is None else use_live_adk

    async def execute_workflow(self, run_id: str) -> OrchestrationRunResult:
        if self.use_live_adk:
            try:
                from agents.orchestrator import RevOpsOrchestrator as LiveOrchestrator
                live = LiveOrchestrator(model_name=self.model_name)
                result = await live.execute_workflow(run_id)
                if hasattr(result, "mode"):
                    result.mode = "live_adk"
                return result
            except Exception as exc:
                return OrchestrationRunResult(
                    run_id=run_id,
                    status="partial_success",
                    timestamp=datetime.utcnow().isoformat(),
                    executive_summary=build_demo_summary(),
                    failures=[OrchestratorFailureLog(agent_name="live_adk_orchestrator", error_message=f"Live ADK failed; returned deterministic fallback. {exc}", timestamp=datetime.utcnow().isoformat())],
                    mode="deterministic_fallback",
                )
        return OrchestrationRunResult(run_id=run_id, status="success", timestamp=datetime.utcnow().isoformat(), executive_summary=build_demo_summary(), mode="deterministic_mock")
