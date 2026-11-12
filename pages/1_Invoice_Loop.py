"""Invoice recovery loop: drives the live Revenue Guardian API on Render."""
import httpx
import streamlit as st

DEFAULT_API_URL = "https://revenue-guardian-api.onrender.com"
TIMEOUT = 60.0  # Render free tier can take a while to wake up

st.set_page_config(page_title="Invoice Loop | Revenue Guardian", layout="wide")


def get_api_url() -> str:
    try:
        return str(st.secrets.get("API_URL", DEFAULT_API_URL)).rstrip("/")
    except Exception:
        return DEFAULT_API_URL


API_URL = get_api_url()


def call(method: str, path: str, json_body=None, params=None):
    """Return (status_code, parsed_body). Never raises on HTTP errors."""
    headers = {}
    token = st.session_state.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.request(
            method, f"{API_URL}{path}", json=json_body, params=params,
            headers=headers, timeout=TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return 0, {"detail": f"Could not reach API: {exc}"}
    try:
        body = resp.json()
    except ValueError:
        body = {"detail": resp.text[:500]}
    return resp.status_code, body


def show_error(status: int, body):
    detail = body.get("detail") if isinstance(body, dict) else body
    if status == 401:
        st.error("Session expired or not logged in. Log in again from the sidebar.")
    else:
        st.error(f"Request failed ({status}): {detail}")


# ---------------------------------------------------------------- header
st.title("Invoice Recovery Loop")
st.caption(
    "Live calls to the Revenue Guardian API on Render. "
    "REAL: PayPal sandbox invoices, signed webhooks, tamper-evident audit chain, "
    "approval gate. SIMULATED: the payment shortcut button. "
    "No reminder is ever sent without a human approval."
)

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.subheader("API")
    st.code(API_URL, language=None)
    if st.button("Check /health"):
        status, body = call("GET", "/health")
        st.json(body) if status == 200 else show_error(status, body)

    st.subheader("Login")
    if st.session_state.get("token"):
        user = st.session_state.get("user", {})
        st.success(f"Signed in as {user.get('username', '?')} ({user.get('role', '?')})")
        if st.button("Log out"):
            st.session_state.pop("token", None)
            st.session_state.pop("user", None)
            st.rerun()
    else:
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in")
        if submitted:
            status, body = call("POST", "/auth/login", {"username": username, "password": password})
            if status == 200:
                st.session_state["token"] = body["access_token"]
                st.session_state["user"] = body.get("user", {})
                st.rerun()
            else:
                show_error(status, body)

if not st.session_state.get("token"):
    st.info("Log in from the sidebar to run the loop.")
    st.stop()

# ---------------------------------------------------------------- tabs
tab_inv, tab_sweep, tab_appr, tab_kpi, tab_audit = st.tabs(
    ["1. Invoices", "2. Overdue sweep", "3. Approvals", "4. KPIs", "5. Audit verify"]
)

with tab_inv:
    st.markdown("Invoices created in the PayPal **sandbox** and synced into the system.")
    status_filter = st.selectbox("Status filter", ["", "OPEN", "SENT", "PAID", "OVERDUE"], index=0,
                                 help="Leave blank for all. Names depend on your data.")
    if st.button("Refresh invoices"):
        status, body = call("GET", "/api/invoices", params={"status": status_filter} if status_filter else None)
        if status == 200:
            if body:
                st.dataframe(body, use_container_width=True)
            else:
                st.warning("No invoices found. Create one with your invoice script first.")
        else:
            show_error(status, body)

with tab_sweep:
    st.markdown(
        "The sweep finds overdue invoices and **proposes** reminders. "
        "Gemini writes the draft; a template is used if the model is unavailable. "
        "Nothing is sent. Each proposal waits for human approval."
    )
    if st.button("Run overdue sweep", type="primary"):
        status, body = call("POST", "/api/invoices/sweep")
        if status == 200:
            c1, c2, c3 = st.columns(3)
            c1.metric("Proposed", body.get("proposed", 0))
            c2.metric("AI-drafted (Gemini)", body.get("ai_drafted", 0))
            c3.metric("Template fallback", body.get("template_fallback", 0))
            if body.get("demo_threshold_minutes"):
                st.caption(f"Demo mode: invoices count as overdue after {body['demo_threshold_minutes']} minutes.")
            for item in body.get("approvals", []):
                with st.expander(f"Proposal #{item.get('id', '?')} — {item.get('target', '')}"):
                    st.json(item)
            if not body.get("proposed"):
                st.warning("Nothing proposed. No invoice is overdue yet, or proposals already exist.")
        else:
            show_error(status, body)

with tab_appr:
    st.markdown("Human-in-the-loop gate: review each proposal, then approve or reject.")
    appr_status = st.selectbox("Show", ["pending", "approved", "rejected", ""],
                               format_func=lambda s: s or "all")
    if st.button("Load approvals"):
        status, body = call("GET", "/api/approvals", params={"status": appr_status})
        if status == 200:
            st.session_state["approvals"] = body
        else:
            show_error(status, body)

    approvals = st.session_state.get("approvals", [])
    if approvals:
        st.dataframe(approvals, use_container_width=True)
        ids = [a.get("id") for a in approvals if a.get("id") is not None]
        if ids:
            chosen = st.selectbox("Approval ID", ids)
            note = st.text_input("Note (optional)")
            col_a, col_r = st.columns(2)

            def decide(candidates):
                last = (0, {})
                for value in candidates:
                    last = call("POST", f"/api/approvals/{chosen}/decision",
                                {"decision": value, "note": note or None})
                    if last[0] == 200:
                        return last
                    if last[0] not in (400, 422):
                        return last
                return last

            if col_a.button("Approve", type="primary"):
                status, body = decide(["approve", "approved"])
                st.success("Decision recorded.") if status == 200 else show_error(status, body)
                if status == 200:
                    st.json(body)
            if col_r.button("Reject"):
                status, body = decide(["reject", "rejected"])
                st.success("Decision recorded.") if status == 200 else show_error(status, body)
                if status == 200:
                    st.json(body)

        st.divider()
        st.markdown("**Payment step**")
        st.caption(
            "Real path: the buyer pays the invoice in PayPal sandbox and PayPal sends a signed webhook. "
            "The button below is a SIMULATED shortcut for the demo only."
        )
        pay_id = st.number_input("Proposal ID to mark as paid (simulated)", min_value=1, step=1)
        if st.button("Simulate payment (demo shortcut)"):
            status, body = call("POST", f"/api/invoices/{int(pay_id)}/simulate-payment")
            st.success("Simulated payment recorded.") if status == 200 else show_error(status, body)
            if status == 200:
                st.json(body)

with tab_kpi:
    st.markdown("Recovery KPIs, computed from real webhook-confirmed payments and demo shortcuts.")
    if st.button("Refresh KPIs"):
        status, body = call("GET", "/api/kpis")
        if status == 200:
            numeric = {k: v for k, v in body.items() if isinstance(v, (int, float))} if isinstance(body, dict) else {}
            if numeric:
                cols = st.columns(min(len(numeric), 4))
                for i, (k, v) in enumerate(numeric.items()):
                    cols[i % len(cols)].metric(k.replace("_", " ").title(), v)
            st.json(body)
        else:
            show_error(status, body)
    if st.button("Show webhook events"):
        status, body = call("GET", "/api/events", params={"limit": 20})
        if status == 200:
            st.dataframe(body, use_container_width=True) if body else st.info("No events yet.")
        else:
            show_error(status, body)

with tab_audit:
    st.markdown("Every login, approval and payment is written to a hash-chained audit log. Verify it has not been tampered with.")
    if st.button("Verify audit chain", type="primary"):
        status, body = call("GET", "/api/audit/verify")
        st.json(body) if status == 200 else show_error(status, body)
    if st.button("Show recent audit log"):
        status, body = call("GET", "/api/audit/logs", params={"limit": 20})
        if status == 200:
            st.dataframe(body, use_container_width=True)
        else:
            show_error(status, body)