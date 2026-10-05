"""Find Google Chrome profiles and remember which one Playwright should use."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.cli.human_input import HumanInput
from src.exceptions import HumanInputRequired
from src.logging_setup import Console

PROFILE_ENV = "JOBHUNTER_CHROME_PROFILE"
PLAYWRIGHT_EXTENSION_ID = "mmlmfjhmonkocbjadbfplnigmagldckm"
PLAYWRIGHT_EXTENSION_URL = (
    "https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm"
)


@dataclass(frozen=True)
class ChromeProfile:
    directory: str
    name: str
    user_name: str
    user_data_dir: Path

    def label(self) -> str:
        who = f" — {self.user_name}" if self.user_name else ""
        title = self.name or self.directory
        return f"{title}{who} ({self.directory})"


@dataclass(frozen=True)
class BrowserTarget:
    """0 is a fresh incognito window. Any other number is a Chrome profile."""

    incognito: bool
    profile: ChromeProfile | None = None

    def label(self) -> str:
        if self.incognito:
            return "Incognito"
        if self.profile is None:
            return "Incognito"
        return self.profile.label()


def playwright_extension_installed(profile: ChromeProfile) -> bool:
    """True when this Chrome profile can be driven in its already-open window."""
    return (profile.user_data_dir / profile.directory / "Extensions" / PLAYWRIGHT_EXTENSION_ID).is_dir()


def chrome_user_data_dir() -> Path:
    override = os.getenv("JOBHUNTER_CHROME_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library/Application Support/Google/Chrome"


def list_chrome_profiles(user_data_dir: Path | None = None) -> list[ChromeProfile]:
    """Read Chrome's Local State and return profiles that still have a folder."""
    root = user_data_dir or chrome_user_data_dir()
    local_state = root / "Local State"
    if not local_state.is_file():
        return []
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cache = data.get("profile", {}).get("info_cache", {})
    if not isinstance(cache, dict):
        return []
    profiles: list[ChromeProfile] = []
    for directory, info in cache.items():
        if not isinstance(directory, str) or not isinstance(info, dict):
            continue
        folder = root / directory
        if not folder.is_dir():
            continue
        profiles.append(
            ChromeProfile(
                directory=directory,
                name=str(info.get("name") or info.get("gaia_name") or "").strip(),
                user_name=str(info.get("user_name") or "").strip(),
                user_data_dir=root,
            )
        )
    profiles.sort(key=lambda profile: _directory_sort_key(profile.directory))
    return profiles


def profile_menu(profiles: list[ChromeProfile], *, saved: str = "") -> list[str]:
    """Numbered CLI choices. 0 is always incognito."""
    saved_key = saved.strip().lower()
    incognito_mark = " (saved)" if saved_key in {"0", "incognito"} else ""
    lines = [f"0. Incognito{incognito_mark}"]
    for index, profile in enumerate(profiles, start=1):
        marker = " (saved)" if profile.directory == saved else ""
        lines.append(f"{index}. {profile.label()}{marker}")
    return lines


def choose_chrome_profile(
    human: HumanInput,
    console: Console,
    *,
    env_path: Path,
    user_data_dir: Path | None = None,
    selection: str | None = None,
) -> BrowserTarget:
    """Resolve --chrome-profile, or ask on the CLI. 0 is incognito."""
    profiles = list_chrome_profiles(user_data_dir)
    saved = os.getenv(PROFILE_ENV, "").strip()
    if selection is not None and selection.strip():
        chosen = resolve_browser_target(selection, profiles)
        if chosen is None:
            raise ValueError(_unknown_profile(selection, len(profiles)))
        _remember_target(env_path, chosen)
        return chosen
    default = saved if _matching_index(profiles, saved) is not None or saved in {"0", "incognito"} else "0"
    console.info("Chrome profile (--chrome-profile N):")
    for line in profile_menu(profiles, saved=saved):
        console.info(f"  {line}")
    if human.noninteractive:
        chosen = resolve_browser_target(saved, profiles) if saved else None
        if chosen is None:
            raise HumanInputRequired(
                "Pass --chrome-profile N. 0 is incognito. "
                "See --list-chrome-profiles for the numbers."
            )
        return chosen
    while True:
        raw = human.ask("Chrome profile number (0 = incognito)", default=default if default != "incognito" else "0")
        chosen = resolve_browser_target(raw, profiles)
        if chosen is not None:
            _remember_target(env_path, chosen)
            return chosen
        console.warn(_unknown_profile(raw, len(profiles)))


def _remember_target(env_path: Path, target: BrowserTarget) -> None:
    if target.incognito or target.profile is None:
        remember_chrome_profile(env_path, "0")
        return
    remember_chrome_profile(env_path, target.profile.directory)


def remember_chrome_profile(env_path: Path, directory: str) -> None:
    """Store the chosen profile directory in .env and the current process."""
    os.environ[PROFILE_ENV] = directory
    key = f"{PROFILE_ENV}="
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    replaced = False
    updated: list[str] = []
    for line in lines:
        if line.strip().startswith(key):
            updated.append(f"{PROFILE_ENV}={directory}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{PROFILE_ENV}={directory}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _directory_sort_key(directory: str) -> tuple[int, int, str]:
    if directory == "Default":
        return (0, 0, directory)
    match = re.fullmatch(r"Profile (\d+)", directory)
    if match:
        return (1, int(match.group(1)), directory)
    return (2, 0, directory)


def resolve_browser_target(raw: str, profiles: list[ChromeProfile]) -> BrowserTarget | None:
    text = raw.strip()
    if text.lower() in {"0", "incognito"}:
        return BrowserTarget(incognito=True)
    profile = _match(profiles, text)
    if profile is None:
        return None
    return BrowserTarget(incognito=False, profile=profile)


def _unknown_profile(raw: str, count: int) -> str:
    if count == 0:
        return f"Unknown Chrome profile {raw!r}. The only choice is 0 (incognito)."
    return f"Unknown Chrome profile {raw!r}. Use 0 for incognito or a number from 1 to {count}."


def _matching_index(profiles: list[ChromeProfile], raw: str) -> int | None:
    if raw.strip().lower() in {"0", "incognito"}:
        return 0
    chosen = _match(profiles, raw)
    if chosen is None:
        return None
    return profiles.index(chosen) + 1


def _match(profiles: list[ChromeProfile], raw: str) -> ChromeProfile | None:
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(profiles):
            return profiles[index - 1]
        return None
    lowered = text.lower()
    for profile in profiles:
        if profile.directory.lower() == lowered or profile.name.lower() == lowered:
            return profile
    return None
