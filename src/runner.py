"""
Main CLI Application Runner for Job Auto Applier & Status Tracker.
"""

import sys
import os
from pathlib import Path

# Fix Windows console UTF-8 encoding so emojis never crash with charmap UnicodeEncodeError
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure root directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time
import argparse
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.config import get_config, load_candidate_profile
from src.google_services.auth import GoogleAuthManager
from src.google_services.sheets_manager import SheetsManager
from src.google_services.gmail_tracker import GmailTracker
from src.applier.factory import get_applier_for_url
from src.parser.email_classifier import EmailClassifier
from src.models import ApplicationStatus, JobApplication

console = Console()


def init_google_services(cfg: dict):
    """Initializes and returns GoogleAuthManager, SheetsManager, and GmailTracker."""
    google_cfg = cfg.get("google", {})
    creds_file = google_cfg.get("client_secrets_file", "config/credentials/credentials.json")
    token_file = google_cfg.get("token_file", "config/credentials/token.json")
    sheet_id = google_cfg.get("spreadsheet_id", "")
    worksheet_name = google_cfg.get("worksheet_name", "Job Applications")

    if not sheet_id:
        console.print("[bold yellow]Warning:[/bold yellow] No Google Sheet ID configured in config/config.yaml or .env!")

    auth_manager = GoogleAuthManager(credentials_file=creds_file, token_file=token_file)

    sheets_mgr = None
    if sheet_id:
        sheets_client = auth_manager.get_sheets_client()
        sheets_mgr = SheetsManager(sheets_client, spreadsheet_id=sheet_id, worksheet_name=worksheet_name)

    gmail_service = auth_manager.get_gmail_service()
    tracker = GmailTracker(gmail_service, sheets_mgr) if sheets_mgr else None

    return auth_manager, sheets_mgr, tracker


def cmd_apply(args, cfg: dict):
    """Applies to one or multiple jobs and logs to Google Sheets."""
    profile_path = cfg.get("applier", {}).get("profile_path", "config/profile.yaml")
    resume_path = cfg.get("applier", {}).get("resume_path", "resumes/resume.pdf")

    try:
        profile = load_candidate_profile(profile_path, resume_path)
    except Exception as e:
        console.print(f"[bold red]Failed to load candidate profile:[/bold red] {e}")
        return

    urls = []
    if args.url:
        urls.append(args.url)
    elif args.file:
        path = Path(args.file)
        if path.exists():
            urls = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
        else:
            console.print(f"[bold red]File not found:[/bold red] {args.file}")
            return

    if not urls:
        console.print("[bold red]No URLs provided to apply to![/bold red]")
        return

    # Check Sheets connection
    sheets_mgr = None
    if not args.no_sheet:
        try:
            _, sheets_mgr, _ = init_google_services(cfg)
        except Exception as e:
            console.print(f"[yellow]Could not connect to Google Sheets ({e}). Skipping sheet logging.[/yellow]")

    dry_run = not args.submit

    for url in urls:
        console.print(f"\n[cyan]Processing job URL:[/cyan] {url}")
        applier = get_applier_for_url(
            url,
            profile,
            headless=cfg.get("applier", {}).get("headless", False),
            slow_mo_ms=cfg.get("applier", {}).get("slow_mo_ms", 500),
        )

        if not applier:
            console.print(f"[bold red]Unsupported job board/ATS URL:[/bold red] {url}")
            continue

        app_record = applier.apply(url, dry_run=dry_run)
        if app_record:
            console.print(f"[bold green]Successfully processed application for {app_record.company} - {app_record.job_title}![/bold green]")
            if sheets_mgr:
                try:
                    sheets_mgr.add_application(app_record)
                    console.print("[bold green]Application logged in Google Sheet.[/bold green]")
                except Exception as e:
                    console.print(f"[red]Failed to log to Google Sheets:[/red] {e}")


def cmd_sync(args, cfg: dict):
    """Scans Gmail for job updates and synchronizes with Google Sheets."""
    console.print(Panel.fit("[bold blue]Starting Gmail to Google Sheets Sync...[/bold blue]"))

    try:
        _, sheets_mgr, tracker = init_google_services(cfg)
    except Exception as e:
        console.print(f"[bold red]Google Authentication Error:[/bold red] {e}")
        return

    if not sheets_mgr or not tracker:
        console.print("[bold red]Google Sheet ID must be set in config/config.yaml or .env to sync![/bold red]")
        return

    lookback = args.lookback or cfg.get("tracker", {}).get("lookback_days", 14)
    results = tracker.sync_to_sheets(lookback_days=lookback)
    console.print(f"[bold green]Sync finished:[/bold green] {results['updated']} updated, {results['added']} new entries.")


