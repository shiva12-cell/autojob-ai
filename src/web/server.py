"""
FastAPI Server for AutoJob AI Web Dashboard (Multi-User Production Edition).
Exposes REST APIs for multi-user authentication, candidate profile customization,
dynamic filters, threshold matching, and headless Naukri automation.
"""

import os
import re
import json
import time
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import (
    FastAPI, BackgroundTasks, UploadFile, File, Body, Query,
    Request, Response, Depends, HTTPException, status
)
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from src.autonomous_engine import engine
from src.resume_parser import extract_text_from_pdf, parse_resume_data
from src.config import save_active_profile, load_candidate_profile, ACTIVE_PROFILE_FILE

from src.database.db import db
from src.auth.manager import auth_manager, get_current_user, get_current_user_optional
from src.engine.user_runner import user_runner_mgr, export_user_applications_to_excel, get_user_workspace
from src.security.middleware import SecurityHeadersMiddleware
from src.security.rate_limiter import RateLimiterMiddleware
from src.security.admin import verify_admin_access

enable_docs = os.getenv("ENABLE_API_DOCS", "false").lower() == "true"
app = FastAPI(
    title="AutoJob AI Dashboard (Production)",
    version="3.0.0",
    docs_url="/docs" if enable_docs else None,
    redoc_url="/redoc" if enable_docs else None,
    openapi_url="/openapi.json" if enable_docs else None
)

# Strict CORS allowed origins
raw_cors = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000")
allowed_origins = [o.strip() for o in raw_cors.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimiterMiddleware)

TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
UNREVIEWED_QUEUE_FILE = Path("config/logs/unreviewed_questions.json")


@app.on_event("startup")
def on_startup():
    """Starts background services and logs startup message."""
    try:
        engine.start_scheduler()
    except Exception:
        pass
    print("🚀 AutoJob AI Multi-User Web Server Online.")


@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    """Serves the main HTML dashboard."""
    if not TEMPLATE_PATH.exists():
        return HTMLResponse("<h1>Dashboard template not found</h1>", status_code=404)
    return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))


# =====================================================================
# Profile & Dynamic Resume Onboarding Endpoints
# =====================================================================

