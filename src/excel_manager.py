"""
Excel Tracker Manager.
Synchronizes job applications into formatted tabs in Job_Applications_Tracker.xlsx:
- 'Naukri Jobs': Dedicated sheet for Naukri.com applications
- 'Indeed Jobs': Dedicated sheet for Indeed applications
- 'All Applications': Master summary sheet
"""

from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
import time
import sys
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from src.models import (
    JobApplication,
    ApplicationStatus,
    JobCategory,
    CATEGORY_HEADERS,
    detect_job_category,
    is_manual_application,
)

def safe_print(msg: str):
    """Safely prints message avoiding Windows charmap UnicodeEncodeError."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass

DEFAULT_EXCEL_FILE = "Job_Applications_Tracker.xlsx"

EXCEL_HEADERS = [
    "Application ID",
    "Date Applied",
    "Company",
    "Job Title",
    "Location",
    "Job URL",
    "Platform",
    "Status",
    "Match %",
    "Chance",
    "Last Updated",
    "Follow-Up Date",
    "Gmail Thread ID",
    "Notes",
]

TAB_ALL = "All Applications"
TAB_NAUKRI = "Naukri Jobs"
TAB_INDEED = "Indeed Jobs"
TAB_MANUAL = "Manual"
TAB_OTHER = "Other Platforms"

CATEGORY_SHEETS = [
    JobCategory.DATA_ANALYST.value,
    JobCategory.BUSINESS_ANALYST.value,
    JobCategory.AI_PRODUCT.value,
    JobCategory.DATA_ENGINEERING.value,
    JobCategory.OTHER.value,
]

CATEGORY_HEADER_COLORS = {
    JobCategory.DATA_ANALYST.value: "1F4E79",       # Navy Blue
    JobCategory.BUSINESS_ANALYST.value: "0A58CA",   # Deep Blue
    JobCategory.AI_PRODUCT.value: "4B0082",         # Indigo / Violet
    JobCategory.DATA_ENGINEERING.value: "005F73",   # Teal / Cyan
    JobCategory.OTHER.value: "4A5568",              # Slate
}

# Soft Green & Orange row fills + distinct Status badges
FILL_AUTO_APPLIED = PatternFill(start_color="E2F0D9", end_color="E2F0D9", fill_type="solid")
STATUS_FILL_AUTO = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
STATUS_FONT_AUTO = Font(name="Calibri", size=10, bold=True, color="006100")

FILL_MANUAL = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
STATUS_FILL_MANUAL = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
STATUS_FONT_MANUAL = Font(name="Calibri", size=10, bold=True, color="9C6500")

# Specific Visual Highlights for Status column updates:
STATUS_STYLE_MAP = {
    # Interview Scheduled: Light lavender/blue, Dark Navy Bold
    "interview": (
        PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid"),
        Font(name="Calibri", size=10, bold=True, color="1F4E79"),
    ),
    # Assessment Sent: Soft Amber/Yellow, Dark Amber Bold
    "assessment": (
        PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"),
        Font(name="Calibri", size=10, bold=True, color="B25900"),
    ),
    # Under Review: Soft Light Cyan, Deep Slate Bold
    "review": (
        PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid"),
        Font(name="Calibri", size=10, bold=True, color="1B365D"),
    ),
    # Applied / Auto-Applied: Crisp Soft Green, Dark Green Bold
    "applied": (
        PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid"),
        Font(name="Calibri", size=10, bold=True, color="006100"),
    ),
    # Rejected: Soft Rose/Pink, Dark Crimson Red Bold
    "rejected": (
        PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid"),
        Font(name="Calibri", size=10, bold=True, color="C00000"),
    ),
    # Manual Apply Needed: Pale Orange/Gold, Dark Ochre Bold
    "manual": (
        PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid"),
        Font(name="Calibri", size=10, bold=True, color="9C6500"),
    ),
}

def get_status_style(status_text: str):
    """Returns (PatternFill, Font) styling tuple for a given status string."""
    st = (status_text or "").lower()
    for key, (s_fill, s_font) in STATUS_STYLE_MAP.items():
        if key in st:
            return s_fill, s_font
    return STATUS_FILL_AUTO, STATUS_FONT_AUTO


class ExcelManager:
    """Manages reading, creating, and updating the local Excel tracker with separated tabs."""

    def __init__(self, file_path: str = DEFAULT_EXCEL_FILE):
        self.file_path = Path(file_path)
        self._ensure_workbook()

    def _format_header(self, ws, title_color: str = "1F4E79") -> None:
        """Applies header style and width to a worksheet."""
        header_fill = PatternFill(start_color=title_color, end_color=title_color, fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for col_idx in range(1, len(EXCEL_HEADERS) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = align

        for col_idx, header in enumerate(EXCEL_HEADERS, 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = max(len(header) + 4, 15)

    def _format_category_header(self, ws, title_color: str = "1F4E79") -> None:
        """Applies header style and width to a category worksheet."""
        header_fill = PatternFill(start_color=title_color, end_color=title_color, fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for col_idx in range(1, len(CATEGORY_HEADERS) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = align

        for col_idx, header in enumerate(CATEGORY_HEADERS, 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = max(len(header) + 4, 15)

    def _ensure_workbook(self, force_recreate: bool = False) -> None:
        """Creates or upgrades the workbook with Platform and Category-wise tabs."""
        if not self.file_path.exists() or force_recreate:
            wb = openpyxl.Workbook()
            # Master sheet
            ws_all = wb.active
            ws_all.title = TAB_ALL
            ws_all.append(EXCEL_HEADERS)
            self._format_header(ws_all, "1F4E79")  # Navy

            ws_naukri = wb.create_sheet(title=TAB_NAUKRI)
            ws_naukri.append(EXCEL_HEADERS)
            self._format_header(ws_naukri, "0A58CA")  # Deep Blue

            ws_indeed = wb.create_sheet(title=TAB_INDEED)
            ws_indeed.append(EXCEL_HEADERS)
            self._format_header(ws_indeed, "2164F3")  # Indeed Royal Blue

            ws_manual = wb.create_sheet(title=TAB_MANUAL)
            ws_manual.append(EXCEL_HEADERS)
            self._format_header(ws_manual, "C55A11")  # Burnt Orange / Bronze

            # Category Sheets
            for cat_name in CATEGORY_SHEETS:
                ws_c = wb.create_sheet(title=cat_name)
                ws_c.append(CATEGORY_HEADERS)
                c_color = CATEGORY_HEADER_COLORS.get(cat_name, "1F4E79")
                self._format_category_header(ws_c, c_color)

            wb.save(self.file_path)
        else:
            # Upgrade existing workbook to have Naukri, Indeed, Manual, and Category tabs if missing
            try:
                wb = openpyxl.load_workbook(self.file_path)
            except Exception:
                self._recover_workbook()
                try:
                    wb = openpyxl.load_workbook(self.file_path)
                except Exception:
                    self._ensure_workbook(force_recreate=True)
                    wb = openpyxl.load_workbook(self.file_path)

            sheet_names = wb.sheetnames

            # Handle legacy 'Applications' tab
            if "Applications" in sheet_names and TAB_ALL not in sheet_names:
                ws_leg = wb["Applications"]
                ws_leg.title = TAB_ALL
                sheet_names = wb.sheetnames

            if TAB_NAUKRI not in sheet_names:
                ws_n = wb.create_sheet(title=TAB_NAUKRI)
                ws_n.append(EXCEL_HEADERS)
                self._format_header(ws_n, "0A58CA")

            if TAB_INDEED not in sheet_names:
                ws_i = wb.create_sheet(title=TAB_INDEED)
                ws_i.append(EXCEL_HEADERS)
                self._format_header(ws_i, "2164F3")

            if TAB_MANUAL not in sheet_names:
                ws_m = wb.create_sheet(title=TAB_MANUAL)
                ws_m.append(EXCEL_HEADERS)
                self._format_header(ws_m, "C55A11")

            # Ensure headers in standard tabs match EXCEL_HEADERS
            for s_name, color in [(TAB_ALL, "1F4E79"), (TAB_NAUKRI, "0A58CA"), (TAB_INDEED, "2164F3"), (TAB_MANUAL, "C55A11")]:
                if s_name in wb.sheetnames:
                    ws_curr = wb[s_name]
                    curr_head = [c.value for c in ws_curr[1][:len(EXCEL_HEADERS)]]
                    if curr_head != EXCEL_HEADERS:
                        for col_i, h_val in enumerate(EXCEL_HEADERS, 1):
                            ws_curr.cell(row=1, column=col_i, value=h_val)
                        self._format_header(ws_curr, color)

            # Ensure all Category-wise sheets exist
            for cat_name in CATEGORY_SHEETS:
                if cat_name not in sheet_names:
                    ws_c = wb.create_sheet(title=cat_name)
                    ws_c.append(CATEGORY_HEADERS)
                    c_color = CATEGORY_HEADER_COLORS.get(cat_name, "1F4E79")
                    self._format_category_header(ws_c, c_color)

            wb.save(self.file_path)

    def _recover_workbook(self) -> None:
        """Restores corrupted workbook from latest backup or creates clean instance."""
        import shutil
        backups = sorted(self.file_path.parent.glob("Job_Applications_Tracker_backup_*.xlsx"), reverse=True)
        for b in backups:
            try:
                wb = openpyxl.load_workbook(b)
                if len(wb.sheetnames) > 0:
                    shutil.copy2(b, self.file_path)
                    return
            except Exception:
                continue
        self._ensure_workbook(force_recreate=True)

    def _load_workbook(self, data_only: bool = False) -> openpyxl.Workbook:
        """Safely loads workbook, handling temporary file locks with retries and recovering only on real corruption."""
        import zipfile
        for attempt in range(5):
            try:
                return openpyxl.load_workbook(self.file_path, data_only=data_only)
            except PermissionError:
                time.sleep(0.3 * (attempt + 1))
            except (zipfile.BadZipFile, openpyxl.utils.exceptions.InvalidFileException, EOFError):
                self._recover_workbook()
                try:
                    return openpyxl.load_workbook(self.file_path, data_only=data_only)
                except Exception:
                    self._ensure_workbook(force_recreate=True)
                    return openpyxl.load_workbook(self.file_path, data_only=data_only)
            except Exception:
                time.sleep(0.2)
        return openpyxl.load_workbook(self.file_path, data_only=data_only)

    def _get_target_sheet_name(self, platform: str) -> str:
        """Determines which sheet tab to use based on platform name."""
        p_lower = (platform or "").lower()
        if "naukri" in p_lower:
            return TAB_NAUKRI
        elif "indeed" in p_lower:
            return TAB_INDEED
        return TAB_ALL

    def is_already_applied(self, company: str, job_title: str) -> bool:
        """Checks if an application already exists in applied tabs (excludes Manual)."""
        if not self.file_path.exists():
            return False
        wb = self._load_workbook(data_only=True)
        target_comp = company.strip().lower()
        target_title = job_title.strip().lower()

        # Check applied worksheets only (exclude Manual)
        for ws in wb.worksheets:
            if ws.title == TAB_MANUAL:
                continue
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or len(row) < 4:
                    continue
                row_comp = str(row[2] or "").strip().lower()
                row_title = str(row[3] or "").strip().lower()
                if not row_comp or not row_title:
                    continue

                comp_match = (target_comp == row_comp) or (target_comp in row_comp) or (row_comp in target_comp)
                title_match = (target_title == row_title) or (target_title in row_title) or (row_title in target_title)
                if comp_match and title_match:
                    wb.close()
                    return True

        wb.close()
        return False

    def is_already_in_manual(self, company: str, job_title: str) -> bool:
        """Checks if an external company site job already exists in the 'Manual' tab."""
        if not self.file_path.exists():
            return False
        wb = self._load_workbook(data_only=True)
        if TAB_MANUAL not in wb.sheetnames:
            wb.close()
            return False
        ws = wb[TAB_MANUAL]
        target_comp = company.strip().lower()
        target_title = job_title.strip().lower()

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 4:
                continue
            row_comp = str(row[2] or "").strip().lower()
            row_title = str(row[3] or "").strip().lower()
            if not row_comp or not row_title:
                continue

            comp_match = (target_comp == row_comp) or (target_comp in row_comp) or (row_comp in target_comp)
            title_match = (target_title == row_title) or (target_title in row_title) or (row_title in target_title)
            if comp_match and title_match:
                wb.close()
                return True

        wb.close()
        return False

    def append_manual_application(self, app: JobApplication, match_pct: float = 85.0, chance: str = "High") -> None:
        """
        Appends an application to the 'Manual' sheet ONLY if match_pct > 80%.
        Strictly excluded from 'All Applications', 'Naukri Jobs', and 'Indeed Jobs'.
        Never counted toward applied totals.
        """
        if match_pct <= 80.0:
            return

        if self.is_already_in_manual(app.company, app.job_title):
            return

        wb = self._load_workbook(data_only=False)
        if TAB_MANUAL not in wb.sheetnames:
            ws_m = wb.create_sheet(title=TAB_MANUAL)
            ws_m.append(EXCEL_HEADERS)
            self._format_header(ws_m, "C55A11")
        ws = wb[TAB_MANUAL]

        # Avoid duplicate by app_id
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r and r[0] == app.app_id:
                wb.close()
                return

        row_data = [
            app.app_id,
            app.date_applied or datetime.now().strftime("%Y-%m-%d"),
            app.company,
            app.job_title,
            app.location,
            app.job_url,
            app.platform or "Company Site",
            ApplicationStatus.MANUAL_APPLY_NEEDED.value,
            f"{match_pct:.0f}%",
            chance,
            app.last_updated or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "",
            "",
            app.notes or f"Apply on company site (Match {match_pct:.0f}% > 80%)",
        ]

        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )

        ws.append(row_data)
        row_num = ws.max_row
        for col_idx in range(1, len(row_data) + 1):
            cell = ws.cell(row=row_num, column=col_idx)
            cell.border = thin_border
            if col_idx in [1, 2, 8, 9, 10, 11]:
                cell.alignment = Alignment(horizontal="center")
            if col_idx == 8:
                s_fill, s_font = get_status_style(ApplicationStatus.MANUAL_APPLY_NEEDED.value)
                cell.fill = s_fill
                cell.font = s_font

        # Also append to corresponding Category sheet with ORANGE styling
        self._append_to_category_sheet(
            wb=wb,
            app=app,
            apply_mode="Manual",
            match_pct_str=f"{match_pct:.0f}%" if isinstance(match_pct, (int, float)) else str(match_pct),
            chance=chance,
            follow_up_date=""
        )

        wb.save(self.file_path)

    def _get_or_create_acw_sheet(self, wb: openpyxl.Workbook, date_str: Optional[str] = None) -> openpyxl.worksheet.worksheet.Worksheet:
        """
        Retrieves or creates a dedicated worksheet named ACW(Date) e.g., 'ACW(2026-10-08)'.
        Created and updated whenever an 'Apply on Company Website' job is processed.
        """
        if not date_str:
            date_str = datetime.now().strftime("%Y-%m-%d")
        sheet_title = f"ACW({date_str})"
        if sheet_title not in wb.sheetnames:
            ws_acw = wb.create_sheet(title=sheet_title)
            ws_acw.append(EXCEL_HEADERS)
            self._format_header(ws_acw, "7030A0")  # Royal Purple for Company Site
        return wb[sheet_title]

    def append_acw_application(
        self,
        app: JobApplication,
        match_pct: float = 85.0,
        chance: str = "High",
        apply_mode: str = "Company Site (Automated)"
    ) -> str:
        """
        Records an application into the dedicated ACW(Date) sheet and highlights the updated status cell.
        Also synchronizes to the appropriate Category tab with full status cell highlighting.
        """
        wb = self._load_workbook(data_only=False)
        today_str = datetime.now().strftime("%Y-%m-%d")
        ws = self._get_or_create_acw_sheet(wb, date_str=today_str)
        sheet_name = ws.title

        # Check for duplicates by app_id
        for r in ws.iter_rows(min_row=2, values_only=True):
            if r and str(r[0]).strip() == str(app.app_id).strip():
                wb.close()
                return sheet_name

        row_data = [
            app.app_id,
            app.date_applied or today_str,
            app.company,
            app.job_title,
            app.location,
            app.job_url,
            app.platform or "Company Website",
            app.status.value,
            f"{match_pct:.0f}%" if isinstance(match_pct, (int, float)) else str(match_pct),
            chance,
            app.last_updated or datetime.now().strftime("%Y-%m-%d %H:%M"),
            "",
            app.gmail_thread_id or "",
            app.notes or f"Processed via Automated Company Website Applier ({match_pct}%)",
        ]

        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )

        ws.append(row_data)
        row_num = ws.max_row
        for col_idx in range(1, len(row_data) + 1):
            cell = ws.cell(row=row_num, column=col_idx)
            cell.border = thin_border
            if col_idx in [1, 2, 8, 9, 10, 11]:
                cell.alignment = Alignment(horizontal="center")
            # Highlight status cell (column 8)
            if col_idx == 8:
                s_fill, s_font = get_status_style(app.status.value)
                cell.fill = s_fill
                cell.font = s_font

        # Also append to Master 'All Applications' tab
        if TAB_ALL in wb.sheetnames:
            ws_all = wb[TAB_ALL]
            exists = any(r and str(r[0]).strip() == str(app.app_id).strip() for r in ws_all.iter_rows(min_row=2, values_only=True))
            if not exists:
                ws_all.append(row_data)
                row_all = ws_all.max_row
                for col_idx in range(1, len(row_data) + 1):
                    cell = ws_all.cell(row=row_all, column=col_idx)
                    cell.border = thin_border
                    if col_idx in [1, 2, 8, 9, 10, 11]:
                        cell.alignment = Alignment(horizontal="center")
                    if col_idx == 8:
                        s_fill, s_font = get_status_style(app.status.value)
                        cell.fill = s_fill
                        cell.font = s_font

        # Also append to Category sheet with status highlighting
        self._append_to_category_sheet(
            wb=wb,
            app=app,
            apply_mode=apply_mode,
            match_pct_str=f"{match_pct:.0f}%" if isinstance(match_pct, (int, float)) else str(match_pct),
            chance=chance,
            follow_up_date=""
        )

        wb.save(self.file_path)
        return sheet_name

    def _format_row_styling(self, ws, row_idx: int, apply_mode: str, app_status: str = "") -> None:
        """Applies green styling for Auto Applied and orange styling for Manual, plus highlighted status badge."""
        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )
        is_auto = "auto" in apply_mode.strip().lower()
        row_fill = FILL_AUTO_APPLIED if is_auto else FILL_MANUAL
        status_fill = STATUS_FILL_AUTO if is_auto else STATUS_FILL_MANUAL
        status_font = STATUS_FONT_AUTO if is_auto else STATUS_FONT_MANUAL

        for col_idx in range(1, len(CATEGORY_HEADERS) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = thin_border
            cell.fill = row_fill
            if col_idx in [1, 2, 5, 8, 9, 10, 11, 12, 13, 14]:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            else:
                cell.alignment = Alignment(vertical="center")

            if col_idx == 9:
                # Column 9 is Status in CATEGORY_HEADERS
                target_st = app_status or str(cell.value or "")
                s_fill, s_font = get_status_style(target_st)
                cell.fill = s_fill
                cell.font = s_font
            elif col_idx == 10:
                cell.fill = status_fill
                cell.font = status_font

    def _append_to_category_sheet(
        self,
        wb: openpyxl.Workbook,
        app: JobApplication,
        apply_mode: str,
        match_pct_str: str = "",
        chance: str = "",
        follow_up_date: str = ""
    ) -> None:
        """Appends an application to its categorized sheet with green/orange styling."""
        cat_name = detect_job_category(app.job_title, app.notes)
        if cat_name not in wb.sheetnames:
            ws_c = wb.create_sheet(title=cat_name)
            ws_c.append(CATEGORY_HEADERS)
            color = CATEGORY_HEADER_COLORS.get(cat_name, "1F4E79")
            self._format_category_header(ws_c, color)
        ws = wb[cat_name]

        # Check for duplicates by app_id or company+title
        clean_comp = app.company.strip().lower()
        clean_title = app.job_title.strip().lower()
        for r in ws.iter_rows(min_row=2, values_only=True):
            if not r:
                continue
            r_id = str(r[0]).strip() if r[0] else ""
            r_comp = str(r[2]).strip().lower() if len(r) > 2 and r[2] else ""
            r_title = str(r[3]).strip().lower() if len(r) > 3 and r[3] else ""
            if (app.app_id and r_id == app.app_id) or (clean_comp and r_comp == clean_comp and r_title == clean_title):
                return

        row_data = [
            app.app_id,
            app.date_applied or datetime.now().strftime("%Y-%m-%d"),
            app.company,
            app.job_title,
            cat_name,
            app.location,
            app.job_url,
            app.platform,
            app.status.value,
            apply_mode,
            match_pct_str or "85%",
            chance or "High",
            app.last_updated or datetime.now().strftime("%Y-%m-%d %H:%M"),
            follow_up_date,
            app.gmail_thread_id,
            app.notes,
        ]
        ws.append(row_data)
        row_idx = ws.max_row
        self._format_row_styling(ws, row_idx, apply_mode, app_status=app.status.value)

        # Also append to any matching custom role sheet created by the user
        for s_name in list(wb.sheetnames):
            if s_name not in [TAB_ALL, TAB_NAUKRI, TAB_INDEED, TAB_MANUAL, TAB_OTHER] and s_name not in CATEGORY_SHEETS and not s_name.startswith("ACW"):
                if s_name.lower() in clean_title:
                    ws_custom = wb[s_name]
                    c_dup = False
                    for cr in ws_custom.iter_rows(min_row=2, values_only=True):
                        if cr and ((app.app_id and str(cr[0]).strip() == str(app.app_id).strip()) or (clean_comp and str(cr[2]).strip().lower() == clean_comp and str(cr[3]).strip().lower() == clean_title)):
                            c_dup = True
                            break
                    if not c_dup:
                        ws_custom.append(row_data)
                        self._format_row_styling(ws_custom, ws_custom.max_row, apply_mode, app_status=app.status.value)

    def ensure_custom_role_sheet(self, role_name: str) -> str:
        """
        Creates a dedicated sheet in the Excel workbook for a user-inputted job role if it does not already exist.
        Returns the sanitized sheet title.
        """
        if not role_name or not role_name.strip():
            return ""
        import re
        # Clean title for Excel sheet name limitations (max 31 chars, no invalid characters)
        clean_title = re.sub(r'[\\/*?:\[\]]', '', role_name.strip())[:31].strip()
        if not clean_title:
            return ""

        try:
            wb = self._load_workbook(data_only=False)
        except Exception:
            self._ensure_workbook()
            wb = self._load_workbook(data_only=False)

        if clean_title not in wb.sheetnames:
            ws = wb.create_sheet(title=clean_title)
            ws.append(CATEGORY_HEADERS)
            self._format_category_header(ws, "1F4E79")
            wb.save(self.file_path)
            safe_print(f"[ExcelManager] 📄 Created dedicated sheet for job role '{clean_title}'.")
        else:
            wb.close()

        return clean_title

    def append_application(self, app: JobApplication, match_pct: float = 85.0, follow_up_date: str = "", chance: str = "High") -> None:
        """
        Appends an application to:
        1. Its platform-specific sheet ('Naukri Jobs' or 'Indeed Jobs')
        2. The master 'All Applications' sheet
        3. Its corresponding Category sheet with GREEN styling
        Includes Chance column: 'High', 'Mid', or 'Low'
        """
        if self.is_already_applied(app.company, app.job_title):
            return

        wb = self._load_workbook(data_only=False)
        target_tab_name = self._get_target_sheet_name(app.platform)

        row_data = [
            app.app_id,
            app.date_applied,
            app.company,
            app.job_title,
            app.location,
            app.job_url,
            app.platform,
            app.status.value,
            f"{match_pct:.0f}%",
            chance,
            app.last_updated,
            follow_up_date,
            app.gmail_thread_id,
            app.notes,
        ]

        thin_border = Border(
            left=Side(style='thin', color='D9D9D9'),
            right=Side(style='thin', color='D9D9D9'),
            top=Side(style='thin', color='D9D9D9'),
            bottom=Side(style='thin', color='D9D9D9')
        )

        def _append_to_sheet(sheet_name: str):
            if sheet_name not in wb.sheetnames:
                ws_new = wb.create_sheet(title=sheet_name)
                ws_new.append(EXCEL_HEADERS)
                self._format_header(ws_new)
            ws = wb[sheet_name]

            # Avoid dup by app_id in this sheet
            for r in ws.iter_rows(min_row=2, values_only=True):
                if r and r[0] == app.app_id:
                    return

            ws.append(row_data)
            row_num = ws.max_row
            for col_idx in range(1, len(row_data) + 1):
                cell = ws.cell(row=row_num, column=col_idx)
                cell.border = thin_border
                if col_idx in [1, 2, 8, 9, 10, 11]:
                    cell.alignment = Alignment(horizontal="center")
                # Highlight Status cell (column 8)
                if col_idx == 8:
                    s_fill, s_font = get_status_style(app.status.value)
                    cell.fill = s_fill
                    cell.font = s_font

        # 1. Append to dedicated platform tab
        _append_to_sheet(target_tab_name)

        # 2. Append to Master 'All Applications' tab
        _append_to_sheet(TAB_ALL)

        # 3. Append to corresponding Category sheet with GREEN styling
        self._append_to_category_sheet(
            wb=wb,
            app=app,
            apply_mode="Auto Applied",
            match_pct_str=f"{match_pct:.0f}%" if isinstance(match_pct, (int, float)) else str(match_pct),
            chance=chance,
            follow_up_date=follow_up_date
        )

        wb.save(self.file_path)

    def sync_all_category_sheets(self, unified_records: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Clears and repopulates all 5 category sheets in local Excel with green/orange styling.
        """
        wb = self._load_workbook(data_only=False)
        counts = {cat: 0 for cat in CATEGORY_SHEETS}

        # Clear existing category data rows
        for cat in CATEGORY_SHEETS:
            if cat in wb.sheetnames:
                ws = wb[cat]
                if ws.max_row > 1:
                    ws.delete_rows(2, amount=ws.max_row - 1)
            else:
                ws = wb.create_sheet(title=cat)
                ws.append(CATEGORY_HEADERS)
                color = CATEGORY_HEADER_COLORS.get(cat, "1F4E79")
                self._format_category_header(ws, color)

        for item in unified_records:
            title = item.get("job_title", "")
            comp = item.get("company", "")
            if not title or not comp:
                continue

            cat_name = detect_job_category(title, item.get("notes", ""))
            apply_mode = item.get("apply_mode", "Auto Applied")
            if cat_name not in wb.sheetnames:
                ws = wb.create_sheet(title=cat_name)
                ws.append(CATEGORY_HEADERS)
                color = CATEGORY_HEADER_COLORS.get(cat_name, "1F4E79")
                self._format_category_header(ws, color)
            ws = wb[cat_name]

            row_data = [
                item.get("app_id", ""),
                item.get("date_applied", datetime.now().strftime("%Y-%m-%d")),
                comp,
                title,
                cat_name,
                item.get("location", "India"),
                item.get("job_url", ""),
                item.get("platform", "Naukri.com"),
                item.get("status", "Applied"),
                apply_mode,
                item.get("match_pct", "85%"),
                item.get("chance", "High"),
                item.get("last_updated", datetime.now().strftime("%Y-%m-%d %H:%M")),
                item.get("follow_up_date", ""),
                item.get("gmail_thread_id", ""),
                item.get("notes", ""),
            ]
            ws.append(row_data)
            row_idx = ws.max_row
            self._format_row_styling(ws, row_idx, apply_mode, app_status=item.get("status", "Applied"))
            counts[cat_name] = counts.get(cat_name, 0) + 1

        wb.save(self.file_path)
        return counts

    def get_category_stats(self) -> Dict[str, Dict[str, int]]:
        """Returns statistics for each category tab."""
        stats = {}
        if not self.file_path.exists():
            return stats
        wb = self._load_workbook(data_only=True)
        for cat in CATEGORY_SHEETS:
            if cat in wb.sheetnames:
                ws = wb[cat]
                rows = list(ws.iter_rows(min_row=2, values_only=True))
                auto_cnt = 0
                man_cnt = 0
                for r in rows:
                    if not r or len(r) < 10:
                        continue
                    mode = str(r[9] or "").strip().lower()
                    if "manual" in mode:
                        man_cnt += 1
                    else:
                        auto_cnt += 1
                stats[cat] = {
                    "total": len(rows),
                    "auto_applied": auto_cnt,
                    "manual": man_cnt,
                }
            else:
                stats[cat] = {"total": 0, "auto_applied": 0, "manual": 0}
        wb.close()
        return stats

    def sync_from_applications(self, apps: List[JobApplication]) -> None:
        """Ensures all JobApplication records exist in the Excel tracker."""
        for app in apps:
            self.append_application(app)

    def get_all_rows(self, sheet_name: str = TAB_ALL) -> List[dict]:
        """Returns applications as list of dictionaries from the specified tab or master."""
        if not self.file_path.exists():
            return []
        wb = self._load_workbook(data_only=True)
        if sheet_name not in wb.sheetnames:
            # Handle ACW wildcard / latest sheet request
            if sheet_name.upper().startswith("ACW"):
                acw_sheets = [s for s in wb.sheetnames if s.upper().startswith("ACW(")]
                if acw_sheets:
                    sheet_name = acw_sheets[-1]
                else:
                    sheet_name = wb.sheetnames[0]
            else:
                sheet_name = wb.sheetnames[0]
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) <= 1:
            wb.close()
            return []
        headers = rows[0]
        result = []
        for r in rows[1:]:
            if any(r):
                result.append(dict(zip(headers, r)))
        wb.close()
        return result

    def backup_and_reset(self, sheet_name: Optional[str] = None) -> str:
        """
        Creates a timestamped backup of the tracker file and resets sheets to empty (preserving headers).
        Allows clearing old phantom/unverified entries so deduplication doesn't block fresh applications.
        """
        import shutil
        if not self.file_path.exists():
            return ""

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.file_path.parent / f"Job_Applications_Tracker_backup_{timestamp}.xlsx"
        shutil.copy2(self.file_path, backup_path)

        wb = self._load_workbook(data_only=False)
        if sheet_name and sheet_name.strip() and sheet_name.lower() not in ["all", "all applications"]:
            if sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                if ws.max_row > 1:
                    ws.delete_rows(2, amount=ws.max_row - 1)
        else:
            for ws in wb.worksheets:
                if ws.max_row > 1:
                    ws.delete_rows(2, amount=ws.max_row - 1)
        wb.save(self.file_path)
        return str(backup_path.name)

    def delete_entry(self, app_id: str) -> bool:
        """Deletes a specific application entry by Application ID from all worksheets."""
        if not self.file_path.exists() or not app_id:
            return False
        wb = self._load_workbook(data_only=False)
        found = False
        for ws in wb.worksheets:
            rows_to_delete = []
            for row_idx in range(2, ws.max_row + 1):
                val = ws.cell(row=row_idx, column=1).value
                if val and str(val).strip() == str(app_id).strip():
                    rows_to_delete.append(row_idx)
            for row_idx in reversed(rows_to_delete):
                ws.delete_rows(row_idx, amount=1)
                found = True
        if found:
            wb.save(self.file_path)
        else:
            wb.close()
        return found

    def update_application_status_highlighted(
        self,
        app_id: str,
        new_status: str,
        notes: Optional[str] = None
    ) -> bool:
        """
        Updates the status for an application across all sheets (master, platform, ACW, category sheets),
        and dynamically highlights the updated status cell with dedicated visual badge styling
        (e.g., Applied, Interview Scheduled, Assessment Sent, Under Review, Rejected).
        """
        if not self.file_path.exists() or not app_id:
            return False

        wb = self._load_workbook(data_only=False)
        updated = False
        s_fill, s_font = get_status_style(new_status)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        for ws in wb.worksheets:
            for row_idx in range(2, ws.max_row + 1):
                val = ws.cell(row=row_idx, column=1).value
                if val and str(val).strip() == str(app_id).strip():
                    # Identify if this is a Category sheet or standard sheet by max cols / headers
                    # Category sheet has 16 columns and Status is at column 9
                    # Standard sheet has 14 columns and Status is at column 8
                    header_col9 = ws.cell(row=1, column=9).value
                    if str(header_col9).strip().lower() == "status":
                        status_col = 9
                        last_updated_col = 13
                        notes_col = 16
                    else:
                        status_col = 8
                        last_updated_col = 11
                        notes_col = 14

                    status_cell = ws.cell(row=row_idx, column=status_col)
                    status_cell.value = new_status
                    status_cell.fill = s_fill
                    status_cell.font = s_font
                    status_cell.alignment = Alignment(horizontal="center", vertical="center")

                    # Update Last Updated timestamp
                    ws.cell(row=row_idx, column=last_updated_col, value=now_str)

                    # Update notes if supplied
                    if notes is not None:
                        curr_notes = ws.cell(row=row_idx, column=notes_col).value or ""
                        if curr_notes:
                            ws.cell(row=row_idx, column=notes_col, value=f"{curr_notes} | {notes}")
                        else:
                            ws.cell(row=row_idx, column=notes_col, value=notes)

                    updated = True

        if updated:
            wb.save(self.file_path)
        else:
            wb.close()
        return updated

    def purge_manual_applications(self) -> int:
        """
        Removes all manual application entries across all sheets in the workbook:
        1. Wipes all data rows from the 'Manual' worksheet while preserving styled headers.
        2. Iterates across all other worksheets (All Applications, category sheets, etc.)
           and deletes any rows where:
           - Application ID starts with 'MAN-'
           - Status is 'Manual Apply Needed'
           - Apply Mode is 'Manual'
        Returns the total number of manual entries removed.
        """
        if not self.file_path.exists():
            return 0

        wb = self._load_workbook(data_only=False)
        removed_count = 0

        # 1. Clean 'Manual' sheet
        if TAB_MANUAL in wb.sheetnames:
            ws_man = wb[TAB_MANUAL]
            if ws_man.max_row > 1:
                manual_rows = ws_man.max_row - 1
                ws_man.delete_rows(2, manual_rows)
                removed_count += manual_rows

        # 2. Clean other sheets
        for ws in wb.worksheets:
            if ws.title == TAB_MANUAL:
                continue

            # Iterate backwards so row deletion indices remain valid
            for row_idx in range(ws.max_row, 1, -1):
                row_vals = [ws.cell(row=row_idx, column=c).value for c in range(1, ws.max_column + 1)]
                app_id = str(row_vals[0] or "").strip()
                
                # Check status column (col 8 or 9)
                status_val = ""
                if len(row_vals) >= 9 and row_vals[8]:
                    status_val = str(row_vals[8]).strip()
                elif len(row_vals) >= 8 and row_vals[7]:
                    status_val = str(row_vals[7]).strip()

                # Check apply mode column (col 10 in category sheets)
                mode_val = str(row_vals[9] or "").strip().lower() if len(row_vals) >= 10 else ""

                is_manual_entry = (
                    app_id.startswith("MAN-") or
                    status_val == ApplicationStatus.MANUAL_APPLY_NEEDED.value or
                    "manual apply needed" in status_val.lower() or
                    mode_val == "manual"
                )

                if is_manual_entry:
                    ws.delete_rows(row_idx, 1)
                    removed_count += 1

        wb.save(self.file_path)
        safe_print(f"[ExcelManager] 🧹 Purged {removed_count} manual application rows across all sheets.")
        return removed_count


