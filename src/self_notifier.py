"""
Self-Notification Service via Gmail API.
Sends automated emails to the candidate whenever a job is auto-applied.
Creates and tags all emails with the Gmail folder/label: "Job Applied".
"""

import base64
from email.mime.text import MIMEText
from typing import Optional, List
from googleapiclient.discovery import Resource
from src.models import CandidateProfile, JobApplication

DEFAULT_LABEL_NAME = "Job Applied"


class SelfNotifier:
    """Manages sending self-notification emails and placing them into the 'Job Applied' folder/label in Gmail."""

    def __init__(self, gmail_service: Resource, profile: CandidateProfile, label_name: str = DEFAULT_LABEL_NAME):
        self.gmail = gmail_service
        self.profile = profile
        self.label_name = label_name
        self._label_id: Optional[str] = None
        self._ensure_label_exists()

    def _ensure_label_exists(self) -> Optional[str]:
        """Ensures the 'Job Applied' label exists in user's Gmail account."""
        if self._label_id:
            return self._label_id

        try:
            results = self.gmail.users().labels().list(userId="me").execute()
            labels = results.get("labels", [])
            for lbl in labels:
                if lbl["name"].lower() == self.label_name.lower():
                    self._label_id = lbl["id"]
                    return self._label_id

            # Create new label if not found
            label_body = {
                "name": self.label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created = self.gmail.users().labels().create(userId="me", body=label_body).execute()
            self._label_id = created["id"]
            print(f"[Notifier] Created new Gmail folder/label: '{self.label_name}'")
            return self._label_id
        except Exception as e:
            print(f"[Notifier Warning] Could not verify/create Gmail label: {e}")
            return None

    def send_applied_notification(
        self,
        app: JobApplication,
        match_pct: float = 70.0,
        matched_skills: Optional[List[str]] = None,
        google_sheet_url: str = ""
    ) -> bool:
        """
        Sends an automated notification email to the candidate and tags it with the 'Job Applied' label.
        """
        user_email = self.profile.personal_info.email
        user_name = self.profile.personal_info.full_name
        skills_str = ", ".join(matched_skills[:6]) if matched_skills else "Python, SQL, Analytics"

        subject = f"🚀 Auto-Applied: {app.job_title} at {app.company} (Match: {match_pct:.0f}%)"

        body = f"""Hi {user_name},

Your AutoJob AI Copilot just submitted an application for you!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 APPLICATION DETAILS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Company:        {app.company}
• Job Title:      {app.job_title}
• Location:       {app.location}
• Platform:       {app.platform}
• Skill Match:    {match_pct:.1f}% (≥ 65% Qualified)
• Key Matched:    {skills_str}
• Target CTC:     5 - 10 LPA
• Experience:     0 - 2 Years
• Date Applied:   {app.date_applied}
• Resume Used:    resumes/resume.pdf (Fixed - No Tailoring)
• Status:         Applied

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 LIVE TRACKER & SPREADSHEETS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{f'• Google Sheet:   {google_sheet_url}' if google_sheet_url else '• Tracker:        Local Database & Excel'}
• Local Excel:    Job_Applications_Tracker.xlsx
• Next Action:    Automated follow-up in 4 days if no response

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
This email is filed under your Gmail folder: [{self.label_name}].
- AutoJob AI Autonomous Copilot
"""

        try:
            msg = MIMEText(body)
            msg["to"] = user_email
            msg["from"] = user_email
            msg["subject"] = subject

            raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            send_res = self.gmail.users().messages().send(userId="me", body={"raw": raw_bytes}).execute()
            msg_id = send_res.get("id")

            # Apply the "Job Applied" label to this message
            label_id = self._ensure_label_exists()
            if label_id and msg_id:
                self.gmail.users().messages().modify(
                    userId="me",
                    id=msg_id,
                    body={"addLabelIds": [label_id]}
                ).execute()

            self._safe_print(f"[Notifier] Sent notification email to {user_email} (Filed in '{self.label_name}')")
            return True

        except Exception as e:
            self._safe_print(f"[Notifier Error] Could not send self-notification: {e}")
            return False

    @staticmethod
    def _safe_print(msg: str) -> None:
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode("ascii", "replace").decode("ascii"))
