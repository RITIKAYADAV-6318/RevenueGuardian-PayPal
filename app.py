"""Revenue Guardian Streamlit dashboard."""
import asyncio
from datetime import datetime
import pandas as pd
import plotly.express as px
import streamlit as st
from agents.deterministic_workflow import RevOpsOrchestrator

st.set_page_config(page_title="Revenue Guardian - Executive Dashboard", page_icon="shield", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
html, body, [class*="css"] { font-family: Inter, Segoe UI, sans-serif; }
.metric-card { background: rgba(17, 25, 40, 0.92); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 18px; margin-bottom: 12px; }
.metric-title { font-size: 13px; color: #9aa4b2; font-weight: 700; text-transform: uppercase; }
.metric-value { font-size: 28px; font-weight: 800; color: #ffffff; margin: 4px 0; }
.metric-delta { font-size: 13px; color: #9aa4b2; }
.log-container { background-color: #0d1117; border: 1px solid #21262d; border-radius: 8px; padding: 14px; max-height: 390px; overflow-y: auto; }
.log-line { margin-bottom: 8px; font-size: 13px; line-height: 1.4; }
.log-timestamp { color: #58a6ff; margin-right: 8px; }
.log-agent { color: #ff7b72; font-weight: 700; margin-right: 8px; }
.log-message { color: #c9d1d9; }
</style>
""", unsafe_allow_html=True)

st.sidebar.image("https://img.icons8.com/nolan/96/shield.png", width=70)
st.sidebar.title("Revenue Guardian")
st.sidebar.caption("Autonomous RevOps audit dashboard")
st.sidebar.markdown("---")
st.sidebar.subheader("System Status")
st.sidebar.markdown("- ADK/Gemini: optional live mode")
st.sidebar.markdown("- CRM database: SQLite")
st.sidebar.markdown("- Gmail/Calendar: live OAuth or mock fallback")
st.sidebar.markdown("---")
run_workflow_button = st.sidebar.button("Run Autonomous Audit", use_container_width=True)

st.title("Revenue Guardian: Operations Center")
st.subheader("Revenue leakage auditing, account risk scoring, and recovery planning")
st.markdown("---")
kpi_cols = st.columns(4)
health_placeholder = kpi_cols[0].empty(); arr_placeholder = kpi_cols[1].empty(); risk_placeholder = kpi_cols[2].empty(); forecast_placeholder = kpi_cols[3].empty()
col_left, col_right = st.columns([3, 2])
with col_right:
    st.subheader("Live Agent Activity")
    progress_bar = st.progress(0)
    logs_placeholder = st.empty()
with col_left:
    report_tabs = st.tabs(["Today's Recommendations", "Executive Report", "Analytics & Forecasts"])
    with report_tabs[0]:
        rec_placeholder = st.empty(); rec_placeholder.info("Click 'Run Autonomous Audit' in the sidebar to start the analysis.")
    with report_tabs[1]:
        report_placeholder = st.empty(); report_placeholder.info("Detailed executive report will appear here after the workflow completes.")
    with report_tabs[2]:
        chart_placeholder = st.empty(); chart_placeholder.info("Financial charts will load after the audit runs.")

def render_metric(target, title: str, value: str, note: str, color: str = "#ffffff") -> None:
    target.markdown(f"""<div class="metric-card"><div class="metric-title">{title}</div><div class="metric-value" style="color: {color};">{value}</div><div class="metric-delta">{note}</div></div>""", unsafe_allow_html=True)

async def execute_live_workflow() -> None:
    progress_bar.progress(0)
    steps = [("Manager Agent", "Starting revenue analysis", 5), ("CRM Agent", "Reading CRM pipeline data", 20), ("Email Agent", "Scanning customer communication signals", 40), ("Calendar Agent", "Checking meetings and follow-up history", 55), ("Prediction Agent", "Calculating win probabilities and revenue at risk", 75), ("Recovery Agent", "Drafting recovery actions", 90), ("Summary Agent", "Preparing executive dashboard output", 100)]
    history = []
    for agent, message, pct in steps:
        timestamp = datetime.now().strftime("%H:%M:%S")
        history.append(f"<div class='log-line'><span class='log-timestamp'>[{timestamp}]</span><span class='log-agent'>{agent}:</span><span class='log-message'>{message}</span></div>")
        logs_placeholder.markdown(f"<div class='log-container'>{''.join(history)}</div>", unsafe_allow_html=True)
        progress_bar.progress(pct)
        await asyncio.sleep(0.35)
    run_id = f"run_{int(datetime.utcnow().timestamp())}"
    with st.spinner("Compiling structured outputs..."):
        result = await RevOpsOrchestrator().execute_workflow(run_id)
    if result.failures:
        st.warning("Workflow used fallback or completed with warnings.")
        for failure in result.failures:
            st.markdown(f"- **{failure.agent_name}**: {failure.error_message}")
    if not result.executive_summary:
        st.error("No executive summary was generated."); return
    summary = result.executive_summary
    render_metric(health_placeholder, "Business Health", f"{summary.business_health_score}/100", result.mode, "#56ab2f")
    render_metric(arr_placeholder, "Total ARR", f"${summary.revenue_summary.total_arr:,.0f}", "Active contracts")
    render_metric(risk_placeholder, "Revenue at Risk", f"${summary.revenue_summary.revenue_at_risk:,.0f}", "Expected loss", "#e53935")
    render_metric(forecast_placeholder, "30-Day Forecast", f"${summary.revenue_summary.forecasted_revenue_next_30_days:,.0f}", "Weighted outlook", "#58a6ff")
    with rec_placeholder.container():
        st.markdown("### Actions Requiring Approval Today")
        for rec in summary.todays_recommendations:
            badge_color = "#e53935" if rec.urgency == "high" else "#d4a373"
            st.markdown(f"""<div style="padding: 14px; border-left: 5px solid {badge_color}; background-color: #161b22; border-radius: 0 8px 8px 0; margin-bottom: 10px;"><div style="display: flex; justify-content: space-between; gap: 12px; align-items: center;"><span style="font-weight: 700; color: #ffffff;">{rec.target} - {rec.action_type.replace('_', ' ').title()}</span><span style="background-color: {badge_color}; padding: 2px 8px; border-radius: 8px; font-size: 11px; font-weight: 700; text-transform: uppercase;">{rec.urgency}</span></div><p style="margin: 8px 0 0 0; color: #c9d1d9;">{rec.description}</p><div style="margin-top: 8px; font-size: 12px; color: #9aa4b2; font-weight: 700;">Estimated impact: <span style="color: #56ab2f;">${rec.impact_value:,.2f}</span></div></div>""", unsafe_allow_html=True)
    with report_placeholder.container():
        st.markdown(summary.morning_briefing); st.markdown(summary.executive_report)
    with chart_placeholder.container():
        df_rev = pd.DataFrame({"Category": ["Total ARR", "Revenue at Risk", "30-Day Forecast"], "Value (USD)": [summary.revenue_summary.total_arr, summary.revenue_summary.revenue_at_risk, summary.revenue_summary.forecasted_revenue_next_30_days]})
        fig = px.bar(df_rev, x="Category", y="Value (USD)", color="Category", color_discrete_map={"Total ARR": "#2e7d32", "Revenue at Risk": "#c62828", "30-Day Forecast": "#1565c0"}, title="Revenue Overview Breakdown")
        fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#ffffff", showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

if not run_workflow_button:
    render_metric(health_placeholder, "Business Health", "--/100", "Awaiting audit", "#9aa4b2")
    render_metric(arr_placeholder, "Total ARR", "$--", "Awaiting audit", "#9aa4b2")
    render_metric(risk_placeholder, "Revenue at Risk", "$--", "Awaiting audit", "#9aa4b2")
    render_metric(forecast_placeholder, "30-Day Forecast", "$--", "Awaiting audit", "#9aa4b2")
    logs_placeholder.info("Click 'Run Autonomous Audit' in the sidebar to start.")
else:
    asyncio.run(execute_live_workflow())
