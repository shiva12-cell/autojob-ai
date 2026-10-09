"""
Live Job Scraper for Naukri & Indeed India.
Scrapes live job postings across multiple pages matching candidate keywords
using genuine Google Chrome and session cookies.
Extracts salary/CTC, experience, location, and description.
"""

import re
import json
import sys
from typing import List, Dict, Any, Optional
from pathlib import Path
from playwright.sync_api import sync_playwright

from src.state_manager import seen_manager

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


NAUKRI_SESSION_FILE = Path("config/credentials/naukri_state.json")
INDEED_SESSION_FILE = Path("config/credentials/indeed_state.json")


def parse_salary_lpa(text: str) -> float:
    """Parses salary text into a numeric minimum LPA value for sorting."""
    if not text:
        return 5.0
    clean = text.lower().replace(",", "")
    # Check for range e.g. 5-8 lacs, 4.5 - 9.5 lpa
    m = re.findall(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", clean)
    if m:
        try:
            val = float(m[0][0])
            if val > 1000:
                val /= 100000.0
            return val
        except Exception:
            pass
    # Check for single number
    m_single = re.findall(r"(\d+(?:\.\d+)?)\s*(?:lacs|lac|lpa)", clean)
    if m_single:
        try:
            return float(m_single[0])
        except Exception:
            pass
    return 5.0


def scrape_naukri_live_jobs(
    keywords: str = "data analyst",
    min_jobs: int = 50,
    direct_only: bool = True,
    apply_mode: str = "all",  # "all", "direct_only", or "company_site_only"
    location: str = "any",
    min_lpa: float = 0.0,
    max_lpa: float = 99.0,
    experience_years: int = 0,
    job_age_days: int = 0,
    work_mode: str = "all",
    sort_by: str = "relevance",
    start_page: Optional[int] = None,
    max_pages: int = 6,
    session_file: Optional[Path] = None,
    session_cookies: Optional[list] = None,
) -> List[Dict[str, Any]]:
    """
    Scrapes multiple pages of live job postings from Naukri using live filters:
    - keywords: Target role / skill query
    - location: City or remote filter
    - min_lpa & max_lpa: Dynamic salary range in LPA
    - experience_years: Minimum experience requirement (e.g. 0 for freshers/entry)
    - job_age_days: Freshness (1 for 24h, 3, 7, 15, 30 days)
    - work_mode: "remote", "wfo", "hybrid", "all"
    - sort_by: "relevance" or "date" (freshness)
    - apply_mode: "all", "direct_only" (1-click direct), or "company_site_only" (external portal)
    - direct_only: Legacy boolean flag; if False and apply_mode=="direct_only", apply_mode takes precedence
    - start_page: Starting pagination page (defaults to persistent cursor)
    - max_pages: Maximum pages to scan per run
    """
    results: List[Dict[str, Any]] = []
    seen_urls = set()

    if start_page is None:
        start_page = seen_manager.get_cursor(keywords)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        loaded_cookies = []
        if session_cookies:
            loaded_cookies = session_cookies
        elif session_file and Path(session_file).exists():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    st = json.load(f)
                    loaded_cookies = st.get("cookies", [])
            except Exception:
                pass
        elif NAUKRI_SESSION_FILE.exists():
            try:
                with open(NAUKRI_SESSION_FILE, "r") as f:
                    st = json.load(f)
                    loaded_cookies = st.get("cookies", [])
            except Exception:
                pass

        if loaded_cookies:
            try:
                context.add_cookies(loaded_cookies)
            except Exception:
                pass

        page = context.new_page()
        query = keywords.replace(" ", "%20")
        keyword_slug = keywords.replace(" ", "-")

        clean_loc = (location or "any").strip().lower()
        loc_slug = ""
        extra_params = []

        # Location parameter
        if clean_loc in ["remote", "wfh", "work from home"]:
            loc_slug = "remote"
            extra_params.append("wfhType=0")
        elif clean_loc not in ["any", "all", "india", "pan-india", ""]:
            loc_slug = re.sub(r"[^\w\s-]", "", clean_loc).strip().replace(" ", "-")
            extra_params.append(f"l={clean_loc.replace(' ', '%20')}")

        # Experience parameter
        if experience_years is not None and experience_years >= 0:
            extra_params.append(f"experience={experience_years}")

        # Work Mode parameter
        if work_mode == "remote":
            extra_params.append("wfhType=0")
        elif work_mode == "wfo":
            extra_params.append("wfhType=1")
        elif work_mode == "hybrid":
            extra_params.append("wfhType=2")

        # Freshness / Job Age parameter
        if job_age_days and job_age_days > 0:
            extra_params.append(f"jobAge={job_age_days}")

        # Sort Order parameter
        if sort_by == "date":
            extra_params.append("sortBy=date")

        # Note: Do not append invalid ctcFilter to Naukri query URL as it breaks Naukri's search
        # and returns 0 jobs. Salary requirements are evaluated accurately in the candidate matching engine.

        param_str = ("&" + "&".join(extra_params)) if extra_params else ""

        # Smart Dynamic Pagination: scan forward until min_jobs is satisfied or catalogue exhausted
        pages_scanned = 0
        last_page_num = start_page
        effective_max_pages = max(max_pages, 12 if (apply_mode == "direct_only" or direct_only) else 8)
        consecutive_empty_pages = 0

        for page_offset in range(effective_max_pages):
            page_num = ((start_page - 1 + page_offset) % 15) + 1
            last_page_num = page_num
            pages_scanned += 1

            if len(results) >= min_jobs:
                break

            if loc_slug and loc_slug != "remote":
                if page_num == 1:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{loc_slug}?k={query}{param_str}"
                else:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-in-{loc_slug}-{page_num}?k={query}{param_str}"
            else:
                if page_num == 1:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs?k={query}{param_str}"
                else:
                    url = f"https://www.naukri.com/{keyword_slug}-jobs-{page_num}?k={query}{param_str}"

            try:
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)

                filter_mode = "direct_only" if (apply_mode == "direct_only" or (apply_mode == "all" and direct_only is True and "apply_mode" not in locals())) else apply_mode
                if apply_mode in ["direct_only", "company_site_only", "all"]:
                    filter_mode = apply_mode
                elif direct_only:
                    filter_mode = "direct_only"
                else:
                    filter_mode = "all"

                raw_jobs = page.evaluate("""(filterMode) => {
                    const items = [];
                    const tuples = document.querySelectorAll('.srp-jobtuple-wrapper, .cust-job-tuple');
                    for (const t of tuples) {
                        const titleEl = t.querySelector('.title, a.title');
                        const compEl = t.querySelector('.comp-name, a.comp-name');
                        const expEl = t.querySelector('.exp-wrap, .experience');
                        const salEl = t.querySelector('.sal-wrap, .salary');
                        const locEl = t.querySelector('.loc-wrap, .loc');
                        const descEl = t.querySelector('.job-desc, .row5');
                        const tags = Array.from(t.querySelectorAll('.tags-gt li, .tag-li')).map(e => e.innerText.trim()).join(', ');
                        if (titleEl && compEl) {
                            const tupleText = (t.innerText || '').toLowerCase();
                            const titleText = (titleEl.innerText || '').toLowerCase();
                            
                            // Check for Walk-in interview announcements (which lack online 1-click apply buttons)
                            const isWalkin = tupleText.includes('walk-in') || tupleText.includes('walk in') || tupleText.includes('walkin') ||
                                             titleText.includes('walk-in') || titleText.includes('walk in') || titleText.includes('walkin');
                            if (filterMode === 'direct_only' && isWalkin) {
                                continue;
                            }

                            const hasCompanySite = tupleText.includes('company site') ||
                                                   tupleText.includes('company website') ||
                                                   tupleText.includes('apply on company') ||
                                                   Boolean(t.querySelector('[class*="company-site"], [id*="company-site"]'));
                            const isDirect = !hasCompanySite && !isWalkin;
                            
                            if (filterMode === 'direct_only' && !isDirect) {
                                continue;
                            }

                            items.push({
                                title: titleEl.innerText.trim(),
                                company: compEl.innerText.trim(),
                                url: titleEl.href || '',
                                experience: expEl ? expEl.innerText.trim() : '0-2 Yrs',
                                salary: salEl ? salEl.innerText.trim() : 'Not disclosed',
                                location: locEl ? locEl.innerText.trim() : 'India',
                                description: (descEl ? descEl.innerText.trim() : '') + ' Skills: ' + tags,
                                is_direct_apply: isDirect
                            });
                        }
                    }
                    return items;
                }""", filter_mode)

                page_cached = 0
                page_fresh = 0

                for item in raw_jobs:
                    u = item.get("url")
                    if not u or u in seen_urls:
                        continue

                    # Instant bypass for jobs already processed / applied / on cooldown
                    if seen_manager.is_seen(u):
                        page_cached += 1
                        continue

                    page_fresh += 1
                    seen_urls.add(u)
                    parsed_sal = parse_salary_lpa(item.get("salary", ""))
                    item["min_lpa"] = parsed_sal

                    # Salary filter check: if disclosed, check range with buffer
                    raw_sal = (item.get("salary") or "").lower()
                    if "not disclosed" not in raw_sal and raw_sal.strip():
                        if min_lpa > 0 and parsed_sal < max(min_lpa * 0.7, 1.0):
                            continue
                        if max_lpa > 0 and max_lpa < 90 and parsed_sal > max_lpa * 1.4:
                            continue

                    results.append(item)
                    if len(results) >= min_jobs:
                        break

                if len(raw_jobs) == 0:
                    consecutive_empty_pages += 1
                    if consecutive_empty_pages >= 3:
                        safe_print(f"[Naukri Scraper] Notice: 3 consecutive empty pages encountered on Naukri. Ending scan.")
                        break
                else:
                    consecutive_empty_pages = 0

                if len(raw_jobs) > 0 and page_fresh == 0:
                    safe_print(f"[Naukri Scraper] ⏩ Page {page_num}: All {page_cached} postings cached/filtered. Fast-forwarding to Page {page_num + 1}...")
                else:
                    safe_print(f"[Naukri Scraper] 📄 Page {page_num}: Collected {page_fresh} fresh jobs ({page_cached} cached). Total: {len(results)}/{min_jobs}")

            except Exception as e:
                safe_print(f"[Naukri Scraper] Page {page_num} notice: {e}")
                break

        # Smart Pagination: Advance cursor so the next cycle continues from the next fresh page
        next_page = (last_page_num % 15) + 1
        seen_manager.set_cursor(keywords, next_page)
        safe_print(f"[Naukri Scraper] 🎯 Completed: {len(results)} fresh jobs found across {pages_scanned} pages. Smart pagination cursor set to Page {next_page} for next cycle.")
        browser.close()

    return results


