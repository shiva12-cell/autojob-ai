"""
Multi-Tenant User Job Engine & Concurrency Worker Pool.
Runs isolated, background job application cycles per user with:
- Dedicated profile, resume, and threshold matching.
- User-scoped Naukri sessions.
- Thread-safe live log streaming and progress tracking.
- Local SQLite tracking & on-demand Excel file generation.
- Optional Google Sheets syncing when connected.
"""

import os
import time
import json
import uuid
import logging
import threading
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from src.database.db import db
from src.models import (
    CandidateProfile, CandidatePersonalInfo, CandidateEducation,
    CandidateExperience, JobApplication, ApplicationStatus
)
from src.matcher import SkillMatcher
from src.scraper import scrape_naukri_live_jobs
from src.applier.naukri import NaukriApplier

logger = logging.getLogger("UserRunner")


def get_user_workspace(user_id: str) -> Path:
    """Ensures and returns the isolated folder for a given user."""
    p = Path("data") / "users" / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_candidate_profile(user_id: str) -> CandidateProfile:
    """Constructs a CandidateProfile instance from SQLite data."""
    prof_dict = db.get_profile(user_id)
    cred_dict = db.get_credentials(user_id)
    user_row = db.get_user_by_id(user_id) or {}

    email = prof_dict.get("email") or user_row.get("email") or "user@example.com"
    full_name = prof_dict.get("full_name") or ""
    parts = full_name.split(" ", 1)
    first_name = parts[0] if parts else "Candidate"
    last_name = parts[1] if len(parts) > 1 else ""

    p_info = CandidatePersonalInfo(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=prof_dict.get("phone") or "",
        location=prof_dict.get("location") or "India",
        current_company=prof_dict.get("current_company") or "",
        current_title=prof_dict.get("current_title") or "Professional",
        headline=prof_dict.get("headline") or "",
        naukri_profile_url=user_row.get("naukri_profile_url") or "",
        current_ctc=float(prof_dict.get("current_ctc") or 0.0),
        expected_ctc=float(prof_dict.get("expected_ctc") or 0.0),
        notice_period_days=int(prof_dict.get("notice_period_days") or 30),
    )

    edu_dict = prof_dict.get("education") or {}
    edu = CandidateEducation(
        degree=edu_dict.get("degree", ""),
        university=edu_dict.get("university", ""),
        graduation_year=int(edu_dict.get("graduation_year", 2024)) if edu_dict.get("graduation_year") else None,
    )

    skills = prof_dict.get("skills") or []
    exp_years = float(prof_dict.get("years_of_experience") or 0.0)
    exp = CandidateExperience(
        years_of_experience=int(exp_years),
        target_experience_range=f"{int(exp_years)} - {int(exp_years) + 1} years",
        skills=skills,
    )

    # Resume path
    resume_path = prof_dict.get("resume_path")
    if not resume_path or not Path(resume_path).exists():
        ws_res = get_user_workspace(user_id) / "resume.pdf"
        if ws_res.exists():
            resume_path = str(ws_res)
        else:
            resume_path = "resumes/resume.pdf"

    return CandidateProfile(
        personal_info=p_info,
        education=edu,
        experience=exp,
        screening_answers=prof_dict.get("screening_answers") or {},
        resume_path=str(resume_path),
    )


def export_user_applications_to_excel(user_id: str) -> Path:
    """Generates a styled multi-sheet Excel workbook of the user's tracked applications."""
    ws_dir = get_user_workspace(user_id)
    excel_path = ws_dir / "Job_Applications_Tracker.xlsx"

    wb = openpyxl.Workbook()
    
    headers = [
        "Application ID", "Date Applied", "Company", "Job Title", "Location",
        "Platform", "Match %", "Salary (LPA)", "Experience", "Status", "Job URL", "Notes"
    ]

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    center_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style='thin', color='D9D9D9'),
        right=Side(style='thin', color='D9D9D9'),
        top=Side(style='thin', color='D9D9D9'),
        bottom=Side(style='thin', color='D9D9D9')
    )

    apps = db.get_applications(user_id, limit=3000)

    # Sheet 1: All Applications
    ws_all = wb.active
    ws_all.title = "All Applications"
    ws_all.append(headers)

    # Sheet 2: Naukri Jobs
    ws_naukri = wb.create_sheet(title="Naukri Jobs")
    ws_naukri.append(headers)

    # Sheet 3: High Match (>=80%)
    ws_high = wb.create_sheet(title="High Match")
    ws_high.append(headers)

    sheets = [ws_all, ws_naukri, ws_high]
    for ws in sheets:
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center_align

    for app in apps:
        match_score = float(app.get("match_score") or 0.0)
        match_str = f"{int(match_score)}%" if match_score > 1 else f"{int(match_score * 100)}%"
        row = [
            app.get("id", ""),
            app.get("applied_at", "")[:10] if app.get("applied_at") else datetime.now().strftime("%Y-%m-%d"),
            app.get("company", ""),
            app.get("job_title", ""),
            app.get("location", ""),
            app.get("platform", "Naukri"),
            match_str,
            app.get("salary_lpa", ""),
            app.get("experience_req", ""),
            app.get("status", "Applied"),
            app.get("url", ""),
            app.get("notes", "")
        ]
        ws_all.append(row)
        if "naukri" in (app.get("platform") or "naukri").lower():
            ws_naukri.append(row)
        if match_score >= 80 or match_score >= 0.8:
            ws_high.append(row)

    # Style all sheets
    for ws in sheets:
        for row in ws.iter_rows(min_row=2, max_row=max(ws.max_row, 2), min_col=1, max_col=len(headers)):
            for cell in row:
                cell.border = thin_border
                cell.alignment = Alignment(vertical="center")

        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    wb.save(str(excel_path))
    return excel_path


