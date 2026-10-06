"""Turn parsed documents into a candidate baseline without inventing history."""

from __future__ import annotations

import re

from src.generator.catalog import SKILL_NAMES
from src.models import CandidateBaseline, ExperienceItem, ParsedDocument
from src.parsers.doc_parser import document_sort_key
from src.textutil import collapse_ws, contains_term

_EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_YEAR_SPAN = re.compile(r"^20\d{2}[\s.-]*20\d{2}$")
_SECTION = re.compile(
    r"^(?:professional\s+|technical\s+|core\s+|work\s+)?"
    r"(summary|profile|about|skills|technical skills|competencies|technical stack|keywords|"
    r"experience|work experience|employment|education|qualifications|"
    r"achievements|awards|projects)\b",
    re.I,
)
_BULLET = re.compile(r"^([●•▪►\-*]|\d+[.)])\s*")
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_HEADER_MAP = {
    "summary": "summary",
    "profile": "summary",
    "about": "summary",
    "skills": "skills",
    "technical skills": "skills",
    "competencies": "skills",
    "technical stack": "skills",
    "keywords": "skills",
    "experience": "experience",
    "work experience": "experience",
    "employment": "experience",
    "projects": "experience",
    "education": "education",
    "qualifications": "education",
    "achievements": "achievements",
    "awards": "achievements",
}


def synthesize(documents: list[ParsedDocument]) -> CandidateBaseline:
    """Build a baseline from the text that is actually present in docs/.

    Identity fields come from the primary CV. Supporting files still contribute skills.
    """
    if not documents:
        return CandidateBaseline()
    ordered = sorted(documents, key=lambda document: document_sort_key(document.path))
    primary = _parse_text(ordered[0].text)
    combined = "\n\n".join(document.text for document in ordered).strip()
    primary.skills = _skills(_sections(_lines(ordered[0].text)).get("skills", []), combined)
    primary.source_files = [document.path for document in ordered]
    primary.raw_text = combined[:200_000]
    return primary


def _parse_text(raw: str) -> CandidateBaseline:
    lines = _lines(raw)
    sections = _sections(lines)
    summary = collapse_ws(" ".join(sections.get("summary", [])))
    if not summary:
        summary = collapse_ws(raw)[:500]
    email_match = _EMAIL.search(raw)
    return CandidateBaseline(
        name=_name(lines),
        email=email_match.group(0) if email_match else "",
        phone=_phone(raw),
        summary=summary,
        skills=_skills(sections.get("skills", []), raw),
        experience=_experience(sections.get("experience", [])),
        education=_collect_lines(sections.get("education", []))[:6],
        achievements=_collect_lines(sections.get("achievements", []))[:6],
        raw_text=raw[:200_000],
    )


def _lines(raw: str) -> list[str]:
    return [line for line in (_clean_line(line) for line in raw.splitlines()) if line]


def _clean_line(line: str) -> str:
    return collapse_ws(line.replace("\x00", ""))


def _phone(raw: str) -> str:
    for match in _PHONE.finditer(raw):
        value = match.group(0).strip()
        digits = re.sub(r"\D", "", value)
        if len(digits) < 8 or len(digits) > 15 or _YEAR_SPAN.match(value):
            continue
        return value
    return ""


def _name(lines: list[str]) -> str:
    for line in lines[:6]:
        if "@" in line or _SECTION.match(line):
            continue
        if any(char.isdigit() for char in line):
            continue
        if 2 <= len(line) <= 60:
            return line
    return ""


def _sections(lines: list[str]) -> dict[str, list[str]]:
    current = "preamble"
    found: dict[str, list[str]] = {"preamble": []}
    for line in lines:
        header = _SECTION.match(line) if len(line) <= 80 and not line.endswith(".") else None
        if header:
            current = _HEADER_MAP.get(header.group(1).lower(), "other")
            found.setdefault(current, [])
            continue
        found.setdefault(current, []).append(line)
    return found


def _skills(skill_lines: list[str], raw: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        key = label.lower()
        if key and key not in seen:
            seen.add(key)
            found.append(label)

    for key, label in sorted(SKILL_NAMES.items(), key=lambda item: len(item[0]), reverse=True):
        if contains_term(raw, key):
            add(label)
    for line in skill_lines:
        pieces = re.split(r"[,|/•●]|(?:\s+-\s+)", _strip_bullet(line))
        for piece in pieces:
            label = piece.strip(" .")
            if 1 < len(label) <= 32 and ":" not in label:
                add(label)
    return found[:32]


def _collect_lines(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        text = _strip_bullet(line)
        if not text:
            continue
        if items and (_YEAR.search(items[-1]) is None or text[0].islower()):
            items[-1] = f"{items[-1]} {text}"
            continue
        items.append(text)
    return items


def _strip_bullet(line: str) -> str:
    return _BULLET.sub("", line).strip()


def _is_job_header(line: str) -> bool:
    """A role line names an organization with a bar, not a wrapped date or sentence."""
    return " | " in line and ("—" in line or "–" in line or _YEAR.search(line) is not None)


def _experience(lines: list[str]) -> list[ExperienceItem]:
    items: list[ExperienceItem] = []
    current: ExperienceItem | None = None
    for raw_line in lines:
        is_bullet = _BULLET.match(raw_line) is not None
        line = _strip_bullet(raw_line)
        if not line:
            continue
        if is_bullet:
            if current is None:
                current = ExperienceItem(title="Experience")
                items.append(current)
            current.bullets.append(line)
            continue
        if _is_job_header(line):
            if len(items) >= 8:
                break
            current = ExperienceItem(title=line)
            items.append(current)
            continue
        if current is not None and not current.bullets:
            if _YEAR.search(current.title) and " | " not in line and len(line) > 40:
                current.bullets.append(line)
            else:
                current.title = f"{current.title} {line}"
            continue
        if current is not None and current.bullets:
            current.bullets[-1] = f"{current.bullets[-1]} {line}"
            continue
        if len(items) >= 8:
            break
        current = ExperienceItem(title=line)
        items.append(current)
    if not items and lines:
        items.append(ExperienceItem(title="Experience", bullets=_collect_lines(lines)[:5]))
    for item in items:
        item.title = collapse_ws(item.title)[:220]
        item.bullets = [collapse_ws(bullet) for bullet in item.bullets[:6]]
    return items[:8]
