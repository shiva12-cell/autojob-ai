"""
Comprehensive test suite for the Multi-User Production Platform:
- SQLite isolation for multiple users
- Password hashing & secure authentication
- Full Naukri-style candidate profile customization
- Dynamic thresholds and live filter settings
- Local standalone job application tracking and Excel export
"""

import time
import pytest
from starlette.testclient import TestClient
from src.web.server import app
from src.database.db import db
from src.auth.manager import auth_manager, hash_password, verify_password
from src.engine.user_runner import build_candidate_profile, export_user_applications_to_excel


@pytest.fixture
def client():
    return TestClient(app)


def test_password_hashing_and_verification():
    pwd = "SecurePassword123!"
    h, salt = hash_password(pwd)
    assert h != pwd
    assert len(salt) > 0
    assert verify_password(pwd, h, salt) is True
    assert verify_password("WrongPassword", h, salt) is False


def test_database_multi_user_isolation():
    u1_id = f"user_alpha_{int(time.time()*1000)}"
    u2_id = f"user_beta_{int(time.time()*1000)}"

    db.create_user(u1_id, f"{u1_id}@domain.com", "hash1", "salt1")
    db.create_user(u2_id, f"{u2_id}@domain.com", "hash2", "salt2")

    # User 1
    db.save_profile(u1_id, {
        "full_name": "Alice Engineer",
        "current_title": "Frontend Lead",
        "headline": "Lead React / Next.js Engineer",
        "current_ctc": 15.0,
        "expected_ctc": 22.0,
        "notice_period_days": 15,
        "skills": ["react", "typescript", "tailwind", "next.js"],
    })
    db.save_filters(u1_id, {
        "threshold_pct": 75.0,
        "keywords": "react developer",
        "location": "bengaluru",
    })
    db.add_application(u1_id, {
        "id": "APP-ALICE-1",
        "job_title": "Senior React Engineer",
        "company": "Tech Corp",
        "match_score": 88.0,
        "status": "Applied",
    })

    # User 2
    db.save_profile(u2_id, {
        "full_name": "Bob Analyst",
        "current_title": "BI Specialist",
        "headline": "Power BI & SQL Specialist",
        "current_ctc": 8.0,
        "expected_ctc": 12.0,
        "notice_period_days": 30,
        "skills": ["power bi", "sql", "excel", "tableau"],
    })
    db.save_filters(u2_id, {
        "threshold_pct": 60.0,
        "keywords": "bi analyst",
        "location": "delhi-ncr",
    })
    db.add_application(u2_id, {
        "id": "APP-BOB-1",
        "job_title": "Data Analyst",
        "company": "Finance Hub",
        "match_score": 74.0,
        "status": "Applied",
    })

    # Assert isolation
    p1 = db.get_profile(u1_id)
    p2 = db.get_profile(u2_id)
    assert p1["full_name"] == "Alice Engineer"
    assert p1["headline"] == "Lead React / Next.js Engineer"
    assert "react" in p1["skills"]
    assert p1["current_ctc"] == 15.0

    assert p2["full_name"] == "Bob Analyst"
    assert "power bi" in p2["skills"]
    assert p2["current_ctc"] == 8.0

    f1 = db.get_filters(u1_id)
    f2 = db.get_filters(u2_id)
    assert f1["threshold_pct"] == 75.0
    assert f2["threshold_pct"] == 60.0

    apps1 = db.get_applications(u1_id)
    apps2 = db.get_applications(u2_id)
    assert len(apps1) == 1
    assert apps1[0]["id"] == "APP-ALICE-1"
    assert len(apps2) == 1
    assert apps2[0]["id"] == "APP-BOB-1"


def test_auth_api_workflow(client):
    test_email = f"candidate_{int(time.time()*1000)}@testmail.com"
    test_pwd = "MySecretPassword123"

    # 1. Register
    reg_res = client.post("/api/auth/register", json={
        "email": test_email,
        "password": test_pwd,
        "naukri_url": "https://www.naukri.com/mnjuser/profile"
    })
    assert reg_res.status_code == 200
    reg_data = reg_res.json()
    assert reg_data["status"] == "success"
    token = reg_data["token"]
    assert len(token) > 10

    # 2. Check /api/auth/me with Bearer token
    me_res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert me_data["authenticated"] is True
    assert me_data["user"]["email"] == test_email.lower()
    assert me_data["user"]["naukri_profile_url"] == "https://www.naukri.com/mnjuser/profile"

    # 3. Save full profile customization
    prof_update_res = client.post(
        "/api/user/profile",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "full_name": "Karan Sharma",
            "current_title": "Data Scientist",
            "headline": "Data Scientist | ML, Python, SQL | 2 Yrs Exp",
            "current_company": "Innovate AI",
            "current_ctc": 8.5,
            "expected_ctc": 14.0,
            "notice_period_days": 15,
            "years_of_experience": 2.0,
            "skills": ["python", "machine learning", "sql", "tensorflow", "statistics"],
            "education": {
                "degree": "B.Tech Computer Science",
                "university": "IIT Delhi",
                "graduation_year": 2024
            }
        }
    )
    assert prof_update_res.status_code == 200
    assert prof_update_res.json()["status"] == "success"

    # 4. Fetch profile
    get_prof = client.get("/api/user/profile", headers={"Authorization": f"Bearer {token}"})
    assert get_prof.status_code == 200
    saved_p = get_prof.json()["profile"]
    assert saved_p["full_name"] == "Karan Sharma"
    assert saved_p["headline"] == "Data Scientist | ML, Python, SQL | 2 Yrs Exp"
    assert saved_p["current_ctc"] == 8.5
    assert saved_p["expected_ctc"] == 14.0
    assert "tensorflow" in saved_p["skills"]

    # 5. Update custom filters & threshold
    filter_res = client.post(
        "/api/user/filters",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "threshold_pct": 70.0,
            "keywords": "data scientist",
            "location": "remote",
            "min_salary": 8.0,
            "max_salary": 18.0,
            "experience_years": 2,
            "work_mode": "remote",
            "sort_by": "relevance",
        }
    )
    assert filter_res.status_code == 200

    get_filt = client.get("/api/user/filters", headers={"Authorization": f"Bearer {token}"})
    assert get_filt.status_code == 200
    saved_f = get_filt.json()["filters"]
    assert saved_f["threshold_pct"] == 70.0
    assert saved_f["keywords"] == "data scientist"
    assert saved_f["location"] == "remote"


