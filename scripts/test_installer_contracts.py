#!/usr/bin/env python3
"""Cross-module contract tests for the CodeKit installer.

These cover invariants that no single module owns: the registry / handler /
version-record parity, the single-sourced helper list, the terminal selector
protocol, the shared platform-agent plan, and the external-command timeout.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_agents as agents  # noqa: E402
import installer_config as config  # noqa: E402
import installer_qoder as qoder  # noqa: E402
import installer_state as detector  # noqa: E402
from installer import Installer, InstallerError, RunResult  # noqa: E402

HELPER_SCRIPT = SCRIPT_DIR / "manage-global-agents.py"
SELECTOR_SCRIPT = SCRIPT_DIR / "component-selector.py"


def bare_installer(home: Path) -> Installer:
    """装配仅含命令执行与平台探测所需字段的安装会话。"""
    installer = Installer.__new__(Installer)
    installer.home = home
    installer.os_name = "Linux"
    installer.state_dir = home / ".local" / "state" / "CodeKit"
    installer.state_dir.mkdir(parents=True, exist_ok=True)
    installer.logger = Mock()
    installer.marketplace_local = None
    installer.changed_paths = []
    return installer


def handler_keys(path: Path, function: str) -> set[str]:
    """用 AST 读取分发函数内 handlers 字典的键，避免执行安装逻辑。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Dict):
                    if any(
                        isinstance(target, ast.Name) and target.id == "handlers"
                        for target in inner.targets
                    ):
                        return {
                            key.value
                            for key in inner.value.keys
                            if isinstance(key, ast.Constant)
                        }
    raise AssertionError(f"handlers dict not found in {path}:{function}")


