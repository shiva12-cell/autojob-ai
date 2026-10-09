"""
Tests for PostgreSQL / Neon cloud adaptation:
- URL normalization & SSL mode detection
- DBConnectionWrapper parameter translation
- Fallback mechanics
"""

import pytest
from src.database.db import (
    normalize_database_url,
    DBConnectionWrapper,
    get_db_connection,
    db
)


def test_normalize_database_url():
    # Convert postgres:// to postgresql://
    raw = "postgres://user:pass@ep-cool-neon.tech/neondb"
    normalized = normalize_database_url(raw)
    assert normalized.startswith("postgresql://")
    assert "sslmode=require" in normalized

    # Preserve already compliant postgresql://
    raw_ssl = "postgresql://user:pass@ep-cool-neon.tech/neondb?sslmode=require"
    assert normalize_database_url(raw_ssl) == raw_ssl

    # Non-neon local URL should not append sslmode unconditionally
    local_url = "postgresql://postgres:secret@localhost:5432/mytest"
    assert normalize_database_url(local_url) == local_url


def test_db_connection_wrapper_sqlite_mode():
    conn = get_db_connection()
    assert conn.is_postgres is False

    # Standard queries execute properly
    with conn:
        cur = conn.execute("SELECT 1 AS num;")
        row = cur.fetchone()
        assert row["num"] == 1
    conn.close()


def test_db_connection_wrapper_postgres_query_translation():
    class DummyCursor:
        def __init__(self):
            self.executed_sql = None
            self.executed_params = None

        def execute(self, sql, params):
            self.executed_sql = sql
            self.executed_params = params

        def fetchone(self):
            return {"status": "ok"}

    class DummyConn:
        def __init__(self):
            self.cursor_instance = DummyCursor()

        def cursor(self, cursor_factory=None):
            return self.cursor_instance

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def close(self):
            pass

    dummy_raw = DummyConn()
    wrapper = DBConnectionWrapper(dummy_raw, is_postgres=True)
    assert wrapper.is_postgres is True

    wrapper.execute("SELECT * FROM users WHERE email = ? AND consent_acknowledged = ?", ("test@example.com", 1))
    assert dummy_raw.cursor_instance.executed_sql == "SELECT * FROM users WHERE email = %s AND consent_acknowledged = %s"
    assert dummy_raw.cursor_instance.executed_params == ("test@example.com", 1)


def test_active_db_is_postgres_property():
    # On local test without DATABASE_URL, is_postgres is False
    assert db.is_postgres is False
