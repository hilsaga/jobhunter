"""Chrome profile discovery and Playwright launch arguments."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cli.human_input import HumanInput
from src.exceptions import HumanInputRequired
from src.logging_setup import Console
from src.mcp_client.chrome_bridge import command_for_browser, command_for_profile
from src.mcp_client.chrome_profiles import (
    choose_chrome_profile,
    list_chrome_profiles,
    remember_chrome_profile,
)


def _write_chrome(root: Path) -> None:
    (root / "Default").mkdir()
    (root / "Profile 11").mkdir()
    (root / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Profile 11": {"name": "Work", "user_name": "work@example.com"},
                        "Default": {"name": "Personal", "user_name": "me@example.com"},
                        "Missing": {"name": "Gone", "user_name": ""},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class ChromeProfileTests(unittest.TestCase):
    def test_lists_existing_profile_folders_in_stable_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_chrome(root)
            profiles = list_chrome_profiles(root)
            self.assertEqual([profile.directory for profile in profiles], ["Default", "Profile 11"])
            self.assertEqual(profiles[1].label(), "Work — work@example.com (Profile 11)")

    def test_command_attaches_to_the_existing_profile(self) -> None:
        command = command_for_profile("npx -y @playwright/mcp@latest", "Profile 11")
        self.assertIn("--extension", command)
        self.assertIn("--profile-dir-name=Profile 11", command)
        self.assertNotIn("--config", command)
        again = command_for_profile(command, "Default")
        self.assertEqual(again.count("--extension"), 1)
        self.assertIn("--profile-dir-name=Default", again)
        self.assertNotIn("Profile 11", again)

    def test_zero_is_incognito(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_chrome(root)
            env_path = root / ".env"
            chosen = choose_chrome_profile(
                HumanInput(noninteractive=True),
                Console(),
                env_path=env_path,
                user_data_dir=root,
                selection="0",
            )
            self.assertTrue(chosen.incognito)
            self.assertIn("JOBHUNTER_CHROME_PROFILE=0", env_path.read_text(encoding="utf-8"))
        command = command_for_browser("npx -y @playwright/mcp@latest --extension", incognito=True)
        self.assertIn("--isolated", command)
        self.assertIn("--browser chrome", command)
        self.assertNotIn("--extension", command)

    def test_choice_is_saved_and_reused_when_noninteractive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_chrome(root)
            env_path = root / ".env"
            env_path.write_text("JOBHUNTER_LLM_API_KEY=secret\n", encoding="utf-8")
            human = HumanInput(noninteractive=False)
            with patch.object(human, "ask", return_value="2"):
                chosen = choose_chrome_profile(
                    human,
                    Console(),
                    env_path=env_path,
                    user_data_dir=root,
                )
            self.assertFalse(chosen.incognito)
            self.assertIsNotNone(chosen.profile)
            self.assertEqual(chosen.profile.directory, "Profile 11")
            text = env_path.read_text(encoding="utf-8")
            self.assertIn("JOBHUNTER_CHROME_PROFILE=Profile 11", text)
            self.assertIn("JOBHUNTER_LLM_API_KEY=secret", text)
            with patch.dict(os.environ, {"JOBHUNTER_CHROME_PROFILE": "Profile 11"}):
                again = choose_chrome_profile(
                    HumanInput(noninteractive=True),
                    Console(),
                    env_path=env_path,
                    user_data_dir=root,
                )
            self.assertIsNotNone(again.profile)
            self.assertEqual(again.profile.directory, "Profile 11")

    def test_noninteractive_without_a_saved_profile_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_chrome(root)
            with patch.dict(os.environ, {"JOBHUNTER_CHROME_PROFILE": ""}, clear=False):
                os.environ.pop("JOBHUNTER_CHROME_PROFILE", None)
                with self.assertRaises(HumanInputRequired):
                    choose_chrome_profile(
                        HumanInput(noninteractive=True),
                        Console(),
                        env_path=root / ".env",
                        user_data_dir=root,
                    )

    def test_remember_replaces_an_existing_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("JOBHUNTER_CHROME_PROFILE=Default\n", encoding="utf-8")
            remember_chrome_profile(env_path, "Profile 4")
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "JOBHUNTER_CHROME_PROFILE=Profile 4\n",
            )
