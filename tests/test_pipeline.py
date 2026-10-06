"""Pipeline tests that do not open a live browser."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.exceptions import ChromeInteractionError, DocsMissing
from src.generator.content import tailor_specialization
from src.generator.heuristic import synthesize
from src.generator.llm import LLMClient
from src.generator.pdf_builder import write_cv
from src.mcp_client.chrome_bridge import (
    _ChromeSession,
    _unwrap_eval,
    adapt_arguments,
    choose_focused_tab,
    file_input_script,
    is_playwright_status_page,
    parse_browser_tabs,
    snapshot_target,
)
from src.models import CandidateBaseline, JobListing, Specialization, TargetProfile
from src.parsers.doc_parser import DocParser, load_sites
from src.pipeline.application_runner import ApplicationRunner, RunConfig
from src.pipeline.matching import MatchAssessment, assess_match, submission_confidence
from src.pipeline.page_extract import extract_listings, plan_form
from src.cli.human_input import HumanInput


SAMPLE_CV = """
Ada Lovelace
ada@example.com
+852 5555 1234

Summary
Engineer focused on Python services and LLM agents.

Skills
Python, TypeScript, React, SQL, Docker, LangChain

Experience
Software Engineer, Analytical Engines, 2020-2024
- Built Python APIs used by research teams
- Shipped an LLM agent that triages support tickets

Education
BSc Mathematics, University of London

