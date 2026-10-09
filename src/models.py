"""
Data models and schemas for Job Auto Applier and Tracker.
"""

from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class ApplicationStatus(str, Enum):
    APPLIED = "Applied"
    UNDER_REVIEW = "Under Review"
    ASSESSMENT = "Assessment Sent"
    INTERVIEW = "Interview Scheduled"
    OFFER = "Offer Received"
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"
    MANUAL_APPLY_NEEDED = "Manual Apply Needed"
    SKIPPED = "Skipped"
    UNKNOWN = "Status Update"


class JobCategory(str, Enum):
    DATA_ANALYST = "Data Analyst"
    BUSINESS_ANALYST = "Business Analyst"
    AI_PRODUCT = "AI & Product Management"
    DATA_ENGINEERING = "Data Engineering & Science"
    OTHER = "Other Tech & Roles"


CATEGORY_HEADERS = [
    "Application ID",
    "Date Applied",
    "Company",
    "Job Title",
    "Category",
    "Location",
    "Job URL",
    "Platform",
    "Status",
    "Apply Mode",
    "Match %",
    "Chance",
    "Last Updated",
    "Follow-Up Date",
    "Gmail Thread ID",
    "Notes",
]


def detect_job_category(job_title: str, description: str = "") -> str:
    """Categorizes a job role based on title and description keywords."""
    t = (job_title or "").lower().strip()
    d = (description or "").lower().strip()

    # 1. AI & Product Management
    if any(k in t for k in [
        "product owner", "product manager", "product management", "ai product",
        "product lead", "associate product", "apm", "po /", "pm /"
    ]):
        return JobCategory.AI_PRODUCT.value

    # 2. Data Engineering & Science
    if any(k in t for k in [
        "data engineer", "data scientist", "machine learning", "ml engineer",
        "big data", "deep learning", "ai engineer", "data modeling", "etl"
    ]):
        return JobCategory.DATA_ENGINEERING.value

    # 3. Business Analyst
    if any(k in t for k in [
        "business analyst", "business operations", "governance", "biz analyst",
        "commercial analyst", "operations analyst", "strategy analyst", "bi analyst"
    ]):
        return JobCategory.BUSINESS_ANALYST.value

    # 4. Data Analyst
    if any(k in t for k in [
        "data analyst", "analytics", "bi &", "bi engineer", "bi developer",
        "power bi", "tableau", "data executive", "data verification",
        "data / research", "data specialist", "mis executive", "mis analyst",
        "reporting analyst"
    ]):
        return JobCategory.DATA_ANALYST.value
    elif "analyst" in t:
        return JobCategory.DATA_ANALYST.value

    # 5. Other Tech & Roles
    return JobCategory.OTHER.value


class JobApplication(BaseModel):
    """Represents a job application row in Google Sheets."""
    app_id: str = Field(description="Unique application ID")
    date_applied: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    company: str
    job_title: str
    location: str = "Remote / Unspecified"
    job_url: str = ""
    platform: str = "Manual / Unknown"
    status: ApplicationStatus = ApplicationStatus.APPLIED
    last_updated: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    gmail_thread_id: str = ""
    notes: str = ""

    def to_sheet_row(self) -> List[str]:
        """Convert to a row list matching Google Sheets column headers."""
        return [
            self.app_id,
            self.date_applied,
            self.company,
            self.job_title,
            self.location,
            self.job_url,
            self.platform,
            self.status.value,
            self.last_updated,
            self.gmail_thread_id,
            self.notes,
        ]

    @classmethod
    def from_sheet_row(cls, row: List[str]) -> "JobApplication":
        """Build JobApplication from a Google Sheets row."""
        # Pad with empty strings if row is short
        padded = row + [""] * (11 - len(row))
        status_val = padded[7].strip()
        # Fallback to APPLIED if status unknown
        try:
            status_enum = ApplicationStatus(status_val)
        except ValueError:
            status_enum = ApplicationStatus.APPLIED

        return cls(
            app_id=padded[0].strip(),
            date_applied=padded[1].strip(),
            company=padded[2].strip(),
            job_title=padded[3].strip(),
            location=padded[4].strip(),
            job_url=padded[5].strip(),
            platform=padded[6].strip(),
            status=status_enum,
            last_updated=padded[8].strip(),
            gmail_thread_id=padded[9].strip(),
            notes=padded[10].strip(),
        )


def is_manual_application(app: JobApplication) -> bool:
    """Returns True if the application was routed to manual apply."""
    if app.status == ApplicationStatus.MANUAL_APPLY_NEEDED:
        return True
    if app.app_id and app.app_id.startswith("MAN-"):
        return True
    if "company site" in (app.platform or "").lower():
        return True
    return False


class EmailJobEvent(BaseModel):
    """Parsed job application update event from Gmail."""
    message_id: str
    thread_id: str
    date_received: datetime
    sender: str
    subject: str
    detected_company: str
    detected_job_title: Optional[str] = None
    status: ApplicationStatus
    snippet: str
    confidence: float = 0.5


class CandidatePersonalInfo(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str
    location: str
    current_company: Optional[str] = ""
    current_title: Optional[str] = ""
    headline: Optional[str] = ""
    naukri_profile_url: Optional[str] = ""
    current_ctc: Optional[float] = 0.0
    expected_ctc: Optional[float] = 0.0
    notice_period_days: Optional[int] = 30
    linkedin_url: Optional[str] = ""
    github_url: Optional[str] = ""
    portfolio_url: Optional[str] = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def model_dump(self, *args, **kwargs):
        d = super().model_dump(*args, **kwargs)
        d["full_name"] = self.full_name
        return d


class CandidateWorkAuthorization(BaseModel):
    authorized_to_work_in_country: bool = True
    requires_sponsorship_now: bool = False
    requires_sponsorship_future: bool = False
    citizenship_status: str = "Authorized"


class CandidateEducation(BaseModel):
    degree: str = ""
    university: str = ""
    graduation_year: Optional[int] = None
    gpa: Optional[str] = ""


class CandidateExperience(BaseModel):
    years_of_experience: int = 0
    target_experience_range: str = "0 - 1 years (Fresher)"
    skills: List[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    personal_info: CandidatePersonalInfo
    work_authorization: CandidateWorkAuthorization = CandidateWorkAuthorization()
    education: Optional[CandidateEducation] = None
    experience: Optional[CandidateExperience] = None
    screening_answers: Dict[str, str] = Field(default_factory=dict)
    cover_letter: str = ""
    resume_path: Optional[str] = None


class ScreeningAnswerItem(BaseModel):
    key: str
    label: str
    value: str
    category: str = "General"


class ScreeningReviewQueueItem(BaseModel):
    id: str
    question_text: str
    company: str = ""
    job_title: str = ""
    timestamp: str = ""
    suggested_answer: str = ""
    resolved_answer: Optional[str] = None
    status: str = "pending"  # "pending" | "resolved"
