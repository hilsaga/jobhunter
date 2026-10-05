"""Choose a CV pair and decide whether a form is complete enough to submit."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.generator.catalog import SKILL_NAMES
from src.models import CandidateBaseline, Specialization
from src.textutil import contains_term

_GENERIC = {
    "engineer",
    "developer",
    "senior",
    "junior",
    "the",
    "and",
    "for",
    "with",
    "role",
}

LOW_MATCH_SCORE = 0.2
TAILOR_SCORE = 0.6


@dataclass
class MaterialChoice:
    specialization: Specialization
    score: float
    tailor: bool
    emphasis: list[str]


def choose_materials(
    job_text: str,
    specs: list[Specialization],
    keywords: list[str],
    baseline: CandidateBaseline,
    job_title: str = "",
) -> MaterialChoice | None:
    if not specs:
        return None
    ranked = sorted(
        specs,
        key=lambda spec: (title_overlap(job_title, spec), score_match(job_text, spec, keywords)),
        reverse=True,
    )
    spec = ranked[0]
    score = score_match(job_text, spec, keywords)
    emphasis = emphasis_skills(job_text, baseline, spec)
    return MaterialChoice(
        specialization=spec,
        score=score,
        tailor=score >= TAILOR_SCORE and len(emphasis) >= 2,
        emphasis=emphasis,
    )


@dataclass(frozen=True)
class MatchAssessment:
    match: int
    proceed: int
    reason: str
    closest: str


def assess_match(
    job_text: str,
    specs: list[Specialization],
    keywords: list[str],
    baseline: CandidateBaseline,
    job_title: str = "",
) -> MatchAssessment:
    """Score the closest saved CV from 0 to 100 and explain the overlap."""
    choice = choose_materials(job_text, specs, keywords, baseline, job_title)
    if choice is None:
        return MatchAssessment(0, 0, "No saved CV to compare with this post.", "")
    match = _percent(choice.score * 100)
    title = choice.specialization.title or choice.specialization.area
    shared = [skill for skill in choice.specialization.skills if contains_term(job_text, skill)]
    if shared:
        reason = f"Closest saved CV is {title}. The post shares {', '.join(shared[:6])}."
    else:
        reason = f"Closest saved CV is {title}. The post does not share that CV's listed skills."
    return MatchAssessment(match=match, proceed=match, reason=reason, closest=title)


def _percent(value: float) -> int:
    return max(0, min(100, int(round(value))))


def is_viable(choice: MaterialChoice) -> bool:
    return choice.score >= LOW_MATCH_SCORE


def skip_list_hit(job_text: str, skip_list: list[str]) -> str:
    """Return the first skip-list phrase contained in the job text."""
    haystack = job_text.lower()
    for phrase in skip_list:
        token = phrase.strip().lower()
        if token and token in haystack:
            return phrase.strip()
    return ""


def title_overlap(job_title: str, spec: Specialization) -> int:
    """How many meaningful words the job title shares with a saved CV name."""
    wanted = _title_tokens(job_title)
    if not wanted:
        return 0
    have = _title_tokens(f"{spec.title} {spec.area.replace('_', ' ')}")
    return len(wanted & have)


def _title_tokens(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 1 and word not in _GENERIC}


def score_match(job_text: str, spec: Specialization, keywords: list[str]) -> float:
    haystack = job_text.lower()
    needles: list[str] = []
    seen: set[str] = set()
    for item in [*spec.skills, *keywords]:
        text = item.strip().lower()
        if len(text) < 2 or text in _GENERIC or text in seen:
            continue
        seen.add(text)
        needles.append(text)
    if not needles:
        return 0.0
    hits = sum(1 for needle in needles if contains_term(haystack, needle))
    return round(hits / len(needles), 4)


def emphasis_skills(job_text: str, baseline: CandidateBaseline, spec: Specialization) -> list[str]:
    """Skills the job asks for that the candidate already has, but this CV does not emphasize."""
    job_haystack = job_text.lower()
    base_haystack = f"{baseline.raw_text} {' '.join(baseline.skills)}".lower()
    already = {skill.lower() for skill in spec.skills}
    found: list[str] = []
    for key, label in SKILL_NAMES.items():
        if (
            contains_term(job_haystack, key)
            and contains_term(base_haystack, key)
            and key not in already
            and label not in found
        ):
            found.append(label)
    return found


def submission_confidence(*, unfilled_required: int, upload_slots: int, uploads_done: int) -> float:
    """Return a 0–1 score. Submissions below 0.8 must stop for a person."""
    score = 1.0
    if upload_slots == 0:
        score -= 0.3
    else:
        score -= 0.25 * max(0, upload_slots - uploads_done)
    score -= 0.25 * max(0, unfilled_required)
    return max(0.0, round(score, 4))