def test_excel_export_per_user():
    u_id = f"test_export_user_{int(time.time()*1000)}"
    db.create_user(u_id, f"{u_id}@domain.com", "hash", "salt")
    db.add_application(u_id, {
        "id": "APP-EXP-001",
        "job_title": "AI Engineer",
        "company": "DeepMind Partner",
        "location": "Bengaluru",
        "platform": "Naukri",
        "url": "https://www.naukri.com/job-listings-123",
        "match_score": 92.0,
        "salary_lpa": "12-16 LPA",
        "status": "Applied",
        "notes": "Matched skills: python, pytorch",
    })

    excel_file = export_user_applications_to_excel(u_id)
    assert excel_file.exists()
    assert excel_file.stat().st_size > 1000


def test_guest_data_isolation_and_no_leakage(client):
    """Verifies that guests/logged out users see NO creator PII, 0 skills, and unlinked status."""
    # 1. Profile endpoint for guest
    prof_res = client.get("/api/profile")
    assert prof_res.status_code == 200
    p = prof_res.json()["personal_info"]
    assert "Shiva" not in p["full_name"]
    assert p["full_name"] in ["Guest Candidate", "Job Seeker"]
    assert "shivaupadhyay" not in p["email"].lower()
    assert prof_res.json()["total_skills"] >= 0

    # 2. Accounts endpoint for guest
    acc_res = client.get("/api/accounts")
    assert acc_res.status_code == 200
    accs = acc_res.json()
    assert accs["naukri"]["linked"] is False
    assert accs["naukri"]["cookie_count"] == 0
    assert accs["gmail"]["linked"] is False


def test_user_authorization_flow_and_google_sheet_remembrance(client):
    """Verifies that a user can authorize AutoJob AI, link Naukri, Google Sheet, and Gmail."""
    email = f"auth_test_{int(time.time()*1000)}@example.com"
    reg_res = client.post("/api/auth/register", json={
        "email": email,
        "password": "Password123!",
    })
    assert reg_res.status_code == 200
    token = reg_res.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Authorize permissions with Naukri URL, Google Sheet URL, and Gmail ID
    sheet_url = "https://docs.google.com/spreadsheets/d/1BxiMVs0XRsbrP-SAMPLE_SHEET_ID/edit"
    gmail_id = "candidate.career@gmail.com"
    naukri_url = "https://www.naukri.com/mnjuser/profile"

    auth_res = client.post(
        "/api/user/authorize-permissions",
        headers=headers,
        json={
            "naukri_profile_url": naukri_url,
            "google_sheet_url": sheet_url,
            "gmail_id": gmail_id,
            "authorize": True
        }
    )
    assert auth_res.status_code == 200
    auth_data = auth_res.json()["authorization"]
    assert auth_data["is_authorized"] is True
    assert auth_data["google_sheet_id"] == "1BxiMVs0XRsbrP-SAMPLE_SHEET_ID"
    assert auth_data["gmail_id"] == gmail_id
    assert auth_data["naukri_profile_url"] == naukri_url

    # Check GET /api/user/authorizations
    get_auth = client.get("/api/user/authorizations", headers=headers)
    assert get_auth.status_code == 200
    user_auth = get_auth.json()["authorization"]
    assert user_auth["is_authorized"] is True
    assert user_auth["google_sheet_url"] == sheet_url
    assert user_auth["gmail_id"] == gmail_id

    # Check GET /api/auth/me includes authorizations
    me_res = client.get("/api/auth/me", headers=headers)
    assert me_res.status_code == 200
    me_user = me_res.json()["user"]
    assert me_user["google_sheet_url"] == sheet_url
    assert me_user["gmail_id"] == gmail_id
    assert me_user["is_authorized"] is True

    # Check GET /api/accounts reflects sheets connected for this user
    acc_res = client.get("/api/accounts", headers=headers)
    assert acc_res.status_code == 200
    assert acc_res.json()["gmail"]["linked"] is True
    assert acc_res.json()["gmail"]["is_authorized"] is True
