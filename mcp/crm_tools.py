"""SQLite-backed CRM tools for the MCP server and tests."""
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from config.settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CRMMCPTools")
DB_PATH = settings.sqlite_db_path

def init_crm_database() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS leads (lead_id TEXT PRIMARY KEY, name TEXT NOT NULL, company TEXT NOT NULL, last_activity_date TEXT NOT NULL, status TEXT NOT NULL)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS opportunities (opportunity_id TEXT PRIMARY KEY, name TEXT NOT NULL, current_stage TEXT NOT NULL, stage_last_updated TEXT NOT NULL, deal_value REAL NOT NULL, owner_email TEXT NOT NULL)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, owner_email TEXT NOT NULL, task_description TEXT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL, FOREIGN KEY (opportunity_id) REFERENCES opportunities(opportunity_id))""")
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM leads")
    if cursor.fetchone()[0] == 0:
        logger.info("Seeding CRM database with demo records...")
        cursor.executemany("INSERT INTO leads VALUES (?, ?, ?, ?, ?)", [("LD_101", "Sarah Jenkins", "Nexus Media", "2026-05-15", "Contacted"), ("LD_102", "Michael Chen", "Apex Logistics", "2026-06-28", "Working"), ("LD_103", "Elena Rostova", "Siberia Tech", "2026-04-10", "Qualified")])
        cursor.executemany("INSERT INTO opportunities VALUES (?, ?, ?, ?, ?, ?)", [("OPP_201", "Acme Corp - Enterprise Expansion", "Proposal/Price Quote", "2026-05-10", 75000.0, "alice.rep@guardian.com"), ("OPP_202", "Globex - API Platform Migration", "Negotiation/Review", "2026-06-25", 120000.0, "bob.rep@guardian.com"), ("OPP_203", "Initech - Security Module Add-on", "Discovery", "2026-06-01", 15000.0, "alice.rep@guardian.com")])
        cursor.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", [("TSK_301", "OPP_201", "alice.rep@guardian.com", "Send revised SLA agreement", "2026-06-15", "Open"), ("TSK_302", "OPP_202", "bob.rep@guardian.com", "Follow up on security review sign-off", "2026-06-29", "Open"), ("TSK_303", "OPP_203", "alice.rep@guardian.com", "Schedule demo call", "2026-07-05", "Open")])
        conn.commit()
    conn.close()

init_crm_database()

def _fetch_all(query: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(query)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

def fetch_leads() -> List[Dict[str, Any]]:
    return _fetch_all("SELECT * FROM leads")

def fetch_opportunities() -> List[Dict[str, Any]]:
    return _fetch_all("SELECT * FROM opportunities")

def fetch_tasks() -> List[Dict[str, Any]]:
    return _fetch_all("SELECT * FROM tasks")

def update_opportunity_stage(opportunity_id: str, stage: str, deal_value: Optional[float] = None) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.utcnow().date().isoformat()
    if deal_value is not None:
        cursor.execute("UPDATE opportunities SET current_stage = ?, stage_last_updated = ?, deal_value = ? WHERE opportunity_id = ?", (stage, today_str, deal_value, opportunity_id))
    else:
        cursor.execute("UPDATE opportunities SET current_stage = ?, stage_last_updated = ? WHERE opportunity_id = ?", (stage, today_str, opportunity_id))
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    if changed == 0:
        return {"status": "error", "message": f"Opportunity '{opportunity_id}' not found."}
    return {"status": "success", "opportunity_id": opportunity_id, "updated_fields": {"current_stage": stage, "stage_last_updated": today_str, "deal_value": deal_value if deal_value is not None else "unchanged"}}
