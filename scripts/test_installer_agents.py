#!/usr/bin/env python3
"""Regression tests for CLI/Desktop coding-agent detection."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import subprocess  # noqa: E402,F401

import installer_state as detector  # noqa: E402
from installer import Installer  # noqa: E402

REPO_DIR = SCRIPT_DIR.parent


class AgentPresenceTests(unittest.TestCase):
    def test_cli_command_counts_as_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(detector.shutil, "which", return_value="/usr/bin/claude"):
                source = detector.agent_presence("claude", Path(directory), "Linux")
        self.assertEqual(source, "claude (/usr/bin/claude)")

    def test_chatgpt_desktop_counts_as_codex_on_macos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "Applications" / "ChatGPT.app").mkdir(parents=True)
            with patch.object(detector.shutil, "which", return_value=None):
                source = detector.agent_presence("codex", home, "Darwin")
        self.assertEqual(source, str(home / "Applications" / "ChatGPT.app"))

    def test_claude_desktop_launcher_counts_as_claude_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            launcher = home / ".local" / "share" / "applications" / "claude-desktop.desktop"
            launcher.parent.mkdir(parents=True)
            launcher.touch()
            with patch.dict("os.environ", {"XDG_DATA_HOME": str(home / ".local" / "share")}, clear=False):
                with patch.object(detector.shutil, "which", return_value=None):
                    source = detector.agent_presence("claude", home, "Linux")
        self.assertEqual(source, str(launcher))

    def test_opencode_desktop_command_counts_as_opencode(self) -> None:
        def which(command: str) -> str | None:
            return "/usr/bin/opencode-desktop" if command == "opencode-desktop" else None

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(detector.shutil, "which", side_effect=which):
                source = detector.agent_presence("opencode", Path(directory), "Linux")
        self.assertEqual(source, "opencode-desktop (/usr/bin/opencode-desktop)")

    def test_qoder_shared_root_counts_as_presence_without_a_cli(self) -> None:
        # The CLI and the desktop IDE read one shared configuration root, so a
        # root created by either surface makes the platform usable.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".qoder-cn").mkdir()
            with patch.object(detector.shutil, "which", return_value=None):
                source = detector.agent_presence("qodercn", home, "Linux")
        self.assertEqual(source, str(home / ".qoder-cn"))

    def test_international_and_cn_qoder_roots_are_tracked_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".qoder").mkdir()
            with patch.object(detector.shutil, "which", return_value=None):
                self.assertEqual(detector.agent_presence("qoder", home, "Linux"), str(home / ".qoder"))
                self.assertEqual(detector.agent_presence("qodercn", home, "Linux"), "")

    def test_global_agents_land_in_the_shared_root_under_bare_names(self) -> None:
        # Qoder schedules subagents by bare name from the shared agents directory,
        # so the helper must write there when an explicit config root is given.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".qoder-cn"
            root.mkdir()
            helper = SCRIPT_DIR / "manage-global-agents.py"
            installed = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "install",
                    "--home",
                    str(home),
                    "--tool",
                    "qodercn",
                    "--config-root",
                    str(root),
                    "--source",
                    str(REPO_DIR / "skills"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout)
            agents = sorted((root / "agents").glob("*.md"))
            self.assertTrue(agents)
            self.assertFalse([item for item in agents if ":" in item.name])
            status = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "status",
                    "--home",
                    str(home),
                    "--tool",
                    "qodercn",
                    "--config-root",
                    str(root),
                    "--source",
                    str(REPO_DIR / "skills"),
                ],
                stdout=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(status.stdout.strip(), "up_to_date")

    def test_preflight_does_not_install_or_prompt_when_no_agent_exists(self) -> None:
        installer = Installer.__new__(Installer)
        with tempfile.TemporaryDirectory() as directory:
            installer.home = Path(directory)
            installer.os_name = "Linux"
            installer.logger = Mock()

            with patch.object(detector, "agent_presence", return_value=""):
                result = installer.preflight_agents()

        self.assertFalse(result)
        installer.logger.error.assert_called_once()
        message = installer.logger.error.call_args[0][0]
        for platform_name in ("Claude", "Codex", "OpenCode", "Qoder", "Qoder CN"):
            self.assertIn(platform_name, message)


if __name__ == "__main__":
    unittest.main()
