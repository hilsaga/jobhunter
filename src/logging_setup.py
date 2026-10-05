"""File and console logging."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


class _HideConsoleEcho(logging.Filter):
    """Drop CLI messages that Console already printed."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("jobhunter.cli")


def configure_logging(log_path: Path, *, verbose: bool = False) -> None:
    """Write a detailed log file and keep the terminal reserved for step output."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    stream = logging.StreamHandler()
    stream.setLevel(logging.DEBUG if verbose else logging.WARNING)
    stream.addFilter(_HideConsoleEcho())
    stream.setFormatter(formatter)

    root.addHandler(file_handler)
    root.addHandler(stream)


class Console:
    """Human-readable progress lines for the CLI."""

    def step(self, number: str, message: str) -> None:
        print(f"\n[{number}] {message}", flush=True)
        logging.getLogger("jobhunter.cli").info("Step %s: %s", number, message)

    def info(self, message: str) -> None:
        print(f"  {message}", flush=True)
        logging.getLogger("jobhunter.cli").info(message)

    def warn(self, message: str) -> None:
        print(f"  ! {message}", file=sys.stderr, flush=True)
        logging.getLogger("jobhunter.cli").warning(message)

    def error(self, message: str) -> None:
        print(f"  x {message}", file=sys.stderr, flush=True)
        logging.getLogger("jobhunter.cli").error(message)