def version_record_ids(path: Path) -> set[str]:
    """用 AST 读取 save_versions 中登记的组件 ID 集合。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "save_versions":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "values"
                    for target in inner.targets
                ):
                    comprehension = inner.value
                    if isinstance(comprehension, ast.ListComp) and comprehension.generators:
                        iterator = comprehension.generators[0].iter
                        if isinstance(iterator, ast.Tuple):
                            return {
                                element.value
                                for element in iterator.elts
                                if isinstance(element, ast.Constant)
                            }
    raise AssertionError(f"save_versions values list not found in {path}")


class HelperContractTests(unittest.TestCase):
    def test_required_helper_list_matches_the_shipped_modules(self) -> None:
        for name in config.REQUIRED_HELPER_FILES:
            with self.subTest(name):
                self.assertTrue((SCRIPT_DIR / name).is_file())

    def test_required_helper_list_covers_every_shared_module(self) -> None:
        # 单一清单：卸载器与安装器共用同一份，不得再出现第二份平行列表。
        self.assertEqual(
            set(config.REQUIRED_HELPER_FILES),
            {
                "component-registry.py",
                "component-selector.py",
                "installer.py",
                "installer_agents.py",
                "installer_config.py",
                "installer_qoder.py",
                "installer_sources.py",
                "installer_state.py",
                "manage-global-agents.py",
            },
        )

    def test_missing_helper_reports_a_single_actionable_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError) as raised:
                config.require_helper_files(Path(directory))
        message = str(raised.exception)
        self.assertIn("installer helper missing", message)
        self.assertNotIn("\n", message)

    def test_broken_checkout_exits_with_a_controlled_message(self) -> None:
        # installer.py 必须在导入期自检：缺 helper 时输出单行错误而不是 traceback。
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            for name in ("installer.py", "installer_config.py"):
                (checkout / name).write_text(
                    (SCRIPT_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
                )
            result = subprocess.run(
                [sys.executable, str(checkout / "installer.py")],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installer helper missing", result.stdout)
        self.assertNotIn("Traceback", result.stdout)


class DispatchParityTests(unittest.TestCase):
    def test_registry_handlers_and_version_records_stay_in_parity(self) -> None:
        registry_ids = {item.component_id for item in detector.registry.COMPONENTS}
        self.assertEqual(
            handler_keys(SCRIPT_DIR / "installer.py", "install_component"), registry_ids
        )
        self.assertEqual(
            handler_keys(SCRIPT_DIR / "uninstaller.py", "uninstall_component"), registry_ids
        )
        self.assertEqual(version_record_ids(SCRIPT_DIR / "installer.py"), registry_ids)


class StrategyTableTests(unittest.TestCase):
    def test_channel_order_is_the_declared_priority(self) -> None:
        self.assertEqual(
            detector.registry.CHANNEL_ORDER,
            ("npm", "brew", "brew_cask", "apt", "pipx", "venv", "script", "release"),
        )

    def test_every_npm_target_is_on_the_official_allowlist(self) -> None:
        # 防止裸名（gh/rtk/glab 等李鬼包）被误装。
        for component in detector.registry.COMPONENTS:
            target = component.channels.get("npm")
            if target:
                self.assertIn(target, detector.registry.OFFICIAL_NPM_PACKAGES)

    def test_non_whitelisted_npm_name_is_rejected(self) -> None:
        bad = detector.registry.Component(
            "bad", "Bad", "ai", command="bad", channels={"npm": "gh"}
        )
        with self.assertRaises(ValueError):
            detector.registry.validate_registry(
                [bad], detector.registry.GROUPS, {}
            )

    def test_git_ai_is_no_longer_offered(self) -> None:
        self.assertIsNone(detector.registry.component_by_id("git_ai"))
        self.assertNotIn(
            "git_ai", {item.component_id for item in detector.registry.COMPONENTS}
        )

    def test_install_via_channels_picks_the_first_available_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            installer.bin_dir = Path(directory) / ".local" / "bin"
            calls: list[str] = []
            with patch.object(Installer, "requirements_met", return_value=(True, "")):
                with patch.object(
                    Installer, "channel_available", side_effect=lambda kind: kind == "brew"
                ):
                    with patch.object(
                        Installer,
                        "install_channel",
                        side_effect=lambda _component, kind: calls.append(kind) or True,
                    ):
                        self.assertTrue(installer.install_via_channels("rtk"))
        self.assertEqual(calls, ["brew"])

    def test_component_requirement_blocks_gitnexus_on_node_24_4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(detector, "command_version", return_value="v24.4.0"):
                met, reason = installer.requirements_met(
                    detector.registry.component_by_id("gitnexus")
                )
        self.assertFalse(met)
        self.assertIn("24.11.0", reason)

    def test_component_requirement_passes_gitnexus_on_node_24_11(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(detector, "command_version", return_value="v24.11.0"):
                met, _reason = installer.requirements_met(
                    detector.registry.component_by_id("gitnexus")
                )
        self.assertTrue(met)


class CliPresenceContractTests(unittest.TestCase):
    def test_presence_formatting_is_shared_between_platform_layers(self) -> None:
        # installer_agents 与 installer_qoder 必须渲染出同一种 `命令 (路径)` 形式。
        with patch.object(agents.shutil, "which", return_value=None):
            self.assertEqual(agents.cli_presence("claude", Path("/home/u")), "")
        self.assertEqual(
            qoder.describe_cli(("qoder", "qodercli"), "/usr/bin/qodercli"),
            "qodercli (/usr/bin/qodercli)",
        )
        self.assertEqual(qoder.describe_cli(("qoder",), ""), "")

    def test_default_agent_directories_are_single_sourced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertEqual(
                agents.default_agent_directory("claude", home),
                home / ".claude" / "agents",
            )
            self.assertEqual(
                agents.default_agent_directory("opencode", home),
                home / ".config" / "opencode" / "agents",
            )
            with self.assertRaises(ValueError):
                agents.default_agent_directory("unknown-tool", home)


class PlatformAgentPlanTests(unittest.TestCase):
    def test_install_and_uninstall_share_one_plan_per_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".qoder-cn"
            root.mkdir()
            installer = bare_installer(home)
            installer.marketplace_local = REPO_DIR
            with patch.dict(os.environ, {"QODERCN_CONFIG_DIR": str(root)}, clear=False):
                install_args, install_dir = installer.platform_agents_plan(
                    "install", "qodercn"
                )
                uninstall_args, uninstall_dir = installer.platform_agents_plan(
                    "uninstall", "qodercn"
                )
        # 两侧必须解析到同一目录与同一 --config-root，避免安装/卸载落点漂移。
        self.assertEqual(install_dir, uninstall_dir)
        self.assertEqual(install_dir, root / "agents")
        self.assertEqual(install_args[2], "install")
        self.assertEqual(uninstall_args[2], "uninstall")
        self.assertIn("--config-root", install_args)
        self.assertIn("--source", install_args)
        self.assertNotIn("--source", uninstall_args)

    def test_non_qoder_platform_uses_the_default_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = bare_installer(home)
            installer.marketplace_local = REPO_DIR
            args, target = installer.platform_agents_plan("install", "codex")
        self.assertEqual(target, home / ".codex" / "agents")
        self.assertNotIn("--config-root", args)


class RunCommandTimeoutTests(unittest.TestCase):
    def test_run_command_terminates_a_hung_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            result = installer.run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5
            )
        self.assertEqual(result.returncode, Installer.TIMEOUT_RETURNCODE)

    def test_qoder_run_forwards_its_timeout_to_run_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(
                installer, "run_command", return_value=RunResult(0, "ok")
            ) as run:
                returncode, output = installer.qoder_run(["--version"], timeout=7)
        run.assert_called_once_with(["--version"], timeout=7)
        self.assertEqual((returncode, output), (0, "ok"))


class SelectorProtocolTests(unittest.TestCase):
    def write_manifest(self, path: Path, fields: int = 6) -> None:
        rows = []
        for index, component in enumerate(detector.registry.COMPONENTS, 1):
            row = [
                str(index),
                component.description,
                component.group,
                detector.registry.GROUPS[component.group],
                " (插件未安装 · CLI 未安装（勾选后自动安装 CLI），勾选后安装插件)",
                "1",
            ]
            rows.append("\t".join(row[:fields]))
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def test_render_lists_every_component_including_both_qoder_flavors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.tsv"
            self.write_manifest(manifest)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR_SCRIPT),
                    "--mode",
                    "install",
                    "--manifest",
                    str(manifest),
                    "--render",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        for description in ("CodeKit Plugin (Qoder)", "CodeKit Plugin (Qoder CN)"):
            self.assertIn(description, result.stdout)
        # 本轮新增的 CLI/插件状态提示必须出现在渲染结果中。
        self.assertIn("CLI 未安装的平台插件默认不勾选", result.stdout)

    def test_render_outputs_the_uninstall_view_without_a_banner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.tsv"
            self.write_manifest(manifest)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR_SCRIPT),
                    "--mode",
                    "uninstall",
                    "--manifest",
                    str(manifest),
                    "--render",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Select Components to Uninstall", result.stdout)
        self.assertNotIn("Plugin", result.stdout.splitlines()[0])

    def test_malformed_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.tsv"
            self.write_manifest(manifest, fields=3)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SELECTOR_SCRIPT),
                    "--mode",
                    "install",
                    "--manifest",
                    str(manifest),
                    "--render",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid selector manifest line", result.stderr)


class SelectComponentsProtocolTests(unittest.TestCase):
    def build(self, home: Path) -> Installer:
        installer = bare_installer(home)
        installer.auto_yes = False
        installer.setup_path_selected = False
        installer.states = {
            component.component_id: detector.ComponentState(component.component_id)
            for component in detector.registry.COMPONENTS
        }
        return installer

    def drive(self, installer: Installer, returncode: int, stdout: str):
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        # 选择器分支要求存在 /dev/tty；CI 无控制终端，这里显式放行。
        with patch.object(detector, "agent_cli_presence", return_value=""):
            with patch("installer.Path.exists", return_value=True):
                with patch("subprocess.run", return_value=completed) as run:
                    installer.select_components()
        return run

    def test_confirm_parses_indices_and_path_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))
            self.drive(installer, 0, "selected=1,3;path=1\n")
        self.assertEqual(installer.selected, [installer.components[0].component_id, installer.components[2].component_id])
        self.assertTrue(installer.setup_path_selected)

    def test_quit_and_interrupt_use_dedicated_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))
            with self.assertRaises(SystemExit) as quit_exit:
                self.drive(installer, 10, "")
            self.assertEqual(quit_exit.exception.code, 0)
            with self.assertRaises(KeyboardInterrupt):
                self.drive(installer, 130, "")

    def test_invalid_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))
            with self.assertRaises(InstallerError):
                self.drive(installer, 0, "garbage")


class PluginComponentTagTests(unittest.TestCase):
    def test_missing_cli_is_reported_on_the_plugin_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            entry = agents.component_agent("claude_plugin")
            state = detector.ComponentState("claude_plugin", status="not_installed")
            with patch.object(detector, "agent_cli_presence", return_value=""):
                installer.agent_cli_cache = {}
                tag = installer.plugin_component_tag(state, entry)
        self.assertIn("CLI 未安装", tag)
        self.assertIn("勾选后安装插件", tag)


class GlobalAgentCleanupTests(unittest.TestCase):
    def run_helper(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HELPER_SCRIPT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    def test_uninstall_removes_managed_agents_and_keeps_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".claude" / "agents"
            target.mkdir(parents=True)
            user_file = target / "mine.md"
            user_file.write_text("# user-authored agent\n", encoding="utf-8")
            managed = target / "codekit-role.md"
            managed.write_text(
                "---\nname: codekit-role\nmanaged-by: codekit\n---\nbody\n",
                encoding="utf-8",
            )
            result = self.run_helper("uninstall", "--home", str(home), "--tool", "claude")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(user_file.is_file())
            self.assertFalse(managed.exists())

    def test_install_backs_up_a_foreign_agent_before_overwriting(self) -> None:
        source_agent = sorted((REPO_DIR / "skills").glob("*/agents/*.md"))[0]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            target = home / ".claude" / "agents"
            target.mkdir(parents=True)
            foreign = target / source_agent.name
            foreign.write_text("# user-authored, not managed by CodeKit\n", encoding="utf-8")
            result = self.run_helper(
                "install",
                "--home",
                str(home),
                "--tool",
                "claude",
                "--source",
                str(REPO_DIR / "skills"),
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            backups = home / ".local" / "state" / "CodeKit" / "agent-backups" / "claude"
            self.assertTrue(list(backups.glob(f"{source_agent.name}*")))


if __name__ == "__main__":
    unittest.main()
