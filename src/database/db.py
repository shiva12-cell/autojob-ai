"""
Database layer for AutoJob AI multi-user platform.
Supports dual-engine storage:
- PostgreSQL (e.g. Neon Serverless Postgres) when DATABASE_URL is configured
- Local SQLite (data/autojob.db) with WAL mode enabled as fallback or default
"""

import json
import sqlite3
import os
import time
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

logger = logging.getLogger("autojob.db")

DB_DIR = Path("data")
DB_FILE = DB_DIR / "autojob.db"


class DBConnectionWrapper:
    """
    Unified database connection wrapper providing identical execute/context API
    for SQLite and PostgreSQL (Neon).
    """

    def __init__(self, raw_conn, is_postgres: bool = False):
        self._conn = raw_conn
        self.is_postgres = is_postgres

    def execute(self, sql: str, params: tuple = ()):
        if self.is_postgres:
            pg_sql = sql.replace("?", "%s")
            cur = self._conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(pg_sql, params)
            return cur
        else:
            return self._conn.execute(sql, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)


def normalize_database_url(url: str) -> str:
    """Normalizes Postgres URL and ensures required SSL mode for Neon."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode=" not in url and ("neon.tech" in url or "amazonaws.com" in url):
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def get_db_connection() -> DBConnectionWrapper:
    """
    Returns a unified connection wrapper for PostgreSQL (Neon) if DATABASE_URL is set,
    otherwise returns a SQLite connection with WAL mode enabled.
    """
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url and PSYCOPG2_AVAILABLE:
        try:
            normalized_url = normalize_database_url(db_url)
            pg_conn = psycopg2.connect(normalized_url)
            return DBConnectionWrapper(pg_conn, is_postgres=True)
        except Exception as e:
            logger.warning("Failed to connect to PostgreSQL at %s... Falling back to local SQLite: %s", db_url[:25], e)

    # Fallback to local SQLite
    DB_DIR.mkdir(parents=True, exist_ok=True)
    sqlite_conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_conn.execute("PRAGMA journal_mode = WAL;")
    sqlite_conn.execute("PRAGMA foreign_keys = ON;")
    return DBConnectionWrapper(sqlite_conn, is_postgres=False)


def _init_sqlite(conn: DBConnectionWrapper):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                naukri_profile_url TEXT DEFAULT '',
                consent_acknowledged INTEGER DEFAULT 0,
                consent_acknowledged_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                full_name TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                location TEXT DEFAULT '',
                headline TEXT DEFAULT '',
                current_title TEXT DEFAULT '',
                current_company TEXT DEFAULT '',
                current_ctc REAL DEFAULT 0.0,
                expected_ctc REAL DEFAULT 0.0,
                notice_period_days INTEGER DEFAULT 30,
                years_of_experience REAL DEFAULT 0.0,
                skills TEXT DEFAULT '[]',
                target_roles TEXT DEFAULT '["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"]',
                education TEXT DEFAULT '{}',
                screening_answers TEXT DEFAULT '{}',
                resume_path TEXT DEFAULT '',
                resume_name TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id TEXT PRIMARY KEY,
                threshold_pct REAL DEFAULT 65.0,
                keywords TEXT DEFAULT 'data analyst',
                location TEXT DEFAULT 'any',
                min_salary REAL DEFAULT 0.0,
                max_salary REAL DEFAULT 99.0,
                experience_years INTEGER DEFAULT 0,
                job_age_days INTEGER DEFAULT 0,
                work_mode TEXT DEFAULT 'all',
                sort_by TEXT DEFAULT 'relevance',
                apply_mode TEXT DEFAULT 'all',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id TEXT PRIMARY KEY,
                naukri_cookies TEXT DEFAULT '[]',
                naukri_username TEXT DEFAULT '',
                naukri_connected_at TEXT DEFAULT '',
                google_sheet_id TEXT DEFAULT '',
                google_sheet_url TEXT DEFAULT '',
                google_token_json TEXT DEFAULT '',
                gmail_id TEXT DEFAULT '',
                is_authorized INTEGER DEFAULT 0,
                authorized_at TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_applications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT DEFAULT '',
                platform TEXT DEFAULT 'Naukri',
                url TEXT DEFAULT '',
                match_score REAL DEFAULT 0.0,
                salary_lpa TEXT DEFAULT '',
                experience_req TEXT DEFAULT '',
                status TEXT DEFAULT 'Applied',
                applied_at TEXT NOT NULL,
                notes TEXT DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_user_id ON user_applications(user_id);")

        for col_stmt in [
            "ALTER TABLE user_profiles ADD COLUMN target_roles TEXT DEFAULT '[\"Data Analyst\", \"Business Analyst\", \"AI Product Owner\", \"Data Engineer\"]'",
            "ALTER TABLE user_credentials ADD COLUMN google_sheet_url TEXT DEFAULT ''",
            "ALTER TABLE user_credentials ADD COLUMN gmail_id TEXT DEFAULT ''",
            "ALTER TABLE user_credentials ADD COLUMN is_authorized INTEGER DEFAULT 0",
            "ALTER TABLE user_credentials ADD COLUMN authorized_at TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN consent_acknowledged INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN consent_acknowledged_at TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_stmt)
            except Exception:
                pass


def _init_postgres(conn: DBConnectionWrapper):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                naukri_profile_url TEXT DEFAULT '',
                consent_acknowledged INTEGER DEFAULT 0,
                consent_acknowledged_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                full_name TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                location TEXT DEFAULT '',
                headline TEXT DEFAULT '',
                current_title TEXT DEFAULT '',
                current_company TEXT DEFAULT '',
                current_ctc REAL DEFAULT 0.0,
                expected_ctc REAL DEFAULT 0.0,
                notice_period_days INTEGER DEFAULT 30,
                years_of_experience REAL DEFAULT 0.0,
                skills TEXT DEFAULT '[]',
                target_roles TEXT DEFAULT '["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"]',
                education TEXT DEFAULT '{}',
                screening_answers TEXT DEFAULT '{}',
                resume_path TEXT DEFAULT '',
                resume_name TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                threshold_pct REAL DEFAULT 65.0,
                keywords TEXT DEFAULT 'data analyst',
                location TEXT DEFAULT 'any',
                min_salary REAL DEFAULT 0.0,
                max_salary REAL DEFAULT 99.0,
                experience_years INTEGER DEFAULT 0,
                job_age_days INTEGER DEFAULT 0,
                work_mode TEXT DEFAULT 'all',
                sort_by TEXT DEFAULT 'relevance',
                apply_mode TEXT DEFAULT 'all',
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                naukri_cookies TEXT DEFAULT '[]',
                naukri_username TEXT DEFAULT '',
                naukri_connected_at TEXT DEFAULT '',
                google_sheet_id TEXT DEFAULT '',
                google_sheet_url TEXT DEFAULT '',
                google_token_json TEXT DEFAULT '',
                gmail_id TEXT DEFAULT '',
                is_authorized INTEGER DEFAULT 0,
                authorized_at TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_applications (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT DEFAULT '',
                platform TEXT DEFAULT 'Naukri',
                url TEXT DEFAULT '',
                match_score REAL DEFAULT 0.0,
                salary_lpa TEXT DEFAULT '',
                experience_req TEXT DEFAULT '',
                status TEXT DEFAULT 'Applied',
                applied_at TEXT NOT NULL,
                notes TEXT DEFAULT ''
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_user_id ON user_applications(user_id);")

        for col_stmt in [
            "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS target_roles TEXT DEFAULT '[\"Data Analyst\", \"Business Analyst\", \"AI Product Owner\", \"Data Engineer\"]'",
            "ALTER TABLE user_credentials ADD COLUMN IF NOT EXISTS google_sheet_url TEXT DEFAULT ''",
            "ALTER TABLE user_credentials ADD COLUMN IF NOT EXISTS gmail_id TEXT DEFAULT ''",
            "ALTER TABLE user_credentials ADD COLUMN IF NOT EXISTS is_authorized INTEGER DEFAULT 0",
            "ALTER TABLE user_credentials ADD COLUMN IF NOT EXISTS authorized_at TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_acknowledged INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS consent_acknowledged_at TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(col_stmt)
            except Exception:
                pass


def init_db():
    """Initializes database schema based on active connection backend."""
    conn = get_db_connection()
    if conn.is_postgres:
        _init_postgres(conn)
    else:
        _init_sqlite(conn)
    conn.close()


class DatabaseManager:
    """Provides typed CRUD access to database (PostgreSQL or SQLite)."""

    def __init__(self):
        init_db()

    def get_connection(self) -> DBConnectionWrapper:
        return get_db_connection()

    @property
    def is_postgres(self) -> bool:
        conn = self.get_connection()
        val = conn.is_postgres
        conn.close()
        return val

    # --- Users ---
    def create_user(self, user_id: str, email: str, password_hash: str, salt: str, naukri_url: str = "") -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, salt, naukri_profile_url, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email.lower().strip(), password_hash, salt, naukri_url, now)
            )
            # Initialize empty profile, filters, credentials
            conn.execute(
                "INSERT INTO user_profiles (user_id, email, updated_at) VALUES (?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, email.lower().strip(), now)
            )
            conn.execute(
                "INSERT INTO user_filters (user_id, updated_at) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, now)
            )
            conn.execute(
                "INSERT INTO user_credentials (user_id, updated_at) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, now)
            )
        conn.close()
        return self.get_user_by_id(user_id)

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_naukri_url(self, user_id: str, url: str):
        conn = self.get_connection()
        with conn:
            conn.execute("UPDATE users SET naukri_profile_url = ? WHERE id = ?", (url.strip(), user_id))
        conn.close()

    def set_user_consent(self, user_id: str, acknowledged: bool = True):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute(
                "UPDATE users SET consent_acknowledged = ?, consent_acknowledged_at = ? WHERE id = ?",
                (1 if acknowledged else 0, now if acknowledged else "", user_id)
            )
        conn.close()

    # --- Sessions ---
    def create_session(self, token: str, user_id: str, expires_at: str):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    user_id=excluded.user_id,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at
            """, (token, user_id, now, expires_at))
        conn.close()

    def get_session(self, token: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def delete_session(self, token: str):
        conn = self.get_connection()
        with conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.close()

    # --- User Profiles ---
    def get_profile(self, user_id: str) -> Dict[str, Any]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return {}
        data = dict(row)
        try:
            data["skills"] = json.loads(data.get("skills") or "[]")
        except Exception:
            data["skills"] = []
        try:
            data["target_roles"] = json.loads(data.get("target_roles") or '["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"]')
        except Exception:
            data["target_roles"] = ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"]
        try:
            data["education"] = json.loads(data.get("education") or "{}")
        except Exception:
            data["education"] = {}
        try:
            data["screening_answers"] = json.loads(data.get("screening_answers") or "{}")
        except Exception:
            data["screening_answers"] = {}
        return data

    def save_profile(self, user_id: str, profile_data: Dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        skills_str = json.dumps(profile_data.get("skills", []))
        default_roles = ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"]
        target_roles_str = json.dumps(profile_data.get("target_roles", default_roles))
        education_str = json.dumps(profile_data.get("education", {}))
        screening_str = json.dumps(profile_data.get("screening_answers", {}))

        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_profiles (
                    user_id, full_name, email, phone, location, headline,
                    current_title, current_company, current_ctc, expected_ctc,
                    notice_period_days, years_of_experience, skills, target_roles, education,
                    screening_answers, resume_path, resume_name, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    full_name=excluded.full_name,
                    email=excluded.email,
                    phone=excluded.phone,
                    location=excluded.location,
                    headline=excluded.headline,
                    current_title=excluded.current_title,
                    current_company=excluded.current_company,
                    current_ctc=excluded.current_ctc,
                    expected_ctc=excluded.expected_ctc,
                    notice_period_days=excluded.notice_period_days,
                    years_of_experience=excluded.years_of_experience,
                    skills=excluded.skills,
                    target_roles=excluded.target_roles,
                    education=excluded.education,
                    screening_answers=excluded.screening_answers,
                    resume_path=CASE WHEN excluded.resume_path != '' THEN excluded.resume_path ELSE user_profiles.resume_path END,
                    resume_name=CASE WHEN excluded.resume_name != '' THEN excluded.resume_name ELSE user_profiles.resume_name END,
                    updated_at=excluded.updated_at
            """, (
                user_id,
                profile_data.get("full_name", ""),
                profile_data.get("email", ""),
                profile_data.get("phone", ""),
                profile_data.get("location", ""),
                profile_data.get("headline", ""),
                profile_data.get("current_title", ""),
                profile_data.get("current_company", ""),
                float(profile_data.get("current_ctc") or 0.0),
                float(profile_data.get("expected_ctc") or 0.0),
                int(profile_data.get("notice_period_days") or 30),
                float(profile_data.get("years_of_experience") or 0.0),
                skills_str,
                target_roles_str,
                education_str,
                screening_str,
                profile_data.get("resume_path", ""),
                profile_data.get("resume_name", ""),
                now
            ))
        conn.close()

    def get_target_roles(self, user_id: str) -> List[str]:
        prof = self.get_profile(user_id)
        return prof.get("target_roles", ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"])

    def add_target_role(self, user_id: str, role: str) -> List[str]:
        clean_role = role.strip()
        if not clean_role:
            return self.get_target_roles(user_id)
        prof = self.get_profile(user_id)
        roles = prof.get("target_roles", ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"])
        if not any(r.lower() == clean_role.lower() for r in roles):
            roles.append(clean_role)
            prof["target_roles"] = roles
            self.save_profile(user_id, prof)
        return roles

    def update_target_role(self, user_id: str, old_role: str, new_role: str) -> List[str]:
        clean_old = old_role.strip().lower()
        clean_new = new_role.strip()
        if not clean_new:
            return self.get_target_roles(user_id)
        prof = self.get_profile(user_id)
        roles = prof.get("target_roles", ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"])
        updated_roles = []
        for r in roles:
            if r.lower() == clean_old:
                updated_roles.append(clean_new)
            else:
                updated_roles.append(r)
        prof["target_roles"] = updated_roles
        self.save_profile(user_id, prof)
        return updated_roles

    def delete_target_role(self, user_id: str, role: str) -> List[str]:
        clean_target = role.strip().lower()
        prof = self.get_profile(user_id)
        roles = prof.get("target_roles", ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"])
        filtered_roles = [r for r in roles if r.lower() != clean_target]
        prof["target_roles"] = filtered_roles
        self.save_profile(user_id, prof)
        return filtered_roles

    # --- User Filters ---
    def get_filters(self, user_id: str) -> Dict[str, Any]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM user_filters WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return {
                "threshold_pct": 65.0,
                "keywords": "data analyst",
                "location": "any",
                "min_salary": 0.0,
                "max_salary": 99.0,
                "experience_years": 0,
                "job_age_days": 0,
                "work_mode": "all",
                "sort_by": "relevance",
                "apply_mode": "all"
            }
        return dict(row)

    def save_filters(self, user_id: str, filters_data: Dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_filters (
                    user_id, threshold_pct, keywords, location, min_salary,
                    max_salary, experience_years, job_age_days, work_mode, sort_by, apply_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    threshold_pct=excluded.threshold_pct,
                    keywords=excluded.keywords,
                    location=excluded.location,
                    min_salary=excluded.min_salary,
                    max_salary=excluded.max_salary,
                    experience_years=excluded.experience_years,
                    job_age_days=excluded.job_age_days,
                    work_mode=excluded.work_mode,
                    sort_by=excluded.sort_by,
                    apply_mode=excluded.apply_mode,
                    updated_at=excluded.updated_at
            """, (
                user_id,
                float(filters_data.get("threshold_pct") or 65.0),
                filters_data.get("keywords", "data analyst"),
                filters_data.get("location", "any"),
                float(filters_data.get("min_salary") or 0.0),
                float(filters_data.get("max_salary") or 99.0),
                int(filters_data.get("experience_years") or 0),
                int(filters_data.get("job_age_days") or 0),
                filters_data.get("work_mode", "all"),
                filters_data.get("sort_by", "relevance"),
                filters_data.get("apply_mode", "all"),
                now
            ))
        conn.close()

    # --- User Credentials (Naukri & Google) ---
    def get_credentials(self, user_id: str) -> Dict[str, Any]:
        conn = self.get_connection()
        cur = conn.execute("SELECT * FROM user_credentials WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return {}
        data = dict(row)
        try:
            data["naukri_cookies"] = json.loads(data.get("naukri_cookies") or "[]")
        except Exception:
            data["naukri_cookies"] = []
        return data

    def save_naukri_cookies(self, user_id: str, cookies: list, username: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_credentials (user_id, naukri_cookies, naukri_username, naukri_connected_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    naukri_cookies=excluded.naukri_cookies,
                    naukri_username=excluded.naukri_username,
                    naukri_connected_at=excluded.naukri_connected_at,
                    updated_at=excluded.updated_at
            """, (user_id, json.dumps(cookies), username, now, now))
        conn.close()

    def save_google_sheet_id(self, user_id: str, sheet_id: str):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_credentials (user_id, google_sheet_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    google_sheet_id=excluded.google_sheet_id,
                    updated_at=excluded.updated_at
            """, (user_id, sheet_id.strip(), now))
        conn.close()

    def save_google_sheet_details(self, user_id: str, sheet_url: str, sheet_id: str = ""):
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_credentials (user_id, google_sheet_url, google_sheet_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    google_sheet_url=excluded.google_sheet_url,
                    google_sheet_id=CASE WHEN excluded.google_sheet_id != '' THEN excluded.google_sheet_id ELSE user_credentials.google_sheet_id END,
                    updated_at=excluded.updated_at
            """, (user_id, sheet_url.strip(), sheet_id.strip(), now))
        conn.close()

    def save_user_authorizations(
        self,
        user_id: str,
        is_authorized: bool,
        google_sheet_url: str = "",
        google_sheet_id: str = "",
        gmail_id: str = ""
    ):
        now = datetime.now(timezone.utc).isoformat()
        auth_int = 1 if is_authorized else 0
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_credentials (user_id, google_sheet_url, google_sheet_id, gmail_id, is_authorized, authorized_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    google_sheet_url=CASE WHEN excluded.google_sheet_url != '' THEN excluded.google_sheet_url ELSE user_credentials.google_sheet_url END,
                    google_sheet_id=CASE WHEN excluded.google_sheet_id != '' THEN excluded.google_sheet_id ELSE user_credentials.google_sheet_id END,
                    gmail_id=CASE WHEN excluded.gmail_id != '' THEN excluded.gmail_id ELSE user_credentials.gmail_id END,
                    is_authorized=excluded.is_authorized,
                    authorized_at=excluded.authorized_at,
                    updated_at=excluded.updated_at
            """, (user_id, google_sheet_url.strip(), google_sheet_id.strip(), gmail_id.strip(), auth_int, now, now))
        conn.close()

    # --- Applications Tracking ---
    def add_application(self, user_id: str, app_data: Dict[str, Any]) -> str:
        app_id = app_data.get("id") or f"APP-{int(time.time() * 1000)}"
        applied_at = app_data.get("applied_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self.get_connection()
        with conn:
            conn.execute("""
                INSERT INTO user_applications (
                    id, user_id, job_title, company, location, platform,
                    url, match_score, salary_lpa, experience_req, status, applied_at, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id=excluded.user_id,
                    job_title=excluded.job_title,
                    company=excluded.company,
                    location=excluded.location,
                    platform=excluded.platform,
                    url=excluded.url,
                    match_score=excluded.match_score,
                    salary_lpa=excluded.salary_lpa,
                    experience_req=excluded.experience_req,
                    status=excluded.status,
                    applied_at=excluded.applied_at,
                    notes=excluded.notes
            """, (
                app_id,
                user_id,
                app_data.get("job_title", ""),
                app_data.get("company", ""),
                app_data.get("location", ""),
                app_data.get("platform", "Naukri"),
                app_data.get("url", ""),
                float(app_data.get("match_score") or 0.0),
                str(app_data.get("salary_lpa") or ""),
                str(app_data.get("experience_req") or ""),
                app_data.get("status", "Applied"),
                applied_at,
                app_data.get("notes", "")
            ))
        conn.close()
        return app_id

    def get_applications(self, user_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cur = conn.execute(
            "SELECT * FROM user_applications WHERE user_id = ? ORDER BY applied_at DESC LIMIT ?",
            (user_id, limit)
        )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def delete_application(self, user_id: str, app_id: str) -> bool:
        conn = self.get_connection()
        with conn:
            cur = conn.execute("DELETE FROM user_applications WHERE user_id = ? AND id = ?", (user_id, app_id))
            affected = cur.rowcount
        conn.close()
        return affected > 0

    def clear_applications(self, user_id: str) -> int:
        conn = self.get_connection()
        with conn:
            cur = conn.execute("DELETE FROM user_applications WHERE user_id = ?", (user_id,))
            affected = cur.rowcount
        conn.close()
        return affected

    def import_legacy_applications(self, user_id: str) -> int:
        """Imports historical applications from local Excel file into user's account."""
        excel_file = Path("Job_Applications_Tracker.xlsx")
        if not excel_file.exists():
            return 0
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(excel_file), data_only=True)
            sheet = wb.active
            rows = list(sheet.iter_rows(values_only=True))
            if len(rows) <= 1:
                return 0
            header = [str(c or '').strip().lower() for c in rows[0]]
            id_idx = header.index("application id") if "application id" in header else 0
            comp_idx = header.index("company") if "company" in header else 2
            title_idx = header.index("job title") if "job title" in header else 3
            loc_idx = header.index("location") if "location" in header else 5
            url_idx = header.index("job url") if "job url" in header else 6
            score_idx = header.index("match %") if "match %" in header else 10
            status_idx = header.index("status") if "status" in header else 8

            count = 0
            for r in rows[1:]:
                if not r or not any(r):
                    continue
                app_id = str(r[id_idx] or f"HIST-{count}")
                comp = str(r[comp_idx] or "Company")
                title = str(r[title_idx] or "Role")
                loc = str(r[loc_idx] or "India") if len(r) > loc_idx else "India"
                url = str(r[url_idx] or "") if len(r) > url_idx else ""
                raw_score = str(r[score_idx] or "70").replace("%", "").strip() if len(r) > score_idx else "70"
                try:
                    score = float(raw_score)
                except Exception:
                    score = 70.0
                st = str(r[status_idx] or "Applied") if len(r) > status_idx else "Applied"

                self.add_application(user_id, {
                    "id": app_id,
                    "company": comp,
                    "job_title": title,
                    "location": loc,
                    "url": url,
                    "match_score": score,
                    "status": st,
                    "platform": "Naukri",
                })
                count += 1
            return count
        except Exception:
            return 0


db = DatabaseManager()
