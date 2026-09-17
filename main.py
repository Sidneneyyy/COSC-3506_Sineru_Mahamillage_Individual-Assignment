"""
Course Catalog API - Phase 1
Ingests an HTML course catalog table and exposes clean JSON via a REST API.
"""

import re
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from bs4 import BeautifulSoup
from pydantic import BaseModel

app = FastAPI(title="Course Catalog API")

courses_db: dict[str, dict] = {}

class Course(BaseModel):
    course_code: str
    title: str
    credits: Optional[int] = None
    prerequisites: List[str] = []
    cross_listed: List[str] = []

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
            continue

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

@app.get("/")
async def root():
    return {"status": "ok", "courses_loaded": len(courses_db)}