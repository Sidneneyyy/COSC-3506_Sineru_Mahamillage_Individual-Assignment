"""
Course Catalog API
Phase 1: ingests an HTML course catalog table and exposes clean JSON via a REST API.
Phase 2: assembles per-student academic profiles from an HTML transcript export
(past history) and a JSON course plan (future courses).
"""

import re
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from bs4 import BeautifulSoup
from pydantic import BaseModel, ValidationError

app = FastAPI(title="Course Catalog API")

courses_db: dict[str, dict] = {}

# Phase 2: one entry per student_id -> {"history": [...], "plan": [...]}.
# Keyed by student so two students can never share state.
students_db: dict[str, dict] = {}

class Course(BaseModel):
    course_code: str
    title: str
    credits: Optional[int] = None
    prerequisites: List[str] = []
    cross_listed: List[str] = []


class HistoryRecord(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryUpdate(BaseModel):
    history: List[HistoryRecord]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanPayload(BaseModel):
    planned_courses: List[PlannedCourse]

# Matches course codes like "COSC 2007", "COSC2007", "ITEC 3506"
COURSE_CODE_PATTERN = re.compile(r"\b([A-Za-z]{2,5})\s*(\d{3,4})\b")


def normalize_code(raw: str) -> str:
    """Turn 'COSC 2007' or 'cosc2007' into 'COSC2007'."""
    match = COURSE_CODE_PATTERN.search(raw)
    if not match:
        return raw.strip().upper().replace(" ", "")
    return f"{match.group(1).upper()}{match.group(2)}"


def extract_course_codes(text: str) -> List[str]:
    """
    Pull every course code mentioned in a free-text cell (e.g.
    "Requires COSC 1046 and either COSC 1047 or ITEC 1047") and return
    them as a clean, deduplicated list. Returns [] for empty/"None" cells.
    """
    if not text or text.strip().lower() in ("none", "n/a", "-", ""):
        return []

    codes = []
    for match in COURSE_CODE_PATTERN.finditer(text):
        code = f"{match.group(1).upper()}{match.group(2)}"
        if code not in codes:
            codes.append(code)
    return codes


def find_header_index(headers: List[str], *keywords: str) -> Optional[int]:
    """
    Find which column index a header belongs to by matching keywords,
    case-insensitively. This is what makes parsing generalize to any
    catalog table, instead of assuming a fixed column order.
    """
    for i, h in enumerate(headers):
        h_lower = h.strip().lower()
        if any(kw in h_lower for kw in keywords):
            return i
    return None


def parse_catalog_html(html: str) -> List[dict]:
    """Parse an HTML course catalog table into a list of course dicts."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("No <table> found in uploaded document.")

    rows = table.find_all("tr")
    if not rows:
        raise ValueError("Table has no rows.")

    # First row = headers
    header_cells = rows[0].find_all(["th", "td"])
    headers = [cell.get_text(strip=True) for cell in header_cells]

    col_code = find_header_index(headers, "code")
    col_title = find_header_index(headers, "title")
    col_credits = find_header_index(headers, "credit")
    col_prereq = find_header_index(headers, "prereq")
    col_cross = find_header_index(headers, "cross")

    if col_code is None or col_title is None:
        raise ValueError(
            "Could not locate required 'Course Code' / 'Title' columns in table header."
        )

    parsed_courses = []

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        def cell_text(idx: Optional[int]) -> str:
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(strip=True)

        raw_code = cell_text(col_code)
        if not raw_code:
            continue  # skip blank rows

        code = normalize_code(raw_code)
        title = cell_text(col_title)

        credits_text = cell_text(col_credits)
        credits_value = int(credits_text) if credits_text.isdigit() else None

        prereq_list = extract_course_codes(cell_text(col_prereq))
        cross_list = extract_course_codes(cell_text(col_cross))

        parsed_courses.append(
            {
                "course_code": code,
                "title": title,
                "credits": credits_value,
                "prerequisites": prereq_list,
                "cross_listed": cross_list,
            }
        )

    return parsed_courses

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    """
    Accepts an uploaded HTML catalog file, parses it, and stores the
    extracted courses in memory (replacing any previous catalog).
    """
    raw_bytes = await file.read()
    try:
        html = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        html = raw_bytes.decode("latin-1")

    try:
        parsed = parse_catalog_html(html)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not parsed:
        raise HTTPException(status_code=400, detail="No courses found in uploaded catalog.")

    courses_db.clear()
    for course in parsed:
        courses_db[course["course_code"]] = course

    return {
        "message": "Catalog imported successfully.",
        "courses_imported": len(parsed),
        "course_codes": list(courses_db.keys()),
    }


@app.get("/api/v1/catalog/courses/{course_code}", response_model=Course)
async def get_course(course_code: str):
    """Return a single course as strict JSON, by course code."""
    key = normalize_code(course_code)
    course = courses_db.get(key)
    if course is None:
        raise HTTPException(status_code=404, detail=f"Course '{course_code}' not found.")
    return course


# ---------------------------------------------------------------------------
# Phase 2: student academic profiles
# ---------------------------------------------------------------------------

PAST_STATUSES = {"Completed", "In-Progress", "Attempted"}
NUMERIC_GRADE = re.compile(r"\d+(\.\d+)?")
LETTER_GRADE = re.compile(r"[A-F][+-]?", re.IGNORECASE)


def decode_upload(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw_bytes.decode("latin-1")


def find_exact_header(headers: List[str], name: str) -> Optional[int]:
    """Index of the header whose text is exactly `name` (case-insensitive)."""
    for i, h in enumerate(headers):
        if h.strip().lower() == name:
            return i
    return None


def cell_text(cell) -> str:
    """A cell's visible text, verbatim (whitespace collapsed at the edges)."""
    return cell.get_text(" ", strip=True)


def parse_credits(text: str) -> int:
    """Credits cell as an integer; 0 when blank or non-numeric (e.g. a failed lab)."""
    try:
        return int(text)
    except ValueError:
        return 0


def grade_rank(grade: str) -> tuple:
    """
    Sort key for picking the most informative duplicate row: a numeric
    grade beats a letter grade, which beats P or blank. Among numeric
    grades the higher one wins.
    """
    g = grade.strip()
    if NUMERIC_GRADE.fullmatch(g):
        return (2, float(g))
    if LETTER_GRADE.fullmatch(g):
        return (1, 0.0)
    return (0, 0.0)


def parse_transcript_html(html: str) -> List[dict]:
    """
    Extract past courses from every transcript table whose header row is
    Status / Course / (title) / Grade / Term / Credits, then deduplicate by
    (course_code, term), since the export repeats a course under each
    requirement group it counts toward.
    """
    soup = BeautifulSoup(html, "html.parser")

    best: dict[tuple, dict] = {}   # (course_code, term) -> candidate row
    order: List[tuple] = []        # first-seen order of keys
    found_table = False

    for table in soup.find_all("table"):
        # Only this table's own rows, never rows of a table nested inside it.
        rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        if not rows:
            continue

        headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        col_status = find_exact_header(headers, "status")
        col_course = find_exact_header(headers, "course")
        col_grade = find_exact_header(headers, "grade")
        col_term = find_exact_header(headers, "term")
        col_credits = find_exact_header(headers, "credits")
        if None in (col_status, col_course, col_term, col_credits):
            continue
        found_table = True

        for row in rows[1:]:
            cells = row.find_all("td", recursive=False)
            if len(cells) <= max(col_status, col_course, col_term, col_credits):
                continue

            status = cell_text(cells[col_status])
            term = cell_text(cells[col_term])
            if status not in PAST_STATUSES or not term:
                continue  # drops Fulfilled / Planned placeholder rows

            code = cell_text(cells[col_course])
            if not code:
                continue

            grade = ""
            if col_grade is not None and col_grade < len(cells):
                grade = cell_text(cells[col_grade])

            record = {
                "course_code": code,
                "term": term,
                "credits_earned": parse_credits(cell_text(cells[col_credits])),
                "status": status,
            }
            score = (grade_rank(grade), record["credits_earned"])

            key = (code, term)
            if key not in best:
                order.append(key)
                best[key] = {"record": record, "score": score}
            elif score > best[key]["score"]:
                best[key] = {"record": record, "score": score}

    if not found_table:
        raise ValueError("No transcript table (Status/Course/Term/Credits) found in uploaded document.")

    return [best[key]["record"] for key in order]


def get_student(student_id: str) -> dict:
    student = students_db.get(student_id)
    if student is None:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found.")
    return student


async def read_json_body(request: Request, model):
    """
    Parse and validate a JSON body after the student lookup, so an unknown
    student always gets 404 rather than a body-validation 422.
    """
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON.")
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_url=False))


