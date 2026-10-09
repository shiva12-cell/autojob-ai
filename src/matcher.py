"""
Skill Matcher & Resume Screening Engine.
Calculates percentage match between candidate profile skills and job descriptions.
Applies:
- Skill match threshold: >= 65%
- Experience range filter: 0 to 2 years (Freshers to 2 yrs)
- Package target: 5 to 10 LPA
- Location: Any / Remote / Pan-India
"""

import re
from typing import List, Tuple, Set, Optional
from src.models import CandidateProfile

# Comprehensive keyword dictionary for Data, Business, Product & Tech Analyst roles
COMMON_ANALYST_SKILLS = [
    "python", "sql", "excel", "advanced excel", "power bi", "tableau",
    "data analysis", "eda", "data visualization", "pandas", "numpy",
    "scikit-learn", "matplotlib", "seaborn", "looker", "looker studio",
    "statistics", "statistical analysis", "hypothesis testing",
    "machine learning", "kpi", "reporting", "dashboard", "etl",
    "data cleaning", "regression", "business intelligence", "bi",
    "churn analysis", "git", "ai automation", "problem solving",
    # Business Analyst, AI Product Owner & Product Management Keywords
    "business analyst", "business analysis", "brd", "frd", "prd", "user stories",
    "product management", "product owner", "ai product owner", "roadmap", "jira",
    "agile", "scrum", "market research", "stakeholder management", "btech cse",
    "computer science", "analytics", "operations analyst", "systems analyst",
    "generative ai", "ai", "llm", "data science", "requirements gathering", "sdlc",
    # Other domains to identify non-matching roles accurately
    "java", "spring boot", "c++", "c#", "dotnet", ".net", "golang",
    "kubernetes", "docker", "aws", "azure", "android", "ios", "flutter",
    "react", "angular", "node.js", "devops", "qa testing"
]


