#!/usr/bin/env python3
"""Run the autonomous job-hunter pipeline."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from src import __version__
from src.exceptions import AbortRun, DocsMissing
from src.logging_setup import Console, configure_logging
from src.mcp_client.chrome_profiles import list_chrome_profiles, profile_menu
from src.pipeline.application_runner import ApplicationRunner, RunConfig


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    args = _parser().parse_args(argv)
    raw = sys.argv[1:] if argv is None else argv
    if not raw:
        from src.gui.app import launch

        configure_logging(root / "logs" / "jobhunter.log", verbose=False)
        return launch(root)
    config = RunConfig(
        root=Path(args.root).resolve(),
        through=args.through,
        mode=args.mode,
        dry_run=args.dry_run,
        noninteractive=args.noninteractive,
        confidence_threshold=args.confidence,
        max_per_site=args.max_per_site,
        docs_path=Path(args.docs).resolve() if args.docs else None,
        sites_path=Path(args.sites).resolve() if args.sites else None,
        verbose=args.verbose,
        chrome_profile=args.chrome_profile,
        skip_documents=args.skip_documents,
    )
    console = Console()
    configure_logging(config.root / "logs" / "jobhunter.log", verbose=config.verbose)
    if args.list_chrome_profiles:
        return _print_chrome_profiles(console)
    console.step("0", f"Autonomous Job Hunter {__version__}")
    console.info(f"Root: {config.root}")
    console.info(f"Through: {config.through}")
    console.info(f"Mode: {config.mode}")
    console.info(f"Dry run: {'yes' if config.dry_run else 'no'}")
    console.info(f"Confidence threshold: {config.confidence_threshold:.0%}")
    if not 0 <= config.confidence_threshold <= 1:
        console.error("Confidence must be between 0 and 1.")
        return 2
    if config.max_per_site < 1:
        console.error("--max-per-site must be at least 1.")
        return 2
    runner = ApplicationRunner(config, console=console)
    if args.standby:
        return _standby(console, runner)
    if args.extra_job_title is not None:
        return _write_extra_job_title(console, runner, args.extra_job_title)
    try:
        summary = asyncio.run(runner.run())
    except AbortRun as exc:
        console.error(f"Stopped: {exc}")
        return 2
    except KeyboardInterrupt:
        console.error("Interrupted.")
        return 2
    except (DocsMissing, FileNotFoundError) as exc:
        console.error(str(exc))
        return 1
    _print_summary(console, summary)
    return 0


def _print_chrome_profiles(console: Console) -> int:
    console.info("Chrome profile (--chrome-profile N):")
    for line in profile_menu(list_chrome_profiles()):
        console.info(f"  {line}")
    console.info("Example: python main.py --chrome-profile 0")
    return 0


def _standby(console: Console, runner: ApplicationRunner) -> int:
    try:
        asyncio.run(runner.standby())
    except (DocsMissing, FileNotFoundError) as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.error("Interrupted.")
        return 2
    return 0


def _write_extra_job_title(console: Console, runner: ApplicationRunner, title: str) -> int:
    requested = None if title == "USE_PROFILE" else title
    try:
        pairs = runner.write_extra_job_title(requested)
    except (DocsMissing, FileNotFoundError, ValueError) as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.error("Interrupted.")
        return 2
    console.step("9", "Extra job title documents")
    for cv_path, letter_path in pairs:
        console.info(f"CV: {cv_path}")
        console.info(f"Cover letter: {letter_path}")
    return 0


def _print_summary(console: Console, summary: object) -> None:
    console.step("9", "Run summary")
    console.info(f"Documents parsed: {summary.documents_parsed}")  # type: ignore[attr-defined]
    specializations = summary.specializations  # type: ignore[attr-defined]
    console.info(f"Specializations: {', '.join(specializations) or 'none'}")
    console.info(f"PDFs written: {summary.pdf_count}")  # type: ignore[attr-defined]
    console.info(f"Submitted: {summary.submitted}")  # type: ignore[attr-defined]
    console.info(f"Skipped: {summary.skipped}")  # type: ignore[attr-defined]
    console.info(f"Dry runs: {summary.dry_runs}")  # type: ignore[attr-defined]
    if summary.profile_path:  # type: ignore[attr-defined]
        console.info(f"Profile: {summary.profile_path}")  # type: ignore[attr-defined]


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs without overriding variables already in the environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"").strip()
        if key and key not in os.environ:
            os.environ[key] = value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze base documents, generate tailored CVs, and apply to jobs in sites.csv.",
        epilog=(
            "Environment:\n"
            "  JOBHUNTER_CHROME_MCP_COMMAND   Chrome MCP launch command, e.g. npx -y @playwright/mcp@latest\n"
            "  JOBHUNTER_MCP_TIMEOUT          Seconds to wait for one browser tool call (default 45)\n"
            "  JOBHUNTER_LLM_API_KEY          Optional chat-completions key; OPENAI_API_KEY is also read\n"
            "  JOBHUNTER_LLM_BASE_URL         Default https://api.openai.com/v1\n"
            "  JOBHUNTER_LLM_MODEL            Default gpt-4o-mini\n\n"
            "Put master_cv.txt (or .pdf/.docx) and optional supporting files in docs/.\n"
            "Generated CVs go to generated_docs/. Sent jobs are stored in applications/ and are not sent again.\n"
            "Semi mode (--mode semi) asks '- skip' or '+ go' before each job.\n"
            "profile.json skip_list drops any job whose text contains one of those phrases.\n"
            "python main.py --extra-job-title \"AI Product Director\" writes a CV and cover letter and appends the title to extra_job_titles.\n"
            "python main.py --extra-job-title writes a pair for every title in extra_job_titles.\n"
            "python main.py --list-chrome-profiles shows the Chrome profile numbers.\n"
            "python main.py --chrome-profile 0 uses an incognito window. Any other number uses that profile.\n"
            "python main.py --skip-documents searches and applies with the CVs already in generated_docs/.\n"
            "python main.py opens the window. Step 1 analyzes the CV and writes profile.json and the docs.\n"
            "Step 2 revises profile.json and writes any missing CV pairs. Step 4 opens a site from sites.csv.\n"
            "python main.py --standby is the same standby flow in the terminal.\n"
            "The browser steps run one site at a time. Ambiguous questions, CAPTCHAs, login walls,\n"
            "and submissions below the confidence threshold stop for a CLI prompt."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent), help="Project root")
    parser.add_argument("--docs", help="Override the docs directory")
    parser.add_argument("--sites", help="Override sites.csv")
    parser.add_argument(
        "--through",
        choices=("profile", "documents", "search", "apply"),
        default="apply",
        help="Stop after this stage (default: apply)",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "semi"),
        default="auto",
        help="auto applies viable jobs; semi asks - skip or + go before each job",
    )
    parser.add_argument("--dry-run", action="store_true", help="Fill applications but do not submit them")
    parser.add_argument(
        "--noninteractive",
        action="store_true",
        help="Accept example defaults and skip jobs that need a person",
    )
    parser.add_argument("--confidence", type=float, default=0.8, help="Minimum score required to submit")
    parser.add_argument("--max-per-site", type=int, default=10, help="Maximum listings to open on one site")
    parser.add_argument(
        "--extra-job-title",
        nargs="?",
        const="USE_PROFILE",
        default=None,
        metavar="TITLE",
        help="Create a CV and cover letter for TITLE, and append it to extra_job_titles in profile.json. "
        "Omit TITLE to write a pair for every saved title.",
    )
    parser.add_argument(
        "--chrome-profile",
        metavar="N",
        help="Chrome profile number. 0 is incognito. Other numbers are listed by --list-chrome-profiles.",
    )
    parser.add_argument(
        "--list-chrome-profiles",
        action="store_true",
        help="Print Chrome profile numbers (0 is incognito) and exit",
    )
    parser.add_argument(
        "--standby",
        action="store_true",
        help="Open the sites in sites.csv, then wait. 1 scores the open job. "
        "0 goes back. 1 creates a CV and cover letter. a fills the form. You press Submit.",
    )
    parser.add_argument(
        "--skip-documents",
        action="store_true",
        help="Skip document analysis and CV rewrite. Search and apply with the PDFs already in generated_docs/.",
    )
    parser.add_argument("--verbose", action="store_true", help="Mirror the detailed log on the terminal")
    return parser


if __name__ == "__main__":
    sys.exit(main())