@app.post("/api/v1/students/{student_id}/history/import", status_code=201)
async def import_history(student_id: str, file: UploadFile = File(...)):
    """Parse an uploaded HTML transcript and store it as this student's history."""
    html = decode_upload(await file.read())
    try:
        history = parse_transcript_html(html)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    student = students_db.setdefault(student_id, {"history": [], "plan": []})
    student["history"] = history
    return {"status": "success", "past_courses_imported": len(history)}


@app.put("/api/v1/students/{student_id}/history")
async def replace_history(student_id: str, request: Request):
    """Overwrite the student's academic history with the supplied array."""
    student = get_student(student_id)
    payload = await read_json_body(request, HistoryUpdate)
    student["history"] = [record.model_dump() for record in payload.history]
    return {"status": "success", "message": "Academic history updated successfully"}


@app.delete("/api/v1/students/{student_id}/history")
async def clear_history(student_id: str):
    student = get_student(student_id)
    student["history"] = []
    return {"status": "success", "message": "Academic history cleared"}


@app.post("/api/v1/students/{student_id}/plan")
async def save_plan(student_id: str, request: Request):
    student = get_student(student_id)
    payload = await read_json_body(request, PlanPayload)
    student["plan"] = [course.model_dump() for course in payload.planned_courses]
    return {"status": "success", "planned_courses_saved": len(student["plan"])}


@app.put("/api/v1/students/{student_id}/plan")
async def replace_plan(student_id: str, request: Request):
    student = get_student(student_id)
    payload = await read_json_body(request, PlanPayload)
    student["plan"] = [course.model_dump() for course in payload.planned_courses]
    return {
        "status": "success",
        "message": "Course plan replaced successfully",
        "planned_courses_saved": len(student["plan"]),
    }


@app.delete("/api/v1/students/{student_id}/plan")
async def clear_plan(student_id: str):
    student = get_student(student_id)
    student["plan"] = []
    return {"status": "success", "message": "Course plan cleared"}


@app.get("/api/v1/students/{student_id}/profile")
async def get_profile(student_id: str):
    """The merged record: exactly student_id, history and plan."""
    student = get_student(student_id)
    return {
        "student_id": student_id,
        "history": student["history"],
        "plan": student["plan"],
    }


@app.get("/")
async def root():
    return {"status": "ok", "courses_loaded": len(courses_db), "students_loaded": len(students_db)}

