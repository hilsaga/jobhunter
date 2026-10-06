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
# Too broad to count. They show up in almost every post and would force 100.
_BROAD = {
    "management",
    "planning",
    "operations",
    "stakeholder",
    "requirements",
    "deployment",
    "roadmap",
    "onsite",
    "backend",
    "frontend",
    "leadership",
}
# Eight job keywords you already have is a full score. The rest of either list does not raise or lower it.
EXCELLENT_KEYWORD_HITS = 8

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
        key=lambda spec: (
            title_overlap(job_title, spec),
            score_match(job_text, spec, keywords, baseline),
        ),
        reverse=True,
    )
    spec = ranked[0]
    score = score_match(job_text, spec, keywords, baseline)
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
    score, reason = explain_match(job_text, choice.specialization, keywords, baseline, job_title)
    match = _percent(score * 100)
    title = choice.specialization.title or choice.specialization.area
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


def score_match(
    job_text: str,
    spec: Specialization,
    keywords: list[str],
    baseline: CandidateBaseline | None = None,
) -> float:
    """How many keywords the job uses that you already have. Eight is a full score."""
    matched, _unmet = _job_keyword_overlap(job_text, spec, keywords, baseline)
    if not matched:
        return 0.0
    return round(min(1.0, len(matched) / EXCELLENT_KEYWORD_HITS), 4)


def explain_match(
    job_text: str,
    spec: Specialization,
    keywords: list[str],
    baseline: CandidateBaseline | None = None,
    job_title: str = "",
) -> tuple[float, str]:
    """Score the job's own keywords against yours, and spell out the count."""
    matched, unmet = _job_keyword_overlap(job_text, spec, keywords, baseline)
    score = 0.0 if not matched else min(1.0, len(matched) / EXCELLENT_KEYWORD_HITS)
    points = _percent(score * 100)
    title = spec.title or spec.area or "saved CV"
    lines = [
        f"Closest CV: {title}.",
        (
            f"This job matches {len(matched)} of your keywords. "
            f"{EXCELLENT_KEYWORD_HITS} or more is 100/100, so this is {points}/100."
        ),
        "Matched: " + (", ".join(matched) if matched else "none") + ".",
    ]
    if unmet:
        shown = unmet[:12]
        rest = len(unmet) - len(shown)
        tail = f", and {rest} more" if rest else ""
        lines.append("The job also asks for: " + ", ".join(shown) + tail + ".")
    if job_title.strip():
        shared = sorted(_title_tokens(job_title) & _title_tokens(f"{spec.title} {spec.area.replace('_', ' ')}"))
        if shared:
            word = "word" if len(shared) == 1 else "words"
            lines.append(f"Job title shares {len(shared)} {word} with this CV: {', '.join(shared)}.")
        else:
            lines.append("Job title shares no words with this CV.")
    return round(score, 4), "\n".join(lines)


def _job_keyword_overlap(
    job_text: str,
    spec: Specialization,
    keywords: list[str],
    baseline: CandidateBaseline | None,
) -> tuple[list[str], list[str]]:
    """Keywords the job uses, split into ones you have and ones you do not."""
    yours = _user_terms(spec, keywords, baseline)
    owned = {term.lower() for term in yours}
    phrases = _phrases_in_job(job_text, [*yours, *SKILL_NAMES.values()])
    matched = [phrase for phrase in phrases if phrase.lower() in owned]
    unmet = [phrase for phrase in phrases if phrase.lower() not in owned]
    return matched, unmet


def _user_terms(
    spec: Specialization,
    keywords: list[str],
    baseline: CandidateBaseline | None,
) -> list[str]:
    owned = [*keywords, *spec.skills]
    if baseline is not None:
        owned.extend(baseline.skills)
    found: list[str] = []
    seen: set[str] = set()
    for item in owned:
        text = item.strip()
        key = text.lower()
        if len(key) < 2 or key in _GENERIC or key in _BROAD or key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found


def _phrases_in_job(job_text: str, phrases: list[str]) -> list[str]:
    haystack = job_text.lower()
    found: list[str] = []
    seen: set[str] = set()
    for item in phrases:
        text = item.strip()
        key = text.lower()
        if len(key) < 2 or key in _GENERIC or key in _BROAD or key in seen:
            continue
        if not contains_term(haystack, key):
            continue
        seen.add(key)
        found.append(text)
    found.sort(key=len, reverse=True)
    kept: list[str] = []
    for phrase in found:
        if any(contains_term(longer, phrase) for longer in kept):
            continue
        kept.append(phrase)
    kept.sort(key=str.lower)
    return kept


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
