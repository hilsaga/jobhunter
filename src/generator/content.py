"""Build grounded specializations and cover letters from a candidate baseline."""

from __future__ import annotations

import logging

from src.generator.catalog import DOMAINS, SKILL_NAMES, Domain
from src.generator.llm import LLMClient
from src.models import CandidateBaseline, ExperienceItem, JobListing, Specialization
from src.textutil import contains_term, slugify

logger = logging.getLogger("jobhunter.generator")

_SYSTEM = (
    "You synthesize a candidate profile and 2 to 4 role specializations from resume text. "
    "Return JSON only with keys name, email, phone, summary, skills, experience, education, "
    "achievements, and specializations. experience is a list of objects with title, organization, "
    "dates, and bullets. specializations is a list of 2 to 4 objects with area (snake_case), "
    "title, summary, highlights, skills, and cover_letter. Do not invent employers, dates, degrees, "
    "or skills that are not supported by the source text. Do not mention salary."
)


def enhance_baseline(baseline: CandidateBaseline, llm: LLMClient) -> CandidateBaseline:
    """Prefer model output when it is present, and keep parsed fields when it is not."""
    if not llm.enabled:
        return baseline
    data = llm.complete_json(_SYSTEM, _baseline_prompt(baseline, []))
    if not data:
        logger.warning("LLM baseline synthesis failed; keeping parsed document text")
        return baseline
    experience = _experience_from_json(data.get("experience"))
    return baseline.model_copy(
        update={
            "name": _text(data.get("name")) or baseline.name,
            "email": _text(data.get("email")) or baseline.email,
            "phone": _text(data.get("phone")) or baseline.phone,
            "summary": _text(data.get("summary")) or baseline.summary,
            "skills": _string_list(data.get("skills")) or baseline.skills,
            "experience": experience or baseline.experience,
            "education": _string_list(data.get("education")) or baseline.education,
            "achievements": _string_list(data.get("achievements")) or baseline.achievements,
        }
    )


def build_specializations(
    baseline: CandidateBaseline,
    keywords: list[str],
    llm: LLMClient,
    *,
    location: str = "",
) -> list[Specialization]:
    heuristic = _heuristic_specializations(baseline, keywords, location)
    if not llm.enabled:
        return heuristic
    data = llm.complete_json(_SYSTEM, _baseline_prompt(baseline, keywords))
    if not data:
        logger.warning("LLM specialization synthesis failed; using grounded drafts")
        return heuristic
    built = _specializations_from_json(data.get("specializations"), baseline)
    if len(built) < 2:
        logger.warning("LLM returned fewer than 2 specializations; using grounded drafts")
        return heuristic
    return built[:4]


def tailor_specialization(
    baseline: CandidateBaseline,
    specialization: Specialization,
    job: JobListing,
    emphasis: list[str],
    llm: LLMClient,
) -> Specialization:
    """Rewrite one CV pair for a job using only skills already present in the base documents."""
    area = slugify(f"custom_{job.job_id}", fallback="custom_job")
    grounded = [skill for skill in emphasis if skill.lower() in _baseline_haystack(baseline)]
    skills = list(dict.fromkeys([*specialization.skills, *grounded]))[:12]
    summary = specialization.summary.strip()
    focus = f"This version is arranged for the {job.title} role"
    if job.company:
        focus += f" at {job.company}"
    summary = f"{focus}. {summary}".strip()
    draft = Specialization(
        area=area,
        title=job.title or specialization.title,
        summary=summary,
        highlights=specialization.highlights[:4],
        skills=skills,
        cover_letter=_cover_letter(
            baseline,
            Specialization(
                area=area,
                title=job.title or specialization.title,
                summary=summary,
                highlights=specialization.highlights,
                skills=skills,
            ),
            "",
            job=job,
        ),
    )
    if not llm.enabled:
        return draft
    data = llm.complete_json(
        _SYSTEM,
        (
            f"{_baseline_prompt(baseline, skills)}\n\n"
            f"Rewrite one specialization for this job title: {job.title}\n"
            f"Company: {job.company}\n"
            f"Job text:\n{job.description[:6000]}\n"
            "Return JSON with a specializations array containing one object. "
            "Use only facts present in the source text."
        ),
    )
    if not data:
        return draft
    built = _specializations_from_json(data.get("specializations"), baseline)
    if not built:
        return draft
    tailored = built[0].model_copy(update={"area": area, "title": job.title or built[0].title})
    tailored.skills = [skill for skill in tailored.skills if skill.lower() in _baseline_haystack(baseline)]
    if not tailored.skills:
        tailored.skills = skills
    if not tailored.cover_letter.strip():
        tailored.cover_letter = draft.cover_letter
    return tailored


def area_for_job_title(title: str) -> str:
    """Folder name for an added job title, without an extra_ prefix."""
    return slugify(title, fallback="role", limit=60)


def build_for_job_title(
    baseline: CandidateBaseline,
    title: str,
    llm: LLMClient,
    *,
    location: str = "",
) -> Specialization:
    """Build one CV pair aimed at a single job title, using only source-backed facts."""
    clean_title = " ".join(title.split())
    specs = build_specializations(baseline, [clean_title], llm, location=location)
    base = specs[0]
    job = JobListing(
        job_id=slugify(clean_title, fallback="extra", limit=60),
        title=clean_title,
        url="extra-job-title",
        description=f"{clean_title}\n{baseline.summary}",
    )
    from src.pipeline.matching import emphasis_skills

    spec = tailor_specialization(
        baseline,
        base,
        job,
        emphasis_skills(job.description, baseline, base),
        llm,
    )
    spec.area = area_for_job_title(clean_title)
    spec.title = clean_title
    spec.cover_letter = _cover_letter(baseline, spec, location, job=job)
    return spec