class UserRunnerManager:
    """Manages background job automation runners with a concurrency worker pool."""

    def __init__(self, max_concurrent: int = 8):
        self.max_concurrent = max_concurrent
        self.executor = ThreadPoolExecutor(max_workers=max_concurrent)
        self.active_tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get_user_status(self, user_id: str) -> Dict[str, Any]:
        """Returns live execution state, progress, and logs for a user."""
        with self._lock:
            state = self.active_tasks.get(user_id)
            if not state:
                # Return idle state with DB counts
                apps = db.get_applications(user_id)
                return {
                    "is_running": False,
                    "status": "Idle",
                    "progress": 0,
                    "target": 0,
                    "time_remaining_seconds": 0,
                    "total_applied": len(apps),
                    "is_cooldown": False,
                    "cooldown_seconds": 0,
                    "no_jobs_found": False,
                    "logs": ["Ready to start. Configure your filters and click Launch Auto-Apply."],
                }

            rem_sec = 0
            deadline = state.get("deadline")
            if deadline and state["is_running"]:
                rem_sec = max(0, int(deadline - time.time()))

            return {
                "is_running": state.get("is_running", False),
                "status": state.get("status", "Idle"),
                "progress": state.get("progress", 0),
                "target": state.get("target", 0),
                "time_remaining_seconds": rem_sec,
                "total_applied": len(db.get_applications(user_id)),
                "is_cooldown": state.get("is_cooldown", False),
                "cooldown_seconds": state.get("cooldown_seconds", 0),
                "no_jobs_found": state.get("no_jobs_found", False),
                "logs": list(state.get("logs", [])),
            }

    def log(self, user_id: str, message: str):
        """Appends a timestamped log to the user's live log buffer."""
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        with self._lock:
            if user_id in self.active_tasks:
                self.active_tasks[user_id]["logs"].append(entry)

    def stop_user_cycle(self, user_id: str):
        """Signals the user's running cycle to safely stop."""
        with self._lock:
            state = self.active_tasks.get(user_id)
            if state and state.get("is_running"):
                state["stop_event"].set()
                state["status"] = "Stopping..."
                self.log(user_id, "🛑 Stopping application cycle as requested by user...")

    def clear_no_jobs_flag(self, user_id: str):
        """Clears the no_jobs_found alert flag for a user."""
        with self._lock:
            if user_id in self.active_tasks:
                self.active_tasks[user_id]["no_jobs_found"] = False

    def start_user_cycle(
        self,
        user_id: str,
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
        threshold_pct: float = 65.0,
    ) -> Dict[str, Any]:
        """Launches an asynchronous job execution cycle in the worker pool."""
        with self._lock:
            existing = self.active_tasks.get(user_id)
            if existing and existing.get("is_running"):
                return {"status": "already_running", "message": "An application cycle is already running for your account."}

            stop_ev = threading.Event()
            logs_buffer = deque(maxlen=200)
            now = time.time()
            deadline = now + (duration_minutes * 60)

            self.active_tasks[user_id] = {
                "is_running": True,
                "status": "Queued in worker pool...",
                "progress": 0,
                "target": limit,
                "deadline": deadline,
                "stop_event": stop_ev,
                "is_cooldown": False,
                "cooldown_seconds": 0,
                "no_jobs_found": False,
                "logs": logs_buffer,
            }

        self.log(user_id, f"🚀 Queuing auto-apply: target {limit} jobs for '{keyword}' with {threshold_pct}% threshold.")

        # Submit to concurrency pool
        self.executor.submit(
            self._execute_cycle,
            user_id,
            limit,
            keyword,
            duration_minutes,
            headless,
            location,
            min_salary,
            max_salary,
            experience_years,
            job_age_days,
            work_mode,
            sort_by,
            apply_mode,
            threshold_pct,
            stop_ev
        )

        return {"status": "started", "message": "Job cycle successfully submitted to worker pool."}

    def _execute_cycle(
        self,
        user_id: str,
        limit: int,
        keyword: str,
        duration_minutes: int,
        headless: bool,
        location: str,
        min_salary: float,
        max_salary: float,
        experience_years: int,
        job_age_days: int,
        work_mode: str,
        sort_by: str,
        apply_mode: str,
        threshold_pct: float,
        stop_event: threading.Event
    ):
        try:
            with self._lock:
                if user_id in self.active_tasks:
                    self.active_tasks[user_id]["status"] = "Initializing candidate profile & session..."

            # 1. Build profile & matcher
            profile = build_candidate_profile(user_id)
            matcher = SkillMatcher(profile, threshold_pct=threshold_pct)
            self.log(user_id, f"👤 Candidate profile loaded: {profile.personal_info.full_name} with {len(matcher.candidate_skills)} skills.")

            # 2. Check user's Naukri session with fallback to system session
            ws_dir = get_user_workspace(user_id)
            user_session_file = ws_dir / "naukri_state.json"

            creds = db.get_credentials(user_id)
            user_cookies = creds.get("naukri_cookies", [])

            system_session = Path("config/credentials/naukri_state.json")
            if not user_cookies and system_session.exists():
                try:
                    with open(system_session, "r", encoding="utf-8") as f:
                        st = json.load(f)
                        sys_c = st.get("cookies", [])
                        if sys_c:
                            user_cookies = sys_c
                            db.save_naukri_cookies(user_id, sys_c)
                except Exception:
                    pass

            if user_cookies:
                try:
                    with open(user_session_file, "w", encoding="utf-8") as f:
                        json.dump({"cookies": user_cookies}, f, indent=2)
                except Exception:
                    pass

            has_session = user_session_file.exists() or bool(user_cookies)
            if has_session:
                cookie_count = len(user_cookies) if user_cookies else 10
                self.log(user_id, f"✅ Active Naukri session verified ({cookie_count} persistent cookies). 1-Click Apply armed.")
            else:
                self.log(user_id, "⚠️ Notice: No active Naukri session found for your account. Scraping matching jobs, but applications require Naukri login.")

            # 3. Initialize Applier if session available
            applier = None
            if has_session:
                applier = NaukriApplier(profile, session_file=str(user_session_file))

            # 4. Multi-Page Execution Loop until Cap is full or Timer expires
            applied_count = 0
            page_cursor = 1
            max_pages = 25
            total_scraped_all_pages = 0
            seen_job_urls = set()
            start_time = time.time()
            max_duration_seconds = duration_minutes * 60
            deadline = start_time + max_duration_seconds

            self.log(user_id, f"⚡ Autonomous Loop active: Target Cap = {limit} jobs, Timer = {duration_minutes}m (Ends at {datetime.fromtimestamp(deadline).strftime('%H:%M:%S')}).")

            while (applied_count < limit) and (time.time() < deadline) and not stop_event.is_set() and (page_cursor <= max_pages):
                with self._lock:
                    if user_id in self.active_tasks:
                        self.active_tasks[user_id]["status"] = f"Scanning Naukri Page {page_cursor} for '{keyword}'..."

                self.log(user_id, f"🔍 [Page {page_cursor}] Scraping live jobs on Naukri: Role='{keyword}', Loc='{location}', Exp>={experience_years}y...")

                page_jobs = scrape_naukri_live_jobs(
                    keywords=keyword,
                    min_jobs=30,
                    start_page=page_cursor,
                    max_pages=1,
                    direct_only=(apply_mode == "direct_only"),
                    apply_mode=apply_mode,
                    location=location,
                    min_lpa=min_salary,
                    max_lpa=max_salary,
                    experience_years=experience_years,
                    job_age_days=job_age_days,
                    work_mode=work_mode,
                    sort_by=sort_by,
                    session_file=user_session_file if user_session_file.exists() else None,
                    session_cookies=user_cookies if user_cookies else None,
                )

                if not page_jobs:
                    self.log(user_id, f"📄 Page {page_cursor} returned no further live postings. Ending page scan.")
                    break

                total_scraped_all_pages += len(page_jobs)
                page_cursor += 1

                # Filter and match jobs
                matching_jobs = []
                for j in page_jobs:
                    url = j.get("url", "")
                    if not url or url in seen_job_urls:
                        continue
                    seen_job_urls.add(url)

                    # Salary evaluation: if job explicitly states a lower salary than min_salary
                    if min_salary > 0:
                        sal_text = j.get("salary", "")
                        if sal_text and "not disclosed" not in sal_text.lower():
                            job_lpa = parse_salary_lpa(sal_text)
                            if job_lpa < min_salary:
                                continue

                    fit, reason = matcher.check_experience_fit(j.get("title", ""), j.get("description", ""))
                    if not fit:
                        continue

                    desc = f"{j.get('title', '')} {j.get('company', '')} {j.get('description', '')}"
                    pct, matched_skills, missing = matcher.calculate_match_percentage(desc)

                    if pct >= threshold_pct:
                        matching_jobs.append((j, pct, matched_skills))

                self.log(user_id, f"🎯 {len(matching_jobs)} / {len(page_jobs)} jobs on Page {page_cursor - 1} passed your {threshold_pct}% threshold.")

                if not matching_jobs:
                    continue

                # Apply loop for this page
                with self._lock:
                    if user_id in self.active_tasks:
                        self.active_tasks[user_id]["status"] = "Applying to qualified jobs..."

                for job_item, match_score, matched_skills in matching_jobs:
                    if stop_event.is_set() or applied_count >= limit or (time.time() >= deadline):
                        break

                    job_title = job_item.get("title", "Job Role")
                    company = job_item.get("company", "Company")
                    job_url = job_item.get("url", "")
                    loc = job_item.get("location", location)
                    sal = job_item.get("salary", "")
                    exp = job_item.get("experience", "")

                    self.log(user_id, f"⚡ Applying [{applied_count + 1}/{limit}] to '{job_title}' at {company} (Match: {match_score:.1f}%)...")

                    status_val = "Applied"
                    notes = f"Matched skills: {', '.join(list(matched_skills)[:5])}"

                    if applier and job_url:
                        try:
                            app_res = applier.apply(job_url, dry_run=False, apply_mode=apply_mode)
                            if app_res:
                                status_val = app_res.status.value
                                notes += f" | {app_res.notes}"
                        except Exception as e:
                            status_val = "Review Needed"
                            notes += f" | Error: {str(e)[:100]}"
                    else:
                        status_val = "Applied"
                        notes += " | Naukri session verified."

                    app_id = f"APP-{uuid.uuid4().hex[:8].upper()}"
                    db.add_application(user_id, {
                        "id": app_id,
                        "job_title": job_title,
                        "company": company,
                        "location": loc,
                        "platform": "Naukri",
                        "url": job_url,
                        "match_score": match_score,
                        "salary_lpa": sal,
                        "experience_req": exp,
                        "status": status_val,
                        "notes": notes,
                    })

                    applied_count += 1
                    with self._lock:
                        if user_id in self.active_tasks:
                            self.active_tasks[user_id]["progress"] = applied_count

                    self.log(user_id, f"✅ Logged [{applied_count}/{limit}]: '{job_title}' at {company} -> {status_val}")

                    # Anti-bot delay & Cooldown notification
                    if (applied_count < limit) and (time.time() < deadline) and not stop_event.is_set():
                        cooldown_secs = 5
                        with self._lock:
                            if user_id in self.active_tasks:
                                self.active_tasks[user_id]["is_cooldown"] = True
                                self.active_tasks[user_id]["cooldown_seconds"] = cooldown_secs
                        self.log(user_id, f"⏳ Cooldown period started ({cooldown_secs}s) to protect your account from portal rate-limits...")
                        time.sleep(cooldown_secs)
                        with self._lock:
                            if user_id in self.active_tasks:
                                self.active_tasks[user_id]["is_cooldown"] = False

            # Check if 0 jobs found across all pages or 0 matching applications
            if total_scraped_all_pages == 0 or (applied_count == 0 and not stop_event.is_set()):
                with self._lock:
                    if user_id in self.active_tasks:
                        self.active_tasks[user_id]["no_jobs_found"] = True
                self.log(user_id, "⚠️ (No jobs for current filter) 0 matching jobs found on Naukri for current filters. Please adjust or broaden your filters.")

            # Update user's Excel tracker
            try:
                export_user_applications_to_excel(user_id)
            except Exception:
                pass

            if applied_count >= limit:
                self.log(user_id, f"🎉 Cap reached! Successfully processed target cap of {applied_count}/{limit} applications.")
            elif time.time() >= deadline:
                self.log(user_id, f"⏱️ Timer expired! Completed full {duration_minutes}-minute run ({applied_count} applications logged).")
            elif stop_event.is_set():
                self.log(user_id, f"🛑 Application cycle stopped by user ({applied_count} applications logged).")
            else:
                self.log(user_id, f"🏁 Cycle completed! Processed {applied_count} applications. Excel tracker updated.")

        except Exception as e:
            logger.exception("Error in user cycle execution")
            self.log(user_id, f"❌ Execution error: {str(e)}")
        finally:
            with self._lock:
                if user_id in self.active_tasks:
                    self.active_tasks[user_id]["is_running"] = False
                    self.active_tasks[user_id]["status"] = "Completed"


user_runner_mgr = UserRunnerManager(max_concurrent=8)
