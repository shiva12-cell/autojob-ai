"""
Tests for Universal Resume Parser, Dynamic Profile Management, and Screening QA Endpoints.
"""

import pytest
from pathlib import Path
from starlette.testclient import TestClient
from src.resume_parser import extract_text_from_pdf, parse_resume_data
from src.web.server import app

client = TestClient(app)


def test_resume_pdf_extraction():
    """Verifies PDF text extraction and semantic data parsing from resume."""
    resume_path = Path("resumes/resume.pdf")
    if not resume_path.exists():
        pytest.skip("resume.pdf not found in resumes/")

    raw_text = extract_text_from_pdf(resume_path)
    assert len(raw_text) > 100
    assert "@" in raw_text or "Analyst" in raw_text

    data = parse_resume_data(raw_text)
    assert "personal_info" in data
    assert "email" in data["personal_info"]
    assert len(data["personal_info"]["email"]) > 0
    assert "skills" in data["experience"]
    assert len(data["experience"]["skills"]) >= 5
    assert "Python" in data["experience"]["skills"] or "SQL" in data["experience"]["skills"]
    assert "screening_answers" in data
    assert "expected_ctc" in data["screening_answers"]
    assert "notice_period" in data["screening_answers"]


def test_api_get_profile():
    """Verifies GET /api/profile returns structured candidate details."""
    response = client.get("/api/profile")
    assert response.status_code == 200
    data = response.json()
    assert "personal_info" in data
    assert "skills" in data["experience"]
    assert "screening_answers" in data
    assert data["total_skills"] >= 0


def test_api_post_profile():
    """Verifies POST /api/profile allows updating candidate preferences."""
    payload = {
        "personal_info": {
            "first_name": "Candidate",
            "last_name": "Applicant",
            "current_title": "Data & AI Product Analyst",
            "location": "Gurugram"
        }
    }
    response = client.post("/api/profile", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    # Verify update persisted
    get_res = client.get("/api/profile")
    assert get_res.json()["personal_info"]["current_title"] == "Data & AI Product Analyst"

    # Reset to default guest candidate
    client.post("/api/profile", json={
        "personal_info": {
            "first_name": "Guest",
            "last_name": "Candidate",
            "full_name": "Guest Candidate",
            "current_title": "Job Seeker"
        }
    })


def test_api_screening_qa_lifecycle():
    """Verifies GET, POST, and queue resolution for Screening QA."""
    # 1. GET screening QA
    res = client.get("/api/screening-qa")
    assert res.status_code == 200
    data = res.json()
    assert "answers" in data
    assert "queue" in data

    # 2. Update screening answers
    update_res = client.post("/api/screening-qa", json={"expected_ctc": "7.5 LPA", "notice_period": "Immediate / 0 days"})
    assert update_res.status_code == 200
    assert update_res.json()["status"] == "success"

    # Verify updated value
    verify_res = client.get("/api/screening-qa")
    assert verify_res.json()["answers"]["expected_ctc"] == "7.5 LPA"

    # 3. Resolve an unreviewed question
    resolve_res = client.post(
        "/api/screening-qa/review-queue/resolve",
        json={
            "id": "test-q1",
            "question_text": "Are you comfortable working in Gurugram?",
            "answer": "Yes, completely comfortable"
        }
    )
    assert resolve_res.status_code == 200
    assert resolve_res.json()["status"] == "success"


def test_api_resume_upload():
    """Verifies POST /api/profile/upload-resume handles PDF upload and reloads profile."""
    resume_path = Path("resumes/resume.pdf")
    if not resume_path.exists():
        pytest.skip("resume.pdf not found in resumes/")

    with open(resume_path, "rb") as f:
        response = client.post(
            "/api/profile/upload-resume",
            files={"file": ("test_resume.pdf", f, "application/pdf")}
        )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "Candidate:" in data["message"]
    assert data["profile"]["skills_count"] > 0

    # Clean up active_profile so global state remains guest-clean
    from src.config import save_active_profile
    from src.web.server import engine
    save_active_profile({
        "personal_info": {"first_name": "Guest", "last_name": "Candidate", "full_name": "Guest Candidate", "email": "", "phone": "", "current_title": "Job Seeker"},
        "education": {"degree": "", "university": ""},
        "experience": {"years_of_experience": 0, "skills": []},
        "screening_answers": {}
    })
    engine.reload_profile()