def cmd_list(args, cfg: dict):
    """Displays applications stored in the Google Sheet."""
    try:
        _, sheets_mgr, _ = init_google_services(cfg)
    except Exception as e:
        console.print(f"[bold red]Google Authentication Error:[/bold red] {e}")
        return

    if not sheets_mgr:
        console.print("[bold red]Google Sheet ID is not configured.[/bold red]")
        return

    apps = sheets_mgr.get_all_applications()
    if not apps:
        console.print("[yellow]No applications found in Google Sheet.[/yellow]")
        return

    table = Table(title="Tracked Job Applications", header_style="bold magenta")
    table.add_column("Row", style="dim", width=4)
    table.add_column("Company", style="bold")
    table.add_column("Job Title")
    table.add_column("Status")
    table.add_column("Applied Date")
    table.add_column("Last Updated")
    table.add_column("Platform")
    table.add_column("Notes", max_width=40)

    status_colors = {
        ApplicationStatus.APPLIED: "blue",
        ApplicationStatus.UNDER_REVIEW: "cyan",
        ApplicationStatus.ASSESSMENT: "yellow",
        ApplicationStatus.INTERVIEW: "bold green",
        ApplicationStatus.OFFER: "bold magenta",
        ApplicationStatus.REJECTED: "red",
        ApplicationStatus.WITHDRAWN: "dim",
        ApplicationStatus.UNKNOWN: "white",
    }

    for row_idx, app in apps:
        color = status_colors.get(app.status, "white")
        table.add_row(
            str(row_idx),
            app.company,
            app.job_title,
            f"[{color}]{app.status.value}[/{color}]",
            app.date_applied,
            app.last_updated,
            app.platform,
            app.notes,
        )

    console.print(table)


def cmd_test_parser(args):
    """Tests the email classifier against a sample subject and body."""
    subject = args.subject or "Thank you for applying to Stripe for Software Engineer"
    body = args.body or "Hi Alex, we have received your application for the Software Engineer role and our team is reviewing it."
    sender = args.sender or "Stripe Recruiting <recruiting@stripe.com>"

    from datetime import datetime
    event = EmailClassifier.parse_message(
        message_id="test-123",
        thread_id="thread-123",
        date_received=datetime.now(),
        sender=sender,
        subject=subject,
        body_text=body,
    )

    console.print(Panel.fit("[bold green]Email Parser Test Result[/bold green]"))
    if event:
        console.print(f"[bold]Detected Company:[/bold] {event.detected_company}")
        console.print(f"[bold]Detected Role:[/bold] {event.detected_job_title}")
        console.print(f"[bold]Detected Status:[/bold] {event.status.value}")
        console.print(f"[bold]Confidence:[/bold] {event.confidence * 100:.1f}%")
        console.print(f"[bold]Snippet:[/bold] {event.snippet}")
    else:
        console.print("[bold red]Message was not detected as a job application communication.[/bold red]")


def cmd_test_auth(args, cfg: dict):
    """Verifies all setup components: profile, resume, credentials, and live Google connection."""
    console.print(Panel.fit("[bold cyan]System Setup & Authentication Diagnostics[/bold cyan]"))

    # 1. Profile Check
    profile_path = cfg.get("applier", {}).get("profile_path", "config/profile.yaml")
    resume_path = cfg.get("applier", {}).get("resume_path", "resumes/resume.pdf")

    try:
        profile = load_candidate_profile(profile_path, resume_path)
        console.print(f"[green]✓ Profile Loaded:[/green] {profile.personal_info.full_name} ({profile.personal_info.email})")
        console.print(f"  Role: {profile.personal_info.current_title or 'Data Analyst'} | Location: {profile.personal_info.location}")
    except Exception as e:
        console.print(f"[red]✗ Profile Error:[/red] {e}")
        return

    # 2. Resume Check
    res_file = Path(resume_path)
    if res_file.exists():
        console.print(f"[green]✓ Resume File Found:[/green] {res_file.name} ({res_file.stat().st_size / 1024:.1f} KB)")
    else:
        console.print(f"[yellow]! Resume Not Found at:[/yellow] {res_file}")

    # 3. Google Credentials Check
    google_cfg = cfg.get("google", {})
    creds_file = Path(google_cfg.get("client_secrets_file", "config/credentials/credentials.json"))
    token_file = Path(google_cfg.get("token_file", "config/credentials/token.json"))
    sheet_id = google_cfg.get("spreadsheet_id", "")

    if creds_file.exists():
        console.print(f"[green]✓ Google Client Secrets Found:[/green] {creds_file.name}")
    else:
        console.print(f"[red]✗ Google Client Secrets Missing:[/red] Expected at {creds_file}")
        return

    if sheet_id:
        console.print(f"[green]✓ Target Spreadsheet ID:[/green] {sheet_id}")
    else:
        console.print(f"[yellow]! No Spreadsheet ID configured in config/config.yaml[/yellow]")

    # 4. Connect to Google APIs
    console.print("\n[bold]Testing Google Authentication & API Connectivity...[/bold]")
    try:
        auth_manager, sheets_mgr, tracker = init_google_services(cfg)

        # Test Gmail API
        gmail_profile = auth_manager.get_gmail_service().users().getProfile(userId="me").execute()
        console.print(f"[green]✓ Gmail Connected:[/green] Logged in as [bold]{gmail_profile.get('emailAddress')}[/bold]")
        console.print(f"  Total Messages: {gmail_profile.get('messagesTotal', 0):,}")

        # Test Sheets API
        if sheets_mgr:
            ws = sheets_mgr.worksheet
            rows = len(ws.get_all_values())
            console.print(f"[green]✓ Google Sheet Connected:[/green] Tab '{sheets_mgr.worksheet_name}' has {rows} rows (including header).")
            console.print(f"  URL: https://docs.google.com/spreadsheets/d/{sheets_mgr.spreadsheet_id}")

        console.print("\n[bold green]All systems operational and ready to use![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Authentication / API Connection Error:[/bold red] {e}")


