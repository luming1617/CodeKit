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

import os  # noqa: E402
import subprocess  # noqa: E402,F401

import installer_agents as agents  # noqa: E402
import installer_config as config  # noqa: E402
import installer_state as detector  # noqa: E402
from installer import Installer, RunResult  # noqa: E402

REPO_DIR = SCRIPT_DIR.parent


def build_installer(home: Path) -> Installer:
    """装配一次只有 CLI 安装所需字段的安装会话。"""
    installer = Installer.__new__(Installer)
    installer.home = home
    installer.os_name = "Linux"
    installer.npm_prefix = home / ".npm-global"
    installer.bin_dir = home / ".local" / "bin"
    installer.logger = Mock()
    installer.changed_paths = []
    installer.agent_cli_cache = {}
    return installer


def presence_after_install():
    """首次探测为空，安装动作之后变为已安装（模拟 npm/脚本安装生效）。"""
    calls = {"count": 0}

    def probe(*_args, **_kwargs) -> str:
        calls["count"] += 1
        return "" if calls["count"] == 1 else "/usr/bin/agent"

    return probe


class AgentCliInstallTests(unittest.TestCase):
    def test_every_platform_has_an_install_channel(self) -> None:
        for entry in agents.AGENT_CLIS:
            with self.subTest(entry.key):
                self.assertTrue(agents.supports_install(entry, "Linux"))
                self.assertTrue(agents.supports_install(entry, "Darwin"))

    def test_install_plan_follows_the_global_channel_order(self) -> None:
        # npm 是全局第一优先通道，brew/cask 只在其后；install_plan 只按
        # CHANNEL_ORDER 列出已声明通道，可用性由调用方探测，因此与平台无关。
        # 镜像源由 set_npm 注入环境变量，命令本身不再附加 --registry。
        claude = agents.agent_cli("claude")
        expected = [
            ("npm", "install", "-g", "@anthropic-ai/claude-code@latest"),
            ("brew", "install", "--cask", "claude-code"),
        ]
        self.assertEqual(agents.install_plan(claude, "Darwin"), expected)
        self.assertEqual(agents.install_plan(claude, "Linux"), expected)

    def test_set_npm_injects_the_user_prefix_and_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = build_installer(home)
            with patch.dict(os.environ, {}, clear=False):
                installer.set_npm()
                self.assertEqual(os.environ["npm_config_prefix"], str(home / ".npm-global"))
                self.assertEqual(os.environ["npm_config_registry"], config.NPM_REGISTRY)
                self.assertTrue(
                    os.environ["PATH"].startswith(str(home / ".npm-global" / "bin"))
                )

    def test_npm_install_relies_on_the_injected_mirror(self) -> None:
        # 镜像源统一由 set_npm 注入的环境变量提供，执行命令不再逐处附加 --registry。
        with tempfile.TemporaryDirectory() as directory:
            installer = build_installer(Path(directory))
            with patch.object(Installer, "command_exists", return_value=True):
                with patch.object(Installer, "run_command", return_value=RunResult(0)) as run:
                    with patch.dict(os.environ, {}, clear=False):
                        self.assertTrue(installer.install_npm_package("gitnexus", "gitnexus"))
                        self.assertEqual(os.environ["npm_config_registry"], config.NPM_REGISTRY)
        self.assertEqual(run.call_args[0][0], ["npm", "install", "-g", "gitnexus@latest"])

    def test_missing_cli_is_installed_before_the_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = build_installer(Path(directory))
            with patch.object(Installer, "channel_available", return_value=True):
                with patch.object(detector, "agent_cli_presence", side_effect=presence_after_install()):
                    with patch.object(Installer, "run_command", return_value=RunResult(0)) as run:
                        self.assertTrue(installer.ensure_agent_cli("claude"))
        self.assertEqual(run.call_args[0][0][:3], ["npm", "install", "-g"])

    def test_unavailable_cli_channel_fails_without_touching_the_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = build_installer(Path(directory))
            with patch.object(Installer, "channel_available", return_value=True):
                with patch.object(detector, "agent_cli_presence", return_value=""):
                    with patch.object(Installer, "run_command", return_value=RunResult(1)):
                        with patch.object(Installer, "run_remote_script", return_value=False):
                            self.assertFalse(installer.ensure_agent_cli("claude"))

    def test_default_selection_skips_plugins_whose_cli_is_missing(self) -> None:
        # 未安装的组件默认勾选；但缺 CLI 的插件要用户显式勾选，避免静默装上 CLI。
        with tempfile.TemporaryDirectory() as directory:
            installer = build_installer(Path(directory))
            installer.auto_yes = True
            installer.states = {
                component.component_id: detector.ComponentState(component.component_id)
                for component in detector.registry.COMPONENTS
            }
            with patch.object(
                detector,
                "agent_cli_presence",
                side_effect=lambda key, *_args, **_kwargs: "qodercn (/usr/bin/qodercn)" if key == "qodercn" else "",
            ):
                selected = installer.select_components()
        self.assertIn("qodercn_plugin", selected)
        self.assertNotIn("claude_plugin", selected)
        self.assertNotIn("qoder_plugin", selected)
        self.assertIn("gh", selected)

    def test_script_channel_prepends_the_shared_entry_directory(self) -> None:
        # Qoder 的官方脚本把 CLI 写进配置根下的 entry/，安装后必须立刻可用。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = build_installer(home)
            with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
                with patch.object(Installer, "channel_available", return_value=True):
                    with patch.object(detector, "agent_cli_presence", side_effect=presence_after_install()):
                        with patch.object(Installer, "run_remote_script", return_value=True) as script:
                            self.assertTrue(installer.ensure_agent_cli("qodercn"))
                self.assertTrue(os.environ["PATH"].startswith(str(home / ".qoder-cn" / "entry")))
        script.assert_called_once_with(agents.agent_cli("qodercn").script_url)


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

    def test_opencode_agents_use_a_boolean_tool_map(self) -> None:
        # OpenCode validates agent `tools` as a tool-id → boolean mapping, so the
        # canonical comma separated list must be rewritten or `opencode plugin`
        # rejects the whole configuration.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            helper = SCRIPT_DIR / "manage-global-agents.py"
            installed = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "install",
                    "--home",
                    str(home),
                    "--tool",
                    "opencode",
                    "--source",
                    str(REPO_DIR / "skills"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout)
            root = home / ".config" / "opencode" / "agents"
            read_only = (root / "tech-critic.md").read_text(encoding="utf-8")
            writable = (root / "impl-implementer.md").read_text(encoding="utf-8")
            claude = (home / ".claude" / "agents" / "tech-critic.md").exists()
        self.assertIn("tools:\n  read: true\n", read_only)
        self.assertIn("  bash: false\n", read_only)
        self.assertIn("  write: false\n", read_only)
        self.assertIn("  bash: true\n", writable)
        self.assertIn("  edit: true\n", writable)
        self.assertNotIn("tools: Read", read_only)
        self.assertFalse(claude)

    def test_canonical_agents_keep_the_comma_separated_tools_list(self) -> None:
        # 只有 OpenCode 需要映射；Claude/Qoder 沿用 canonical frontmatter。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            helper = SCRIPT_DIR / "manage-global-agents.py"
            installed = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "install",
                    "--home",
                    str(home),
                    "--tool",
                    "claude",
                    "--source",
                    str(REPO_DIR / "skills"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout)
            content = (
                home / ".claude" / "agents" / "tech-critic.md"
            ).read_text(encoding="utf-8")
        self.assertIn("tools: Read, Grep, Glob\n", content)

    def test_preflight_continues_and_only_warns_when_no_cli_exists(self) -> None:
        installer = Installer.__new__(Installer)
        with tempfile.TemporaryDirectory() as directory:
            installer.home = Path(directory)
            installer.os_name = "Linux"
            installer.logger = Mock()

            with patch.object(detector, "agent_cli_presence", return_value=""):
                result = installer.preflight_agents()

        # 没有 CLI 不再中断流程：选择器里勾选平台插件后由安装器自动补 CLI。
        self.assertTrue(result)
        installer.logger.warn.assert_called_once()
        message = "\n".join(
            str(call.args[0]) for call in installer.logger.info.call_args_list
        )
        for platform_name in ("Claude Code", "Codex", "OpenCode", "Qoder", "Qoder CN"):
            self.assertIn(platform_name, message)


if __name__ == "__main__":
    unittest.main()
