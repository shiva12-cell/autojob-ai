"""
Naukri.com Auto Applier & Session Manager.
Supports interactive 1-time login, cookie persistence, 5-attempt strategic submission,
and automated job applying.
"""

import os
import re
import sys
import uuid
from typing import Optional, List
from pathlib import Path
from playwright.sync_api import sync_playwright

from src.applier.base import BaseApplier
from src.applier.question_solver import QuestionSolver
from src.applier.submission_resolver import NaukriSubmissionResolver
from src.models import CandidateProfile, JobApplication, ApplicationStatus

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def safe_print(msg: str):
    """Safely prints message avoiding Windows charmap UnicodeEncodeError."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


NAUKRI_SESSION_FILE = "config/credentials/naukri_state.json"


class NaukriApplier(BaseApplier):
    """Automates job applications on Naukri.com using persistent session state and 5-attempt resolution."""

    def __init__(self, profile: CandidateProfile, session_file: str = NAUKRI_SESSION_FILE, **kwargs):
        super().__init__(profile, **kwargs)
        self.session_file = Path(session_file)
        self.solver = QuestionSolver(profile)
        self.resolver = NaukriSubmissionResolver(profile)

    def can_handle(self, url: str) -> bool:
        return "naukri.com" in url.lower()

    def interactive_login(self) -> bool:
        """
        Opens a visible browser for the user to log into Naukri once.
        Saves the authenticated cookies and storage state to naukri_state.json.
        """
        safe_print("\n" + "=" * 65)
        safe_print("NAUKRI.COM 1-TIME SESSION LOGIN")
        safe_print("A browser window will open to Naukri.com.")
        safe_print("Please log in with your email/phone and password or OTP.")
        safe_print("Once you are logged into your dashboard, the session will be saved.")
        safe_print("=" * 65 + "\n")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                ],
                ignore_default_args=["--enable-automation"],
                slow_mo=self.slow_mo_ms,
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            page.goto("https://www.naukri.com/nlogin/login", timeout=self.timeout_ms)
            safe_print("[Naukri] Browser opened.")
            safe_print("[Naukri] TIP: You can log in using:")
            safe_print("         1) Your Email/Phone and Password, OR")
            safe_print("         2) 'Use OTP to Login' (sends SMS OTP to your phone), OR")
            safe_print("         3) Google Sign-In.")
            safe_print("\n[Naukri] Once you have logged in and see your Naukri homepage, press ENTER here in the terminal to save your session...")

            try:
                import sys
                import time

                safe_print("[Naukri] Waiting for authentication... (Log into your Naukri account in the opened window)")
                logged_in = False
                for _ in range(150):  # Wait up to 5 minutes
                    time.sleep(2)
                    try:
                        if page.is_closed():
                            safe_print("[Naukri] Browser window was closed by user.")
                            break

                        curr_url = page.url.lower()
                        # Detect successful redirect away from login page
                        if "nlogin" not in curr_url and ("naukri.com" in curr_url or "my.naukri.com" in curr_url):
                            if any(marker in curr_url for marker in ["homepage", "user/home", "profile", "mnjuser", "mynaukri"]) or curr_url.rstrip("/").endswith("naukri.com"):
                                safe_print(f"[Naukri] Login detected on URL: {page.url}!")
                                logged_in = True
                                break

                        # Also check if profile or user avatar exists in DOM
                        if page.query_selector(".user-info, .nI-gNb-drawer__header, .profile-summary, a[href*='profile'], .view-profile-wrapper, .user-name"):
                            safe_print("[Naukri] Profile session elements detected!")
                            logged_in = True
                            break

                        # Also check if authentication cookies exist in browser context
                        cookies = context.cookies()
                        cookie_names = {c.get('name', '').lower() for c in cookies}
                        if any(auth_c in cookie_names for auth_c in ['nauk_sess', 'isloggedin', 'user_id', 'sur', 'nprofile']):
                            safe_print("[Naukri] Session cookies detected in browser context!")
                            logged_in = True
                            break

                    except Exception:
                        # Page might be navigating or temporarily busy, continue loop unless closed
                        try:
                            if page.is_closed():
                                break
                        except Exception:
                            break
                        continue

                try:
                    if not page.is_closed():
                        page.wait_for_timeout(3000)
                except Exception:
                    pass

                safe_print("[Naukri] Saving session state...")
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(self.session_file))
                safe_print(f"[Naukri] ✓ Session saved successfully to {self.session_file}!")
                return True
            except Exception as e:
                safe_print(f"[Naukri] Saving state before exit: {e}")
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    context.storage_state(path=str(self.session_file))
                except Exception:
                    pass
                return True
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    def apply_to_job(self, url: str, submit: bool = True, apply_mode: str = "all", **kwargs) -> Optional[JobApplication]:
        """Convenience alias for apply()."""
        return self.apply(url=url, dry_run=not submit, apply_mode=apply_mode, **kwargs)

    def apply(self, url: str, dry_run: bool = True, apply_mode: str = "all", **kwargs) -> Optional[JobApplication]:
        safe_print(f"\n[Naukri] Launching applier for: {url}")
        safe_print(f"[Naukri] Mode: {'DRY RUN (Will not submit)' if dry_run else 'LIVE SUBMISSION'} | Pathway: {apply_mode}")

        if not self.session_file.exists():
            safe_print(f"[Naukri Warning] No saved session found at {self.session_file}.")
            safe_print("Please run: python src/runner.py naukri-login first to log in!")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-infobars",
                ],
                ignore_default_args=["--enable-automation"],
                slow_mo=self.slow_mo_ms,
            )

            context_kwargs = {
                "viewport": {"width": 1280, "height": 900},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            }
            if self.session_file.exists():
                context_kwargs["storage_state"] = str(self.session_file)

            context = browser.new_context(**context_kwargs)
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            try:
                page.goto(url, timeout=self.timeout_ms)
                page.wait_for_load_state("domcontentloaded")
                self.human_delay(2)

                # Extract Job Title & Company
                job_title = "Data Analyst / Role"
                company = "Unknown Company"

                title_elem = page.query_selector("h1.styles_jd-header-title__rZwM1, h1.jd-header-title, h1")
                if title_elem:
                    job_title = title_elem.inner_text().strip()

                comp_elem = page.query_selector(".styles_jd-header-comp-name__MvqAI, a.pad-rt-8, .company-name")
                if comp_elem:
                    raw_comp = comp_elem.inner_text().strip()
                    company = raw_comp.split("\n")[0].strip()

                safe_comp = re.sub(r"[^\w\-_\.]", "_", company)[:30]
                safe_print(f"[Naukri] Target: {company} - {job_title}")

                # Give dynamic React elements time to hydrate
                try:
                    page.wait_for_selector("[class*='apply'], [id*='apply']", timeout=6000)
                except Exception:
                    pass
                self.human_delay(1)

                # Check if job was expired / closed
                if page.query_selector(":has-text('expired')") or "expjd=true" in page.url.lower():
                    safe_print(f"[Naukri] Post was closed or expired by recruiter for {company}.")
                    return None

                # Check for "Apply on company site" button FIRST
                company_site_btn = page.query_selector(
                    "button#company-site-button, button.company-site-button, a[id*='company-site'], [class*='company-site-button'], button:has-text('Apply on company site'), a:has-text('Apply on company site')"
                )
                has_company_btn = bool(company_site_btn and company_site_btn.is_visible())

                if has_company_btn:
                    if apply_mode == "direct_only":
                        safe_print(f"[Naukri] Bypassing {company} (Company Site job, but Apply Pathway is set to Direct Apply Only).")
                        return JobApplication(
                            app_id=f"NAU-SKIP-{uuid.uuid4().hex[:6]}",
                            company=company,
                            job_title=job_title,
                            location="India",
                            job_url=url,
                            platform="Naukri.com",
                            status=ApplicationStatus.SKIPPED,
                            notes="Skipped: Company site posting while in Direct Apply Only mode",
                        )

                    safe_print(f"[Naukri] Detected: 'Apply on company site' for {company}. Resolving external portal redirect...")
                    from src.applier.naukri_redirect_resolver import resolve_company_site_redirect
                    external_url = resolve_company_site_redirect(page, context) or url
                    safe_print(f"[Naukri] Canonical External Career Portal: {external_url}")

                    # Delegate to specialized ATS / Universal Company Applier
                    if external_url and "naukri.com" not in external_url:
                        from src.applier.factory import get_applier_for_url
                        ats_applier = get_applier_for_url(external_url, self.profile, headless=self.headless)
                        if ats_applier and not isinstance(ats_applier, NaukriApplier):
                            safe_print(f"[Naukri] Delegating to {ats_applier.__class__.__name__} for automated submission...")
                            external_result = ats_applier.apply(external_url, dry_run=dry_run)
                            if external_result:
                                if not external_result.company or external_result.company == "Company":
                                    external_result.company = company
                                if not external_result.job_title or external_result.job_title == "Candidate Application":
                                    external_result.job_title = job_title
                                return external_result

                    # Fallback if external redirect could not be resolved or form required login wall
                    return JobApplication(
                        app_id=f"ACW-{uuid.uuid4().hex[:8]}",
                        company=company,
                        job_title=job_title,
                        location="India",
                        job_url=external_url,
                        platform="Naukri (Company Site)",
                        status=ApplicationStatus.MANUAL_APPLY_NEEDED,
                        notes=f"Apply on company site (External: {external_url})",
                    )

                # If in Company Site Only mode, bypass direct apply listings
                if apply_mode == "company_site_only":
                    safe_print(f"[Naukri] Bypassing {company} (Direct Apply job, but Apply Pathway is set to Company Site Only).")
                    return JobApplication(
                        app_id=f"NAU-SKIP-{uuid.uuid4().hex[:6]}",
                        company=company,
                        job_title=job_title,
                        location="India",
                        job_url=url,
                        platform="Naukri.com",
                        status=ApplicationStatus.SKIPPED,
                        notes="Skipped: Direct Apply posting while in Company Site Only mode",
                    )

                # Check for Direct "Apply" button or pre-existing "Already applied" status
                already_applied = page.query_selector(
                    "#already-applied, .already-applied, :has-text('Already applied')"
                )
                if already_applied and already_applied.is_visible():
                    safe_print(f"[Naukri] Status: Already applied previously to {company}.")
                    return JobApplication(
                        app_id=f"NAU-{uuid.uuid4().hex[:8]}",
                        company=company,
                        job_title=job_title,
                        location="India",
                        job_url=url,
                        platform="Naukri.com",
                        status=ApplicationStatus.APPLIED,
                        notes="Already applied on Naukri",
                    )

                # Execute 5-Attempt Submission Resolution Engine
                submission_res = self.resolver.resolve_submission(
                    page=page,
                    company=company,
                    job_title=job_title,
                    job_url=url,
                    dry_run=dry_run,
                )

                if submission_res.success:
                    safe_print(f"[Naukri] ✓ Verified submission on portal for {company} (Attempt {submission_res.attempt_used}: {submission_res.verification_source})")
                    return JobApplication(
                        app_id=f"NAU-{uuid.uuid4().hex[:8]}",
                        company=company,
                        job_title=job_title,
                        location="India",
                        job_url=url,
                        platform="Naukri.com",
                        status=submission_res.status,
                        notes=f"Auto-applied via Naukri Resolver [Attempt {submission_res.attempt_used}: {submission_res.verification_source}]",
                    )
                elif submission_res.verification_source == "REDIRECTED_TO_COMPANY_SITE":
                    safe_print(f"[Naukri] Identified redirect to external company portal for {company}. Extracting destination URL...")
                    external_dest = None
                    try:
                        pages = context.pages
                        for p_extra in pages:
                            if p_extra != page and "naukri.com" not in p_extra.url and p_extra.url.startswith("http"):
                                external_dest = p_extra.url
                                break

                        if not external_dest:
                            ext_links = page.evaluate("""() => {
                                return Array.from(document.querySelectorAll('a[href]'))
                                    .map(a => a.href)
                                    .filter(h => h.startsWith('http') && !h.includes('naukri.com'));
                            }""")
                            if ext_links:
                                external_dest = ext_links[0]
                    except Exception:
                        pass

                    if external_dest:
                        safe_print(f"[Naukri] Captured external company portal destination: {external_dest}")
                        from src.applier.factory import get_applier_for_url
                        ats_applier = get_applier_for_url(external_dest, self.profile, headless=self.headless)
                        if ats_applier and not isinstance(ats_applier, NaukriApplier):
                            safe_print(f"[Naukri] Delegating redirect to {ats_applier.__class__.__name__}...")
                            ext_res = ats_applier.apply(external_dest, dry_run=dry_run)
                            if ext_res:
                                if not ext_res.company or ext_res.company == "Company":
                                    ext_res.company = company
                                if not ext_res.job_title or ext_res.job_title == "Candidate Application":
                                    ext_res.job_title = job_title
                                return ext_res

                    target_url = external_dest or url
                    return JobApplication(
                        app_id=f"ACW-{uuid.uuid4().hex[:8]}",
                        company=company,
                        job_title=job_title,
                        location="India",
                        job_url=target_url,
                        platform="Naukri (Company Site)",
                        status=ApplicationStatus.MANUAL_APPLY_NEEDED,
                        notes=f"Redirected to external company website: {target_url}",
                    )
                else:
                    safe_print(f"[Naukri] ⚠️ Warning: Application could not be confirmed for {company}. (Diagnostics collected in config/logs/submission_failures.json)")
                    return None

            except Exception as e:
                safe_print(f"[Naukri Error] Application failed with exception: {e}")
                return None
            finally:
                browser.close()