def scrape_indeed_live_jobs(keywords: str = "data analyst", min_jobs: int = 25) -> List[Dict[str, str]]:
    """
    Scrapes live jobs from Indeed India (in.indeed.com) matching keywords.
    """
    results: List[Dict[str, str]] = []
    seen_urls = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        if INDEED_SESSION_FILE.exists():
            try:
                with open(INDEED_SESSION_FILE, "r") as f:
                    st = json.load(f)
                    context.add_cookies(st.get("cookies", []))
            except Exception:
                pass

        page = context.new_page()
        query = keywords.replace(" ", "+")
        url = f"https://in.indeed.com/jobs?q={query}&l=India&explvl=entry_level"

        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)

            raw_jobs = page.evaluate("""() => {
                const items = [];
                const cards = document.querySelectorAll('.job_seen_beacon, .resultContent');
                for (const c of cards) {
                    const titleEl = c.querySelector('h2.jobTitle a, a[data-jk], h2 a');
                    const compEl = c.querySelector('[data-testid="company-name"], .companyName, span.companyName');
                    const locEl = c.querySelector('[data-testid="text-location"], .companyLocation');
                    const salEl = c.querySelector('[data-testid="attribute_snippet_testid"], .salary-snippet-container, .metadata');
                    const snipEl = c.querySelector('.job-snippet, table.jobCardShelfContainer');
                    if (titleEl && compEl) {
                        items.push({
                            title: titleEl.innerText.trim(),
                            company: compEl.innerText.trim(),
                            url: titleEl.href || '',
                            experience: '0-2 Yrs',
                            salary: salEl ? salEl.innerText.trim() : 'Not disclosed',
                            location: locEl ? locEl.innerText.trim() : 'India',
                            description: snipEl ? snipEl.innerText.trim() : ''
                        });
                    }
                }
                return items;
            }""")

            for item in raw_jobs:
                u = item.get("url")
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    item["min_lpa"] = parse_salary_lpa(item.get("salary", ""))
                    results.append(item)
                    if len(results) >= min_jobs:
                        break

        except Exception as e:
            safe_print(f"[Indeed Scraper] Query notice: {e}")
        finally:
            browser.close()

    return results