def _heuristic_specializations(
    baseline: CandidateBaseline,
    keywords: list[str],
    location: str,
) -> list[Specialization]:
    haystack = _baseline_haystack(baseline)
    keyword_text = " ".join(keywords).lower()
    chosen = [
        domain
        for domain in DOMAINS
        if any(contains_term(haystack, key) or contains_term(keyword_text, key) for key in domain.keywords)
    ][:4]
    if len(chosen) < 2:
        for domain in DOMAINS:
            if domain not in chosen:
                chosen.append(domain)
            if len(chosen) == 2:
                break
        logger.info("Base documents matched fewer than 2 domains; filling with general drafts")
    return [_from_domain(baseline, domain, keywords, location) for domain in chosen]


def _from_domain(
    baseline: CandidateBaseline,
    domain: Domain,
    keywords: list[str],
    location: str,
) -> Specialization:
    skills = _skills_for_domain(baseline, domain)
    highlights = baseline.achievements[:4] or _experience_highlights(baseline)
    summary = baseline.summary or f"{baseline.name or 'Candidate'} background aligned to {domain.title} work."
    spec = Specialization(
        area=domain.area,
        title=domain.title,
        summary=summary,
        highlights=highlights,
        skills=skills,
    )
    spec.cover_letter = _cover_letter(baseline, spec, location, job=None)
    if keywords:
        logger.debug("Built %s with keywords %s", domain.area, keywords)
    return spec


def _skills_for_domain(baseline: CandidateBaseline, domain: Domain) -> list[str]:
    haystack = _baseline_haystack(baseline)
    found: list[str] = []
    seen: set[str] = set()
    for key in domain.keywords:
        if contains_term(haystack, key):
            label = SKILL_NAMES.get(key, key.title())
            if label.lower() not in seen:
                seen.add(label.lower())
                found.append(label)
    for skill in baseline.skills:
        if skill.lower() in seen:
            continue
        if any(key in skill.lower() for key in domain.keywords):
            seen.add(skill.lower())
            found.append(skill)
    if not found:
        found = baseline.skills[:8]
    return found[:10]


def _experience_highlights(baseline: CandidateBaseline) -> list[str]:
    highlights: list[str] = []
    for item in baseline.experience:
        highlights.extend(item.bullets)
        if len(highlights) >= 4:
            break
    if highlights:
        return highlights[:4]
    return [item.title for item in baseline.experience if item.title][:4]


def _cover_letter(
    baseline: CandidateBaseline,
    spec: Specialization,
    location: str,
    *,
    job: JobListing | None,
) -> str:
    skill_text = ", ".join(spec.skills[:6]) or "the work described in my CV"
    highlight = spec.highlights[0] if spec.highlights else ""
    if job is not None:
        target = job.title or spec.title
        if job.company:
            opening = f"I am writing to apply for the {target} role at {job.company}."
        else:
            opening = f"I am writing to apply for the {target} role."
    else:
        opening = f"I am writing to apply for {spec.title} opportunities."
    if location:
        opening += f" I am targeting roles in {location}."
    summary = baseline.summary.strip()
    if summary:
        opening = f"{opening} {summary}"
    middle = f"My recent work has involved {skill_text}."
    if highlight:
        middle = f"{middle} {highlight}"
    name = baseline.name or "Candidate"
    return (
        f"Dear Hiring Manager,\n\n{opening}\n\n{middle}\n\n"
        "I would welcome the chance to discuss how this background fits the role.\n\n"
        f"Sincerely,\n{name}"
    )


def _baseline_prompt(baseline: CandidateBaseline, keywords: list[str]) -> str:
    saved = baseline.model_dump()
    return (
        f"Target keywords: {', '.join(keywords)}\n\n"
        f"Parsed profile:\n{saved}\n\n"
        f"Source text:\n{baseline.raw_text[:18000]}"
    )


def _baseline_haystack(baseline: CandidateBaseline) -> str:
    return " ".join(
        [
            baseline.raw_text,
            " ".join(baseline.skills),
            baseline.summary,
            " ".join(baseline.achievements),
        ]
    ).lower()


def _specializations_from_json(value: object, baseline: CandidateBaseline) -> list[Specialization]:
    if not isinstance(value, list):
        return []
    specs: list[Specialization] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        area = slugify(_text(item.get("area")) or title, fallback="specialization", limit=60)
        if not title:
            continue
        spec = Specialization(
            area=area,
            title=title,
            summary=_text(item.get("summary")) or baseline.summary,
            highlights=_string_list(item.get("highlights"))[:6],
            skills=_string_list(item.get("skills"))[:12] or baseline.skills[:8],
            cover_letter=_text(item.get("cover_letter")),
        )
        if not spec.cover_letter:
            spec.cover_letter = _cover_letter(baseline, spec, "", job=None)
        specs.append(spec)
    return specs


def _experience_from_json(value: object) -> list[ExperienceItem]:
    if not isinstance(value, list):
        return []
    items: list[ExperienceItem] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            items.append(ExperienceItem(title=item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        items.append(
            ExperienceItem(
                title=_text(item.get("title")),
                organization=_text(item.get("organization")),
                dates=_text(item.get("dates")),
                bullets=_string_list(item.get("bullets"))[:6],
            )
        )
    return items[:6]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