Achievements
- Reduced batch runtime by 40%
""".strip()


SEARCH_HTML = """
<html><body>
<a href="https://www.jobsdb.com/job/12345-python-engineer">Python Engineer</a>
</body></html>
"""

JOB_HTML = """
<html><body>
<h1>Python Engineer</h1>
<p>We need Python, SQL, and Docker for backend APIs. Easy Apply.</p>
</body></html>
"""

FORM_HTML = """
<html><body>
<form>
<label for="email">Email</label>
<input id="email" name="email" type="email" required>
<input id="resume" name="resume" type="file">
<button type="submit">Submit application</button>
</form>
</body></html>
"""


class ScriptedBrowser:
    def __init__(self) -> None:
        self.mode = "search"
        self.events: list[tuple[str, ...]] = []

    async def navigate(self, url: str) -> str:
        self.events.append(("navigate", url))
        self.mode = "job" if "/job/" in url else "search"
        return "ok"

    async def get_html(self) -> str:
        pages = {
            "search": SEARCH_HTML,
            "job": JOB_HTML,
            "form": FORM_HTML,
            "done": "<html><body><h1>Application submitted</h1></body></html>",
        }
        return pages[self.mode]

    async def click(self, *, selector: str = "", description: str = "") -> str:
        self.events.append(("click", description, selector))
        label = description.lower()
        if "easy apply" in label:
            self.mode = "form"
        elif "submit" in label:
            self.mode = "done"
        return "ok"

    async def fill(self, *, selector: str = "", value: str, description: str = "") -> str:
        self.events.append(("fill", description, selector, value))
        return "ok"

    async def upload(self, paths: list[Path], *, selector: str = "", description: str = "") -> str:
        self.events.append(("upload", description, selector, ",".join(str(path) for path in paths)))
        return "ok"

    async def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        self.events.append(("screenshot", str(path)))
        return path


class FakeBridge:
    def __init__(self, client: ScriptedBrowser) -> None:
        self.configured = True
        self.client = client

    def session(self):  # type: ignore[no-untyped-def]
        client = self.client

        class _Session:
            async def __aenter__(self) -> ScriptedBrowser:
                return client

            async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
                return None

        return _Session()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_sites_csv_and_bare_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "sites.csv"
            csv_path.write_text(
                "site_name,url,location_filter_param\nJobsDB,https://www.jobsdb.com,location\n",
                encoding="utf-8",
            )
            sites = load_sites(csv_path)
            self.assertEqual(sites[0].site_name, "JobsDB")
            self.assertEqual(sites[0].location_filter_param, "location")

            bare = root / "bare.csv"
            bare.write_text("https://www.jobsdb.com\n", encoding="utf-8")
            bare_sites = load_sites(bare)
            self.assertEqual(bare_sites[0].url, "https://www.jobsdb.com")

    def test_heuristic_baseline_uses_source_text(self) -> None:
        baseline = synthesize_sample()
        self.assertEqual(baseline.name, "Ada Lovelace")
        self.assertEqual(baseline.email, "ada@example.com")
        self.assertIn("5555", baseline.phone)
        self.assertNotIn("2020", baseline.phone)
        self.assertIn("Python", baseline.skills)
        self.assertTrue(baseline.experience)
        self.assertTrue(baseline.experience[0].bullets)

    def test_salary_question_stops_for_a_person(self) -> None:
        profile = TargetProfile(
            targeted_income="$120,000 USD / yr",
            targeted_location="Remote / Hong Kong",
            interested_keywords=["Python"],
            candidate=CandidateBaseline(name="Ada Lovelace", email="ada@example.com"),
        )
        html = """
        <label for="salary">Salary Expectations</label>
        <input id="salary" name="salary" type="text" required>
        <label for="email">Email</label>
        <input id="email" name="email" type="email" required>
        <input type="file" name="resume">
        """
        plan = plan_form(html, profile)
        self.assertTrue(any("salary" in field.label.lower() for field in plan.human_fields))
        self.assertEqual(plan.auto_fields[0].value, "ada@example.com")
        self.assertEqual(len(plan.file_inputs), 1)
        self.assertIsNone(plan.blocked_reason)

    def test_listing_extraction(self) -> None:
        listings = extract_listings(SEARCH_HTML, "https://www.jobsdb.com", "JobsDB")
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].job_id, "12345_python_engineer")
        self.assertEqual(listings[0].title, "Python Engineer")

    def test_confidence_drops_without_an_upload(self) -> None:
        self.assertLess(submission_confidence(unfilled_required=0, upload_slots=0, uploads_done=0), 0.8)
        self.assertEqual(submission_confidence(unfilled_required=0, upload_slots=1, uploads_done=1), 1.0)
        self.assertLess(submission_confidence(unfilled_required=1, upload_slots=1, uploads_done=1), 0.8)

    def test_tailor_does_not_invent_skills(self) -> None:
        baseline = CandidateBaseline(
            name="Ada Lovelace",
            skills=["Python"],
            summary="Python services",
            raw_text="Python services",
        )
        spec = Specialization(
            area="software_engineering",
            title="Software Engineer",
            summary="Python services",
            skills=["Python"],
        )
        job = JobListing(
            job_id="42",
            title="Platform Engineer",
            url="https://example.com/job/42",
            description="Kubernetes and Python",
        )
        tailored = tailor_specialization(baseline, spec, job, ["Kubernetes", "Python"], LLMClient(api_key=""))
        self.assertNotIn("Kubernetes", tailored.skills)
        self.assertEqual(tailored.area, "custom_42")

    def test_pdf_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=synthesize_sample(),
            )
            spec = Specialization(
                area="software_engineering",
                title="Software Engineer",
                summary=profile.candidate.summary,
                skills=["Python", "SQL"],
                highlights=["Reduced batch runtime by 40%"],
                cover_letter="Dear Hiring Manager,\n\nI am applying.\n\nSincerely,\nAda Lovelace",
            )
            pdf_path = write_cv(root / "cv.pdf", profile, spec)
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF"))
            docs = root / "docs"
            docs.mkdir()
            target = docs / "master_cv.pdf"
            target.write_bytes(pdf_path.read_bytes())
            parsed = DocParser(docs).scan()
            self.assertEqual(len(parsed), 1)
            self.assertIn("Ada Lovelace", parsed[0].text)

    def test_snapshot_target_picks_jobsdb_fields(self) -> None:
        snapshot = """
        - combobox "What" [ref=e12]
        - combobox "Where" [ref=e18]
        - button "SEEK" [ref=e21]
        - link "Python Engineer" [ref=e40]
        """
        self.assertEqual(snapshot_target(snapshot, "location search"), "e18")
        self.assertEqual(snapshot_target(snapshot, "job search box"), "e12")
        self.assertEqual(snapshot_target(snapshot, "search"), "e21")
        self.assertEqual(snapshot_target(snapshot, "Easy Apply button"), None)

    def test_playwright_type_maps_selector_to_target(self) -> None:
        payload = adapt_arguments(
            {
                "type": "object",
                "properties": {
                    "element": {"type": "string"},
                    "target": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["target", "text"],
            },
            {
                "selector": "input[name='location']",
                "value": "Remote / Hong Kong",
                "description": "location search",
            },
        )
        self.assertEqual(payload["target"], "input[name='location']")
        self.assertEqual(payload["text"], "Remote / Hong Kong")
        self.assertEqual(payload["element"], "location search")

    def test_tool_schema_requires_missing_fields(self) -> None:
        with self.assertRaises(ChromeInteractionError):
            adapt_arguments(
                {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string"},
                        "element": {"type": "string"},
                    },
                    "required": ["ref", "element"],
                },
                {"description": "Easy Apply button", "selector": "button"},
            )

    async def test_missing_documents_stop_a_noninteractive_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = ApplicationRunner(
                RunConfig(root=root, through="documents", noninteractive=True),
                human=HumanInput(noninteractive=True),
                llm=LLMClient(api_key=""),
            )
            with self.assertRaises(DocsMissing):
                await runner.run()

    async def test_apply_one_job_and_skip_the_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            (docs / "master_cv.txt").write_text(SAMPLE_CV, encoding="utf-8")
            (root / "sites.csv").write_text(
                "site_name,url,location_filter_param\nJobsDB,https://www.jobsdb.com,location\n",
                encoding="utf-8",
            )
            first = ScriptedBrowser()
            runner = _runner(root, first)
            summary = await runner.run()
            self.assertEqual(summary.submitted, 1)
            self.assertGreaterEqual(summary.documents_parsed, 1)
            self.assertGreaterEqual(summary.pdf_count, 2)
            record = root / "applications" / "12345_python_engineer"
            self.assertTrue((record / "job_details.json").is_file())
            self.assertTrue((record / "cv_used.pdf").read_bytes().startswith(b"%PDF"))
            self.assertTrue((record / "cover_letter_used.pdf").read_bytes().startswith(b"%PDF"))
            self.assertTrue((record / "snapshot.png").read_bytes().startswith(b"\x89PNG"))
            index = json.loads((root / "applications" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["jobs"][0]["job_id"], "12345_python_engineer")
            self.assertEqual(index["jobs"][0]["source_url"], "https://www.jobsdb.com/job/12345-python-engineer")
            self.assertTrue(any(event[0] == "upload" for event in first.events))
            self.assertTrue(
                any(event[0] == "click" and "submit" in event[1].lower() for event in first.events)
            )
            self.assertTrue(any(event[0] == "fill" and "ada@example.com" in event for event in first.events))

            second = ScriptedBrowser()
            again = await _runner(root, second).run()
            self.assertEqual(again.submitted, 0)
            self.assertGreaterEqual(again.skipped, 1)
            self.assertFalse(
                any(event[0] == "navigate" and "/job/" in event[1] for event in second.events)
            )


    async def test_skip_documents_uses_saved_cvs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=CandidateBaseline(name="Stored Candidate", email="stored@example.com", skills=["Python"]),
                specializations=["software_engineer"],
            )
            generated = root / "generated_docs"
            folder = generated / "software_engineer"
            folder.mkdir(parents=True)
            (folder / "cv.pdf").write_bytes(b"%PDF-1.4\n")
            (folder / "cover_letter.pdf").write_bytes(b"%PDF-1.4\n")
            profile_path = generated / "profile.json"
            profile_path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
            before = profile_path.read_text(encoding="utf-8")
            browser = ScriptedBrowser()
            runner = ApplicationRunner(
                RunConfig(root=root, through="apply", noninteractive=True, skip_documents=True, max_per_site=5),
                human=HumanInput(noninteractive=True),
                bridge=FakeBridge(browser),
                llm=LLMClient(api_key=""),
            )
            summary = await runner.run()
            self.assertEqual(summary.documents_parsed, 0)
            self.assertEqual(summary.specializations, ["software_engineer"])
            self.assertEqual(profile_path.read_text(encoding="utf-8"), before)
            self.assertEqual(summary.submitted, 1)

    async def test_standby_recommends_then_fills_without_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=CandidateBaseline(name="Stored Candidate", email="stored@example.com", skills=["Python"]),
                specializations=["software_engineer"],
            )
            generated = root / "generated_docs"
            folder = generated / "software_engineer"
            folder.mkdir(parents=True)
            (folder / "cv.pdf").write_bytes(b"%PDF-1.4\n")
            (folder / "cover_letter.pdf").write_bytes(b"%PDF-1.4\n")
            (generated / "profile.json").write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
            class _JobReady(ScriptedBrowser):
                async def navigate(self, url: str) -> str:
                    self.events.append(("navigate", url))
                    self.mode = "job"
                    return "ok"

            browser = _JobReady()
            answers = iter(("1", "0", "a", "q"))

            class _Script(HumanInput):
                def ask(self, prompt: str, *, default: str | None = None, required: bool = False) -> str:
                    return next(answers)

            runner = ApplicationRunner(
                RunConfig(root=root, noninteractive=False),
                human=_Script(noninteractive=False),
                bridge=FakeBridge(browser),
                llm=LLMClient(api_key=""),
            )
            await runner.standby()
            self.assertEqual(browser.events[0][:2], ("navigate", "https://www.jobsdb.com"))
            self.assertFalse(any(event[0] == "click" and "submit" in event[1].lower() for event in browser.events))
            self.assertTrue(any(event[0] == "click" and "easy apply" in event[1].lower() for event in browser.events))
            self.assertTrue(any(event[0] == "fill" and "stored@example.com" in event for event in browser.events))
            self.assertFalse(any(path.name.startswith("extra_") for path in generated.iterdir() if path.is_dir()))

    async def test_standby_review_can_create_a_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=CandidateBaseline(name="Stored Candidate", email="stored@example.com", skills=["Python"]),
                specializations=["software_engineer"],
            )
            generated = root / "generated_docs"
            folder = generated / "software_engineer"
            folder.mkdir(parents=True)
            (folder / "cv.pdf").write_bytes(b"%PDF-1.4\n")
            (folder / "cover_letter.pdf").write_bytes(b"%PDF-1.4\n")
            (generated / "profile.json").write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")

            class _JobReady(ScriptedBrowser):
                async def navigate(self, url: str) -> str:
                    self.events.append(("navigate", url))
                    self.mode = "job"
                    return "ok"

            browser = _JobReady()
            answers = iter(("1", "1", "q"))

            class _Script(HumanInput):
                def ask(self, prompt: str, *, default: str | None = None, required: bool = False) -> str:
                    return next(answers)

            runner = ApplicationRunner(
                RunConfig(root=root, noninteractive=False),
                human=_Script(noninteractive=False),
                bridge=FakeBridge(browser),
                llm=LLMClient(api_key=""),
            )
            await runner.standby()
            created = generated / "python_engineer"
            self.assertTrue((created / "cv.pdf").is_file())
            self.assertTrue((created / "cover_letter.pdf").is_file())
            self.assertFalse(any(event[0] == "click" and "submit" in event[1].lower() for event in browser.events))

    def test_standby_uses_jobsdb_tab_not_playwright_welcome(self) -> None:
        listing = "\n".join(
            [
                '- 0: (current) [Welcome](chrome-extension://abc/connect.html)',
                '- 1: [Python Engineer](https://hk.jobsdb.com/job/12345)',
            ]
        )
        tabs = parse_browser_tabs(listing)
        self.assertIsNone(choose_focused_tab(tabs))
        focused_job = choose_focused_tab(tabs, "https://hk.jobsdb.com/job/12345")
        assert focused_job is not None
        self.assertEqual(focused_job.index, 1)
        self.assertEqual(focused_job.url, "https://hk.jobsdb.com/job/12345")
        home_and_job = parse_browser_tabs(
            "\n".join(
                [
                    "- 0: (current) [JobsDB](https://hk.jobsdb.com/)",
                    "- 1: [Python Engineer](https://hk.jobsdb.com/job/12345)",
                ]
            )
        )
        post = choose_focused_tab(home_and_job, "https://hk.jobsdb.com/job/12345")
        assert post is not None
        self.assertEqual(post.index, 1)
        home = choose_focused_tab(home_and_job, "https://hk.jobsdb.com/")
        assert home is not None
        self.assertEqual(home.index, 0)
        two_jobs = parse_browser_tabs(
            "\n".join(
                [
                    "- 0: (current) [Older role](https://hk.jobsdb.com/job/111)",
                    "- 1: [The role in front](https://hk.jobsdb.com/job/999)",
                ]
            )
        )
        front = choose_focused_tab(two_jobs, "https://hk.jobsdb.com/job/999")
        assert front is not None
        self.assertEqual(front.index, 1)
        raw = (
            '" \\n Welcome \\n \\"mcp\\" connected. \\n\\n " ### Ran Playwright code '
            "```js await page.evaluate('() => document.documentElement.outerHTML');"
        )
        self.assertNotIn("Ran Playwright", _unwrap_eval(raw))
        wrapped = """### Result