class SkillMatcher:
    """Matches candidate profile against job posting descriptions."""

    def __init__(self, profile: CandidateProfile, threshold_pct: float = 65.0):
        self.profile = profile
        self.threshold = threshold_pct
        self.candidate_skills: Set[str] = set()

        # 1. Load from profile.experience.skills if present
        if self.profile.experience and self.profile.experience.skills:
            for s in self.profile.experience.skills:
                self.candidate_skills.add(s.lower().strip())

        # 2. Load from screening_answers.skills
        raw_skills = self.profile.screening_answers.get("skills", "")
        if isinstance(raw_skills, list):
            self.candidate_skills.update(s.lower().strip() for s in raw_skills)
        elif isinstance(raw_skills, str) and raw_skills:
            self.candidate_skills.update(s.lower().strip() for s in raw_skills.split(","))

        # 3. Enrich candidate skills with common semantic equivalents
        skill_expansions = {
            "power bi": ["bi", "business intelligence", "dashboard", "data visualization"],
            "tableau": ["bi", "dashboard", "data visualization"],
            "data analysis": ["analytics", "eda", "data cleaning", "reporting", "kpi"],
            "statistics": ["statistical analysis", "hypothesis testing", "eda"],
            "python": ["pandas", "numpy", "eda", "machine learning", "ai"],
            "sql": ["querying", "etl", "database"],
            "excel": ["advanced excel", "reporting", "dashboard"],
            "business analysis": ["business analyst", "requirements gathering", "agile", "reporting", "user stories"],
            "react": ["javascript", "frontend", "web development"],
            "node.js": ["backend", "javascript", "express", "api"],
            "devops": ["ci/cd", "docker", "kubernetes", "cloud"],
        }
        for base, exp in list(skill_expansions.items()):
            if base in self.candidate_skills:
                self.candidate_skills.update(exp)

        # 4. If title, headline, or skills specify domain competencies, add target indicators
        title_lower = (self.profile.personal_info.current_title or "").lower()
        headline_lower = getattr(self.profile.personal_info, "headline", "") or ""
        comb_title = f"{title_lower} {headline_lower.lower()}"
        if any(w in comb_title for w in ["product", "owner", "manager", "pm"]):
            self.candidate_skills.update(["product management", "product owner", "agile", "jira"])
        if any(w in comb_title for w in ["business analyst", "ba"]):
            self.candidate_skills.update(["business analyst", "business analysis", "requirements gathering"])

        if any(s in self.candidate_skills for s in ["data analysis", "python", "analytics", "sql", "statistics", "ai"]):
            self.candidate_skills.update([
                "ai product owner", "product management", "product owner", "ai", "agile",
                "business analyst", "business analysis"
            ])

        # 5. Only if no candidate skills could be found anywhere, fallback to general defaults
        if not self.candidate_skills:
            default_candidate_skills = [
                "python", "sql", "excel", "power bi", "tableau", "looker studio",
                "data analysis", "eda", "data cleaning", "kpi reporting",
                "statistics", "machine learning", "ai automation", "git",
                "business analysis", "business analyst", "analytics", "agile",
                "problem solving", "product management", "jira"
            ]
            self.candidate_skills.update(default_candidate_skills)

    def extract_skills_from_text(self, text: str) -> Set[str]:
        """Identifies skills mentioned in job description or title."""
        found = set()
        lower_text = text.lower()
        all_search_skills = set(COMMON_ANALYST_SKILLS).union(self.candidate_skills)
        for skill in all_search_skills:
            if not skill or len(skill.strip()) < 2:
                continue
            pattern = rf"\b{re.escape(skill)}\b"
            if re.search(pattern, lower_text):
                found.add(skill)
        return found

    def check_experience_fit(self, job_title: str, job_description: str) -> Tuple[bool, str]:
        """
        Ensures the job fits the candidate's experience range.
        For entry-level/freshers (0-1 yrs), rejects jobs requiring 3+ years.
        For experienced candidates, allows jobs up to candidate_exp + 2 years.
        """
        cand_exp = 0
        if self.profile.experience:
            cand_exp = self.profile.experience.years_of_experience
        elif "experience" in self.profile.screening_answers:
            try:
                cand_exp = int(self.profile.screening_answers["experience"])
            except Exception:
                pass

        max_allowed = max(2, cand_exp + 2)
        combined = f"{job_title}\n{job_description}".lower()

        # Reject explicitly high senior experience demands (> max_allowed years)
        high_exp_pattern = r"\b([3-9]|\d{2})\+?\s*(?:to|-)\s*\d+\s*(?:years?|yrs?)\b|\b([3-9]|\d{2})\+\s*(?:years?|yrs?)\b"
        match = re.search(high_exp_pattern, combined)
        if match:
            try:
                req_years = int(match.group(1) or match.group(2) or 3)
                if req_years > max_allowed:
                    return False, f"Requires {match.group(0)} (exceeds candidate experience cap of {max_allowed} yrs)"
            except Exception:
                return False, f"Requires {match.group(0)}"

        # Senior/Executive titles for freshers
        if cand_exp <= 1 and any(term in job_title.lower() for term in ["lead data analyst", "principal", "director", "head of"]):
            return False, "Senior/Management role (exceeds candidate experience range)"

        return True, f"Experience fits candidate profile ({cand_exp} yrs)"

    def calculate_match(self, job_title: str, job_description: str) -> Tuple[float, List[str], List[str]]:
        """
        Calculates skill match percentage:
        Returns: (match_percentage, matched_skills, missing_skills)
        """
        combined = f"{job_title}\n{job_description}"
        required_skills = self.extract_skills_from_text(combined)

        # If job is an analyst or product role with very short description, treat standard analyst skills as base
        is_analyst_role = any(kw in combined.lower() for kw in [
            "data", "analyst", "analytics", "bi", "business intelligence", "reporting",
            "business analyst", "product owner", "product management", "ai product owner"
        ])
        if len(required_skills) < 3 and is_analyst_role:
            required_skills.update(["sql", "excel", "data analysis", "python"])

        matched = [s for s in required_skills if s in self.candidate_skills]
        missing = [s for s in required_skills if s not in self.candidate_skills]

        score = (len(matched) / max(len(required_skills), 1)) * 100.0
        score = min(round(score, 1), 100.0)

        return score, matched, missing

    def calculate_chance(self, score: float, matched_skills: List[str], job_title: str, job_description: str) -> str:
        """
        Calculates interview/callback probability: 'High', 'Mid', or 'Low'
        Based on probability of candidate's verified B.Tech CSE projects & core competencies:
        - High: Match >= 80% OR strong overlap with core project tools (Python, SQL, Power BI, EDA, Machine Learning)
        - Mid: Match 65% - 79% (solid foundational fit)
        - Low: Below 65%
        """
        core_project_tools = {
            "python", "sql", "power bi", "tableau", "machine learning", "data analysis", "eda",
            "business analysis", "business analyst", "product management", "ai product owner", "btech cse"
        }
        has_project_overlap = len(set(matched_skills).intersection(core_project_tools)) >= 2

        if score >= 80.0 or (score >= 70.0 and has_project_overlap):
            return "High"
        elif score >= 65.0:
            return "Mid"
        else:
            return "Low"

    def is_match(self, job_title: str, job_description: str) -> Tuple[bool, float, List[str], str]:
        """
        Returns (True/False, score, matched_skills, chance_level) if:
        1. Skill match score >= 65%
        2. Experience fits 0-2 years
        """
        # 1. Experience check (0-2 years)
        exp_fits, exp_reason = self.check_experience_fit(job_title, job_description)
        if not exp_fits:
            return False, 0.0, [], "Low"

        # 2. Skill match check (>= 65%)
        score, matched, _ = self.calculate_match(job_title, job_description)
        chance = self.calculate_chance(score, matched, job_title, job_description)
        return score >= self.threshold, score, matched, chance
