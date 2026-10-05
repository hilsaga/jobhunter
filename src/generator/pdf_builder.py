"""Render a specialization into a CV PDF and a cover-letter PDF.

Generated CVs use the same page, type, and sections as docs/master_cv.txt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from src.generator.master_layout import (
    JobBlock,
    MasterCv,
    _ACCENT,
    _JOB,
    _fonts,
    _paint,
    _pdf_styles,
    _write_pdf,
    parse_master_cv,
)
from src.models import ExperienceItem, Specialization, TargetProfile
from src.textutil import escape_xml

logger = logging.getLogger("jobhunter.pdf")


def write_cv(
    path: Path,
    profile: TargetProfile,
    specialization: Specialization,
    *,
    master_text: str = "",
) -> Path:
    document = _styled_cv(profile, specialization, master_text)
    _write_pdf(document, path)
    if not path.is_file() or path.stat().st_size < 100:
        raise OSError(f"PDF was not written: {path}")
    logger.info("Wrote %s (%s bytes)", path, path.stat().st_size)
    return path


def write_cover_letter(path: Path, profile: TargetProfile, specialization: Specialization) -> Path:
    name = profile.candidate.name or "Candidate"
    paragraphs = [part.strip() for part in specialization.cover_letter.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [
            "Dear Hiring Manager,",
            f"I am applying for {specialization.title} roles.",
            f"Sincerely,\n{name}",
        ]
    _write_letter(path, name=name, contact=_contact(profile), paragraphs=paragraphs, title=specialization.title)
    if not path.is_file() or path.stat().st_size < 100:
        raise OSError(f"PDF was not written: {path}")
    logger.info("Wrote %s (%s bytes)", path, path.stat().st_size)
    return path


def _styled_cv(profile: TargetProfile, specialization: Specialization, master_text: str) -> MasterCv:
    """Keep the master CV's layout, and aim the headline and summary at this role."""
    parsed = parse_master_cv(master_text) if master_text.strip() else None
    if parsed is not None and (parsed.jobs or parsed.keywords or parsed.summary):
        if profile.candidate.name.strip():
            parsed.name = profile.candidate.name.strip()
        if specialization.title.strip():
            parsed.roles = [specialization.title.strip()]
        if specialization.summary.strip():
            parsed.summary = specialization.summary.strip()
        parsed.jobs = [_tilt_job(job, specialization.title) for job in parsed.jobs]
        return parsed
    candidate = profile.candidate
    skills = specialization.skills or candidate.skills
    return MasterCv(
        name=candidate.name or "Candidate",
        contact=_contact(profile),
        languages="",
        roles=[specialization.title] if specialization.title else [],
        summary=specialization.summary or candidate.summary,
        keywords=list(skills),
        stack=[],
        jobs=[_tilt_job(_job_block(item), specialization.title) for item in candidate.experience],
        education=list(candidate.education),
    )


_AI_WORD = re.compile(r"\b(ai|llm|agentic|mcp|gpt)\b|artificial intelligence|machine learning", re.I)


def _role_can_mention_ai(dates: str) -> bool:
    """AI wording belongs on roles still open, or roles that end in 2021 or later."""
    if re.search(r"present|current", dates, re.I):
        return True
    years = [int(year) for year in re.findall(r"(?:19|20)\d{2}", dates)]
    if not years:
        return False
    return years[-1] >= 2021


def _tilt_job(job: JobBlock, target_title: str) -> JobBlock:
    """Point the first bullet at the target title. Do not add AI to roles that ended before 2021."""
    title = " ".join(target_title.split())
    if not title or not job.bullets:
        return job
    if _AI_WORD.search(title) and not _role_can_mention_ai(job.dates):
        return job
    phrase = f"toward {title} work"
    first = job.bullets[0]
    if phrase.lower() in first.lower():
        return job
    if first.endswith("."):
        tilted = first[:-1] + f", {phrase}."
    else:
        tilted = f"{first}, {phrase}."
    return replace(job, bullets=[tilted, *job.bullets[1:]])


def _job_block(item: ExperienceItem) -> JobBlock:
    match = _JOB.match(item.title.strip())
    if match:
        return JobBlock(
            organization=match.group("org").strip(),
            place=match.group("place").strip(),
            title=match.group("title").strip(),
            dates=match.group("dates").strip(),
            bullets=list(item.bullets),
        )
    organization = item.organization.strip()
    place = ""
    if " | " in organization:
        organization, place = (part.strip() for part in organization.split(" | ", 1))
    title = item.title.strip()
    if organization and title == item.organization.strip():
        title = ""
    return JobBlock(
        organization=organization or title,
        place=place,
        title="" if not organization else title,
        dates=item.dates.strip(),
        bullets=list(item.bullets),
    )


def _contact(profile: TargetProfile) -> str:
    candidate = profile.candidate
    parts = [part for part in (candidate.phone, candidate.email, profile.targeted_location) if part]
    return " | ".join(parts)


def _write_letter(path: Path, *, name: str, contact: str, paragraphs: list[str], title: str) -> None:
    fonts = _fonts()
    styles = _pdf_styles(fonts)
    story: list[object] = [
        Paragraph(escape_xml(name), styles["name"]),
        Spacer(1, 8),
    ]
    if contact.strip():
        story.append(Paragraph(escape_xml(contact.replace(" | ", "    ·    ")), styles["contact"]))
        story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.8, color=_ACCENT, spaceBefore=2, spaceAfter=12))
    for paragraph in paragraphs:
        story.append(Paragraph(escape_xml(paragraph).replace("\n", "<br/>"), styles["body"]))
        story.append(Spacer(1, 10))
    path.parent.mkdir(parents=True, exist_ok=True)
    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=58,
        rightMargin=58,
        topMargin=46,
        bottomMargin=42,
        title=f"{name} cover letter — {title}",
        author=name,
    )
    document.build(story, onFirstPage=_paint, onLaterPages=_paint)