"<!DOCTYPE html><html><body><h1>Python Engineer</h1><p>We need Python. Easy Apply.</p></body></html>"
### Ran Playwright code
```js
await page.evaluate('() => document.documentElement.outerHTML');
```"""
        page = _unwrap_eval(wrapped)
        self.assertIn("<h1>Python Engineer</h1>", page)
        self.assertNotIn("page.evaluate", page)
        self.assertFalse(is_playwright_status_page(page))

    async def test_standby_does_not_analyze_the_welcome_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=CandidateBaseline(name="Stored Candidate", email="stored@example.com", skills=["Python"]),
                specializations=["software_engineer"],
            )
            generated = root / "generated_docs"
            folder = generated / "software_engineer"
            folder.mkdir(parents=True)
            (folder / "cv.pdf").write_bytes(b"%PDF-1.4\n")
            (folder / "cover_letter.pdf").write_bytes(b"%PDF-1.4\n")
            (generated / "profile.json").write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")

            class _Welcome(ScriptedBrowser):
                async def focus_site(self, site_urls: list[str]) -> str:
                    self.events.append(("focus", site_urls[0]))
                    raise ChromeInteractionError("No open tab is on https://www.jobsdb.com")

            browser = _Welcome()
            answers = iter(("1", "q"))

            class _Script(HumanInput):
                def ask(self, prompt: str, *, default: str | None = None, required: bool = False) -> str:
                    return next(answers)

            runner = ApplicationRunner(
                RunConfig(root=root, noninteractive=False),
                human=_Script(noninteractive=False),
                bridge=FakeBridge(browser),
                llm=LLMClient(api_key=""),
            )
            await runner.standby()
            self.assertEqual(browser.events[0][:2], ("navigate", "https://www.jobsdb.com"))
            self.assertEqual(browser.events[1][0], "focus")
            self.assertFalse(any(path.name.startswith("extra_") for path in generated.iterdir()))

    def test_revise_writes_only_missing_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=CandidateBaseline(name="Stored Candidate", email="stored@example.com", skills=["Python"]),
                specializations=["software_engineer"],
                extra_job_titles=["Data Lead"],
            )
            generated = root / "generated_docs"
            folder = generated / "software_engineer"
            folder.mkdir(parents=True)
            (folder / "cv.pdf").write_bytes(b"%PDF-1.4 existing\n")
            (folder / "cover_letter.pdf").write_bytes(b"%PDF-1.4 existing\n")
            (generated / "profile.json").write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
            runner = ApplicationRunner(
                RunConfig(root=root, noninteractive=True),
                human=HumanInput(noninteractive=True),
                llm=LLMClient(api_key=""),
            )
            runner.save_profile_fields(
                income="$120,000 USD / yr",
                location="Hong Kong",
                keywords=["Python"],
                skip_list=["intern"],
                extra_job_titles=["Data Lead"],
            )
            written = runner.generate_missing_documents()
            self.assertEqual(written, ["data_lead"])
            self.assertEqual((folder / "cv.pdf").read_bytes(), b"%PDF-1.4 existing\n")
            self.assertTrue((generated / "data_lead" / "cv.pdf").is_file())
            self.assertTrue((generated / "data_lead" / "cover_letter.pdf").is_file())
            saved = json.loads((generated / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["targeted_location"], "Hong Kong")
            self.assertEqual(saved["skip_list"], ["intern"])

    def test_window_home_has_the_three_actions(self) -> None:
        import tkinter as tk

        from src.gui.app import JobHunterApp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            window = tk.Tk()
            window.withdraw()
            app = JobHunterApp(root, window=window)
            labels: list[str] = []

            def _texts(widget: tk.Misc) -> None:
                if isinstance(widget, tk.Label):
                    labels.append(str(widget.cget("text")))
                for child in widget.winfo_children():
                    _texts(child)

            _texts(app.body)
            window.update_idletasks()
            app.close()
            self.assertTrue(any("Analyze CV" in label for label in labels))
            self.assertTrue(any("Revise profile.json" in label for label in labels))
            self.assertTrue(any("Open site" in label for label in labels))

    def test_buttons_are_disabled_while_busy(self) -> None:
        import tkinter as tk

        from src.gui.app import _DISABLED_INK, _INK, JobHunterApp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            window = tk.Tk()
            window.withdraw()
            app = JobHunterApp(root, window=window)
            label = app._buttons[0]["label"]
            assert isinstance(label, tk.Label)
            self.assertEqual(str(label.cget("fg")), _INK)
            self.assertEqual(str(label.cget("cursor")), "hand2")
            app._set_busy(True)
            self.assertEqual(str(label.cget("fg")), _DISABLED_INK)
            self.assertEqual(str(label.cget("cursor")), "")
            app._set_busy(False)
            self.assertEqual(str(label.cget("fg")), _INK)
            self.assertEqual(str(label.cget("cursor")), "hand2")
            app.close()

    def test_generated_cv_keeps_the_master_layout(self) -> None:
        from pypdf import PdfReader

        from src.generator.pdf_builder import write_cv

        master = """
