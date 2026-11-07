"""
Revenue Guardian - Pytest Configuration & Fixtures
===================================================

This module configures the pytest environment, setting up temporary test databases
and mocking external API dependencies.
"""

import os
import pytest
import sqlite3
import shutil

# Force the configuration to use a test database
TEST_DB_PATH = "test_crm.db"

@pytest.fixture(scope="function", autouse=True)
def setup_test_database():
    """
    Fixture to set up a clean, seeded temporary SQLite database
    before each test, and remove it afterward.
    """
    # 1. Override the DB_PATH in the modules
    import mcp.crm_tools
    import services.audit_service
    import services.approval_service
    import services.webhook_service
    
    mcp.crm_tools.DB_PATH = TEST_DB_PATH
    services.audit_service.DB_PATH = TEST_DB_PATH
    services.approval_service.DB_PATH = TEST_DB_PATH
    services.webhook_service.DB_PATH = TEST_DB_PATH

    # Remove any existing test database file
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    # 2. Initialize the tables and seed them
    mcp.crm_tools.init_crm_database()
    services.audit_service.init_audit_log_database()
    services.approval_service.init_approval_database()
    services.webhook_service.init_webhook_database()
    services.webhook_service.reset_rate_limits()

    yield

    # 3. Clean up the test database file
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass  # Handle Windows file lock issues gracefully
