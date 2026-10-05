"""Interactive prompts used whenever the pipeline must stop for a person."""

from __future__ import annotations

import getpass
import logging

from src.exceptions import AbortRun, HumanInputRequired
from src.textutil import clip

logger = logging.getLogger("jobhunter.hitl")


class FallbackDecision:
    """What the human wants to do after a browser action fails."""

    def __init__(self, action: str, value: str = "") -> None:
        self.action = action
        self.value = value


class HumanInput:
    """CLI prompts. In non-interactive mode, only explicit defaults are accepted."""

    def __init__(self, *, noninteractive: bool = False) -> None:
        self.noninteractive = noninteractive

    def ask(self, prompt: str, *, default: str | None = None, required: bool = False) -> str:
        if self.noninteractive:
            if required or default is None:
                logger.info("Human input required and no default is allowed: %s", prompt)
                raise HumanInputRequired(prompt)
            logger.info("Using default for '%s'", prompt)
            return default
        suffix = f" [{default}]" if default is not None else ""
        while True:
            try:
                raw = input(f"{prompt}{suffix}: ").strip()
            except EOFError as exc:
                raise HumanInputRequired(prompt) from exc
            if raw:
                return raw
            if default is not None:
                return default
            print("A value is required.")

    def ask_secret(self, prompt: str) -> str:
        """Read a credential without echoing it or accepting a stored default."""
        if self.noninteractive:
            raise HumanInputRequired(prompt)
        value = getpass.getpass(f"{prompt}: ").strip()
        if not value:
            raise HumanInputRequired(prompt)
        logger.info("Received a secret value for '%s'", prompt)
        return value

    def decide(self, prompt: str) -> str:
        """Semi mode: '-' skips the job and '+' continues."""
        if self.noninteractive:
            raise HumanInputRequired(prompt)
        print("\nSemi mode", flush=True)
        while True:
            raw = self.ask(f"{prompt}\n- skip, + go", required=True).strip().lower()
            if raw in {"-", "skip"}:
                return "skip"
            if raw in {"+", "go"}:
                return "go"
            print("Enter - to skip or + to go.")

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        if self.noninteractive:
            logger.info("Non-interactive confirm '%s' -> %s", prompt, default)
            return default
        label = "Y/n" if default else "y/N"
        raw = self.ask(f"{prompt} ({label})", default="y" if default else "n")
        return raw.strip().lower() in {"y", "yes"}

    def fallback_menu(self, *, reason: str, context: str) -> FallbackDecision:
        """Menu shown when a browser action fails or a page is blocked."""
        print("\n--- Human assistance needed ---", flush=True)
        print(f"Reason: {clip(reason, 400)}", flush=True)
        if context:
            print(f"Context: {clip(context, 300)}", flush=True)
        print("1) Retry the last browser action", flush=True)
        print("2) Continue without this action", flush=True)
        print("3) Skip this job", flush=True)
        print("4) Abort the run", flush=True)
        print("On a required step, continue skips the job.", flush=True)
        if self.noninteractive:
            raise HumanInputRequired(reason)
        while True:
            raw = self.ask("Choose 1-4", default="3").strip().lower()
            if raw in {"1", "retry"}:
                return FallbackDecision("retry")
            if raw in {"2", "continue"}:
                return FallbackDecision("continue")
            if raw in {"3", "skip"}:
                return FallbackDecision("skip")
            if raw in {"4", "abort"}:
                return FallbackDecision("abort")
            print("Enter 1, 2, 3, or 4.")
