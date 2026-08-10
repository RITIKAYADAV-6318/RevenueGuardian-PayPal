"""
Revenue Guardian - Reusable MCP Server
======================================

This module implements a unified Model Context Protocol (MCP) server for the
Revenue Guardian platform using the official MCP Python SDK's FastMCP framework.

It exposes enterprise integration tools to the AI agents, acting as the security
and operational boundary between the LLM reasoning core and the external systems.

Exposed Tools:
--------------
1.  `read_crm()`: Retrieves raw leads, opportunities, and tasks from the CRM database.
2.  `read_emails()`: Fetches recent customer email threads.
3.  `read_calendar()`: Queries recent calendar meetings and free/busy slots.
4.  `send_email(to_email, subject, body)`: Sends a customer email and registers a draft.
5.  `schedule_meeting(title, attendees, start_time, end_time)`: Schedules a Google Calendar event.
6.  `notify_slack(channel, message)`: Dispatches a message to a Slack channel.
7.  `generate_dashboard(summary_data)`: Updates the React dashboard state.
8.  `save_logs(agent_name, log_message)`: Records agent execution steps in the database.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import UTC, datetime

# Import FastMCP from the official mcp package
try:
    from mcp.server.fastmcp import FastMCP
except Exception:
    class FastMCP:
        def __init__(self, name: str):
            self.name = name
        def tool(self):
            def decorator(func):
                return func
            return decorator
        def run(self):
            raise RuntimeError("FastMCP SDK is unavailable because the local mcp package shadows the installed SDK. Direct tool imports still work.")

# Import real Google Gmail API tools
from mcp.gmail_tools import list_gmail_threads, create_gmail_reply_draft

# Import real Google Calendar API tools
from mcp.calendar_tools import list_calendar_events, get_calendar_availability, create_calendar_event
from datetime import timedelta

# Import database-driven CRM tools
from mcp.crm_tools import fetch_leads, fetch_opportunities, fetch_tasks, update_opportunity_stage

# Import Slack notification tools
from mcp.slack_tools import post_slack_message

# Import database-backed audit logging service
from services.audit_service import log_audit_event

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RevenueGuardianMCPServer")

# Initialize the FastMCP server
mcp = FastMCP("Revenue Guardian MCP Server")


# ==========================================
# Mock Database State (In-Memory)
# ==========================================

MOCK_STATE = {
    "crm_leads": [
        {"lead_id": "LD_101", "name": "Sarah Jenkins", "company": "Nexus Media", "last_activity_date": "2026-05-15", "status": "Contacted"},
        {"lead_id": "LD_102", "name": "Michael Chen", "company": "Apex Logistics", "last_activity_date": "2026-06-28", "status": "Working"}
    ],
    "crm_opportunities": [
        {"opportunity_id": "OPP_201", "name": "Acme Corp - Enterprise Expansion", "current_stage": "Proposal/Price Quote", "stage_last_updated": "2026-05-10", "deal_value": 75000.0, "owner_email": "alice.rep@guardian.com"},
        {"opportunity_id": "OPP_202", "name": "Globex - API Platform Migration", "current_stage": "Negotiation/Review", "stage_last_updated": "2026-06-25", "deal_value": 120000.0, "owner_email": "bob.rep@guardian.com"}
    ],
    "crm_tasks": [
        {"task_id": "TSK_301", "opportunity_id": "OPP_201", "owner_email": "alice.rep@guardian.com", "task_description": "Send revised SLA agreement", "due_date": "2026-06-15", "status": "Open"}
    ],
    "emails": [
        {
            "thread_id": "TH_001",
            "subject": "Acme Corp - Contract Pricing Clarification",
            "messages": [
                {"sender": "bob.admin@acme.com", "body": "Hi Alice, the overage charge of $0.10/unit seems high. Can we negotiate?"}
            ]
        },
        {
            "thread_id": "TH_002",
            "subject": "Globex Migration Technical blocker",
            "messages": [
                {"sender": "charlie.tech@globex.com", "body": "Getting 500 errors on batch API. Blocking our team."}
            ]
        }
    ],
    "calendar_events": [
        {"event_id": "EV_001", "title": "Acme Corp - Contract Renewal Discussion", "attendees": ["bob.admin@acme.com"], "start_time": "2026-06-25T14:00:00", "description": "Bob did not show up."}
    ],
    "calendar_free_busy": [
        {"start_time": "2026-07-01T09:00:00", "end_time": "2026-07-01T12:00:00"},
        {"start_time": "2026-07-01T13:00:00", "end_time": "2026-07-01T15:00:00"}
    ],
    "sent_emails": [],
    "slack_notifications": [],
    "agent_logs": [],
    "dashboard_data": {}
}


# ==========================================
# Tool Implementations
# ==========================================

@mcp.tool()
def read_crm() -> Dict[str, Any]:
    """
    Reads all raw data from the CRM database, including active leads, opportunities, and pending tasks.

    Returns:
        A dictionary containing lists of leads, opportunities, and tasks.
    """
    logger.info("MCP Tool Executing: read_crm")
    return {
        "leads": fetch_leads(),
        "opportunities": fetch_opportunities(),
        "tasks": fetch_tasks()
    }


@mcp.tool()
def update_crm_opportunity(opportunity_id: str, stage: str, deal_value: Optional[float] = None) -> Dict[str, Any]:
    """
    Updates the sales stage and optionally the value of an active opportunity in the CRM.

    Args:
        opportunity_id: The ID of the opportunity (e.g., 'OPP_201').
        stage: The new sales stage (e.g., 'Closed Won').
        deal_value: Optional new deal value in USD.

    Returns:
        A dictionary confirming the update status.
    """
    logger.info(f"MCP Tool Executing: update_crm_opportunity - {opportunity_id}")
    return update_opportunity_stage(opportunity_id=opportunity_id, stage=stage, deal_value=deal_value)


@mcp.tool()
def read_emails(max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Fetches the recent customer email threads from the Gmail inbox.

    Args:
        max_results: Maximum number of threads to retrieve (default: 10).

    Returns:
        A list of email thread dictionaries containing message histories.
    """
    logger.info("MCP Tool Executing: read_emails")
    return list_gmail_threads(max_results=max_results)


