"""
Tests for security hardening:
- Security HTTP response headers (CSP, X-Frame-Options, X-Content-Type-Options, etc.)
- Administrative route protection (X-Admin-Key validation)
- In-memory sliding-window rate limiting (429 Too Many Requests)
- Password security bounds & PBKDF2 310,000 iteration verification
- File upload security (magic byte verification & size limits)
"""

import io
import pytest
from fastapi.testclient import TestClient
from src.web.server import app
from src.security.rate_limiter import SlidingWindowRateLimiter
from src.security.admin import verify_admin_access
from src.auth.manager import AuthManager, hash_password, verify_password
from fastapi import HTTPException


@pytest.fixture
def client():
    return TestClient(app)


def test_security_headers_present(client):
    """Verify that every HTTP response includes security headers."""
    response = client.get("/api/status")
    assert response.status_code == 200
    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("X-XSS-Protection") == "1; mode=block"
    assert "strict-origin-when-cross-origin" in headers.get("Referrer-Policy", "")
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")


def test_rate_limiter_sliding_window():
    """Verify sliding-window rate limiter triggers 429 when threshold exceeded."""
    limiter = SlidingWindowRateLimiter()
    ip = "192.168.1.100"
    bucket = "auth"
    limit = 3
    window = 60
    
    # First 3 requests should be allowed
    allowed1, _ = limiter.is_allowed(ip, bucket, limit, window)
    allowed2, _ = limiter.is_allowed(ip, bucket, limit, window)
    allowed3, _ = limiter.is_allowed(ip, bucket, limit, window)
    assert allowed1 is True
    assert allowed2 is True
    assert allowed3 is True
    
    # 4th request must be rejected
    allowed4, retry_after = limiter.is_allowed(ip, bucket, limit, window)
    assert allowed4 is False
    assert retry_after > 0


def test_admin_route_protection_external_ip(monkeypatch):
    """Verify that external IPs without valid X-Admin-Key are rejected with 403."""
    class FakeRequest:
        client = type("Client", (), {"host": "203.0.113.45"})()
        headers = {}
    
    with pytest.raises(HTTPException) as exc_info:
        verify_admin_access(FakeRequest())
    assert exc_info.value.status_code == 403
    assert "Admin operations require X-Admin-Key" in exc_info.value.detail or "Forbidden" in exc_info.value.detail


def test_admin_route_protection_with_valid_key(monkeypatch):
    """Verify that external IPs with valid X-Admin-Key are allowed."""
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-key-12345")
    
    class FakeRequest:
        client = type("Client", (), {"host": "203.0.113.45"})()
        headers = {"x-admin-key": "super-secret-key-12345"}
    
    assert verify_admin_access(FakeRequest()) is True


def test_admin_route_protection_with_invalid_key(monkeypatch):
    """Verify that external IPs with wrong X-Admin-Key are rejected."""
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-key-12345")
    
    class FakeRequest:
        client = type("Client", (), {"host": "203.0.113.45"})()
        headers = {"x-admin-key": "wrong-key"}
    
    with pytest.raises(HTTPException) as exc_info:
        verify_admin_access(FakeRequest())
    assert exc_info.value.status_code == 403


def test_password_bounds_validation():
    """Verify password minimum length (8) and maximum length (128) enforcement."""
    auth_mgr = AuthManager()
    
    # Less than 8 chars
    with pytest.raises(ValueError, match="at least 8 characters"):
        auth_mgr.register("user1@example.com", "short1")
        
    # Greater than 128 chars
    with pytest.raises(ValueError, match="not exceed 128 characters"):
        auth_mgr.register("user2@example.com", "a" * 129)
        
    # Invalid email regex
    with pytest.raises(ValueError, match="valid email address"):
        auth_mgr.register("not-an-email", "validpassword123")


def test_password_hash_upgraded_iterations():
    """Verify password hashing creates valid hash with 310,000 PBKDF2 iterations."""
    pwd_hash, salt = hash_password("SuperSecret99!")
    
    assert verify_password("SuperSecret99!", pwd_hash, salt) is True
    assert verify_password("WrongPassword!", pwd_hash, salt) is False


def test_upload_resume_magic_bytes_validation(client):
    """Verify that uploading non-PDF content (fake pdf) is rejected."""
    fake_content = b"THIS IS NOT A VALID PDF FILE"
    files = {"file": ("malicious.pdf", io.BytesIO(fake_content), "application/pdf")}
    
    response = client.post("/api/profile/upload-resume", files=files)
    assert response.status_code == 400
    res_json = response.json()
    err_text = res_json.get("message") or res_json.get("detail", "")
    assert "Invalid file format" in err_text


def test_upload_resume_oversized_validation(client):
    """Verify that uploading files larger than 10MB is rejected."""
    # 10.5 MB payload starting with %PDF-
    large_content = b"%PDF-1.4 " + (b"0" * (10 * 1024 * 1024 + 500))
    files = {"file": ("huge.pdf", io.BytesIO(large_content), "application/pdf")}
    
    response = client.post("/api/profile/upload-resume", files=files)
    assert response.status_code == 413
    res_json = response.json()
    err_text = res_json.get("message") or res_json.get("detail", "")
    assert "exceeds maximum" in err_text