@app.get("/api/profile")
def get_profile(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns candidate profile for authenticated user, or a clean scrubbed template if unauthenticated."""
    if not isinstance(current_user, dict):
        current_user = None

    if current_user:
        prof_data = db.get_profile(current_user["id"])
        resume_name = prof_data.get("resume_name") or "No resume uploaded"
        skills = prof_data.get("skills") or []
        return {
            "personal_info": {
                "full_name": prof_data.get("full_name") or "Candidate",
                "first_name": prof_data.get("full_name", "").split()[0] if prof_data.get("full_name") else "Candidate",
                "last_name": " ".join(prof_data.get("full_name", "").split()[1:]) if prof_data.get("full_name") else "",
                "email": current_user.get("email", ""),
                "phone": prof_data.get("phone", ""),
                "current_title": prof_data.get("current_title") or "Job Seeker",
                "current_company": prof_data.get("current_company") or "",
                "location": prof_data.get("location") or "India",
                "headline": prof_data.get("headline") or "",
                "naukri_profile_url": current_user.get("naukri_profile_url", ""),
            },
            "education": prof_data.get("education") or {},
            "experience": {
                "years_of_experience": prof_data.get("years_of_experience", 0),
                "skills": skills
            },
            "screening_answers": prof_data.get("screening_answers") or {},
            "resume_path": prof_data.get("resume_path") or "",
            "resume_name": resume_name,
            "total_skills": len(skills),
        }

    # For unauthenticated visitors / guests: read sanitized active memory profile
    p = engine.profile.personal_info
    first = p.first_name or "Guest"
    last = p.last_name or "Candidate"
    email = p.email or ""
    phone = p.phone or ""
    full = p.full_name or f"{first} {last}".strip() or "Guest Candidate"
    title = p.current_title or "Job Seeker"
    skills = list(engine.profile.experience.skills)

    return {
        "personal_info": {
            "full_name": full,
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": phone,
            "current_title": title,
            "current_company": getattr(p, "current_company", ""),
            "location": p.location or "India",
            "headline": getattr(p, "headline", "") or "Please sign in or register to customize your candidate profile & auto-apply.",
            "naukri_profile_url": "",
        },
        "education": {
            "degree": engine.profile.education.degree,
            "university": engine.profile.education.university,
        },
        "experience": {
            "years_of_experience": engine.profile.experience.years_of_experience,
            "skills": skills,
        },
        "screening_answers": engine.profile.screening_answers,
        "resume_path": "",
        "resume_name": "No resume uploaded yet",
        "total_skills": len(skills),
    }


@app.post("/api/profile/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    """Accepts uploaded PDF resume, extracts skills & personal details, and updates active candidate profile."""
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Only PDF resume files (.pdf) are supported."})

    MAX_RESUME_SIZE = 10 * 1024 * 1024  # 10 MB limit
    content = await file.read(MAX_RESUME_SIZE + 1)
    if len(content) > MAX_RESUME_SIZE:
        return JSONResponse(status_code=413, content={"status": "error", "message": "File exceeds maximum size limit of 10 MB."})

    if not content.startswith(b"%PDF"):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid file format: file must be a genuine PDF document."})

    resumes_dir = Path("resumes")
    resumes_dir.mkdir(parents=True, exist_ok=True)
    clean_name = Path(re.sub(r"[^\w\-_\.]", "_", file.filename)).name
    target_path = resumes_dir / clean_name

    with open(target_path, "wb") as buffer:
        buffer.write(content)

    try:
        raw_text = extract_text_from_pdf(target_path)
        parsed = parse_resume_data(raw_text)
        parsed["resume_path"] = str(target_path)

        # Merge with existing screening answers if present
        existing_answers = dict(engine.profile.screening_answers)
        existing_answers.update(parsed.get("screening_answers", {}))
        parsed["screening_answers"] = existing_answers

        save_active_profile(parsed)
        engine.reload_profile()

        return {
            "status": "success",
            "message": f"Resume '{file.filename}' parsed successfully! Candidate: {parsed['personal_info']['full_name']}.",
            "profile": {
                "full_name": parsed["personal_info"]["full_name"],
                "email": parsed["personal_info"]["email"],
                "phone": parsed["personal_info"]["phone"],
                "location": parsed["personal_info"]["location"],
                "title": parsed["personal_info"]["current_title"],
                "skills_count": len(parsed["experience"]["skills"]),
                "skills": parsed["experience"]["skills"],
            }
        }
    except Exception as e:
        return {"status": "error", "message": f"Failed to parse resume: {str(e)}"}


@app.post("/api/profile")
def update_profile(data: dict = Body(...)):
    """Updates candidate profile fields directly from the dashboard."""
    curr = engine.profile.model_dump()
    if "personal_info" in data:
        curr.setdefault("personal_info", {}).update(data["personal_info"])
    if "education" in data:
        curr.setdefault("education", {}).update(data["education"])
    if "experience" in data:
        curr.setdefault("experience", {}).update(data["experience"])
    if "screening_answers" in data:
        curr.setdefault("screening_answers", {}).update(data["screening_answers"])

    save_active_profile(curr)
    engine.reload_profile()
    return {"status": "success", "message": "Candidate profile updated successfully."}


@app.get("/api/screening-qa")
def get_screening_qa(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns candidate's active screening QA answers. Isolated per authenticated candidate."""
    from src.applier.question_superset import QUESTION_SUPERSET_SCHEMA
    if not isinstance(current_user, dict):
        current_user = None

    if not current_user:
        return {
            "answers": engine.profile.screening_answers,
            "queue": [],
            "count_queue": 0,
            "superset": QUESTION_SUPERSET_SCHEMA,
        }

    prof = db.get_profile(current_user["id"])
    answers = prof.get("screening_answers") or {}
    queue = []
    if UNREVIEWED_QUEUE_FILE.exists():
        try:
            with open(UNREVIEWED_QUEUE_FILE, "r", encoding="utf-8") as f:
                raw_q = json.load(f)
                queue = [q for q in raw_q if q.get("user_id") == current_user["id"]]
        except Exception:
            queue = []

    return {
        "answers": answers,
        "queue": queue,
        "count_queue": len([q for q in queue if q.get("status") == "pending"]),
        "superset": QUESTION_SUPERSET_SCHEMA,
    }


@app.get("/api/superset")
def get_superset(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns the 12-Pillar Question Superset structure populated with candidate profile answers."""
    from src.applier.question_superset import QUESTION_SUPERSET_SCHEMA
    if not isinstance(current_user, dict):
        current_user = None

    answers = {}
    if current_user:
        prof = db.get_profile(current_user["id"])
        answers = prof.get("screening_answers") or {}

    return {
        "schema": QUESTION_SUPERSET_SCHEMA,
        "answers": answers,
    }


@app.post("/api/screening-qa")
def update_screening_qa(answers: dict = Body(...), current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Updates approved screening question answers from UI edits."""
    if current_user and isinstance(current_user, dict):
        prof = db.get_profile(current_user["id"])
        existing = prof.get("screening_answers") or {}
        existing.update(answers)
        prof["screening_answers"] = existing
        db.save_profile(current_user["id"], prof)
    else:
        curr = engine.profile.model_dump()
        curr.setdefault("screening_answers", {}).update(answers)
        save_active_profile(curr)
        engine.reload_profile()
    return {"status": "success", "message": "Screening answers updated and saved."}


@app.post("/api/screening-qa/review-queue/resolve")
def resolve_unreviewed_question(item: dict = Body(...)):
    """Resolves an unreviewed question: saves answer and updates question status."""
    q_id = item.get("id")
    q_text = item.get("question_text", "").lower()
    answer = item.get("answer", "")

    # Save to screening answers
    curr = engine.profile.model_dump()
    curr.setdefault("screening_answers", {})[q_text] = answer
    save_active_profile(curr)
    engine.reload_profile()

    # Update queue file
    if UNREVIEWED_QUEUE_FILE.exists():
        try:
            with open(UNREVIEWED_QUEUE_FILE, "r", encoding="utf-8") as f:
                queue = json.load(f)
            for q in queue:
                if q.get("id") == q_id:
                    q["status"] = "resolved"
                    q["resolved_answer"] = answer
            with open(UNREVIEWED_QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2)
        except Exception:
            pass

    return {"status": "success", "message": f"Answer for '{q_text}' saved to knowledge base."}


@app.delete("/api/screening-qa/review-queue/{question_id}")
def delete_queue_item(question_id: str):
    """Dismisses an item from the screening review queue."""
    if UNREVIEWED_QUEUE_FILE.exists():
        try:
            with open(UNREVIEWED_QUEUE_FILE, "r", encoding="utf-8") as f:
                queue = json.load(f)
            queue = [q for q in queue if q.get("id") != question_id]
            with open(UNREVIEWED_QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2)
        except Exception:
            pass
    return {"status": "success", "message": "Queue item dismissed."}


# =====================================================================
# Application Trigger Endpoint
# =====================================================================
@app.post("/api/trigger")
def trigger_pipeline(
    background_tasks: BackgroundTasks,
    limit: int = 50,
    keyword: str = "data analyst",
    duration_minutes: int = 30,
    headless: bool = True,
    location: str = "any",
    min_salary: float = 0.0,
    max_salary: float = 99.0,
    experience_years: int = 0,
    job_age_days: int = 0,
    work_mode: str = "all",
    sort_by: str = "relevance",
    apply_mode: str = "all",
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """Triggers application cycle with user-specified limit, keyword, duration timer, location, salary range, live Naukri filters, and apply_mode."""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in or register to launch an application cycle."
        )

    background_tasks.add_task(
        engine.run_pipeline,
        max_limit=limit,
        keyword=keyword,
        duration_minutes=duration_minutes,
        dry_run=False,
        headless=headless,
        enable_indeed=False,
        location=location,
        min_salary_lpa=min_salary,
        max_salary_lpa=max_salary,
        experience_years=experience_years,
        job_age_days=job_age_days,
        work_mode=work_mode,
        sort_by=sort_by,
        apply_mode=apply_mode
    )
    sal_str = f"{min_salary}-{max_salary} LPA" if (min_salary > 0 or max_salary < 99) else "Any CTC"
    mode_str = "silent background mode (no popups)" if headless else "visible browser mode"
    return {
        "status": "triggered",
        "message": f"Cycle launched in {mode_str}: target {limit} jobs for '{keyword}' in '{location}' ({sal_str}), timer {duration_minutes}m."
    }


@app.post("/api/reset-tracker")
def reset_tracker(_admin: bool = Depends(verify_admin_access)):
    """Backs up existing tracker and resets all sheets so deduplication unblocks fresh applications."""
    backup_file = engine.excel_mgr.backup_and_reset()
    engine.stats["total_applied"] = 0
    engine.stats["matched_65_pct"] = 0
    engine.log(f"🧹 Tracker reset & backed up to '{backup_file}'. Deduplication unblocked for fresh applications!")
    return {
        "status": "success",
        "backup_file": backup_file,
        "message": f"Tracker backed up to {backup_file} and reset cleanly."
    }


@app.post("/api/clear-entries")
def clear_entries(tab: str = "all", _admin: bool = Depends(verify_admin_access)):
    """
    Clears applications from the Excel tracker (either a specific sheet tab or all sheets).
    Always creates an automatic timestamped backup first.
    """
    sheet_name = None if tab.lower() in ["all", "all applications"] else tab
    backup_file = engine.excel_mgr.backup_and_reset(sheet_name=sheet_name)
    try:
        all_rows = engine.excel_mgr.get_all_rows(sheet_name="All Applications")
        engine.stats["total_applied"] = len(all_rows)
    except Exception:
        pass
    engine.log(f"🧹 Tracker entries cleared for tab '{tab}' (backed up to '{backup_file}').")
    return {
        "status": "success",
        "backup_file": backup_file,
        "message": f"Successfully cleared entries for '{tab}'. Backup saved to {backup_file}."
    }


@app.post("/api/purge-manual")
def purge_manual_jobs(_admin: bool = Depends(verify_admin_access)):
    """Purges all manual application entries across sheets and wipes MANUAL_SAVED from seen cache."""
    excel_purged = engine.excel_mgr.purge_manual_applications()
    seen_purged = engine.seen_mgr.purge_manual_seen_jobs()
    engine.stats["manual_needed"] = 0
    engine.log(f"🧹 Purged {excel_purged} manual rows from Excel tracker and {seen_purged} entries from seen cache.")
    return {
        "status": "success",
        "excel_purged": excel_purged,
        "seen_purged": seen_purged,
        "message": f"Purged {excel_purged} manual sheet entries and {seen_purged} cached manual records."
    }


@app.delete("/api/applications/{app_id}")
def delete_application(app_id: str, _admin: bool = Depends(verify_admin_access)):
    """Deletes a specific application entry by ID from the Excel tracker."""
    deleted = engine.excel_mgr.delete_entry(app_id)
    if deleted:
        try:
            all_rows = engine.excel_mgr.get_all_rows(sheet_name="All Applications")
            engine.stats["total_applied"] = len(all_rows)
        except Exception:
            pass
        engine.log(f"🗑️ Deleted application entry '{app_id}' from tracker.")
        return {"status": "success", "message": f"Entry {app_id} deleted."}
    return {"status": "error", "message": f"Entry {app_id} not found."}


@app.get("/api/status")
def get_status(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns engine state, run duration timer, next schedule, stats, and logs."""
    if not isinstance(current_user, dict):
        current_user = None

    if not current_user:
        # Load any custom roles for guest
        guest_roles = []
        try:
            with open("config/profiles/active_profile.json", "r", encoding="utf-8") as f:
                p_data = json.load(f)
            guest_roles = p_data.get("target_roles", [])
        except Exception:
            pass
        custom_roles = [r for r in guest_roles if r.lower() not in ["data analyst", "business analyst", "ai product owner", "data engineer"]]
        custom_tab_counts = {r: 0 for r in custom_roles}

        return {
            "is_executing": False,
            "autopilot_active": False,
            "next_run": None,
            "last_run": None,
            "stats": {
                "total_applied": 0,
                "matched_65_pct": 0,
                "seen_jobs_cached": 0,
                "manual_needed": 0,
                "acw_total": 0,
                "avg_jobs_per_cycle": "0.0",
                "interviews_scheduled": 0,
                "follow_ups_sent": 0,
                "category_counts": {},
                "custom_roles": custom_tab_counts,
                "tab_counts": {
                    "all": 0,
                    "naukri": 0,
                    "indeed": 0,
                    "manual": 0,
                    "acw": 0,
                    "data_analyst": 0,
                    "business_analyst": 0,
                    "ai_product": 0,
                    "data_engineering": 0,
                    "other_tech": 0,
                    **custom_tab_counts
                },
            },
            "is_cooldown": False,
            "cooldown_seconds": 0,
            "no_jobs_found": False,
            "logs": [
                "⚡ AutoJob AI Multi-User Cloud Engine Online.",
                "👋 Guest candidate mode: Enter your Naukri profile & upload resume to launch automation."
            ],
        }

    user_id = current_user["id"]
    u_state = user_runner_mgr.get_user_status(user_id)
    is_exec = u_state.get("is_running", False)
    u_logs = u_state.get("logs", [])
    u_apps = db.get_applications(user_id, limit=1000)

    from src.excel_manager import detect_job_category
    cat_counts = {}
    naukri_count = sum(1 for a in u_apps if (a.get("platform") or "").lower() == "naukri")
    manual_count = sum(1 for a in u_apps if (a.get("status") or "").lower() == "manual apply needed")
    acw_count = sum(1 for a in u_apps if "company" in (a.get("platform") or "").lower())

    # User's custom roles
    user_roles = db.get_target_roles(user_id)
    custom_roles = [r for r in user_roles if r.lower() not in ["data analyst", "business analyst", "ai product owner", "data engineer"]]
    custom_tab_counts = {}
    for r in custom_roles:
        r_cnt = sum(1 for a in u_apps if r.lower() in (a.get("job_title", "")).lower() or r.lower() in (a.get("notes", "")).lower())
        custom_tab_counts[r] = r_cnt

    tab_counts = {
        "all": len(u_apps),
        "naukri": naukri_count,
        "indeed": 0,
        "manual": manual_count,
        "acw": acw_count,
        "data_analyst": 0,
        "business_analyst": 0,
        "ai_product": 0,
        "data_engineering": 0,
        "other_tech": 0,
        **custom_tab_counts
    }
    for a in u_apps:
        cat = detect_job_category(a.get("job_title", ""), a.get("notes", ""))
        cat_key = cat.lower().replace(" & ", "_").replace(" ", "_")
        if cat_key in tab_counts:
            tab_counts[cat_key] += 1
        cat_counts.setdefault(cat, {"total": 0, "auto": 0, "manual": 0})
        cat_counts[cat]["total"] += 1

    matched_count = sum(1 for a in u_apps if float(a.get("match_score") or 0.0) >= 65)

    active_rem = max(0, int(u_state.get("deadline", 0) - time.time())) if is_exec else 0

    return {
        "is_executing": is_exec,
        "autopilot_active": False,
        "next_run": None,
        "last_run": None,
        "is_cooldown": u_state.get("is_cooldown", False),
        "cooldown_seconds": u_state.get("cooldown_seconds", 0),
        "no_jobs_found": u_state.get("no_jobs_found", False),
        "stats": {
            "total_applied": len(u_apps),
            "matched_65_pct": matched_count,
            "seen_jobs_cached": len(u_apps),
            "manual_needed": manual_count,
            "acw_total": acw_count,
            "avg_jobs_per_cycle": f"{len(u_apps):.1f}" if u_apps else "0.0",
            "active_time_remaining_seconds": active_rem,
            "interviews_scheduled": 0,
            "follow_ups_sent": 0,
            "category_counts": cat_counts,
            "custom_roles": custom_tab_counts,
            "tab_counts": tab_counts,
        },
        "logs": u_logs or [f"⚡ Active candidate session for {current_user.get('email', '')}. Ready to apply."],
    }


DEFAULT_KEYWORD_ROLES = [
    {"id": "data analyst", "label": "Data Analyst"},
    {"id": "business analyst", "label": "Business Analyst"},
    {"id": "ai product owner", "label": "AI Product Owner"},
    {"id": "product management", "label": "Product Management"},
    {"id": "analyst", "label": "Analyst (B.Tech CSE)"},
    {"id": "all", "label": "⚡ All Target Roles (Rotate)"},
]


@app.get("/api/keywords")
def get_keywords(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns candidate keyword roles, combining defaults with custom user-saved roles."""
    if current_user and isinstance(current_user, dict):
        user_roles = db.get_target_roles(current_user["id"])
    else:
        try:
            with open("config/profiles/active_profile.json", "r", encoding="utf-8") as f:
                p_data = json.load(f)
            user_roles = p_data.get("target_roles", ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"])
        except Exception:
            user_roles = ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"]

    seen = set()
    result = []
    # Add user's custom roles first
    for r in user_roles:
        r_clean = r.strip()
        r_id = r_clean.lower()
        if r_id and r_id not in seen:
            seen.add(r_id)
            is_custom = r_id not in ["data analyst", "business analyst", "ai product owner", "product management", "analyst", "all"]
            result.append({"id": r_clean, "label": r_clean, "is_custom": is_custom})

    # Ensure standard defaults are present
    for def_role in DEFAULT_KEYWORD_ROLES:
        if def_role["id"] not in seen:
            seen.add(def_role["id"])
            result.append({**def_role, "is_custom": False})

    return result


@app.post("/api/keywords")
def add_keyword_role(
    payload: Dict[str, Any] = Body(...),
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """
    Adds a new custom job role (one or many words).
    1. Saves role to candidate profile in DB or active_profile.json.
    2. Automatically creates a dedicated sheet in the Excel tracker for this role.
    3. Returns the created sheet name and updated target roles.
    """
    role = (payload.get("role") or payload.get("keyword") or "").strip()
    if not role:
        raise HTTPException(status_code=400, detail="Role name cannot be empty.")

    # 1. Create dedicated sheet in Excel tracker
    sheet_name = engine.excel_mgr.ensure_custom_role_sheet(role)

    # 2. Save in profile
    if current_user and isinstance(current_user, dict):
        updated_roles = db.add_target_role(current_user["id"], role)
    else:
        try:
            prof_path = Path("config/profiles/active_profile.json")
            if prof_path.exists():
                with open(prof_path, "r", encoding="utf-8") as f:
                    p_data = json.load(f)
            else:
                p_data = {}
            roles = p_data.get("target_roles", ["Data Analyst", "Business Analyst", "AI Product Owner", "Data Engineer"])
            if not any(r.lower() == role.lower() for r in roles):
                roles.append(role)
            p_data["target_roles"] = roles
            with open(prof_path, "w", encoding="utf-8") as f:
                json.dump(p_data, f, indent=2)
            updated_roles = roles
        except Exception:
            updated_roles = [role]

    engine.log(f"📋 Added custom target role '{role}'. Excel sheet '{sheet_name}' ensured.")
    return {
        "status": "success",
        "message": f"Target role '{role}' added to profile and Excel sheet created.",
        "role": role,
        "sheet_name": sheet_name,
        "target_roles": updated_roles
    }


@app.put("/api/keywords")
def update_keyword_role(
    payload: Dict[str, Any] = Body(...),
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """Updates/edits an existing target job role."""
    old_role = (payload.get("old_role") or "").strip()
    new_role = (payload.get("new_role") or "").strip()
    if not old_role or not new_role:
        raise HTTPException(status_code=400, detail="Both old_role and new_role must be provided.")

    sheet_name = engine.excel_mgr.ensure_custom_role_sheet(new_role)
    if current_user and isinstance(current_user, dict):
        updated_roles = db.update_target_role(current_user["id"], old_role, new_role)
    else:
        try:
            prof_path = Path("config/profiles/active_profile.json")
            with open(prof_path, "r", encoding="utf-8") as f:
                p_data = json.load(f)
            roles = p_data.get("target_roles", [])
            updated_roles = [new_role if r.lower() == old_role.lower() else r for r in roles]
            p_data["target_roles"] = updated_roles
            with open(prof_path, "w", encoding="utf-8") as f:
                json.dump(p_data, f, indent=2)
        except Exception:
            updated_roles = [new_role]

    return {
        "status": "success",
        "message": f"Updated role to '{new_role}'.",
        "role": new_role,
        "sheet_name": sheet_name,
        "target_roles": updated_roles
    }


@app.delete("/api/keywords")
def delete_keyword_role(
    role: Optional[str] = Query(None),
    payload: Optional[Dict[str, Any]] = Body(None),
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """Deletes a custom target job role from user profile."""
    target_role = role or (payload.get("role") if isinstance(payload, dict) else None) or ""
    clean_role = target_role.strip()
    if not clean_role:
        raise HTTPException(status_code=400, detail="Role name required.")

    if current_user and isinstance(current_user, dict):
        updated_roles = db.delete_target_role(current_user["id"], clean_role)
    else:
        try:
            prof_path = Path("config/profiles/active_profile.json")
            with open(prof_path, "r", encoding="utf-8") as f:
                p_data = json.load(f)
            roles = p_data.get("target_roles", [])
            updated_roles = [r for r in roles if r.lower() != clean_role.lower()]
            p_data["target_roles"] = updated_roles
            with open(prof_path, "w", encoding="utf-8") as f:
                json.dump(p_data, f, indent=2)
        except Exception:
            updated_roles = []

    return {
        "status": "success",
        "message": f"Deleted role '{clean_role}'.",
        "target_roles": updated_roles
    }


@app.get("/api/applications")
def get_applications(
    tab: str = "All Applications",
    current_user: Optional[dict] = Depends(get_current_user_optional)
):
    """Returns tracked applications from SQLite for authenticated user. Returns empty list for guests."""
    if not isinstance(current_user, dict):
        current_user = None

    if not current_user:
        return []

    user_id = current_user["id"]
    raw_apps = db.get_applications(user_id, limit=500)
    from src.excel_manager import detect_job_category

    formatted = []
    tab_lower = tab.lower()
    for a in raw_apps:
        cat = detect_job_category(a.get("job_title", ""), a.get("notes", ""))
        match_score = float(a.get("match_score") or 0.0)
        match_pct_str = f"{int(match_score)}%" if match_score > 1 else f"{int(match_score * 100)}%"
        chance = "High" if match_score >= 80 or match_score >= 0.8 else ("Mid" if match_score >= 60 or match_score >= 0.6 else "Low")
        raw_mode = "Auto Applied"
        if (a.get("status") or "").lower() == "manual apply needed":
            raw_mode = "Manual"

        row = {
            "Application ID": a.get("id", ""),
            "Date Applied": (a.get("applied_at", "")[:10] if a.get("applied_at") else datetime.now().strftime("%Y-%m-%d")),
            "Company": a.get("company", ""),
            "Job Title": a.get("job_title", ""),
            "Category": cat,
            "Location": a.get("location", ""),
            "Job URL": a.get("url", ""),
            "Platform": a.get("platform", "Naukri"),
            "Status": a.get("status", "Applied"),
            "Apply Mode": raw_mode,
            "Match %": match_pct_str,
            "Chance": chance,
            "Last Updated": a.get("applied_at", ""),
            "Follow-Up Date": "In 4 Days",
            "Notes": a.get("notes", "")
        }

        # Tab filtering
        if tab_lower in ["all", "all applications"]:
            formatted.append(row)
        elif tab_lower == "naukri jobs" and "naukri" in (a.get("platform") or "").lower():
            formatted.append(row)
        elif tab_lower == "acw" and "company" in (a.get("platform") or "").lower():
            formatted.append(row)
        elif tab_lower == "manual" and (a.get("status") or "").lower() == "manual apply needed":
            formatted.append(row)
        elif tab_lower == cat.lower():
            formatted.append(row)

    return formatted


@app.get("/api/failures")
def get_failures():
    """Returns captured diagnostic records for applications that failed after 5 attempts."""
    log_file = Path("config/logs/submission_failures.json")
    if log_file.exists():
        try:
            import json
            with open(log_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


@app.get("/api/cache-stats")
def get_cache_stats():
    """Returns telemetry on seen jobs cache and smart pagination cursors."""
    return engine.seen_mgr.get_stats()


@app.post("/api/reset-cache")
def reset_cache():
    """Clears the seen jobs cache, re-syncs from local Excel, and resets pagination."""
    cleared = engine.seen_mgr.clear_seen_cache()
    synced = engine.seen_mgr.sync_from_excel(engine.excel_mgr)
    engine.stats["seen_jobs_cached"] = len(engine.seen_mgr.data.get("seen_jobs", {}))
    engine.log(f"🔄 Seen jobs cache reset & re-synced from Excel ({synced} records).")
    return {
        "status": "success",
        "cleared": cleared,
        "synced": synced,
        "message": f"Cache reset and re-synced {synced} jobs from Excel tracker.",
        "stats": engine.seen_mgr.get_stats(),
    }


@app.post("/api/reset-pagination")
def reset_pagination():
    """Resets pagination cursors for all search roles to Page 1."""
    engine.seen_mgr.reset_cursor()
    engine.log("📑 Smart pagination cursors reset to Page 1 for all keywords.")
    return {
        "status": "success",
        "message": "Pagination cursors reset to Page 1 for all keywords.",
        "cursors": engine.seen_mgr.data.get("pagination_cursors", {}),
    }


@app.get("/api/accounts")
def get_accounts(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns linked platform session statuses for current user or clean unlinked state for guests."""
    if not isinstance(current_user, dict):
        current_user = None

    if not current_user:
        return {
            "naukri": {
                "linked": False,
                "name": "Naukri.com (Primary)",
                "cookie_count": 0,
                "file_size_kb": 0.0,
                "username": ""
            },
            "gmail": {
                "linked": False,
                "name": "Gmail & Google Sheets",
                "sheet_id": "",
                "sheet_url": "",
                "gmail_id": "",
                "is_authorized": False
            }
        }

    creds = db.get_credentials(current_user["id"])
    user_cookies = creds.get("naukri_cookies", [])
    has_naukri = len(user_cookies) > 0
    sheet_id = creds.get("google_sheet_id") or ""
    sheet_url = creds.get("google_sheet_url") or ""
    gmail_id = creds.get("gmail_id") or current_user.get("email") or ""
    is_authorized = bool(creds.get("is_authorized"))
    has_sheets = bool(sheet_id or sheet_url or creds.get("google_token_json"))

    return {
        "naukri": {
            "linked": has_naukri,
            "name": "Naukri.com (Primary)",
            "cookie_count": len(user_cookies),
            "file_size_kb": 28.0 if has_naukri else 0.0,
            "username": creds.get("naukri_username", "")
        },
        "gmail": {
            "linked": has_sheets,
            "name": "Gmail & Google Sheets",
            "sheet_id": sheet_id,
            "sheet_url": sheet_url,
            "gmail_id": gmail_id,
            "is_authorized": is_authorized
        }
    }


@app.post("/api/link-indeed")
def link_indeed():
    """Indeed integration disabled; application pipeline focuses 100% on Naukri.com."""
    return {"status": "disabled", "message": "Indeed integration disabled. Focusing on Naukri.com."}


@app.post("/api/link-naukri")
def link_naukri(background_tasks: BackgroundTasks, current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Launches interactive browser to log into Naukri and save session."""
    user_id = current_user.get("id") if (isinstance(current_user, dict)) else None

    from src.applier.naukri import NaukriApplier
    def _run():
        applier = NaukriApplier(engine.profile)
        applier.interactive_login()
        # When interactive login completes, sync cookies to db if user is logged in
        if user_id:
            try:
                state_file = Path("config/credentials/naukri_state.json")
                if state_file.exists():
                    import json
                    state_data = json.loads(state_file.read_text(encoding="utf-8"))
                    cookies = state_data.get("cookies", [])
                    if cookies:
                        db.save_naukri_cookies(user_id, cookies)
            except Exception as e:
                print(f"Error syncing cookies to db for user {user_id}: {e}")

    background_tasks.add_task(_run)
    return {"status": "started", "message": "Naukri login browser opened."}


@app.post("/api/shutdown")
def shutdown_app(_admin: bool = Depends(verify_admin_access)):
    """Kill switch: safely stops engine, scheduler, and shuts down the process."""
    engine.stop_scheduler()
    engine.log("🛑 Kill switch activated. AutoJob AI shutting down cleanly...")
    import threading
    def _kill():
        import time
        time.sleep(1)
        import os
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()
    return {"status": "shutdown", "message": "AutoJob AI successfully stopped."}


@app.post("/api/toggle-autopilot")
def toggle_autopilot(_admin: bool = Depends(verify_admin_access)):
    """Toggles the 30-minute background scheduler."""
    engine.autopilot_active = not engine.autopilot_active
    state = "Active" if engine.autopilot_active else "Paused"
    engine.log(f"30-Minute Autopilot state switched to: {state}")
    return {"autopilot_active": engine.autopilot_active, "state": state}


@app.get("/download/excel")
def download_excel(_admin: bool = Depends(verify_admin_access)):
    """Directly downloads the local Excel tracker file."""
    excel_file = Path("Job_Applications_Tracker.xlsx")
    if not excel_file.exists():
        engine.excel_mgr._ensure_workbook()
    return FileResponse(
        path=str(excel_file),
        filename="Job_Applications_Tracker.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# =====================================================================
# Multi-User Authentication & Account Management APIs
# =====================================================================

@app.post("/api/auth/register")
def register_user(request: Request, response: Response, data: dict = Body(...)):
    """Registers a new user and sets their session cookie."""
    email = data.get("email", "")
    password = data.get("password", "")
    naukri_url = data.get("naukri_url", "")

    try:
        user = auth_manager.register(email, password, naukri_url)
        _, token = auth_manager.login(email, password)
        if "shiva" in email.lower() and len(db.get_applications(user["id"])) == 0:
            db.import_legacy_applications(user["id"])

        is_secure = request.url.scheme == "https" or os.getenv("ENV") == "production"
        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=is_secure,
            max_age=30 * 86400
        )
        return {
            "status": "success",
            "message": "Account created successfully!",
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "naukri_profile_url": user["naukri_profile_url"]
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")


@app.post("/api/auth/login")
def login_user(request: Request, response: Response, data: dict = Body(...)):
    """Logs in an existing user and returns a session token."""
    email = data.get("email", "")
    password = data.get("password", "")

    try:
        user, token = auth_manager.login(email, password)
        if "shiva" in email.lower() and len(db.get_applications(user["id"])) == 0:
            db.import_legacy_applications(user["id"])

        is_secure = request.url.scheme == "https" or os.getenv("ENV") == "production"
        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=is_secure,
            max_age=30 * 86400
        )
        return {
            "status": "success",
            "message": "Logged in successfully!",
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "naukri_profile_url": user["naukri_profile_url"]
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")


@app.post("/api/auth/logout")
def logout_user(request: Request, response: Response):
    """Logs out current user and clears session token."""
    token = request.cookies.get("session_token")
    if token:
        auth_manager.logout(token)
    response.delete_cookie(key="session_token")
    return {"status": "success", "message": "Logged out successfully."}


@app.get("/api/auth/me")
def get_auth_me(current_user: Optional[dict] = Depends(get_current_user_optional)):
    """Returns active user session details or unauthenticated status."""
    if not current_user:
        return {"authenticated": False, "user": None}

    prof = db.get_profile(current_user["id"])
    creds = db.get_credentials(current_user["id"])
    sheet_url = creds.get("google_sheet_url") or (f"https://docs.google.com/spreadsheets/d/{creds['google_sheet_id']}/edit" if creds.get("google_sheet_id") else "")
    return {
        "authenticated": True,
        "user": {
            "id": current_user["id"],
            "email": current_user["email"],
            "naukri_profile_url": current_user.get("naukri_profile_url", ""),
            "full_name": prof.get("full_name") or "",
            "headline": prof.get("headline") or "",
            "resume_name": prof.get("resume_name") or "",
            "google_sheet_url": sheet_url,
            "google_sheet_id": creds.get("google_sheet_id") or "",
            "gmail_id": creds.get("gmail_id") or current_user.get("email") or "",
            "is_authorized": bool(creds.get("is_authorized")),
            "authorized_at": creds.get("authorized_at") or "",
            "consent_acknowledged": bool(current_user.get("consent_acknowledged", 0)),
            "consent_acknowledged_at": current_user.get("consent_acknowledged_at") or "",
            "naukri_connected": len(creds.get("naukri_cookies", [])) > 0,
        }
    }


@app.post("/api/user/consent")
def acknowledge_user_consent(current_user: dict = Depends(get_current_user)):
    """Marks prototype guidelines & consent as acknowledged for the candidate."""
    db.set_user_consent(current_user["id"], acknowledged=True)
    return {
        "status": "success",
        "message": "Consent and prototype usage guidelines acknowledged."
    }


@app.get("/api/user/authorizations")
def get_user_authorizations(current_user: dict = Depends(get_current_user)):
    """Returns candidate's authorization status for Naukri, Google Sheets, and Gmail."""
    user_id = current_user["id"]
    creds = db.get_credentials(user_id)
    return {
        "status": "success",
        "authorization": {
            "is_authorized": bool(creds.get("is_authorized")),
            "authorized_at": creds.get("authorized_at") or "",
            "naukri_profile_url": current_user.get("naukri_profile_url", ""),
            "google_sheet_url": creds.get("google_sheet_url") or "",
            "google_sheet_id": creds.get("google_sheet_id") or "",
            "gmail_id": creds.get("gmail_id") or current_user.get("email") or "",
            "naukri_connected": len(creds.get("naukri_cookies", [])) > 0
        }
    }


@app.post("/api/user/authorize-permissions")
def authorize_user_permissions(
    payload: Dict[str, Any] = Body(...),
    current_user: dict = Depends(get_current_user)
):
    """
    Authorizes AutoJob AI to act on candidate's behalf:
    1. Access Naukri.com to apply to jobs.
    2. Edit and add rows to their personal Google Sheet tracker.
    3. Send job status & recruiter alerts to their Gmail address.
    """
    user_id = current_user["id"]
    naukri_url = (payload.get("naukri_url") or payload.get("naukri_profile_url") or "").strip()
    google_sheet_url = (payload.get("google_sheet_url") or payload.get("sheet_url") or "").strip()
    gmail_id = (payload.get("gmail_id") or payload.get("email") or "").strip()
    authorize = bool(payload.get("authorize", True))

    sheet_id = ""
    if google_sheet_url:
        import re
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", google_sheet_url)
        sheet_id = m.group(1) if m else google_sheet_url.split("/")[-1].split("?")[0]
        if not google_sheet_url.startswith("http"):
            google_sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    db.save_user_authorizations(
        user_id=user_id,
        is_authorized=authorize,
        google_sheet_url=google_sheet_url,
        google_sheet_id=sheet_id,
        gmail_id=gmail_id or current_user.get("email", "")
    )

    if naukri_url:
        db.update_naukri_url(user_id, naukri_url)

    engine.log(f"🔐 Candidate '{current_user.get('email')}' authorized AutoJob AI to act on their behalf (Naukri, Sheets, Gmail).")

    return {
        "status": "success",
        "message": "AutoJob AI successfully authorized! Bot is ready to apply on Naukri, update your Google Sheet, and dispatch Gmail alerts on your behalf.",
        "authorization": {
            "is_authorized": authorize,
            "naukri_profile_url": naukri_url or current_user.get("naukri_profile_url", ""),
            "google_sheet_url": google_sheet_url,
            "google_sheet_id": sheet_id,
            "gmail_id": gmail_id or current_user.get("email", ""),
        }
    }


# =====================================================================
# Per-User Candidate Profile & Resume APIs
# =====================================================================

@app.get("/api/user/profile")
def get_user_profile(current_user: dict = Depends(get_current_user)):
    """Returns the authenticated candidate's customized profile."""
    prof = db.get_profile(current_user["id"])
    full_name = prof.get("full_name") or ""
    parts = full_name.split(" ", 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""
    skills = prof.get("skills") or []
    personal_info = {
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "email": prof.get("email") or current_user.get("email") or "",
        "phone": prof.get("phone") or "",
        "current_title": prof.get("current_title") or "Professional",
        "current_company": prof.get("current_company") or "",
        "location": prof.get("location") or "India",
        "headline": prof.get("headline") or "",
        "naukri_profile_url": current_user.get("naukri_profile_url", ""),
    }
    return {
        "status": "success",
        "profile": prof,
        "personal_info": personal_info,
        "education": prof.get("education") or {},
        "experience": {
            "years_of_experience": prof.get("years_of_experience", 0),
            "skills": skills,
        },
        "screening_answers": prof.get("screening_answers") or {},
        "resume_path": prof.get("resume_path") or "",
        "resume_name": prof.get("resume_name") or "resume.pdf",
        "skills": skills,
        "total_skills": len(skills),
        "naukri_profile_url": current_user.get("naukri_profile_url", "")
    }


@app.post("/api/user/profile")
def save_user_profile(data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """Updates candidate profile fields (Naukri-style customization)."""
    user_id = current_user["id"]
    existing = db.get_profile(user_id)
    existing.update(data)
    db.save_profile(user_id, existing)

    if "naukri_profile_url" in data:
        db.update_naukri_url(user_id, data["naukri_profile_url"])

    return {"status": "success", "message": "Candidate profile updated successfully."}


@app.post("/api/user/naukri-link")
def update_naukri_link(data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """Updates candidate's Naukri profile link."""
    url = data.get("naukri_profile_url", "").strip()
    db.update_naukri_url(current_user["id"], url)
    return {"status": "success", "message": "Naukri profile URL saved.", "url": url}


@app.post("/api/user/upload-resume")
async def user_upload_resume(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """
    Accepts candidate PDF resume, extracts skills, experience, education,
    and updates their isolated candidate profile in SQLite.
    """
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF resume files (.pdf) are supported.")

    MAX_RESUME_SIZE = 10 * 1024 * 1024  # 10 MB limit
    content = await file.read(MAX_RESUME_SIZE + 1)
    if len(content) > MAX_RESUME_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds maximum size limit of 10 MB.")

    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Invalid file format: file must be a genuine PDF document.")

    user_id = current_user["id"]
    ws_dir = get_user_workspace(user_id)
    clean_name = Path(re.sub(r"[^\w\-_\.]", "_", file.filename)).name
    target_path = ws_dir / clean_name

    with open(target_path, "wb") as buffer:
        buffer.write(content)

    try:
        raw_text = extract_text_from_pdf(target_path)
        parsed = parse_resume_data(raw_text)

        # Merge extracted data with user's profile
        existing = db.get_profile(user_id)
        if parsed.get("personal_info"):
            pi = parsed["personal_info"]
            if pi.get("full_name"):
                existing["full_name"] = pi["full_name"]
            if pi.get("email"):
                existing["email"] = pi["email"]
            if pi.get("phone"):
                existing["phone"] = pi["phone"]
            if pi.get("location"):
                existing["location"] = pi["location"]
            if pi.get("current_title"):
                existing["current_title"] = pi["current_title"]
            if pi.get("current_company"):
                existing["current_company"] = pi["current_company"]
            if pi.get("headline"):
                existing["headline"] = pi["headline"]

        if parsed.get("experience"):
            exp_data = parsed["experience"]
            new_skills = exp_data.get("skills", [])
            curr_skills = set(existing.get("skills", []))
            curr_skills.update(new_skills)
            existing["skills"] = list(curr_skills)
            if exp_data.get("years_of_experience") is not None:
                existing["years_of_experience"] = exp_data["years_of_experience"]

        if parsed.get("education"):
            existing["education"] = parsed["education"]

        if parsed.get("screening_answers"):
            curr_scr = existing.get("screening_answers", {})
            curr_scr.update(parsed["screening_answers"])
            existing["screening_answers"] = curr_scr

        existing["resume_path"] = str(target_path)
        existing["resume_name"] = file.filename

        db.save_profile(user_id, existing)
        full_name = existing.get("full_name") or ""
        parts = full_name.split(" ", 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

        return {
            "status": "success",
            "message": f"Resume '{file.filename}' parsed successfully! Candidate: {full_name}.",
            "profile": existing,
            "personal_info": {
                "full_name": full_name,
                "first_name": first_name,
                "last_name": last_name,
                "email": existing.get("email") or current_user.get("email") or "",
                "phone": existing.get("phone") or "",
                "location": existing.get("location") or "",
                "title": existing.get("current_title") or "",
                "current_title": existing.get("current_title") or "",
                "skills_count": len(existing.get("skills", [])),
                "skills": existing.get("skills", []),
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse resume: {str(e)}")


# =====================================================================
# Per-User Search Filters & Match Threshold APIs
# =====================================================================

@app.get("/api/user/filters")
def get_user_filters(current_user: dict = Depends(get_current_user)):
    """Returns candidate's customized Naukri filters and threshold."""
    filters = db.get_filters(current_user["id"])
    return {"status": "success", "filters": filters}


@app.post("/api/user/filters")
def save_user_filters(data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """Saves candidate's customized Naukri filters and threshold."""
    db.save_filters(current_user["id"], data)
    return {"status": "success", "message": "Filters and threshold updated successfully."}


# =====================================================================
# Per-User Platform Accounts & Integrations APIs
# =====================================================================

@app.get("/api/user/accounts")
def get_user_accounts(current_user: dict = Depends(get_current_user)):
    """Returns status of user's connected platforms."""
    creds = db.get_credentials(current_user["id"])
    cookies = creds.get("naukri_cookies", [])
    state_file = Path("config/credentials/naukri_state.json")

    # Auto-sync from state file if db empty
    if not cookies and state_file.exists():
        try:
            import json
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            file_cookies = state_data.get("cookies", [])
            if file_cookies:
                cookies = file_cookies
                db.save_naukri_cookies(current_user["id"], cookies)
        except Exception:
            pass

    naukri_connected = len(cookies) > 0
    google_sheet_id = creds.get("google_sheet_id", "")

    return {
        "naukri": {
            "connected": naukri_connected,
            "cookie_count": len(cookies),
            "username": creds.get("naukri_username", ""),
            "connected_at": creds.get("naukri_connected_at", "")
        },
        "google": {
            "connected": bool(google_sheet_id),
            "sheet_id": google_sheet_id,
            "optional": True
        }
    }


@app.post("/api/user/naukri-session")
def save_user_naukri_session(data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """Saves exported session cookies or credentials for the authenticated user."""
    cookies = data.get("cookies", [])
    username = data.get("username", "")
    if not isinstance(cookies, list):
        raise HTTPException(status_code=400, detail="Cookies must be a list of cookie objects.")

    db.save_naukri_cookies(current_user["id"], cookies, username=username)
    return {
        "status": "success",
        "message": f"Successfully connected {len(cookies)} Naukri session cookies for your account."
    }


@app.post("/api/user/google-sheet")
def save_user_google_sheet(data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """Saves Google Sheet URL or ID for the candidate."""
    raw = (data.get("sheet_id") or data.get("sheet_url") or data.get("google_sheet_url") or "").strip()
    sheet_id = raw
    sheet_url = raw
    if "/spreadsheets/d/" in raw:
        import re
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
        if m:
            sheet_id = m.group(1)
            sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    elif raw and not raw.startswith("http"):
        sheet_url = f"https://docs.google.com/spreadsheets/d/{raw}/edit"

    db.save_google_sheet_details(current_user["id"], sheet_url, sheet_id)
    return {
        "status": "success",
        "message": "Google Sheet URL and ID saved. Applications will sync to your spreadsheet automatically.",
        "sheet_id": sheet_id,
        "sheet_url": sheet_url
    }


# =====================================================================
# Per-User Job Execution & Monitoring APIs
# =====================================================================

@app.post("/api/user/trigger")
def trigger_user_cycle(data: dict = Body(...), current_user: dict = Depends(get_current_user)):
    """Triggers an isolated auto-apply cycle for the authenticated candidate."""
    user_id = current_user["id"]
    filters = db.get_filters(user_id)

    limit = int(data.get("limit") or 25)
    keyword = data.get("keyword") or filters.get("keywords") or "data analyst"
    duration_minutes = int(data.get("duration_minutes") or 30)
    location = data.get("location") or filters.get("location") or "any"
    min_salary = float(data.get("min_salary") or filters.get("min_salary") or 0.0)
    max_salary = float(data.get("max_salary") or filters.get("max_salary") or 99.0)
    experience_years = int(data.get("experience_years") or filters.get("experience_years") or 0)
    job_age_days = int(data.get("job_age_days") or filters.get("job_age_days") or 0)
    work_mode = data.get("work_mode") or filters.get("work_mode") or "all"
    sort_by = data.get("sort_by") or filters.get("sort_by") or "relevance"
    apply_mode = data.get("apply_mode") or filters.get("apply_mode") or "all"
    threshold_pct = float(data.get("threshold_pct") or filters.get("threshold_pct") or 65.0)

    res = user_runner_mgr.start_user_cycle(
        user_id=user_id,
        limit=limit,
        keyword=keyword,
        duration_minutes=duration_minutes,
        headless=True,
        location=location,
        min_salary=min_salary,
        max_salary=max_salary,
        experience_years=experience_years,
        job_age_days=job_age_days,
        work_mode=work_mode,
        sort_by=sort_by,
        apply_mode=apply_mode,
        threshold_pct=threshold_pct,
    )

    return res


@app.post("/api/user/stop")
def stop_user_cycle(current_user: dict = Depends(get_current_user)):
    """Safely signals the candidate's running cycle to stop."""
    user_runner_mgr.stop_user_cycle(current_user["id"])
    return {"status": "stopping", "message": "Application cycle stopping safely..."}


@app.post("/api/user/dismiss-no-jobs")
def dismiss_no_jobs(current_user: dict = Depends(get_current_user)):
    """Clears the no_jobs_found popup flag for the candidate."""
    user_runner_mgr.clear_no_jobs_flag(current_user["id"])
    return {"status": "success", "message": "No-jobs flag cleared."}


@app.get("/api/user/status")
def get_user_status(current_user: dict = Depends(get_current_user)):
    """Returns live runner state, progress, and logs for the authenticated user."""
    return user_runner_mgr.get_user_status(current_user["id"])


@app.get("/api/user/applications")
def get_user_applications(limit: int = 500, current_user: dict = Depends(get_current_user)):
    """Returns tracked applications from SQLite for the authenticated user."""
    apps = db.get_applications(current_user["id"], limit=limit)
    return {"status": "success", "applications": apps, "total": len(apps)}


@app.delete("/api/user/applications/{app_id}")
def delete_user_application(app_id: str, current_user: dict = Depends(get_current_user)):
    """Deletes an application entry for the authenticated user."""
    deleted = db.delete_application(current_user["id"], app_id)
    if deleted:
        return {"status": "success", "message": f"Application {app_id} deleted."}
    raise HTTPException(status_code=404, detail=f"Application {app_id} not found.")


@app.post("/api/user/clear-applications")
def clear_user_applications(current_user: dict = Depends(get_current_user)):
    """Clears all tracked application entries for the authenticated user."""
    count = db.clear_applications(current_user["id"])
    return {"status": "success", "message": f"Cleared {count} application records."}


@app.get("/api/user/download-excel")
def download_user_excel(current_user: dict = Depends(get_current_user)):
    """Generates and downloads the user's isolated Excel tracker."""
    excel_path = export_user_applications_to_excel(current_user["id"])
    return FileResponse(
        path=str(excel_path),
        filename=f"Job_Applications_{current_user['id'][:8]}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

