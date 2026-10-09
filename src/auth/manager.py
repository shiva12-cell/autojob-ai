"""
Authentication and session management module.
Uses standard library hashlib.pbkdf2_hmac for zero-dependency, cryptographically secure password hashing.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from fastapi import Request, HTTPException, status, Depends

from src.database.db import db


import os
import re

DEFAULT_PBKDF2_ITERATIONS = 310000
LEGACY_PBKDF2_ITERATIONS = 100000
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Hashes a password with PBKDF2-HMAC-SHA256 (310,000 iterations)."""
    if not salt:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=DEFAULT_PBKDF2_ITERATIONS
    )
    return key.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """
    Verifies a password against stored hash and salt.
    Supports 310,000 iterations with fallback to legacy 100,000 for seamless migration.
    """
    # Primary check with 310,000 iterations
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=DEFAULT_PBKDF2_ITERATIONS
    )
    if secrets.compare_digest(key.hex(), stored_hash):
        return True

    # Fallback check for legacy hashes
    legacy_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=LEGACY_PBKDF2_ITERATIONS
    )
    return secrets.compare_digest(legacy_key.hex(), stored_hash)


class AuthManager:
    """Manages user registration, login, and session tokens."""

    def register(self, email: str, password: str, naukri_url: str = "") -> Dict[str, Any]:
        email_clean = email.lower().strip()
        if not email_clean or not EMAIL_REGEX.match(email_clean):
            raise ValueError("A valid email address (e.g. user@domain.com) is required.")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if len(password) > 128:
            raise ValueError("Password must not exceed 128 characters.")

        existing = db.get_user_by_email(email_clean)
        if existing:
            raise ValueError("An account with this email already exists.")

        user_id = f"user_{secrets.token_hex(8)}"
        pwd_hash, salt = hash_password(password)
        user = db.create_user(user_id, email_clean, pwd_hash, salt, naukri_url)
        return user

    def login(self, email: str, password: str) -> tuple[Dict[str, Any], str]:
        email_clean = email.lower().strip()
        user = db.get_user_by_email(email_clean)
        if not user:
            raise ValueError("Invalid email or password.")

        if not verify_password(password, user["password_hash"], user["salt"]):
            raise ValueError("Invalid email or password.")

        lifetime_days = int(os.getenv("SESSION_LIFETIME_DAYS", 30))
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=lifetime_days)).isoformat()
        db.create_session(token, user["id"], expires_at)
        return user, token

    def logout(self, token: str):
        db.delete_session(token)

    def get_user_from_token(self, token: str) -> Optional[Dict[str, Any]]:
        session = db.get_session(token)
        if not session:
            return None
        # Check expiry
        try:
            exp = datetime.fromisoformat(session["expires_at"])
            if datetime.now(timezone.utc) > exp:
                db.delete_session(token)
                return None
        except Exception:
            pass

        return db.get_user_by_id(session["user_id"])


auth_manager = AuthManager()


def get_current_user_optional(request: Request) -> Optional[Dict[str, Any]]:
    """FastAPI dependency to extract active user from Cookie or Authorization Header."""
    token = None
    # 1. Bearer header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    # 2. Cookie
    if not token:
        token = request.cookies.get("session_token")

    # 3. Query param
    if not token:
        token = request.query_params.get("token")

    if not token:
        return None

    return auth_manager.get_user_from_token(token)


def get_current_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency enforcing authentication."""
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in or register.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
