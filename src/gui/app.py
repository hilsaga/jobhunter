"""Tkinter window for analyze, revise, and the site standby step."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from src.cli.human_input import HumanInput
from src.exceptions import ChromeInteractionError, DocsMissing
from src.logging_setup import Console
from src.mcp_client.chrome_profiles import list_chrome_profiles
from src.models import JobDetails, JobListing
from src.parsers.doc_parser import load_sites
from src.pipeline.application_runner import ApplicationRunner, RunConfig, _job_from_open_page
from src.pipeline.matching import MatchAssessment, assess_match
from src.pipeline.recorder import record_application
from src.textutil import parse_keywords

_PAPER = "#101410"
_INK = "#C6F25C"
_ACCENT = "#1A2614"
_ACCENT_DARK = "#24361A"
_CARD = "#182016"
_LINE = "#314228"
_MUTED = "#8FBF55"
_FIELD = "#141A14"
_SLATE = "#3E4C5A"
_SLATE_SOFT = "#2C3844"
_SLATE_ACTIVE = "#5A6B7C"
_DISABLED_FACE = "#242B32"
_DISABLED_INK = "#6A7560"


class GuiConsole(Console):
    """Send pipeline lines to the window as well as the log file."""

    def __init__(self, emit) -> None:  # type: ignore[no-untyped-def]
        self._emit = emit

    def step(self, number: str, message: str) -> None:
        super().step(number, message)
        self._emit(f"[{number}] {message}")

    def info(self, message: str) -> None:
        super().info(message)
        self._emit(message)

    def warn(self, message: str) -> None:
        super().warn(message)
        self._emit(f"! {message}")

    def error(self, message: str) -> None:
        super().error(message)
        self._emit(f"x {message}")


class JobHunterApp:
    """Three home actions, then a site, a Chrome profile, and a job reading."""

    def __init__(self, project_root: Path, *, window: tk.Tk | None = None) -> None:
        self.project_root = project_root
        self.window = window or tk.Tk()
        self.window.title("Job Hunter")
        self.window.configure(bg=_PAPER)
        self.window.minsize(820, 640)
        self.window.geometry("880x700")
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._loop_thread.start()
        self._browser_cm = None
        self.client = None
        self.site = None
        self.job: JobListing | None = None
        self.report: MatchAssessment | None = None
        self.pinned = None
        self.page_html = ""
        self.page_url = ""
        self._busy = False
        self._buttons: list[dict[str, object]] = []
        self.console = GuiConsole(self._log_later)
        self.runner = ApplicationRunner(
            RunConfig(root=project_root, through="documents", noninteractive=True),
            human=HumanInput(noninteractive=True),
            console=self.console,
        )
        self.body = tk.Frame(self.window, bg=_PAPER)
        self.body.pack(fill="both", expand=True, padx=28, pady=(22, 8))
        self.log = tk.Text(
            self.window,
            height=8,
            bg=_FIELD,
            fg=_INK,
            insertbackground=_INK,
            selectbackground=_ACCENT_DARK,
            selectforeground=_INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=_LINE,
            font=("Helvetica", 12),
            wrap="word",
            padx=12,
            pady=8,
        )
        self.log.pack(fill="x", padx=28, pady=(0, 18))
        self.log.bind("<Key>", lambda _event: "break")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.show_home()

    def run(self) -> None:
        self.window.mainloop()

    def close(self) -> None:
        if self._browser_cm is not None and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._close_browser(), self.loop)
            try:
                future.result(timeout=8)
            except Exception:
                pass
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.window.destroy()

    def show_home(self) -> None:
        self._clear()
        self._heading("Job Hunter")
        self._note("Analyze the CV, revise the profile, or open a site and read the job you pick.")
        self._wide_button(
            "1   Analyze CV  +  Generate profile.json  +  Generate docs",
            self._analyze,
        )
        self._wide_button(
            "2   Revise profile.json  and  generate missing docs",
            self.show_revise,
        )
        self._wide_button("4   Open site", self.show_sites)

    def show_revise(self) -> None:
        profile = self.runner._load_profile()
        if profile is None:
            self._log("Analyze the CV first. profile.json is not written yet.")
            return
        self._clear()
        self._heading("Revise profile")
        self._note("Edits here do not change employers, dates, or degrees. Missing CV pairs are written after you save.")
        form = tk.Frame(self.body, bg=_PAPER)
        form.pack(fill="x", pady=(8, 0))
        fields = {
            "Targeted income": profile.targeted_income,
            "Targeted location": profile.targeted_location,
            "Keywords": ", ".join(profile.interested_keywords),
            "Skip list": ", ".join(profile.skip_list),
            "Extra job titles": ", ".join(profile.extra_job_titles),
        }
        self._entries: dict[str, tk.Entry] = {}
        for label, value in fields.items():
            tk.Label(form, text=label, bg=_PAPER, fg=_MUTED, font=("Helvetica", 12)).pack(anchor="w", pady=(10, 2))
            entry = tk.Entry(
                form,
                bg=_FIELD,
                fg=_INK,
                insertbackground=_INK,
                selectbackground=_ACCENT_DARK,
                selectforeground=_INK,
                relief="flat",
                highlightthickness=1,
                highlightbackground=_LINE,
                font=("Helvetica", 14),
            )
            entry.insert(0, value)
            entry.pack(fill="x", ipady=6)
            self._entries[label] = entry
        actions = tk.Frame(self.body, bg=_PAPER)
        actions.pack(fill="x", pady=18)
        self._pair_button(actions, "Back", self.show_home, primary=False)
        self._pair_button(actions, "Save and generate missing docs", self._save_revision, primary=True)

    def show_sites(self) -> None:
        try:
            sites = load_sites(self.runner.paths.sites)
        except (OSError, ValueError) as exc:
            self._log(str(exc))
            return
        if not sites:
            self._log(f"No sites in {self.runner.paths.sites}.")
            return
        self._clear()
        self._heading("Open a site")
        self._note("Choose a site, then a Chrome profile. Incognito is first.")
        row = tk.Frame(self.body, bg=_PAPER)
        row.pack(fill="x", pady=(12, 8))
        for site in sites:
            self._chip(row, site.site_name, lambda site=site: self._show_profiles(site))
        self.profile_row = tk.Frame(self.body, bg=_PAPER)
        self.profile_row.pack(fill="x", pady=(16, 8))
        self._paint_button(self.body, "Back", self.show_home, face=_SLATE_SOFT, font=("Helvetica", 13), padx=16, pady=8).pack(
            anchor="w", pady=(18, 0)
        )

    def show_ready(self) -> None:
        name = self.site.site_name if self.site is not None else "Site"
        self._clear()
        self._heading(name)
        self._note("The site is open. Open a job post in that window, then read the page.")
        self._wide_button("Read page", self._read_page)
        self._paint_button(
            self.body,
            "Choose another site",
            self._leave_site,
            face=_SLATE_SOFT,
            font=("Helvetica", 13),
            padx=16,
            pady=8,
        ).pack(anchor="w", pady=(16, 0))

    def show_analysis(self) -> None:
        if self.report is None or self.job is None:
            self.show_ready()
            return
        self._clear()
        self._heading(self.job.title or "Open job")
        scores = tk.Frame(self.body, bg=_PAPER)
        scores.pack(fill="x", pady=(8, 12))
        scores.columnconfigure(0, weight=1)
        scores.columnconfigure(1, weight=1)
        self._score_card(scores, 0, "Likelihood", self.report.match)
        self._score_card(scores, 1, "Success rate", self.report.proceed)
        tk.Label(self.body, text="Why it fits, or does not", bg=_PAPER, fg=_MUTED, font=("Helvetica", 12)).pack(
            anchor="w"
        )
        reason = tk.Text(
            self.body,
            height=5,
            wrap="word",
            bg=_FIELD,
            fg=_INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=_LINE,
            font=("Helvetica", 14),
            padx=12,
            pady=10,
        )
        reason.insert("1.0", self.report.reason)
        reason.bind("<Key>", lambda _event: "break")
        reason.pack(fill="x", pady=(4, 14))
        self._wide_button("Customize", self._customize)
        suggestion = tk.Frame(self.body, bg=_PAPER)
        suggestion.pack(fill="x", pady=(4, 16))
        suggestion.columnconfigure(0, weight=1)
        spec = self._suggested_spec()
        title = spec.title if spec is not None else (self.report.closest or "No saved CV")
        tk.Label(
            suggestion,
            text=title,
            bg=_PAPER,
            fg=_INK,
            font=("Helvetica", 16),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self._paint_button(
            suggestion,
            "Open folder",
            self._open_cv_folder,
            font=("Helvetica", 13),
            padx=14,
            pady=8,
        ).grid(row=0, column=1, sticky="e")
        actions = tk.Frame(self.body, bg=_PAPER)
        actions.pack(fill="x", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self._paint_button(
            actions, "Go back", self.show_ready, face=_SLATE_SOFT, font=("Helvetica", 14), padx=12, pady=12
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._paint_button(actions, "Record", self._record, font=("Helvetica", 14), padx=12, pady=12).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

    def _analyze(self) -> None:
        def work() -> str:
            self.runner.config.through = "documents"
            self.runner.config.skip_documents = False
            summary = asyncio.run(self.runner.run())
            return f"Profile and documents are ready. PDFs written: {summary.pdf_count}."

        self._run_thread(work, self._log, "Analyzing the CV and writing documents.")

    def _save_revision(self) -> None:
        income = self._entries["Targeted income"].get()
        location = self._entries["Targeted location"].get()
        keywords = parse_keywords(self._entries["Keywords"].get())
        skip_list = parse_keywords(self._entries["Skip list"].get())
        extra = parse_keywords(self._entries["Extra job titles"].get())

        def work() -> str:
            self.runner.save_profile_fields(
                income=income,
                location=location,
                keywords=keywords,
                skip_list=skip_list,
                extra_job_titles=extra,
            )
            written = self.runner.generate_missing_documents()
            if not written:
                return "Profile saved. No missing documents."
            return "Profile saved. Wrote " + ", ".join(written) + "."

        self._run_thread(work, self._after_revise, "Saving the profile and writing missing documents.")

    def _after_revise(self, message: str) -> None:
        self._log(message)
        self.show_home()

    def _show_profiles(self, site) -> None:  # type: ignore[no-untyped-def]
        self.site = site
        for child in self.profile_row.winfo_children():
            child.destroy()
        tk.Label(
            self.profile_row,
            text=f"Chrome profile for {site.site_name}",
            bg=_PAPER,
            fg=_MUTED,
            font=("Helvetica", 12),
        ).pack(anchor="w", pady=(0, 8))
        choices = [("0  Incognito", "0")]
        choices.extend(
            (f"{index}  {profile.name or profile.directory}", str(index))
            for index, profile in enumerate(list_chrome_profiles(), start=1)
        )
        wrap = tk.Frame(self.profile_row, bg=_PAPER)
        wrap.pack(fill="x")
        for offset, (label, number) in enumerate(choices):
            button = self._paint_button(
                wrap,
                label,
                lambda number=number: self._open_site(site, number),
                face=_SLATE_SOFT,
                font=("Helvetica", 13),
                padx=14,
                pady=10,
            )
            button.grid(row=offset // 3, column=offset % 3, sticky="ew", padx=(0, 8), pady=4)
        for column in range(3):
            wrap.columnconfigure(column, weight=1)

    def _open_site(self, site, profile_number: str) -> None:  # type: ignore[no-untyped-def]
        async def work() -> str:
            await self._close_browser()
            self.runner.config.chrome_profile = profile_number
            self._browser_cm = self.runner._attached_browser()
            self.client = await self._browser_cm.__aenter__()
            await self.client.navigate(site.url)
            return site.site_name

        def opened(name: str) -> None:
            self._log(f"{name} is open.")
            self.show_ready()

        self._run_async(work(), opened, f"Opening {site.site_name}.")

    def _read_page(self) -> None:
        if self.client is None or self.site is None:
            self._log("Open a site before reading the page.")
            return

        async def work() -> tuple[JobListing, MatchAssessment] | None:
            if not self.runner.specs:
                self.runner._load_existing_documents()
            page = await self.runner._read_listed_site(self.client, [self.site])
            if page is None:
                return None
            url, html = page
            job = _job_from_open_page(html)
            if url:
                job = job.model_copy(update={"url": url})
            report = assess_match(
                job.description,
                self.runner.specs,
                self.runner.profile.interested_keywords,
                self.runner.profile.candidate,
                job.title,
            )
            report = self.runner._llm_match_review(job, report)
            self.page_html = html
            self.page_url = job.url
            return job, report

        def shown(result: tuple[JobListing, MatchAssessment] | None) -> None:
            if result is None:
                self._log("The open tab is not a job post yet.")
                return
            self.job, self.report = result
            self.pinned = None
            self.show_analysis()

        self._run_async(work(), shown, "Reading the open job.")

    def _record(self) -> None:
        if self.job is None or self.report is None:
            self.show_ready()
            return
        spec = self._suggested_spec()
        if spec is None:
            self._log("No saved CV to record.")
            return
        try:
            cv_path, letter_path = self.runner._material_paths(spec)
            details = JobDetails(
                job_id=self.job.job_id,
                job_title=self.job.title,
                company=self.job.company,
                location=self.job.location,
                job_description=self.job.description[:20_000],
                source_url=self.page_url or self.job.url,
                applied_timestamp=datetime.now(timezone.utc).isoformat(),
                cv_path_used=str(cv_path),
                cover_letter_path_used=str(letter_path),
                match_score=self.report.match / 100,
                specialization=spec.area,
            )
            folder = record_application(
                self.runner.paths,
                details,
                cv_path,
                letter_path,
                page_source=self.page_html or self.job.description,
            )
        except (OSError, DocsMissing, FileNotFoundError) as exc:
            self._log(str(exc))
            return
        self._log(f"Recorded {folder.name}.")
        self.show_ready()

    def _customize(self) -> None:
        if self.job is None:
            self._log("Read a job page before customizing a CV.")
            return
        job = self.job

        def work():  # type: ignore[no-untyped-def]
            return self.runner.customize_for_job(job)

        def ready(spec) -> None:  # type: ignore[no-untyped-def]
            self.pinned = spec
            folder = self.runner.paths.specialization_dir(spec.area)
            self._log(f"Best CV for this job: {spec.title}")
            self._log(str(folder))
            self.show_analysis()
            _reveal(folder)

        self._run_thread(work, ready, "Finding the closest saved CV for this job title.")

    def _open_cv_folder(self) -> None:
        spec = self._suggested_spec()
        if spec is None:
            self._log("No saved CV folder to open.")
            return
        folder = self.runner.paths.specialization_dir(spec.area)
        folder.mkdir(parents=True, exist_ok=True)
        _reveal(folder)

    def _suggested_spec(self):  # type: ignore[no-untyped-def]
        if self.pinned is not None:
            return self.pinned
        if self.report is None:
            return None
        wanted = self.report.closest.strip().lower()
        for spec in self.runner.specs:
            if spec.title.lower() == wanted or spec.area.lower() == wanted:
                return spec
        return None

    def _leave_site(self) -> None:
        def left(_: object) -> None:
            self.show_sites()

        self._run_async(self._close_browser(), left, "Closing the browser session.")

    async def _close_browser(self) -> None:
        browser = self._browser_cm
        self._browser_cm = None
        self.client = None
        if browser is not None:
            await browser.__aexit__(None, None, None)

    def _run_thread(self, work, done, busy: str) -> None:  # type: ignore[no-untyped-def]
        if self._busy:
            return
        self._set_busy(True)
        self._log(busy)

        def runner() -> None:
            try:
                result = work()
                error: Exception | None = None
            except Exception as exc:
                result = None
                error = exc

            def finish() -> None:
                self._set_busy(False)
                if error is not None:
                    self._log(str(error))
                    return
                done(result)

            self.window.after(0, finish)

        threading.Thread(target=runner, daemon=True).start()

    def _run_async(self, coro, done, busy: str) -> None:  # type: ignore[no-untyped-def]
        if self._busy:
            return
        self._set_busy(True)
        self._log(busy)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def poll() -> None:
            if not future.done():
                self.window.after(120, poll)
                return
            self._set_busy(False)
            try:
                result = future.result()
            except (ChromeInteractionError, DocsMissing, OSError, RuntimeError) as exc:
                self._log(str(exc))
                return
            except Exception as exc:
                self._log(str(exc))
                return
            done(result)

        self.window.after(120, poll)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.window.configure(cursor="watch" if busy else "")
        self._apply_busy()

    def _apply_busy(self) -> None:
        alive: list[dict[str, object]] = []
        for record in self._buttons:
            shell = record["shell"]
            label = record["label"]
            try:
                if not isinstance(shell, tk.Misc) or not shell.winfo_exists():
                    continue
                if not isinstance(label, tk.Misc) or not label.winfo_exists():
                    continue
            except tk.TclError:
                continue
            alive.append(record)
            face = _DISABLED_FACE if self._busy else str(record["face"])
            ink = _DISABLED_INK if self._busy else _INK
            cursor = "" if self._busy else "hand2"
            shell.configure(bg=face, cursor=cursor)
            label.configure(bg=face, fg=ink, cursor=cursor)
        self._buttons = alive

    def _clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def _heading(self, text: str) -> None:
        tk.Label(self.body, text=text, bg=_PAPER, fg=_INK, font=("Georgia", 26)).pack(anchor="w")

    def _note(self, text: str) -> None:
        tk.Label(
            self.body,
            text=text,
            bg=_PAPER,
            fg=_MUTED,
            font=("Helvetica", 13),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(6, 16))

    def _wide_button(self, text: str, command) -> None:  # type: ignore[no-untyped-def]
        self._paint_button(
            self.body,
            text,
            command,
            anchor="w",
            font=("Helvetica", 15),
            padx=18,
            pady=14,
        ).pack(fill="x", pady=7)

    def _chip(self, parent: tk.Frame, text: str, command) -> None:  # type: ignore[no-untyped-def]
        self._paint_button(
            parent,
            text,
            command,
            face=_SLATE_SOFT,
            font=("Helvetica", 13),
            padx=14,
            pady=10,
        ).pack(side="left", padx=(0, 8), pady=4)

    def _pair_button(self, parent: tk.Frame, text: str, command, *, primary: bool) -> None:  # type: ignore[no-untyped-def]
        self._paint_button(
            parent,
            text,
            command,
            face=_SLATE if primary else _SLATE_SOFT,
            font=("Helvetica", 14),
            padx=14,
            pady=10,
        ).pack(side="right" if primary else "left")

    def _paint_button(
        self,
        parent: tk.Misc,
        text: str,
        command,  # type: ignore[no-untyped-def]
        *,
        face: str = _SLATE,
        anchor: str = "center",
        font: tuple[str, int] = ("Helvetica", 14),
        padx: int = 16,
        pady: int = 12,
    ) -> tk.Frame:
        """A painted control. macOS draws a native Button white and ignores bg."""
        shell = tk.Frame(parent, bg=face, cursor="hand2", highlightthickness=0, bd=0)
        label = tk.Label(
            shell,
            text=text,
            bg=face,
            fg=_INK,
            font=font,
            anchor=anchor,
            cursor="hand2",
            padx=padx,
            pady=pady,
        )
        label.pack(fill="both", expand=True)
        self._buttons.append({"shell": shell, "label": label, "face": face})

        def paint(color: str) -> None:
            if self._busy:
                return
            shell.configure(bg=color)
            label.configure(bg=color)

        def click(_event: object) -> None:
            if self._busy:
                return
            command()

        for widget in (shell, label):
            widget.bind("<Enter>", lambda _event: paint(_SLATE_ACTIVE))
            widget.bind("<Leave>", lambda _event: paint(face))
            widget.bind("<Button-1>", click)
        if self._busy:
            self._apply_busy()
        return shell

    def _score_card(self, parent: tk.Frame, column: int, label: str, score: int) -> None:
        card = tk.Frame(parent, bg=_CARD, padx=16, pady=14)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        tk.Label(card, text=label, bg=_CARD, fg=_MUTED, font=("Helvetica", 12)).pack(anchor="w")
        tk.Label(card, text=f"{score}/100", bg=_CARD, fg=_INK, font=("Georgia", 28)).pack(anchor="w", pady=(4, 0))

    def _log(self, message: str) -> None:
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")

    def _log_later(self, message: str) -> None:
        try:
            self.window.after(0, lambda: self._log(message))
        except tk.TclError:
            return


def _reveal(folder: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])  # noqa: S603
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", str(folder)])  # noqa: S603
    else:
        subprocess.Popen(["xdg-open", str(folder)])  # noqa: S603


def launch(project_root: Path) -> int:
    """Open the Job Hunter window. The process stays here until the window closes."""
    JobHunterApp(project_root).run()
    return 0