LAU, Hon Hei (Hilson)
Hong Kong | +852 90221206 | hilsaga@gmail.com
Languages: Native Cantonese and Mandarin, Fluent English
AI Director and Architect | Technical Project Management

PROFESSIONAL SUMMARY
Original summary stays unless a role summary replaces it.

KEYWORDS
Alpha, Beta, Gamma

CORE COMPETENCIES & TECHNICAL STACK
Agentic AI, MCP
Python, SQL

PROFESSIONAL EXPERIENCE
Garlican Tech Limited | Hong Kong  -  Director (Nov 2019 - Present)
- Built the trading platform.
KPMG China | Hong Kong  -  Assistant Manager (Dec 2015 - Apr 2016)
- Led business analysis for clients.

EDUCATION
Master of Science (M.S.) in Statistics | West Virginia University, USA (Aug 2004 - May 2009)
""".strip()
        profile = TargetProfile(
            targeted_income="$120,000 USD / yr",
            targeted_location="Remote / Hong Kong",
            interested_keywords=["Python"],
            candidate=CandidateBaseline(name="LAU, Hon Hei (Hilson)", email="hilsaga@gmail.com"),
        )
        spec = Specialization(
            area="ai_engineer",
            title="AI Engineer",
            summary="Tailored summary for this role.",
            skills=["Python"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cv.pdf"
            write_cv(path, profile, spec, master_text=master)
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        self.assertIn("AI Engineer", text)
        self.assertIn("Tailored summary for this role.", text)
        self.assertIn("Garlican", text)
        self.assertIn("Alpha", text)
        self.assertIn("Built the trading platform, toward AI Engineer work.", text)
        self.assertIn("Led business analysis for clients.", text)
        self.assertNotIn("Led business analysis for clients, toward AI Engineer work.", text)

    def test_extra_job_title_writes_cv_and_cover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            runner = ApplicationRunner(
                RunConfig(root=root, through="documents", noninteractive=True),
                human=HumanInput(noninteractive=True),
                llm=LLMClient(api_key=""),
            )
            pairs = runner.write_extra_job_title("Forward Deployed Engineer")
            cv_path, letter_path = pairs[0]
            self.assertTrue(cv_path.read_bytes().startswith(b"%PDF"))
            self.assertTrue(letter_path.read_bytes().startswith(b"%PDF"))
            self.assertIn("forward_deployed_engineer", cv_path.parts)
            self.assertNotIn("extra_forward_deployed_engineer", cv_path.parts)
            saved = json.loads((root / "generated_docs" / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["extra_job_titles"], ["Forward Deployed Engineer"])
            self.assertNotIn("extra_job_title", saved)
            runner.write_extra_job_title("AI Product Director")
            saved = json.loads((root / "generated_docs" / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(
                saved["extra_job_titles"],
                ["Forward Deployed Engineer", "AI Product Director"],
            )
            again = runner.write_extra_job_title(None)
            self.assertEqual(len(again), 2)
            self.assertEqual(again[0][0], cv_path)
            self.assertIn("ai_product_director", again[1][0].parts)

    def test_customize_writes_a_pair_from_the_best_saved_cv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                candidate=CandidateBaseline(name="Stored Candidate", email="stored@example.com", skills=["Python"]),
                specializations=["full_stack_development", "product_manager"],
            )
            generated = root / "generated_docs"
            for name in ("full_stack_development", "product_manager"):
                folder = generated / name
                folder.mkdir(parents=True)
                (folder / "cv.pdf").write_bytes(b"%PDF-1.4 existing\n")
                (folder / "cover_letter.pdf").write_bytes(b"%PDF-1.4 existing\n")
            (generated / "profile.json").write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
            runner = ApplicationRunner(
                RunConfig(root=root, noninteractive=True),
                human=HumanInput(noninteractive=True),
                llm=LLMClient(api_key=""),
            )
            job = JobListing(
                job_id="product-manager",
                title="Product Manager",
                company="Example Co",
                url="https://hk.jobsdb.com/jobs?jobId=95057500",
                description="Product Manager for a trading platform.",
            )
            spec = runner.customize_for_job(job)
            self.assertEqual(spec.area, "custom_95057500")
            self.assertEqual(spec.title, "Product Manager")
            custom = generated / "custom_95057500"
            self.assertTrue((custom / "cv.pdf").read_bytes().startswith(b"%PDF"))
            self.assertTrue((custom / "cover_letter.pdf").read_bytes().startswith(b"%PDF"))
            self.assertEqual((generated / "full_stack_development" / "cv.pdf").read_bytes(), b"%PDF-1.4 existing\n")
            self.assertEqual((generated / "product_manager" / "cv.pdf").read_bytes(), b"%PDF-1.4 existing\n")
            report = assess_match(
                job.description,
                [spec],
                profile.interested_keywords,
                profile.candidate,
                job.title,
            )
            self.assertEqual(report.closest, "Product Manager")
            self.assertGreaterEqual(report.match, 0)
            self.assertLessEqual(report.match, 100)

    def test_extra_cv_skills_do_not_wipe_out_a_real_overlap(self) -> None:
        spec = Specialization(
            area="ai_engineer",
            title="AI Engineer",
            summary="Builds agentic systems.",
            skills=[
                "Python",
                "React",
                "SQL",
                "Management",
                "Planning",
                "Operations",
                "Roadmap",
                "Agile",
                "SDLC",
                "UAT",
                "Stakeholder",
                "Deployment",
            ],
        )
        report = assess_match(
            "We need an AI Engineer who knows Python and React.",
            [spec],
            ["Python", "Full Stack", "AI Engineer"],
            CandidateBaseline(name="Ada", skills=["Python", "React"]),
            "AI Engineer",
        )
        self.assertEqual(report.match, 38)
        self.assertEqual(report.proceed, 38)
        self.assertIn("This job matches 3 of your keywords.", report.reason)
        self.assertIn("8 or more is 100/100, so this is 38/100.", report.reason)
        self.assertIn("Matched: AI Engineer, Python, React.", report.reason)
        self.assertNotIn("Full Stack", report.reason.split("Matched:", 1)[-1].split("The job also", 1)[0])
        self.assertIn("Job title shares 1 word with this CV: ai.", report.reason)

    def test_eight_job_keywords_is_a_full_score(self) -> None:
        spec = Specialization(
            area="ai_engineer",
            title="AI Engineer",
            summary="",
            skills=["Python", "React", "SQL", "TypeScript", "FastAPI", "AWS", "Docker", "Kubernetes", "Go"],
        )
        job = "Python React SQL TypeScript FastAPI AWS Docker Kubernetes and also Go and Java."
        report = assess_match(job, [spec], ["Python"], CandidateBaseline(name="Ada", skills=["Python"]), "Backend Engineer")
        self.assertEqual(report.match, 100)
        self.assertIn("This job matches 9 of your keywords.", report.reason)
        self.assertIn("The job also asks for: Java.", report.reason)

    def test_analysis_screen_has_customize_above_the_cv_row(self) -> None:
        import tkinter as tk

        from src.gui.app import JobHunterApp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            window = tk.Tk()
            window.withdraw()
            app = JobHunterApp(root, window=window)
            app.job = JobListing(
                job_id="python-engineer",
                title="Python Engineer",
                url="https://hk.jobsdb.com/job/12345",
                description="Python services.",
            )
            app.report = MatchAssessment(80, 70, "Closest saved CV is Software Engineer.", "Software Engineer")
            app.show_analysis()
            labels: list[str] = []

            def _texts(widget: tk.Misc) -> None:
                if isinstance(widget, tk.Label):
                    labels.append(str(widget.cget("text")))
                for child in widget.winfo_children():
                    _texts(child)

            _texts(app.body)
            window.update_idletasks()
            app.close()
            self.assertLess(labels.index("Customize"), labels.index("Open folder"))
            self.assertLess(labels.index("Open folder"), labels.index("Go back"))

    def test_playwright_stays_connected_and_can_upload_the_cv(self) -> None:
        import tkinter as tk

        from src.gui.app import JobHunterApp

        class _Session:
            def __init__(self) -> None:
                self.events: list[str] = []

            async def __aenter__(self) -> _Session:
                self.events.append("enter")
                return self

            async def __aexit__(self, *_args: object) -> None:
                self.events.append("exit")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            window = tk.Tk()
            window.withdraw()
            app = JobHunterApp(root, window=window)
            session = _Session()
            app.runner._attached_browser = lambda: session  # type: ignore[method-assign]
            future = asyncio.run_coroutine_threadsafe(app._connect(), app.loop)
            future.result(timeout=5)
            self.assertIs(app.client, session)
            self.assertEqual(session.events, ["enter"])
            app.job = JobListing(
                job_id="python-engineer",
                title="Python Engineer",
                url="https://hk.jobsdb.com/job/12345",
                description="Python services.",
            )
            app.report = MatchAssessment(80, 70, "Closest saved CV is Software Engineer.", "Software Engineer")
            app.show_analysis()
            labels: list[str] = []

            def _texts(widget: tk.Misc) -> None:
                if isinstance(widget, tk.Label):
                    labels.append(str(widget.cget("text")))
                for child in widget.winfo_children():
                    _texts(child)

            _texts(app.body)
            window.update_idletasks()
            app.close()
            self.assertLess(labels.index("Customize"), labels.index("Choose file to upload"))
            self.assertLess(labels.index("Choose file to upload"), labels.index("Open folder"))

    def test_upload_sends_the_file_the_person_picks(self) -> None:
        import tkinter as tk
        from unittest.mock import patch

        from src.gui.app import JobHunterApp

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            chosen = root / "docs" / "cv.pdf"
            chosen.write_bytes(b"%PDF-1.4 mine\n")
            window = tk.Tk()
            window.withdraw()
            app = JobHunterApp(root, window=window)
            app.client = object()
            with patch("src.gui.app.filedialog.askopenfilename", return_value=str(chosen)):
                picked = app._choose_upload_file()
            self.assertEqual(picked, chosen)
            future = asyncio.run_coroutine_threadsafe(app._close_browser(), app.loop)
            future.result(timeout=5)
            self.assertIsNone(app.client)
            with (
                patch("src.gui.app.subprocess.run") as copied,
                patch("src.gui.app.subprocess.Popen") as revealed,
            ):
                app._stage_upload(chosen)
            self.assertEqual(copied.call_args.args[0], ["pbcopy"])
            self.assertEqual(revealed.call_args.args[0][:2], ["open", "-R"])
            with patch("src.gui.app.filedialog.askopenfilename", return_value=""):
                self.assertIsNone(app._choose_upload_file())
            app.close()

    async def test_upload_places_the_file_on_the_page_field(self) -> None:
        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name
                self.inputSchema = {"type": "object", "properties": {"code": {}, "paths": {}}}

        class _Block:
            def __init__(self, text: str) -> None:
                self.text = text

        class _Result:
            def __init__(self, text: str, *, error: bool = False) -> None:
                self.content = [_Block(text)]
                self.isError = error

        class _Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def call_tool(self, name: str, payload: dict[str, object]) -> _Result:
                self.calls.append((name, dict(payload)))
                if name == "browser_file_upload":
                    return _Result("No file chooser visible", error=True)
                return _Result('### Result\n"set:cv.pdf"')

        with tempfile.TemporaryDirectory() as tmp:
            chosen = Path(tmp) / "cv.pdf"
            chosen.write_bytes(b"%PDF-1.4 mine\n")
            script = file_input_script(chosen)
            self.assertIn("input[type=\"file\"]", script)
            self.assertIn("cv.pdf", script)
            self.assertIn("application/pdf", script)
            browser = _Client()
            session = _ChromeSession(
                browser,
                [_Tool("browser_file_upload"), _Tool("browser_run_code_unsafe")],
                timeout=5,
            )
            placed = await session.upload([chosen])
            self.assertEqual(placed, "set:cv.pdf")
            self.assertEqual(browser.calls[0][0], "browser_file_upload")
            self.assertNotIn("paths", browser.calls[0][1])
            self.assertEqual(browser.calls[1][0], "browser_run_code_unsafe")
            self.assertIn("cv.pdf", str(browser.calls[1][1]["code"]))

    async def test_skip_list_blocks_a_matching_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            profile = TargetProfile(
                targeted_income="$120,000 USD / yr",
                targeted_location="Remote / Hong Kong",
                interested_keywords=["Python"],
                skip_list=["Python Engineer"],
            )
            profile_path = root / "generated_docs" / "profile.json"
            profile_path.parent.mkdir(parents=True)
            profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
            browser = ScriptedBrowser()
            summary = await _runner(root, browser).run()
            self.assertEqual(summary.submitted, 0)
            self.assertFalse(any(event[0] == "navigate" and "/job/" in event[1] for event in browser.events))

    async def test_semi_mode_skip_and_go(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            skipped = ScriptedBrowser()
            summary = await _runner(root, skipped, mode="semi", human=_DecisionHuman(["skip"])).run()
            self.assertEqual(summary.submitted, 0)
            self.assertFalse(any(event[0] == "navigate" and "/job/" in event[1] for event in skipped.events))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_sample_project(root)
            chosen = ScriptedBrowser()
            summary = await _runner(root, chosen, mode="semi", human=_DecisionHuman(["go"])).run()
            self.assertEqual(summary.submitted, 1)


class _DecisionHuman(HumanInput):
    def __init__(self, decisions: list[str]) -> None:
        super().__init__(noninteractive=False)
        self._decisions = list(decisions)

    def ask(self, prompt: str, *, default: str | None = None, required: bool = False) -> str:
        if default is not None:
            return default
        if required:
            return "+"
        return ""

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return default

    def decide(self, prompt: str) -> str:
        choice = self._decisions.pop(0)
        return "skip" if choice == "skip" else "go"


def synthesize_sample() -> CandidateBaseline:
    from src.models import ParsedDocument

    return synthesize([ParsedDocument(path="master_cv.txt", text=SAMPLE_CV)])


def _write_sample_project(root: Path) -> None:
    docs = root / "docs"
    docs.mkdir()
    (docs / "master_cv.txt").write_text(SAMPLE_CV, encoding="utf-8")
    (root / "sites.csv").write_text(
        "site_name,url,location_filter_param\nJobsDB,https://www.jobsdb.com,location\n",
        encoding="utf-8",
    )


def _runner(
    root: Path,
    browser: ScriptedBrowser,
    *,
    mode: str = "auto",
    human: HumanInput | None = None,
) -> ApplicationRunner:
    return ApplicationRunner(
        RunConfig(root=root, through="apply", mode=mode, noninteractive=human is None, max_per_site=5),
        human=human or HumanInput(noninteractive=True),
        bridge=FakeBridge(browser),
        llm=LLMClient(api_key=""),
    )


if __name__ == "__main__":
    unittest.main()
