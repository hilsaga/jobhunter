"""Orchestrate document analysis, CV generation, search, and applications."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from src.cli.human_input import HumanInput
from src.exceptions import (
    AbortRun,
    ChromeInteractionError,
    ChromeNotConfigured,
    DocsMissing,
    HumanInputRequired,
    SkipJob,
)
from src.generator.catalog import DOMAINS
from src.generator.content import (
    area_for_job_title,
    build_for_job_title,
    build_specializations,
    enhance_baseline,
    tailor_specialization,
)
from src.generator.heuristic import synthesize
from src.generator.llm import LLMClient
from src.generator.pdf_builder import write_cover_letter, write_cv
from src.logging_setup import Console
from src.mcp_client.chrome_bridge import (
    BrowserSession,
    ChromeBridge,
    is_playwright_status_page,
    is_site_home,
    url_on_listed_site,
)
from src.mcp_client.chrome_profiles import (
    PLAYWRIGHT_EXTENSION_URL,
    choose_chrome_profile,
    playwright_extension_installed,
)
from src.models import JobDetails, JobListing, Site, Specialization, TargetProfile
from src.parsers.doc_parser import DocParser, load_sites
from src.paths import ProjectPaths
from src.pipeline.matching import (
    TAILOR_SCORE,
    assess_match,
    choose_materials,
    is_viable,
    skip_list_hit,
    submission_confidence,
)
from src.pipeline.page_extract import (
    application_mode,
    css_selector,
    extract_heading,
    extract_listings,
    job_id_from_url,
    plan_form,
)
from src.pipeline.recorder import already_sent, record_application
from src.textutil import html_to_text, parse_keywords, slugify

logger = logging.getLogger("jobhunter.pipeline")

DEFAULT_INCOME = "$120,000 USD / yr"
DEFAULT_LOCATION = "Remote / Hong Kong"
DEFAULT_KEYWORDS = ["Python", "Full Stack", "AI Engineer"]


def _remember_title(titles: list[str], title: str) -> list[str]:
    clean = " ".join(title.split())
    key = clean.lower()
    if not clean or any(item.lower() == key for item in titles):
        return list(titles)
    return [*titles, clean]
T = TypeVar("T")


@dataclass
class RunConfig:
    root: Path
    through: str = "apply"
    mode: str = "auto"
    dry_run: bool = False
    noninteractive: bool = False
    confidence_threshold: float = 0.8
    max_per_site: int = 10
    docs_path: Path | None = None
    sites_path: Path | None = None
    verbose: bool = False
    chrome_profile: str | None = None
    skip_documents: bool = False


@dataclass
class RunSummary:
    documents_parsed: int = 0
    specializations: list[str] = field(default_factory=list)
    pdf_count: int = 0
    submitted: int = 0
    skipped: int = 0
    dry_runs: int = 0
    profile_path: str = ""


class ApplicationRunner:
    """Run steps 1–9. Browser work starts only after local documents exist."""

    def __init__(
        self,
        config: RunConfig,
        *,
        human: HumanInput | None = None,
        bridge: ChromeBridge | object | None = None,
        llm: LLMClient | None = None,
        console: Console | None = None,
    ) -> None:
        self.config = config
        self.paths = ProjectPaths(config.root, docs=config.docs_path, sites=config.sites_path)
        self.human = human or HumanInput(noninteractive=config.noninteractive)
        self.bridge = bridge or ChromeBridge()
        self.llm = llm or LLMClient()
        self.console = console or Console()
        self.summary = RunSummary()
        self.profile = TargetProfile(
            targeted_income=DEFAULT_INCOME,
            targeted_location=DEFAULT_LOCATION,
            interested_keywords=list(DEFAULT_KEYWORDS),
        )
        self.specs: list[Specialization] = []

    async def run(self) -> RunSummary:
        self.paths.ensure()
        if self.config.skip_documents:
            if self.config.through in {"profile", "documents"}:
                raise DocsMissing(
                    "--skip-documents starts at search. Omit --through, or set it to search or apply."
                )
            self._load_existing_documents()
        else:
            self._prepare_documents()
            if self.config.through in {"profile", "documents"}:
                return self.summary
        await self._run_sites()
        return self.summary

    async def standby(self) -> RunSummary:
        """Open the sites in sites.csv, then wait on the job post the person opens."""
        self.paths.ensure()
        self._load_existing_documents()
        sites = load_sites(self.paths.sites)
        if not sites:
            raise DocsMissing(f"No sites in {self.paths.sites}.")
        self.console.step("4", "Standby")
        try:
            async with self._attached_browser() as client:
                if not await self._open_listed_sites(client, sites):
                    return self.summary
                names = ", ".join(site.site_name for site in sites)
                self.console.info(f"{names} is open.")
                self.console.info("Open a job post in that window, then type a command.")
                self.console.info("1  score this job, then go back or create a new CV and cover letter")
                self.console.info("a  fill the open form. You press Submit.")
                self.console.info("q  quit")
                while True:
                    raw = self.human.ask("Standby", required=True).strip().lower()
                    if raw in {"q", "quit", "exit"}:
                        break
                    if raw not in {"1", "a"}:
                        self.console.warn("Type 1, a, or q.")
                        continue
                    self.console.info("Reading the open job.")
                    page = await self._read_listed_site(client, sites)
                    if page is None:
                        continue
                    _url, html = page
                    job = _job_from_open_page(html)
                    self.console.info(f"Open page: {job.title}")
                    if raw == "1":
                        self._review_job(job)
                        continue
                    await self._fill_open_form(client, job, html)
        except ChromeNotConfigured as exc:
            self.console.error(str(exc))
        return self.summary

    async def _open_listed_sites(self, client: BrowserSession, sites: list[Site]) -> bool:
        for site in sites:
            self.console.info(f"Opening {site.site_name}: {site.url}")
            try:
                await client.navigate(site.url)
            except ChromeInteractionError as exc:
                self.console.error(f"Could not open {site.site_name}: {exc}")
                return False
        return True

    async def _read_listed_site(self, client: BrowserSession, sites: list[Site]) -> tuple[str, str] | None:
        """Read the tab in front, not a different job tab or the Playwright welcome tab."""
        names = ", ".join(site.site_name for site in sites)
        url = ""
        focus = getattr(client, "focus_site", None)
        if focus is not None:
            try:
                url = await focus([site.url for site in sites])
            except ChromeInteractionError as exc:
                self.console.warn(str(exc))
                return None
            if is_site_home(url):
                self.console.warn(f"{names} is open. Open a job post in that window, then read the page.")
                return None
            self.console.info(f"Reading {url}")
        html = await client.get_html()
        on_listed_site = bool(url and url_on_listed_site(url, [site.url for site in sites]))
        if not on_listed_site and is_playwright_status_page(html):
            self.console.warn(
                f"That tab is the Playwright connection page. Open a job on {names}, then read the page again."
            )
            return None
        return url, html

    def save_profile_fields(
        self,
        *,
        income: str,
        location: str,
        keywords: list[str],
        skip_list: list[str],
        extra_job_titles: list[str],
    ) -> None:
        """Update the saved target fields and leave the candidate history as it is."""
        profile = self._load_profile()
        if profile is None:
            raise DocsMissing(
                f"No profile at {self.paths.profile}. Analyze the CV and generate the profile first."
            )
        self.profile = profile.model_copy(
            update={
                "targeted_income": income.strip(),
                "targeted_location": location.strip(),
                "interested_keywords": keywords,
                "skip_list": skip_list,
                "extra_job_titles": extra_job_titles,
            }
        )
        self._save_profile()

    def generate_missing_documents(self) -> list[str]:
        """Write a CV and cover letter for each saved title that does not have both PDFs yet."""
        profile = self._load_profile()
        if profile is None:
            raise DocsMissing(
                f"No profile at {self.paths.profile}. Analyze the CV and generate the profile first."
            )
        self.profile = profile
        written: list[str] = []
        missing_areas = [area for area in profile.specializations if not self._pair_exists(area)]
        built: dict[str, Specialization] = {}
        if missing_areas:
            for spec in build_specializations(
                profile.candidate,
                profile.interested_keywords,
                self.llm,
                location=profile.targeted_location,
            ):
                built[spec.area] = spec
        domains = {domain.area: domain.title for domain in DOMAINS}
        for area in missing_areas:
            spec = built.get(area)
            if spec is None:
                spec = build_for_job_title(
                    profile.candidate,
                    domains.get(area, area.replace("_", " ")),
                    self.llm,
                    location=profile.targeted_location,
                )
                spec.area = area
            self._write_pdfs(spec)
            written.append(area)
            self.console.info(f"Wrote missing pair for {spec.title}")
        for title in profile.extra_job_titles:
            area = area_for_job_title(title)
            legacy = slugify(f"extra_{title}", fallback="extra_role", limit=60)
            if self._pair_exists(area) or (legacy != area and self._pair_exists(legacy)):
                continue
            spec = build_for_job_title(
                profile.candidate,
                title,
                self.llm,
                location=profile.targeted_location,
            )
            if spec.area not in self.profile.specializations:
                self.profile.specializations = [*self.profile.specializations, spec.area]
            self._write_pdfs(spec)
            written.append(spec.area)
            self.console.info(f"Wrote missing pair for {title}")
        if written:
            self._save_profile()
        else:
            self.console.info("Every saved CV and cover letter is already on disk.")
        return written

    def _pair_exists(self, area: str) -> bool:
        folder = self.paths.specialization_dir(area)
        return (folder / "cv.pdf").is_file() and (folder / "cover_letter.pdf").is_file()

    def _prepare_documents(self) -> None:
        self.console.step("1", "Base document analysis")
        documents = DocParser(self.paths.docs).scan()
        self.summary.documents_parsed = len(documents)
        if not documents:
            message = (
                f"No resume documents found in {self.paths.docs}. "
                "Add a PDF, DOCX, or TXT file such as docs/master_cv.pdf."
            )
            self.console.warn(message)
            if self.human.noninteractive or not self.human.confirm(
                "Continue without base documents?",
                default=False,
            ):
                raise DocsMissing(message)
        for document in documents:
            self.console.info(f"Read {Path(document.path).name}")

        baseline = enhance_baseline(synthesize(documents), self.llm)
        self.console.info(f"Candidate: {baseline.name or 'name not found in the documents'}")
        income, location, keywords, skip_list, extra_job_titles = self._target_preferences()
        self.profile = TargetProfile(
            targeted_income=income,
            targeted_location=location,
            interested_keywords=keywords,
            skip_list=skip_list,
            extra_job_titles=extra_job_titles,
            candidate=baseline,
        )
        self.console.step("2", "Target profile")
        self._save_profile()
        self.console.info(f"Wrote {self._display(self.paths.profile)}")
        self.console.info(f"Location: {location}")
        self.console.info(f"Keywords: {', '.join(keywords)}")
        self.console.info(f"Skip list: {', '.join(skip_list) or 'none'}")
        self.console.info(f"Extra job titles: {', '.join(extra_job_titles) or 'none'}")
        self.console.info(f"Mode: {self.config.mode}")
        if self.config.through == "profile":
            return

        self.console.step("3", "Domain-specific CVs and cover letters")
        self.specs = build_specializations(baseline, keywords, self.llm, location=location)
        self.profile.specializations = [spec.area for spec in self.specs]
        self._save_profile()
        for spec in self.specs:
            self._write_pdfs(spec)
            self.console.info(f"{spec.title} -> generated_docs/{spec.area}/")
        self.summary.specializations = [spec.area for spec in self.specs]

    def _load_existing_documents(self) -> None:
        """Search with the profile and CV pairs already stored in generated_docs/."""
        self.console.step("1", "Using existing CVs")
        profile = self._load_profile()
        if profile is None:
            raise DocsMissing(
                f"No profile at {self.paths.profile}. Run once without --skip-documents to create the CVs."
            )
        self.profile = profile
        from src.generator.catalog import DOMAINS

        titles = {domain.area: domain.title for domain in DOMAINS}
        specs: list[Specialization] = []
        if self.paths.generated_docs.is_dir():
            folders = sorted(path for path in self.paths.generated_docs.iterdir() if path.is_dir())
            for folder in folders:
                if not (folder / "cv.pdf").is_file() or not (folder / "cover_letter.pdf").is_file():
                    continue
                area = folder.name
                specs.append(
                    Specialization(
                        area=area,
                        title=titles.get(area, area.replace("_", " ")),
                        summary=profile.candidate.summary,
                        skills=list(profile.candidate.skills),
                    )
                )
        if not specs:
            raise DocsMissing(
                "No CV and cover letter pairs in generated_docs/. Run once without --skip-documents."
            )
        self.specs = specs
        self.summary.specializations = [spec.area for spec in specs]
        self.summary.profile_path = str(self.paths.profile)
        self.console.info(f"Profile: {self._display(self.paths.profile)}")
        self.console.info(f"CVs: {', '.join(spec.area for spec in specs)}")
        self.console.info("Skipped document analysis and CV rewrite.")

    def _target_preferences(self) -> tuple[str, str, list[str], list[str], list[str]]:
        existing = self._load_profile()
        if existing is not None and (
            self.human.noninteractive
            or self.human.confirm("Reuse target preferences from generated_docs/profile.json?", default=True)
        ):
            keywords = existing.interested_keywords or list(DEFAULT_KEYWORDS)
            return (
                existing.targeted_income,
                existing.targeted_location,
                keywords,
                existing.skip_list,
                list(existing.extra_job_titles),
            )
        if self.human.noninteractive:
            self.console.info("Using example target preferences because this run is non-interactive.")
            stored = list(existing.extra_job_titles) if existing is not None else []
            return DEFAULT_INCOME, DEFAULT_LOCATION, list(DEFAULT_KEYWORDS), [], stored
        income = self.human.ask("Targeted income", default=DEFAULT_INCOME)
        location = self.human.ask("Targeted location", default=DEFAULT_LOCATION)
        raw_keywords = self.human.ask(
            "Interested keywords, separated by commas",
            default=", ".join(DEFAULT_KEYWORDS),
        )
        raw_skip = self.human.ask(
            "Skip list, comma-separated words that should skip a job (Enter for none)",
            default="",
            required=False,
        )
        extra_default = ", ".join(existing.extra_job_titles) if existing is not None else ""
        raw_extra = self.human.ask(
            "Extra job titles, comma-separated (Enter for none)",
            default=extra_default,
            required=False,
        )
        keywords = parse_keywords(raw_keywords) or list(DEFAULT_KEYWORDS)
        return income, location, keywords, parse_keywords(raw_skip), parse_keywords(raw_extra)

    def write_extra_job_title(self, title: str | None) -> list[tuple[Path, Path]]:
        """Append TITLE to extra_job_titles and write a CV plus cover letter for it.

        With no title, write one pair for every title already stored.
        """
        self.paths.ensure()
        documents = DocParser(self.paths.docs).scan()
        if not documents:
            raise DocsMissing(
                f"No resume documents found in {self.paths.docs}. "
                "Add a PDF, DOCX, or TXT file such as docs/master_cv.txt."
            )
        baseline = enhance_baseline(synthesize(documents), self.llm)
        existing = self._load_profile()
        stored = list(existing.extra_job_titles) if existing is not None else []
        chosen = (title or "").strip()
        if chosen:
            titles = _remember_title(stored, chosen)
        else:
            titles = stored
        if not titles:
            if self.human.noninteractive:
                raise ValueError(
                    "Pass a title with --extra-job-title TITLE, or add titles to extra_job_titles in generated_docs/profile.json."
                )
            chosen = self.human.ask("Extra job title", required=True).strip()
            titles = _remember_title(stored, chosen)
        if existing is None:
            self.profile = TargetProfile(
                targeted_income=DEFAULT_INCOME,
                targeted_location=DEFAULT_LOCATION,
                interested_keywords=list(DEFAULT_KEYWORDS),
                extra_job_titles=titles,
                candidate=baseline,
            )
        else:
            self.profile = existing.model_copy(update={"extra_job_titles": titles, "candidate": baseline})
        written: list[tuple[Path, Path]] = []
        areas: list[str] = []
        targets = [chosen] if chosen else titles
        for item in targets:
            self.console.step("1", f"CV and cover letter for {item}")
            spec = build_for_job_title(
                baseline,
                item,
                self.llm,
                location=self.profile.targeted_location,
            )
            if spec.area not in self.profile.specializations:
                self.profile.specializations = [*self.profile.specializations, spec.area]
            cv_path, letter_path = self._write_pdfs(spec)
            written.append((cv_path, letter_path))
            areas.append(spec.area)
            self.console.info(f"Extra job title: {item}")
            self.console.info(f"CV: {self._display(cv_path)}")
            self.console.info(f"Cover letter: {self._display(letter_path)}")
        self._save_profile()
        self.summary.specializations = areas
        self.summary.pdf_count = len(written) * 2
        self.summary.documents_parsed = len(documents)
        return written

    def _load_profile(self) -> TargetProfile | None:
        if not self.paths.profile.is_file():
            return None
        try:
            return TargetProfile.model_validate_json(self.paths.profile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.exception("Could not read %s", self.paths.profile)
            return None

    def _save_profile(self) -> None:
        self.profile.updated_at = datetime.now(timezone.utc).isoformat()
        self.paths.profile.parent.mkdir(parents=True, exist_ok=True)
        self.paths.profile.write_text(self.profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
        if not self.paths.profile.is_file():
            raise OSError(f"Failed to write {self.paths.profile}")
        self.summary.profile_path = str(self.paths.profile)

    def _master_cv_text(self) -> str:
        path = self.paths.docs / "master_cv.txt"
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.info("Could not read %s for CV layout", path)
            return ""

    def _write_pdfs(self, spec: Specialization) -> tuple[Path, Path]:
        folder = self.paths.specialization_dir(spec.area)
        cv_path = folder / "cv.pdf"
        letter_path = folder / "cover_letter.pdf"
        write_cv(cv_path, self.profile, spec, master_text=self._master_cv_text())
        write_cover_letter(letter_path, self.profile, spec)
        self.summary.pdf_count += 2
        return cv_path, letter_path

    async def _run_sites(self) -> None:
        self.console.step("4", "Site processing")
        if not self.paths.sites.is_file():
            raise FileNotFoundError(f"Site list not found: {self.paths.sites}")
        sites = load_sites(self.paths.sites)
        if not sites:
            self.console.warn(f"No sites to process in {self.paths.sites}")
            return
        if not getattr(self.bridge, "configured", False):
            self.console.warn(
                "Chrome MCP is not configured. Set JOBHUNTER_CHROME_MCP_COMMAND to search and apply. "
                "Document generation is complete."
            )
            return
        try:
            async with self._attached_browser() as client:
                for index, site in enumerate(sites, start=1):
                    self.console.info(f"Site {index}/{len(sites)}: {site.site_name}")
                    await self._process_site(client, site)
        except HumanInputRequired as exc:
            self.console.warn(str(exc))
            return
        except ValueError as exc:
            self.console.error(str(exc))
            return
        except ChromeNotConfigured as exc:
            self.console.error(str(exc))
        except ChromeInteractionError as exc:
            logger.exception("Chrome MCP session failed")
            self.console.error(str(exc))

    @asynccontextmanager
    async def _attached_browser(self):  # type: ignore[no-untyped-def]
        if isinstance(self.bridge, ChromeBridge):
            target = choose_chrome_profile(
                self.human,
                self.console,
                env_path=self.paths.root / ".env",
                selection=self.config.chrome_profile,
            )
            self.console.info(f"Using Chrome profile: {target.label()}")
            if target.incognito:
                self.console.info("Opening an incognito Chrome window.")
            elif target.profile is not None and not playwright_extension_installed(target.profile):
                raise ChromeNotConfigured(
                    "This profile can be driven only when the Playwright extension is installed in "
                    f"{target.profile.label()}. Install it from {PLAYWRIGHT_EXTENSION_URL} and run again."
                )
            else:
                self.console.info("Using the existing Chrome window for this profile.")
            session = self.bridge.session(
                None if target.profile is None else target.profile.directory,
                incognito=target.incognito,
                user_data_dir=None if target.profile is None else target.profile.user_data_dir,
                config_path=self.paths.logs.parent / "playwright-mcp.json",
            )
        else:
            session = self.bridge.session()  # type: ignore[attr-defined]
        async with session as client:
            yield client

    def _review_job(self, job: JobListing) -> None:
        """Score the open post, then go back or write a new CV and cover letter."""
        report = assess_match(
            job.description,
            self.specs,
            self.profile.interested_keywords,
            self.profile.candidate,
            job.title,
        )
        report = self._llm_match_review(job, report)
        skip = 100 - report.proceed
        if report.proceed >= 50:
            suggestion = f"Proceed {report.proceed}/100. Skip {skip}/100."
        else:
            suggestion = f"Skip {skip}/100. Proceed {report.proceed}/100."
        self.console.info(f"Match likelihood: {report.match}/100")
        self.console.info(f"Reason: {report.reason}")
        self.console.info(f"Suggestion: {suggestion}")
        self.console.info("0  go back")
        self.console.info("1  create a new CV and cover letter")
        answer = self.human.ask("Review", required=True).strip()
        if answer != "1":
            self.console.info("Back to standby.")
            return
        spec = self._create_pair(job)
        cv_path, letter_path = self._material_paths(spec)
        self.console.info("Created a new pair.")
        self.console.info(f"CV: {self._display(cv_path)}")
        self.console.info(f"Cover letter: {self._display(letter_path)}")

    def _llm_match_review(self, job: JobListing, report, specs: list[Specialization] | None = None):  # type: ignore[no-untyped-def]
        if not self.llm.enabled:
            return report
        catalog = "\n".join(f"- {spec.title}: {', '.join(spec.skills[:8])}" for spec in (specs or self.specs))
        data = self.llm.complete_json(
            "Score how well the saved CVs fit this job. Use only the job text and the CV list. "
            "Do not invent employers, dates, degrees, or skills. "
            'Reply with JSON {"match": <0-100>, "proceed": <0-100>, "reason": "<one or two sentences>"}. '
            "match is the fit of the closest saved CV. proceed is whether to apply: 0 is skip, 100 is proceed.",
            f"Job title: {job.title}\n\n{job.description[:8000]}\n\nSaved CVs:\n{catalog}",
        )
        if not data:
            return report
        extra = str(data.get("reason") or "").strip()
        reason = report.reason if not extra or extra in report.reason else f"{report.reason}\n{extra}"
        return type(report)(match=report.match, proceed=report.proceed, reason=reason, closest=report.closest)

    def _suggest_for_job(self, job: JobListing) -> tuple[Specialization, Path, Path]:
        spec, created = self._materials_for_job(job)
        cv_path, letter_path = self._material_paths(spec)
        if created:
            self.console.info("None of the saved CVs fit this post. Created a new pair.")
        else:
            self.console.info(f"Use {spec.title}.")
        self.console.info(f"CV: {self._display(cv_path)}")
        self.console.info(f"Cover letter: {self._display(letter_path)}")
        return spec, cv_path, letter_path

    def _materials_for_job(self, job: JobListing) -> tuple[Specialization, bool]:
        decision = self._llm_cv_choice(job)
        if isinstance(decision, Specialization):
            return decision, False
        if decision == "create":
            return self._create_pair(job), True
        choice = choose_materials(
            job.description,
            self.specs,
            self.profile.interested_keywords,
            self.profile.candidate,
            job.title,
        )
        if choice is not None and choice.score >= TAILOR_SCORE:
            self.console.info(f"Closest saved CV is {choice.specialization.title} ({choice.score:.0%}).")
            return choice.specialization, False
        if choice is not None:
            self.console.info(f"Closest saved CV is only {choice.score:.0%}.")
        return self._create_pair(job), True

    def _llm_cv_choice(self, job: JobListing) -> Specialization | str | None:
        if not self.llm.enabled:
            self.console.info("No LLM key is set. Comparing the job text with the saved CVs.")
            return None
        catalog = "\n".join(f"- {spec.area}: {spec.title}" for spec in self.specs)
        data = self.llm.complete_json(
            "Choose one saved CV for this job, or say none fit. "
            "Reply with JSON {\"fit\": true, \"area\": \"<area from the list>\", \"reason\": \"...\"} "
            "or {\"fit\": false, \"reason\": \"...\"}. Do not invent a CV area.",
            f"Job title: {job.title}\n\n{job.description[:8000]}\n\nSaved CVs:\n{catalog}",
        )
        if not data:
            self.console.info("The model did not return a choice. Comparing the job text instead.")
            return None
        reason = str(data.get("reason") or "").strip()
        if reason:
            self.console.info(reason)
        if data.get("fit") is True:
            wanted = str(data.get("area") or "").strip().lower()
            for spec in self.specs:
                if spec.area.lower() == wanted or spec.title.lower() == wanted:
                    return spec
            self.console.info("The model named a CV that is not on disk. Comparing the job text instead.")
            return None
        if data.get("fit") is False:
            return "create"
        return None

    def customize_for_job(self, job: JobListing) -> Specialization:
        """Write a new CV and cover from the closest saved CV, aimed at this job."""
        if self.profile is None or not self.specs:
            self._load_existing_documents()
        bases = [spec for spec in self.specs if not spec.area.startswith("custom_")]
        choice = choose_materials(
            job.description or job.title,
            bases or self.specs,
            self.profile.interested_keywords,
            self.profile.candidate,
            job.title,
        )
        if choice is None:
            raise DocsMissing("No saved CV to match with this job.")
        base = choice.specialization
        self.console.info(f"Best saved CV for {job.title}: {base.title}")
        spec = tailor_specialization(
            self.profile.candidate,
            base,
            job,
            choice.emphasis,
            self.llm,
        )
        token = job.job_id
        if job.url.startswith(("http://", "https://")):
            token = job_id_from_url(job.url)
        area = slugify(f"custom_{token}", fallback="custom_job", limit=60)
        title = " ".join((job.title or spec.title).split()) or spec.title
        spec = spec.model_copy(update={"area": area, "title": title})
        self.specs = [item for item in self.specs if item.area != area]
        self.specs.append(spec)
        self.profile.extra_job_titles = _remember_title(self.profile.extra_job_titles, title)
        if area not in self.profile.specializations:
            self.profile.specializations = [*self.profile.specializations, area]
        self._save_profile()
        cv_path, letter_path = self._write_pdfs(spec)
        self.console.info(f"CV: {cv_path}")
        self.console.info(f"Cover letter: {letter_path}")
        return spec

    def _create_pair(self, job: JobListing) -> Specialization:
        title = job.title.strip()
        if not title or title == "Open job":
            title = self.human.ask("Job title for the new CV", required=True).strip()
        spec = build_for_job_title(
            self.profile.candidate,
            title,
            self.llm,
            location=self.profile.targeted_location,
        )
        self.specs.append(spec)
        self.profile.extra_job_titles = _remember_title(self.profile.extra_job_titles, title)
        if spec.area not in self.profile.specializations:
            self.profile.specializations = [*self.profile.specializations, spec.area]
        self._save_profile()
        self._write_pdfs(spec)
        return spec

    async def _fill_open_form(self, client: BrowserSession, job: JobListing, html: str) -> None:
        spec, cv_path, letter_path = self._suggest_for_job(job)
        plan = plan_form(html, self.profile)
        if not plan.file_inputs and not plan.auto_fields:
            label = "Easy Apply" if "easy apply" in html.lower() or "quick apply" in html.lower() else "Apply"
            await self._attempt(
                "Open application",
                lambda label=label: client.click(selector="button", description=label),
                optional=True,
                context=job.title,
            )
            html = await client.get_html()
        await self._complete_form(client, job, html or "", spec, cv_path, letter_path, 0.0, submit=False)
        self.console.info("Form filled. Press Submit in Chrome.")

    async def _process_site(self, client: BrowserSession, site: Site) -> None:
        try:
            self.console.step("5", f"Search {site.site_name}")
            listings = await self._search_site(client, site)
        except SkipJob as exc:
            self.console.warn(f"Skipped {site.site_name}: {exc}")
            self.summary.skipped += 1
            return
        except HumanInputRequired as exc:
            self.console.warn(f"Skipped {site.site_name}; human input required: {exc}")
            self.summary.skipped += 1
            return
        if not listings:
            self.console.warn(f"No job listings found on {site.site_name}")
            return
        self.console.info(f"Found {len(listings)} listing(s) on {site.site_name}")
        if self.config.through == "search":
            for job in listings:
                self.console.info(f"{job.title} — {job.url}")
            return
        self.console.step("7", f"Applications on {site.site_name}")
        for job in listings:
            await self._process_job(client, job)

    async def _search_site(self, client: BrowserSession, site: Site) -> list[JobListing]:
        queries = _search_queries(self.profile, self.specs)
        self.console.info(f"Queries: {', '.join(queries)}")
        listings: list[JobListing] = []
        seen: set[str] = set()
        for query in queries:
            html = await self._open_search(client, site, query)
            for item in extract_listings(html, site.url, site.site_name):
                if item.job_id in seen:
                    continue
                seen.add(item.job_id)
                listings.append(item)
            if len(listings) >= self.config.max_per_site:
                break
        listings = listings[: self.config.max_per_site]
        if listings:
            return listings
        pasted = self.human.ask(
            "No job links were found. Paste job URLs separated by commas, or press Enter to skip this site",
            default="",
            required=False,
        )
        return _listings_from_paste(pasted, site)

    async def _open_search(self, client: BrowserSession, site: Site, query: str) -> str:
        await self._attempt(
            "Open site",
            lambda url=site.url: client.navigate(url),
            optional=False,
            context=site.site_name,
        )
        if self.profile.targeted_location:
            await self._attempt(
                "Fill location",
                lambda: client.fill(
                    selector=_location_selector(site),
                    value=self.profile.targeted_location,
                    description="location search",
                ),
                optional=True,
                context=site.site_name,
            )
        await self._attempt(
            "Fill search",
            lambda value=query: client.fill(
                selector="input[type='search']",
                value=value,
                description="job search box",
            ),
            optional=False,
            context=site.site_name,
        )
        await self._attempt(
            "Run search",
            lambda: client.click(selector="button[type='submit']", description="search"),
            optional=True,
            context=site.site_name,
        )
        html = await self._attempt(
            "Read search results",
            client.get_html,
            optional=False,
            context=site.site_name,
        )
        return html or ""

    async def _process_job(self, client: BrowserSession, job: JobListing) -> None:
        try:
            if already_sent(self.paths, job.job_id, job.url):
                self.console.info(f"Job {job.job_id} was already sent; skipping.")
                self.summary.skipped += 1
                return
            blocked = skip_list_hit(
                " ".join([job.title, job.company, job.location, job.description, job.url]),
                self.profile.skip_list,
            )
            if blocked:
                self.console.info(f"Skip list matched '{blocked}' in {job.title}; skipping.")
                self.summary.skipped += 1
                return
            if self.config.mode == "semi":
                decision = self.human.decide(f"{job.title} — {job.url}")
                if decision == "skip":
                    raise SkipJob("skipped in semi mode")
            await self._attempt(
                "Open job",
                lambda url=job.url: client.navigate(url),
                optional=False,
                context=job.title,
            )
            html = await self._attempt("Read job page", client.get_html, optional=False, context=job.title)
            html = await self._clear_wall(client, html or "", job)
            job = _with_page_details(job, html)
            outcome = await self._apply(client, job, html)
            if outcome == "submitted":
                self.summary.submitted += 1
            elif outcome == "dry_run":
                self.summary.dry_runs += 1
            else:
                self.summary.skipped += 1
        except AbortRun:
            raise
        except SkipJob as exc:
            self.console.warn(f"Skipped {job.title}: {exc}")
            self.summary.skipped += 1
        except HumanInputRequired as exc:
            self.console.warn(f"Skipped {job.title}; human input required.")
            logger.info("Human input required for %s: %s", job.job_id, exc)
            self.summary.skipped += 1
        except Exception:
            logger.exception("Job %s failed", job.job_id)
            self.console.warn(f"Job {job.job_id} failed. Details are in logs/jobhunter.log.")
            self.summary.skipped += 1

    async def _apply(self, client: BrowserSession, job: JobListing, html: str) -> str:
        if not self.specs:
            raise SkipJob("No generated_docs CVs are available")
        choice = choose_materials(
            job.description,
            self.specs,
            self.profile.interested_keywords,
            self.profile.candidate,
            job.title,
        )
        if choice is None:
            raise SkipJob("No specialization is available")
        if not is_viable(choice):
            apply_anyway = self.human.confirm(
                f"Low match ({choice.score:.0%}) for '{job.title}'. Apply with {choice.specialization.title} anyway?",
                default=False,
            )
            if not apply_anyway:
                raise SkipJob(f"match score {choice.score:.0%} is below 20%")
        spec = choice.specialization
        if choice.tailor:
            self.console.info(f"Tailoring a CV for {job.title}")
            spec = tailor_specialization(
                self.profile.candidate,
                spec,
                job,
                choice.emphasis,
                self.llm,
            )
        cv_path, letter_path = self._material_paths(spec)
        self.console.info(
            f"{job.title}: {spec.area} ({choice.score:.0%})"
        )
        mode = application_mode(html)
        if mode == "unknown":
            easy = self.human.confirm(f"Treat '{job.title}' as an Easy Apply flow?", default=True)
            mode = "easy" if easy else "external"
        if mode == "external":
            await self._attempt(
                "Open external application",
                lambda: client.click(selector="a", description="Apply on company website"),
                optional=False,
                context=job.title,
            )
        else:
            await self._attempt(
                "Easy Apply",
                lambda: client.click(selector="button", description="Easy Apply button"),
                optional=False,
                context=job.title,
            )
        form_html = await self._attempt("Read application form", client.get_html, optional=False, context=job.title)
        return await self._complete_form(client, job, form_html or "", spec, cv_path, letter_path, choice.score)

    def _material_paths(self, spec: Specialization) -> tuple[Path, Path]:
        cv_path, letter_path = (
            self.paths.specialization_dir(spec.area) / "cv.pdf",
            self.paths.specialization_dir(spec.area) / "cover_letter.pdf",
        )
        if not cv_path.is_file() or not letter_path.is_file():
            cv_path, letter_path = self._write_pdfs(spec)
        return cv_path, letter_path

    async def _complete_form(
        self,
        client: BrowserSession,
        job: JobListing,
        html: str,
        spec: Specialization,
        cv_path: Path,
        letter_path: Path,
        score: float,
        submit: bool = True,
    ) -> str:
        html = await self._clear_wall(client, html, job)
        plan = plan_form(html, self.profile)
        if plan.human_fields:
            self.console.info("Answer the remaining questions. Type SKIP to skip this job or ABORT to stop.")
        unfilled_required = 0
        for item in plan.auto_fields:
            filled = await self._attempt(
                f"Fill {item.field.label or item.field.name or 'field'}",
                lambda item=item: client.fill(
                    selector=css_selector(item.field),
                    value=item.value,
                    description=item.field.label or item.field.name or "field",
                ),
                optional=not item.field.required,
                context=job.title,
            )
            if filled is None and item.field.required:
                unfilled_required += 1
        for form_field in plan.human_fields:
            value = self._ask_for_field(job, form_field)
            self._check_sentinel(value)
            filled = await self._attempt(
                f"Fill {form_field.label or form_field.name or 'question'}",
                lambda form_field=form_field, value=value: client.fill(
                    selector=css_selector(form_field),
                    value=value,
                    description=form_field.label or form_field.name or "question",
                ),
                optional=not form_field.required,
                context=job.title,
            )
            if filled is None and form_field.required:
                unfilled_required += 1

        uploads_done = 0
        materials = [cv_path, letter_path]
        for index, form_field in enumerate(plan.file_inputs):
            material = materials[min(index, len(materials) - 1)]
            uploaded = await self._attempt(
                f"Upload {material.name}",
                lambda form_field=form_field, material=material: client.upload(
                    [material],
                    selector=css_selector(form_field),
                    description=form_field.label or material.name,
                ),
                optional=False,
                context=job.title,
            )
            if uploaded is not None:
                uploads_done += 1
        confidence = submission_confidence(
            unfilled_required=unfilled_required,
            upload_slots=len(plan.file_inputs),
            uploads_done=uploads_done,
        )
        self.console.info(f"Submission confidence for {job.title}: {confidence:.0%}")
        if not submit:
            return "filled"
        if confidence < self.config.confidence_threshold:
            approved = self.human.confirm(
                f"Confidence {confidence:.0%} is below {self.config.confidence_threshold:.0%}. Submit anyway?",
                default=False,
            )
            if not approved:
                raise SkipJob("submission confidence is below the threshold")
        if self.config.dry_run:
            self.console.info(f"Dry run: would submit {job.title}")
            return "dry_run"
        self.console.step("8", f"Submit {job.title}")
        await self._attempt(
            "Submit application",
            lambda: client.click(selector="button[type='submit']", description="Submit application"),
            optional=False,
            context=job.title,
        )
        snapshot_path, page_source = await self._capture(client, job)
        details = JobDetails(
            job_id=job.job_id,
            job_title=job.title,
            company=job.company,
            location=job.location,
            job_description=job.description[:20_000],
            source_url=job.url,
            applied_timestamp=datetime.now(timezone.utc).isoformat(),
            cv_path_used=str(cv_path.resolve()),
            cover_letter_path_used=str(letter_path.resolve()),
            match_score=score,
            specialization=spec.area,
        )
        record_application(
            self.paths,
            details,
            cv_path,
            letter_path,
            snapshot_path=snapshot_path,
            page_source=page_source,
        )
        self.console.info(f"Recorded applications/{job.job_id}/")
        return "submitted"

    def _ask_for_field(self, job: JobListing, form_field: object) -> str:
        label = getattr(form_field, "label", "") or getattr(form_field, "name", "") or "question"
        options = getattr(form_field, "options", [])
        if options:
            label = f"{label} ({', '.join(options[:8])})"
        prompt = f"{job.title}: {label}"
        if getattr(form_field, "input_type", "") == "password":
            return self.human.ask_secret(prompt)
        blob = " ".join(
            [
                getattr(form_field, "label", ""),
                getattr(form_field, "name", ""),
                getattr(form_field, "field_id", ""),
            ]
        ).lower()
        default = self.profile.targeted_income if any(
            token in blob for token in ("salary", "compensation", "pay rate", "remuneration")
        ) else None
        return self.human.ask(prompt, default=default, required=True)

    def _check_sentinel(self, value: str) -> None:
        token = value.strip().upper()
        if token == "SKIP":
            raise SkipJob("Skipped by human")
        if token == "ABORT":
            raise AbortRun("Aborted by human")

    async def _clear_wall(self, client: BrowserSession, html: str, job: JobListing) -> str:
        current_html = html
        for _ in range(3):
            plan = plan_form(current_html, self.profile)
            if not plan.blocked_reason:
                return current_html
            self.console.warn(f"{job.title}: {plan.blocked_reason} wall")
            if plan.blocked_reason == "login" and self.human.confirm(
                "Type credentials into this login form?",
                default=False,
            ):
                await self._fill_login(client, plan, job)
                await self._attempt(
                    "Submit login",
                    lambda: client.click(selector="button[type='submit']", description="Sign in"),
                    optional=False,
                    context=job.title,
                )
            else:
                decision = self.human.fallback_menu(
                    reason=(
                        f"The page looks like a {plan.blocked_reason} wall. "
                        "Finish it in the open Chrome window, then retry."
                    ),
                    context=job.title,
                )
                if decision.action == "abort":
                    raise AbortRun(plan.blocked_reason or "blocked page")
                if decision.action != "retry":
                    raise SkipJob(plan.blocked_reason or "blocked page")
            refreshed = await self._attempt(
                "Re-read page",
                client.get_html,
                optional=False,
                context=job.title,
            )
            current_html = refreshed or ""
        raise SkipJob("The page stayed blocked")

    async def _fill_login(self, client: BrowserSession, plan: object, job: JobListing) -> None:
        fields = [*getattr(plan, "auto_fields", []), *getattr(plan, "human_fields", [])]
        for item in fields:
            form_field = getattr(item, "field", item)
            value = getattr(item, "value", None)
            if not isinstance(value, str) or not value:
                value = self._ask_for_field(job, form_field)
                self._check_sentinel(value)
            await self._attempt(
                f"Fill {getattr(form_field, 'label', '') or 'login field'}",
                lambda form_field=form_field, value=value: client.fill(
                    selector=css_selector(form_field),
                    value=value,
                    description=getattr(form_field, "label", "") or "login field",
                ),
                optional=False,
                context=job.title,
            )

    async def _capture(self, client: BrowserSession, job: JobListing) -> tuple[Path | None, str | None]:
        folder = self.paths.application_dir(job.job_id)
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = folder / "snapshot.png"
        try:
            await client.screenshot(snapshot)
            if snapshot.is_file() and snapshot.stat().st_size > 0:
                return snapshot, None
        except ChromeInteractionError:
            logger.exception("Screenshot failed for %s", job.job_id)
        try:
            html = await client.get_html()
        except ChromeInteractionError:
            html = ""
        if html.strip():
            self.console.warn(f"Saved page source for {job.job_id} because the screenshot failed.")
            return None, html
        await self._attempt(
            "Capture completion page",
            lambda: client.screenshot(snapshot),
            optional=False,
            context=job.title,
        )
        return snapshot, None

    async def _attempt(
        self,
        label: str,
        action: Callable[[], Awaitable[T]],
        *,
        optional: bool,
        context: str,
    ) -> T | None:
        self.console.info(f"{context}: {label}" if context else label)
        last_error: Exception | None = None
        for _ in range(5):
            try:
                return await action()
            except ChromeInteractionError as exc:
                last_error = exc
                logger.exception("Browser action failed: %s", label)
                if optional:
                    self.console.warn(f"{label} failed; continuing.")
                    return None
                try:
                    decision = self.human.fallback_menu(reason=f"{label}: {exc}", context=context)
                except HumanInputRequired as human_exc:
                    if optional:
                        self.console.warn(f"{label} needs a person; continuing.")
                        return None
                    raise SkipJob(str(human_exc)) from human_exc
                if decision.action == "retry":
                    continue
                if decision.action == "abort":
                    raise AbortRun(label)
                if decision.action == "skip" or not optional:
                    raise SkipJob(label)
                self.console.warn(f"Continuing without {label}.")
                return None
        raise SkipJob(f"{label} failed after retries: {last_error}")

    def _display(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.paths.root))
        except ValueError:
            return str(path)


def _search_queries(profile: TargetProfile, specs: list[Specialization]) -> list[str]:
    queries = [keyword.strip() for keyword in profile.interested_keywords if keyword.strip()]
    if not queries:
        queries = [spec.title for spec in specs if spec.title]
    return queries[:5] or ["software engineer"]


def _job_from_open_page(html: str) -> JobListing:
    title = extract_heading(html).strip()
    text = html_to_text(html)
    if not title:
        title = text.split("\n", 1)[0].strip()[:120] or "Open job"
    return JobListing(
        job_id=slugify(title, fallback="open-job", limit=60),
        title=title,
        url="open-page",
        description=text,
    )


def _location_selector(site: Site) -> str:
    param = site.location_filter_param.strip()
    if param and all(char.isalnum() or char in {"_", "-"} for char in param):
        return f"input[name='{param}']"
    return "input[name='location']"


def _listings_from_paste(raw: str, site: Site) -> list[JobListing]:
    listings: list[JobListing] = []
    for piece in raw.split(","):
        url = piece.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            continue
        listings.append(
            JobListing(
                job_id=job_id_from_url(url),
                title="Untitled role",
                url=url,
                source_site=site.site_name,
            )
        )
    return listings


def _with_page_details(job: JobListing, html: str) -> JobListing:
    heading = extract_heading(html)
    description = html_to_text(html)
    updates: dict[str, str] = {"description": description}
    if heading:
        updates["title"] = heading
    return job.model_copy(update=updates)