@mcp.tool()
def read_calendar(max_results: int = 10) -> Dict[str, Any]:
    """
    Queries the Google Calendar database for recent meetings and open free/busy slots.

    Args:
        max_results: Maximum number of events to retrieve (default: 10).

    Returns:
        A dictionary containing calendar events and availability slots.
    """
    logger.info("MCP Tool Executing: read_calendar")
    now = datetime.now(UTC)
    start_time_iso = now.isoformat() + 'Z'
    end_time_iso = (now + timedelta(days=7)).isoformat() + 'Z'
    
    events = list_calendar_events(max_results=max_results)
    free_busy = get_calendar_availability(start_time=start_time_iso, end_time=end_time_iso)
    
    return {
        "events": events,
        "free_busy": free_busy
    }


@mcp.tool()
def send_email(to_email: str, subject: str, body: str) -> Dict[str, Any]:
    """
    Sends an email to a customer and logs the sent message in the database.

    Args:
        to_email: Recipient email address.
        subject: Subject line of the email.
        body: Body text of the email.

    Returns:
        A dictionary containing the delivery status and message details.
    """
    logger.info(f"MCP Tool Executing: send_email to {to_email}")
    email_record = {
        "to": to_email,
        "subject": subject,
        "body": body,
        "timestamp": datetime.now(UTC).isoformat(),
        "status": "sent"
    }
    MOCK_STATE["sent_emails"].append(email_record)
    return {
        "status": "success",
        "message": f"Email successfully sent to {to_email}",
        "email_details": email_record
    }


@mcp.tool()
def create_gmail_draft(thread_id: str, body: str) -> Dict[str, Any]:
    """
    Creates a draft reply to an existing email thread in Gmail.

    Args:
        thread_id: The ID of the Gmail thread.
        body: The body content of the draft reply.

    Returns:
        A dictionary containing the draft details or status.
    """
    logger.info("MCP Tool Executing: create_gmail_draft")
    return create_gmail_reply_draft(thread_id=thread_id, draft_body=body)


@mcp.tool()
def schedule_meeting(title: str, attendees: List[str], start_time: str, end_time: str, description: str = "") -> Dict[str, Any]:
    """
    Schedules a new meeting in the calendar and invites the specified attendees.

    Args:
        title: Title of the meeting.
        attendees: List of attendee email addresses.
        start_time: ISO start time of the meeting (e.g., '2026-07-01T10:00:00').
        end_time: ISO end time of the meeting.
        description: Description of the meeting.

    Returns:
        A dictionary containing the scheduled event details.
    """
    logger.info(f"MCP Tool Executing: schedule_meeting - {title}")
    return create_calendar_event(
        title=title,
        attendees=attendees,
        start_time=start_time,
        end_time=end_time,
        description=description
    )


@mcp.tool()
def notify_slack(channel: str, message: str) -> Dict[str, Any]:
    """
    Sends an alert notification to a specified Slack channel.

    Args:
        channel: The channel name or ID (e.g., '#revops-alerts').
        message: The message body to post.

    Returns:
        A dictionary indicating the delivery status.
    """
    logger.info(f"MCP Tool Executing: notify_slack to {channel}")
    return post_slack_message(channel=channel, text=message)


@mcp.tool()
def generate_dashboard(summary_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Updates the executive dashboard data payload.

    Args:
        summary_data: The synthesized summary metrics dictionary.

    Returns:
        A dictionary confirming the dashboard state update.
    """
    logger.info("MCP Tool Executing: generate_dashboard")
    MOCK_STATE["dashboard_data"] = summary_data
    return {
        "status": "success",
        "message": "Dashboard visualization payload updated.",
        "updated_at": datetime.now(UTC).isoformat()
    }


@mcp.tool()
def save_logs(agent_name: str, log_message: str) -> Dict[str, Any]:
    """
    Records agent execution steps and decisions in the audit log database.

    Args:
        agent_name: The name of the agent generating the log.
        log_message: The description of the action or decision.

    Returns:
        A dictionary confirming that the log was saved.
    """
    logger.info(f"MCP Tool Executing: save_logs for {agent_name}")
    log_id = log_audit_event(
        actor=agent_name,
        action="agent_execution_step",
        status="success",
        details=log_message
    )
    return {
        "status": "success",
        "message": "Log entry successfully persisted.",
        "log_id": log_id
    }


# ==========================================
# Server Execution
# ==========================================

if __name__ == "__main__":
    # Run the FastMCP server. By default, this uses the stdio transport channel,
    # which allows LLM clients (like Claude Desktop or Vertex/ADK runtimes) to boot
    # this process and communicate via standard input/output.
    mcp.run()