def cmd_daemon(args, cfg: dict):
    """Runs periodic syncing in the background."""
    interval_minutes = args.interval or 30
    console.print(f"[bold blue]Starting daemon: syncing Gmail & Google Sheets every {interval_minutes} minutes...[/bold blue]")
    console.print("Press Ctrl+C to stop.")

    while True:
        try:
            cmd_sync(args, cfg)
        except Exception as e:
            console.print(f"[red]Error during daemon cycle:[/red] {e}")

        console.print(f"Sleeping for {interval_minutes} minutes...")
        time.sleep(interval_minutes * 60)


def main():
    parser = argparse.ArgumentParser(description="Job Auto Applier & Tracker with Gmail and Google Sheets Integration")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # test-auth command
    subparsers.add_parser("test-auth", help="Verify profile, credentials, and test connection to Gmail and Google Sheets")

    # apply command
    apply_parser = subparsers.add_parser("apply", help="Auto-apply to jobs")
    apply_parser.add_argument("--url", help="Job posting URL to apply to")
    apply_parser.add_argument("--file", help="Path to text file containing job URLs (one per line)")
    apply_parser.add_argument("--submit", action="store_true", help="Actually submit the form (defaults to dry-run for safety)")
    apply_parser.add_argument("--no-sheet", action="store_true", help="Do not log to Google Sheets")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Scan Gmail and update Google Sheet")
    sync_parser.add_argument("--lookback", type=int, default=14, help="Days to look back for emails")

    # list command
    subparsers.add_parser("list", help="List tracked job applications from Google Sheets")

    # test-parser command
    test_parser = subparsers.add_parser("test-parser", help="Test email classification logic")
    test_parser.add_argument("--subject", help="Sample email subject")
    test_parser.add_argument("--body", help="Sample email body")
    test_parser.add_argument("--sender", help="Sample email sender")

    # naukri-login command
    subparsers.add_parser("naukri-login", help="Open browser to log into Naukri.com once and save session")

    # indeed-login command
    subparsers.add_parser("indeed-login", help="Open browser to log into Indeed once and save session")

    # dashboard & web commands
    dashboard_parser = subparsers.add_parser("dashboard", help="Launch the HTML Web Dashboard with Auto Apply trigger")
    dashboard_parser.add_argument("--port", type=int, default=8000, help="Web server port (default 8000)")
    dashboard_parser.add_argument("--host", default="127.0.0.1", help="Web server host")
    dashboard_parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")

    web_parser = subparsers.add_parser("web", help="Alias for dashboard: launch the HTML Web Dashboard")
    web_parser.add_argument("--port", type=int, default=8000, help="Web server port (default 8000)")
    web_parser.add_argument("--host", default="127.0.0.1", help="Web server host")
    web_parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser")

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Run background monitor to auto-update sheet periodically")
    daemon_parser.add_argument("--interval", type=int, default=30, help="Interval in minutes between Gmail scans")
    daemon_parser.add_argument("--lookback", type=int, default=7, help="Days to look back on each scan")

    args = parser.parse_args()
    cfg = get_config()

    if args.command in ["dashboard", "web"]:
        import uvicorn
        import webbrowser
        port = args.port or 8000
        host = args.host or "127.0.0.1"
        url = f"http://{host}:{port}"
        console.print(Panel.fit(f"[bold green]Starting AutoJob AI Web Dashboard on {url}[/bold green]\n4-Hour Autonomous Loop armed. 65% Skill Threshold active."))
        if not args.no_browser:
            webbrowser.open(url)
        uvicorn.run("src.web.server:app", host=host, port=port, reload=False)
    elif args.command == "test-auth":
        cmd_test_auth(args, cfg)
    elif args.command == "naukri-login":
        from src.applier.naukri import NaukriApplier
        profile_path = cfg.get("applier", {}).get("profile_path", "config/profile.yaml")
        profile = load_candidate_profile(profile_path)
        applier = NaukriApplier(profile)
        applier.interactive_login()
    elif args.command == "indeed-login":
        from src.applier.indeed_manual_login import login_indeed
        login_indeed()
    elif args.command == "apply":
        cmd_apply(args, cfg)
    elif args.command == "sync":
        cmd_sync(args, cfg)
    elif args.command == "list":
        cmd_list(args, cfg)
    elif args.command == "test-parser":
        cmd_test_parser(args)
    elif args.command == "daemon":
        cmd_daemon(args, cfg)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
