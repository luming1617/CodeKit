#!/usr/bin/env python3
"""Cross-module contract tests for the CodeKit installer.

These cover invariants that no single module owns: the registry / handler /
version-record parity, the single-sourced helper list, the terminal selector
protocol, the shared platform-agent plan, and the external-command timeout.
"""

from __future__ import annotations

import ast
import json
import os
import stat
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
import installer as installer_module  # noqa: E402
import installer_codex as codex  # noqa: E402
import installer_config as config  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_qoder as qoder  # noqa: E402
import installer_state as detector  # noqa: E402
import installer_toolchain as toolchain  # noqa: E402
from installer import Installer, InstallerError, RunResult  # noqa: E402
from uninstaller import Uninstaller  # noqa: E402
import uninstaller as uninstaller_module  # noqa: E402

HELPER_SCRIPT = SCRIPT_DIR / "manage-global-agents.py"
SELECTOR_SCRIPT = SCRIPT_DIR / "component-selector.py"

# 只要宿主导出过 QODER*_CONFIG_DIR，Qoder 配置根就会被解析到真实用户目录，用例
# 会去改用户机器上的 settings.json 与 skills。整模块运行期间先移除它们。
_AMBIENT_ENV_PATCH = None


def setUpModule() -> None:
    """隔离宿主导出的 Qoder 根目录覆盖。"""
    global _AMBIENT_ENV_PATCH
    _AMBIENT_ENV_PATCH = patch.dict(os.environ, {}, clear=False)
    _AMBIENT_ENV_PATCH.start()
    for key in ("QODER_CONFIG_DIR", "QODERCN_CONFIG_DIR"):
        os.environ.pop(key, None)


def tearDownModule() -> None:
    """恢复本模块运行前的环境。"""
    global _AMBIENT_ENV_PATCH
    if _AMBIENT_ENV_PATCH is not None:
        _AMBIENT_ENV_PATCH.stop()
        _AMBIENT_ENV_PATCH = None


def bare_installer(home: Path) -> Installer:
    """装配仅含命令执行与平台探测所需字段的安装会话。"""
    installer = Installer.__new__(Installer)
    installer.home = home
    # 固定为 Linux 适配对象：用例不应随宿主平台改变行为。
    installer.platform = platform_layer.platform_for("Linux")
    installer.os_name = installer.platform.os_name
    installer.state_dir = home / ".local" / "state" / "CodeKit"
    installer.state_dir.mkdir(parents=True, exist_ok=True)
    installer.version_file = installer.state_dir / "installed_versions"
    installer.context = detector.Context(
        home=home,
        marketplace_local=None,
        version_file=installer.version_file,
    )
    # 飞书 skills 的归属记账默认未同步；用到它的用例自己置位。
    installer.feishu_skills_synced = False
    installer.bin_dir = home / ".local" / "bin"
    installer.npm_bin_dir = home / ".npm-global" / "bin"
    installer.logger = Mock()
    installer.marketplace_local = None
    installer.changed_paths = []
    installer.selected = []
    # __init__ 会建这个快照；绕过 __init__ 的夹具必须自己补上，否则任何走到
    # requirements_met / toolchain_python 的路径都会 AttributeError。
    installer.toolchain = toolchain.RuntimeState()
    return installer


def bare_uninstaller(home: Path) -> Uninstaller:
    """装配仅含卸载所需字段的卸载会话；平台对象用 Mock，便于逐例控制交互能力。

    路径助手接回真实的平台对象：卸载要删工具链根与 venv，用 Mock 会让 rmtree 拿到
    一个非路径对象并抛 TypeError，用例就测不到清理逻辑。交互能力仍由 Mock 控制。
    """
    uninstaller = Uninstaller.__new__(Uninstaller)
    uninstaller.home = home
    paths = platform_layer.platform_for("Windows")
    uninstaller.platform = Mock(can_interact=Mock(return_value=True))
    for helper in (
        "toolchain_root",
        "npm_prefix_private",
        "venv_dir",
        "venv_exe",
        "shim_name",
        "binary_filename",
        "state_dir",
    ):
        setattr(uninstaller.platform, helper, getattr(paths, helper))
    uninstaller.os_name = "Windows"
    uninstaller.bin_dir = home / ".local" / "bin"
    uninstaller.logger = Mock()
    uninstaller.purge = False
    uninstaller.toolchain_purged = False
    return uninstaller


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


def version_record_ids(installer: Installer) -> set[str]:
    """跑一次 save_versions，返回状态文件里实际写入的键集合。"""
    installer.save_versions()
    return {
        line.split("=", 1)[0]
        for line in installer.version_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    }


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
                "installer_codex.py",
                "installer_config.py",
                "installer_platform.py",
                "installer_preflight.py",
                "installer_qoder.py",
                "installer_sources.py",
                "installer_state.py",
                "installer_toolchain.py",
                "manage-global-agents.py",
                # 技能侧共享脚本：Codex 的 TOML 读写与探测只有那一份实现，
                # installer_codex 按路径加载它，因此它也是安装器的硬依赖。
                "../skills/_shared/scripts/probe-collab.py",
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
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(detector, "component_version", return_value="1.2.3"):
                recorded = version_record_ids(installer)
        self.assertEqual(registry_ids, recorded)


class StrategyTableTests(unittest.TestCase):
    def test_channel_order_is_the_declared_priority(self) -> None:
        self.assertEqual(
            detector.registry.CHANNEL_ORDER,
            (
                "npm",
                "brew",
                "brew_cask",
                "apt",
                "winget",
                "venv",
                "release",
                "script",
                "powershell",
            ),
        )

    def test_root_channel_declaration_requires_an_entry_kind(self) -> None:
        """落点不变量在注册表层就成立：漏配 entry_kind 的根内通道过不了校验。"""
        registry = detector.registry
        leaked = registry.Component(
            "sneaky",
            "Sneaky",
            "intel",
            command="sneaky",
            channels={"venv": "sneaky"},
        )
        with self.assertRaises(ValueError):
            registry.validate_registry((leaked,), dict(registry.GROUPS), {})

    def test_root_bound_component_cannot_mix_in_an_outside_channel(self) -> None:
        registry = detector.registry
        mixed = registry.Component(
            "mixed",
            "Mixed",
            "intel",
            command="mixed",
            channels={"venv": "mixed", "brew": "mixed"},
            entry_kind="python-venv",
        )
        with self.assertRaises(ValueError):
            registry.validate_registry((mixed,), dict(registry.GROUPS), {})

    def test_windows_only_channels_never_leak_into_other_platforms(self) -> None:
        # winget/powershell 的存在不得影响 macOS/Linux 的通道选择。
        order = detector.registry.CHANNEL_ORDER
        self.assertGreater(order.index("winget"), order.index("apt"))
        self.assertLess(order.index("release"), order.index("script"))
        self.assertLess(order.index("release"), order.index("powershell"))

    def test_release_is_probed_before_the_remote_script(self) -> None:
        # 校验过摘要的发布资产必须排在 `curl | bash` 之前，否则 rtk 这类同时声明
        # 两个通道的组件会一直走远程脚本。
        order = detector.registry.CHANNEL_ORDER
        self.assertLess(order.index("release"), order.index("script"))

    def test_rtk_release_asset_names_follow_the_rust_target_triple(self) -> None:
        component = detector.registry.component_by_id("rtk")
        spec = component.release
        self.assertIsNotNone(spec)
        # 脚本通道保留为发布资产解析失败时的回退。
        self.assertEqual(component.channels["release"], spec.repo)
        self.assertIn("script", component.channels)
        cases = {
            ("Linux", "x86_64"): "rtk-x86_64-unknown-linux-musl.tar.gz",
            # arm64 的 Linux 资产是 gnu 而不是 musl（实测 v0.49.0）。
            ("Linux", "arm64"): "rtk-aarch64-unknown-linux-gnu.tar.gz",
            ("Darwin", "arm64"): "rtk-aarch64-apple-darwin.tar.gz",
            ("Darwin", "x86_64"): "rtk-x86_64-apple-darwin.tar.gz",
            # Windows 资产是 msvc 三元组 + zip，与 Unix 的 tar.gz 不同。
            ("Windows", "x86_64"): "rtk-x86_64-pc-windows-msvc.zip",
        }
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            for (os_name, arch), expected in cases.items():
                with self.subTest(platform=f"{os_name}-{arch}"):
                    installer.os_name = os_name
                    with patch.object(installer, "release_arch", return_value=arch):
                        self.assertEqual(
                            installer.release_asset_name(spec, "0.48.0"), expected
                        )

    def test_windows_release_assets_use_the_native_os_token_and_zip(self) -> None:
        # 两个项目都用全小写 windows（实测 gh_2.100.0_windows_amd64.zip、glab_1.117.0_windows_amd64.zip）；
        # 写成 `Windows` 会解析不到资产。
        cases = {
            "gh": "gh_2.100.0_windows_amd64.zip",
            "glab": "glab_1.117.0_windows_amd64.zip",
        }
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            installer.os_name = "Windows"
            for component_id, expected in cases.items():
                with self.subTest(component=component_id):
                    spec = detector.registry.component_by_id(component_id).release
                    with patch.object(installer, "release_arch", return_value="x86_64"):
                        self.assertEqual(
                            installer.release_asset_name(spec, expected.split("_")[1]),
                            expected,
                        )

    def test_windows_only_channels_are_declared_where_windows_needs_them(self) -> None:
        # gh/glab 在 Windows 上没有 brew/apt，必须有一条可用的原生通道。
        for component_id in ("gh", "glab"):
            with self.subTest(component=component_id):
                channels = detector.registry.component_by_id(component_id).channels
                self.assertIn("winget", channels)
                self.assertIn("Windows", detector.registry.component_by_id(component_id).release.os_tokens)
        qoder = detector.registry.AGENT_CLI_CHANNELS["qoder"]
        self.assertTrue(qoder["powershell"].startswith("https://"))
        self.assertTrue(qoder["script"].startswith("https://"))
        self.assertIn("winget", detector.registry.AGENT_CLI_CHANNELS["claude"])

    def test_dingtalk_has_no_static_unzip_requirement(self) -> None:
        # Windows 用 Expand-Archive，静态 requires 会让该组件在 Windows 上被误判。
        component = detector.registry.component_by_id("dingtalk_cli")
        self.assertNotIn("unzip", component.requires)

    def test_rtk_prefers_the_verified_release_asset_over_the_script(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            calls: list[str] = []
            with patch.object(installer, "requirements_met", return_value=(True, "")), patch.object(
                installer, "channel_available", return_value=True
            ), patch.object(
                installer,
                "install_channel",
                side_effect=lambda _component, kind: calls.append(kind) or kind == "release",
            ):
                self.assertTrue(installer.install_via_channels("rtk"))
        self.assertEqual(calls, ["brew", "release"])

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
                encoding="utf-8",
                errors="replace",
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
                encoding="utf-8",
                errors="replace",
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
                encoding="utf-8",
                errors="replace",
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
        # 选择器分支要求可交互终端；CI 无控制终端，这里显式放行。
        with patch.object(detector, "agent_cli_presence", return_value=""):
            with patch.object(platform_layer.Platform, "can_interact", return_value=True):
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


def plugin_context(home: Path) -> detector.Context:
    """构造以仓库为插件源的检测上下文，供状态与版本用例共用。"""
    return detector.Context(
        home=home,
        marketplace_local=REPO_DIR,
        version_file=home / ".local" / "state" / "CodeKit" / "installed_versions",
    )


class InstalledPluginVersionTests(unittest.TestCase):
    def test_claude_registry_version_wins_over_the_repository_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            registry = home / ".claude" / "plugins" / "installed_plugins.json"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps({"version": 2, "plugins": {"codekit@co-daming": [{"version": "9.9.9"}]}}),
                encoding="utf-8",
            )
            version = detector.installed_plugin_version("claude_plugin", plugin_context(home))
        self.assertEqual(version, "9.9.9")

    def test_codex_version_comes_from_the_cache_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex" / "plugins" / "cache" / "co-daming" / "codekit" / "1.2.3").mkdir(parents=True)
            version = detector.installed_plugin_version("codex_plugin", plugin_context(home))
        self.assertEqual(version, "1.2.3")

    def test_opencode_falls_back_to_the_repository_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = plugin_context(Path(directory))
            version = detector.installed_plugin_version("opencode_plugin", context)
        self.assertEqual(version, detector.repository_plugin_version(context))


class PluginStateVersionTests(unittest.TestCase):
    def test_marketplace_revision_is_not_reported_as_a_plugin_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = plugin_context(Path(directory))
            with patch.object(detector, "plugin_ready", return_value=True), patch.object(
                detector, "platform_subagents_state", return_value="up_to_date"
            ), patch.object(detector, "saved_marketplace_sha", return_value="aaa1111"), patch.object(
                detector, "marketplace_sha", return_value="bbb2222"
            ):
                state = detector.detect_component("claude_plugin", context, {})
        self.assertEqual(state.status, "installed")
        self.assertEqual(state.latest_version, detector.repository_plugin_version(context))


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

    def test_installed_plugin_tag_prints_its_version_and_cli_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            entry = agents.component_agent("claude_plugin")
            state = detector.ComponentState(
                "claude_plugin", status="installed", installed_version="1.2.3"
            )
            with patch.object(detector, "agent_cli_presence", return_value="claude"), patch.object(
                agents, "cli_version", return_value="2.1.0"
            ):
                installer.agent_cli_cache = {}
                tag = installer.plugin_component_tag(state, entry)
        self.assertIn("插件有更新 1.2.3", tag)
        self.assertIn("CLI 已安装 2.1.0", tag)


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
            # 备份根跟随平台状态目录：Windows 上是 %LOCALAPPDATA%，测试里指到临时
            # 目录，避免写进真实用户目录；父进程改了 os.environ，子进程会继承。
            local_appdata = str(home / "AppData" / "Local")
            with patch.dict(os.environ, {"LOCALAPPDATA": local_appdata}, clear=False):
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
                backups = (
                    platform_layer.platform_for().state_dir(home) / "agent-backups" / "claude"
                )
                self.assertTrue(list(backups.glob(f"{source_agent.name}*")))


class _FakeUrlResponse:
    """最小可用的 urlopen 返回值，供镜像与代理回退用例替换网络调用。"""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "_FakeUrlResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class HomebrewMirrorTests(unittest.TestCase):
    def test_mirror_environment_targets_ustc_and_disables_auto_update(self) -> None:
        env = config.HOMEBREW_MIRROR_ENV
        self.assertEqual(env["HOMEBREW_NO_AUTO_UPDATE"], "1")
        for key in (
            "HOMEBREW_API_DOMAIN",
            "HOMEBREW_BOTTLE_DOMAIN",
            "HOMEBREW_BREW_GIT_REMOTE",
            "HOMEBREW_CORE_GIT_REMOTE",
            "HOMEBREW_CASK_GIT_REMOTE",
        ):
            with self.subTest(key):
                self.assertTrue(env[key].startswith("https://mirrors.ustc.edu.cn/"))

    def test_brew_overrides_preexisting_values_with_the_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            previous = {key: os.environ.get(key) for key in config.HOMEBREW_MIRROR_ENV}
            try:
                os.environ.update({key: "pre-existing" for key in config.HOMEBREW_MIRROR_ENV})
                with patch.object(installer, "run_command", return_value=RunResult(0)) as run:
                    installer.brew("install", "rtk")
                self.assertEqual(run.call_args[0][0], ["brew", "install", "rtk"])
                for key, value in config.HOMEBREW_MIRROR_ENV.items():
                    self.assertEqual(os.environ.get(key), value)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class GithubProxyTests(unittest.TestCase):
    GITHUB_URLS = (
        "https://github.com/rtk-ai/rtk/releases/download/v1.2.3/rtk.tar.gz",
        "https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh",
        "https://api.github.com/repos/rtk-ai/rtk/releases/latest",
        "https://objects.githubusercontent.com/codekit/asset",
    )

    def test_download_urls_put_the_proxy_first_and_keep_a_direct_fallback(self) -> None:
        for url in self.GITHUB_URLS:
            with self.subTest(url):
                self.assertEqual(config.download_urls(url), (f"{config.GH_PROXY}/{url}", url))

    def test_non_github_urls_are_never_proxied(self) -> None:
        for url in (
            "https://mirrors.ustc.edu.cn/homebrew-bottles/api/rtk.json",
            "https://registry.npmmirror.com/gitnexus/latest",
            "https://gitlab.com/api/v4/projects/34675721/releases/permalink/latest",
            "https://qoder.com/install",
        ):
            with self.subTest(url):
                self.assertEqual(config.download_urls(url), (url,))
                self.assertEqual(config.probe_urls(url), (url,))

    def test_version_probes_skip_the_direct_github_attempt(self) -> None:
        # 探测失败只是退回“已是最新”，不值得再等一次国内必然超时的直连。
        url = "https://api.github.com/repos/rtk-ai/rtk/releases/latest"
        self.assertEqual(config.probe_urls(url), (f"{config.GH_PROXY}/{url}",))

    def test_fetch_text_falls_back_to_the_direct_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            attempts: list[str] = []

            def fake_urlopen(request, timeout=None):
                attempts.append(request.full_url)
                if len(attempts) == 1:
                    raise OSError("proxy unreachable")
                return _FakeUrlResponse(b"install rtk\n")

            url = "https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh"
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                text = installer.fetch_text(url)
        self.assertEqual(text, "install rtk\n")
        self.assertEqual(attempts, [f"{config.GH_PROXY}/{url}", url])

    def test_download_release_tries_the_proxy_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            requested: list[str] = []

            def fake_run_command(args, **kwargs):
                requested.append(args[-1])
                Path(args[args.index("-o") + 1]).write_bytes(b"payload")
                return RunResult(0)

            url = "https://github.com/cli/cli/releases/download/v2.0.0/gh.tar.gz"
            with patch.object(installer, "run_command", side_effect=fake_run_command):
                archive = installer.download_release(url, "")
                try:
                    self.assertIsNotNone(archive)
                    self.assertEqual(requested, [f"{config.GH_PROXY}/{url}"])
                finally:
                    if archive is not None:
                        archive.unlink(missing_ok=True)

    def test_version_probe_sends_the_github_api_through_the_proxy(self) -> None:
        attempts: list[str] = []

        def fake_urlopen(request, timeout=None):
            attempts.append(request.full_url)
            return _FakeUrlResponse(json.dumps({"tag_name": "v0.9.0"}).encode("utf-8"))

        url = "https://api.github.com/repos/rtk-ai/rtk/releases/latest"
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            document = detector.fetch_json(url)
        self.assertEqual(document, {"tag_name": "v0.9.0"})
        self.assertEqual(attempts, [f"{config.GH_PROXY}/{url}"])


class BrewChannelFallbackTests(unittest.TestCase):
    def test_command_installed_outside_brew_lets_the_next_channel_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=True), patch.object(
                installer, "brew_manages", return_value=False
            ), patch.object(installer, "brew") as brew:
                self.assertFalse(installer.brew_install_or_upgrade("rtk", "rtk"))
            brew.assert_not_called()

    def test_missing_formula_fails_the_channel_after_a_single_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=False), patch.object(
                installer, "brew_manages", return_value=False
            ), patch.object(installer, "brew", return_value=RunResult(1)) as brew:
                self.assertFalse(installer.brew_install_or_upgrade("rtk", "rtk"))
            brew.assert_called_once_with("install", "rtk")

    def test_failed_upgrade_keeps_a_brew_managed_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=True), patch.object(
                installer, "brew_manages", return_value=True
            ), patch.object(installer, "brew", return_value=RunResult(1)) as brew:
                self.assertTrue(installer.brew_install_or_upgrade("rtk", "rtk"))
            brew.assert_called_once_with("upgrade", "rtk")

    def test_cask_installed_outside_brew_lets_the_next_channel_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=True), patch.object(
                installer, "brew_manages", return_value=False
            ), patch.object(installer, "brew") as brew:
                self.assertFalse(installer.install_brew_cask("claude-code", "claude"))
            brew.assert_not_called()

    def test_rtk_upgrade_falls_through_to_the_script_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "requirements_met", return_value=(True, "")), patch.object(
                installer, "channel_available", side_effect=lambda kind: kind in {"brew", "script"}
            ), patch.object(
                installer, "brew_install_or_upgrade", return_value=False
            ), patch.object(installer, "run_remote_script", return_value=True) as script, patch.object(
                installer, "command_exists", return_value=True
            ):
                self.assertTrue(installer.install_via_channels("rtk"))
            self.assertEqual(
                script.call_args[0][0],
                detector.registry.component_by_id("rtk").channels["script"],
            )


class FeishuSkillsSyncTests(unittest.TestCase):
    """飞书 skills 同步是附加项：失败不判组件失败，已装则不再 clone GitHub。"""

    def _installer(self, home: Path, sync_result: int):
        installer = bare_installer(home)
        installer.install_via_channels = Mock(return_value=True)
        installer.command_exists = Mock(return_value=True)
        installer.run_command = Mock(return_value=RunResult(sync_result))
        return installer

    def test_failed_skills_sync_still_reports_the_component_as_done(self) -> None:
        # 实测 `npx skills add` 会 clone github.com；国内网络失败时飞书 CLI 本身已装好，
        # 不能因为这一步把整个组件判成「未完成」。
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), sync_result=1)
            self.assertTrue(installer.install_feishu_cli())
            installer.logger.error.assert_not_called()
            self.assertTrue(installer.logger.warn.called)

    def test_skills_sync_success_keeps_the_same_return_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), sync_result=0)
            self.assertTrue(installer.install_feishu_cli())
            self.assertTrue(installer.run_command.called)
            installer.logger.warn.assert_not_called()

    def test_installed_skills_skip_the_network_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".agents" / "skills" / "lark-base").mkdir(parents=True)
            installer = self._installer(home, sync_result=0)
            self.assertTrue(installer.install_feishu_cli())
            installer.run_command.assert_not_called()

    def test_missing_skills_library_still_runs_the_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".agents" / "skills" / "dingtalk-chat").mkdir(parents=True)
            installer = self._installer(home, sync_result=0)
            self.assertTrue(installer.install_feishu_cli())
            self.assertTrue(installer.run_command.called)


class GraphifyRegistrationCwdTests(unittest.TestCase):
    """graphify 的注册命令按 cwd 落盘，安装器必须显式给出目录而不是继承启动位置。

    实测：`graphify install --platform opencode` 无条件往 cwd 写项目级
    `.opencode/plugins/graphify.js`，`graphify hook install` 要求 cwd 在 git 仓库内。
    从 `C:\\Windows` 启动安装器时前者抛 PermissionError、后者抛
    `No git repository found at or above C:\\Windows`，整个组件被判失败。
    """

    def _installer(self, home: Path, repo_root: str = "") -> Installer:
        installer = bare_installer(home)
        installer.install_via_channels = Mock(return_value=True)
        installer.command_exists = Mock(return_value=True)
        installer.run_capture = Mock(
            return_value=RunResult(0, repo_root) if repo_root else RunResult(1)
        )
        installer.run_command = Mock(return_value=RunResult(0))
        return installer

    def _commands(self, installer: Installer) -> list[list[str]]:
        return [entry.args[0] for entry in installer.run_command.call_args_list]

    def _cwds(self, installer: Installer) -> set[Path]:
        return {entry.kwargs.get("cwd") for entry in installer.run_command.call_args_list}

    def test_registration_runs_inside_the_launched_repository(self) -> None:
        # 在仓库里启动时，graphify 的项目级产物（.opencode/plugins、git hooks）应当
        # 落在这个仓库，而不是别处。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "project"
            installer = self._installer(home, repo_root=str(project))
            self.assertTrue(installer.install_graphify())
            self.assertIn(["graphify", "hook", "install"], self._commands(installer))
            self.assertEqual(self._cwds(installer), {project})

    def test_hook_failure_inside_a_repository_still_fails_the_component(self) -> None:
        # 仓库里钩子装不上是真失败，不能被「不在仓库就跳过」的逻辑吞掉。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = self._installer(home, repo_root=str(home / "project"))
            installer.run_command = Mock(
                side_effect=lambda args, **_: RunResult(
                    1 if list(args)[:2] == ["graphify", "hook"] else 0
                )
            )
            self.assertFalse(installer.install_graphify())

    def test_missing_repository_skips_hooks_into_a_writable_owned_directory(self) -> None:
        # 仓库外启动（双击快捷方式时 cwd 可能是 C:\Windows）：没有仓库可挂钩子，
        # 注册改为落在安装器自己的可写目录，只告警、不把组件判成失败。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = self._installer(home)
            self.assertTrue(installer.install_graphify())
            self.assertNotIn(["graphify", "hook", "install"], self._commands(installer))
            fallback = installer.state_dir / "graphify-project"
            self.assertEqual(self._cwds(installer), {fallback})
            self.assertTrue(fallback.is_dir())
            self.assertTrue(installer.logger.warn.called)


class WindowsPlatformAdapterTests(unittest.TestCase):
    """Windows 分支的离线用例：靠假 Platform 覆盖，不要求宿主是 Windows。"""

    def test_windows_paths_never_reuse_posix_locations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environ = {
                "LOCALAPPDATA": str(home / "LocalAppData"),
                "APPDATA": str(home / "Roaming"),
            }
            with patch.dict(os.environ, environ, clear=False):
                windows = platform_layer.platform_for("Windows")
                self.assertTrue(windows.is_windows)
                self.assertTrue(windows.os_name == "Windows")
                self.assertEqual(windows.state_dir(home), home / "LocalAppData" / "CodeKit")
                prefix = windows.npm_prefix(home)
                self.assertEqual(prefix, home / "Roaming" / "npm")
                # npm 的 shim 直接落在前缀根目录，Windows 上没有 bin/ 子目录。
                self.assertEqual(windows.npm_bin_dir(prefix), prefix)
                self.assertEqual(windows.bin_dir(home, prefix), prefix)
                # 没有可写的 shell 配置。
                self.assertIsNone(windows.shell_config(home))
                self.assertEqual(windows.shell_rc_files(home), ())

    def test_windows_probed_npm_prefix_wins_over_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            windows = platform_layer.platform_for("Windows")
            probed = home / "custom" / "npm"
            self.assertEqual(windows.npm_prefix(home, str(probed)), probed)
            # 探测为空时回落到 %APPDATA%\npm，而不是 POSIX 的 ~/.npm-global。
            with patch.dict(os.environ, {"APPDATA": str(home / "Roaming")}, clear=False):
                self.assertEqual(windows.npm_prefix(home, "  "), home / "Roaming" / "npm")

    def test_windows_venv_layout_and_entry_names(self) -> None:
        windows = platform_layer.platform_for("Windows")
        venv = Path("C:/CodeKit/venvs/graphify")
        self.assertEqual(windows.venv_pip(venv).name, "pip.exe")
        self.assertEqual(windows.venv_exe(venv, "graphify").name, "graphify.exe")
        self.assertEqual(windows.shim_name("graphify"), "graphify.cmd")
        self.assertEqual(windows.binary_filename("rtk"), "rtk.exe")
        self.assertEqual(windows.binary_filename("gh.exe"), "gh.exe")
        self.assertEqual(
            windows.release_target(Path("C:/npm"), "rtk"), Path("C:/npm/rtk.exe")
        )

    def test_windows_command_shim_is_a_cmd_forwarder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            windows = platform_layer.platform_for("Windows")
            target = Path(directory) / "venvs" / "Scripts" / "graphify.exe"
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            shim = windows.expose_command(Path(directory) / "npm", "graphify", target)
            self.assertEqual(shim.name, "graphify.cmd")
            self.assertIn(str(target), shim.read_text(encoding="utf-8"))

    @unittest.skipIf(
        platform_layer.is_windows(), "Windows 用 .cmd 转发脚本，而不是符号链接"
    )
    def test_posix_command_exposure_stays_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            linux = platform_layer.platform_for("Linux")
            target = Path(directory) / "real"
            target.write_text("", encoding="utf-8")
            link = linux.expose_command(Path(directory) / "bin", "graphify", target)
            self.assertTrue(link.is_symlink())

    def test_rmtree_clears_the_read_only_bit(self) -> None:
        # Windows 上删除只读文件会抛 PermissionError，适配层必须先清只读位。
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory) / "tree"
            tree.mkdir()
            locked = tree / "locked.txt"
            locked.write_text("x", encoding="utf-8")
            os.chmod(locked, stat.S_IREAD)
            platform_layer.rmtree(tree)
            self.assertFalse(tree.exists())

    def test_windows_only_channels_are_unavailable_off_windows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=True), patch.object(
                platform_layer, "remote_script_command", return_value=("/usr/bin/pwsh",)
            ):
                self.assertFalse(installer.channel_available("winget"))
                self.assertFalse(installer.channel_available("powershell"))

    def test_resolve_command_expands_a_bare_name_to_a_full_path(self) -> None:
        # Windows 的 CreateProcess 不展开 PATHEXT，必须先把 argv[0] 换成全路径。
        self.assertEqual(
            Installer._resolve_command([sys.executable, "-c", "pass"])[0], sys.executable
        )
        resolved = platform_layer.resolve_exe("python3")
        if resolved:
            self.assertEqual(Installer._resolve_command(["python3", "-V"])[0], resolved)

    def test_resolve_command_leaves_paths_alone(self) -> None:
        self.assertEqual(Installer._resolve_command(["./tool", "-v"])[0], "./tool")
        self.assertEqual(Installer._resolve_command(["C:/tools/x.exe"])[0], "C:/tools/x.exe")

    def test_resolve_argv_expands_only_a_bare_command_name(self) -> None:
        # 平台层是解析 argv[0] 的唯一入口：状态探测与安装/卸载共用同一份规则。
        with patch.object(platform_layer, "resolve_exe", return_value="/npm/lark-cli.cmd"):
            self.assertEqual(
                platform_layer.resolve_argv(["lark-cli", "--version"]),
                ["/npm/lark-cli.cmd", "--version"],
            )
            self.assertEqual(
                platform_layer.resolve_argv(["C:/tools/x.exe", "-v"]),
                ["C:/tools/x.exe", "-v"],
            )
            self.assertEqual(platform_layer.resolve_argv(["./tool"]), ["./tool"])


    def windows_installer(self, home: Path) -> Installer:
        installer = bare_installer(home)
        installer.platform = platform_layer.platform_for("Windows")
        installer.os_name = installer.platform.os_name
        return installer

    def test_script_channel_is_disabled_on_windows_even_with_git_bash(self) -> None:
        # Git for Windows 也会把 bash 放进 PATH；只看"有没有 shell"的话，Windows 上会
        # 用 Git Bash 执行 Unix 安装脚本，把文件装进 MSYS 目录树而不是 npm 目录。
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(platform_layer, "remote_script_command", return_value=("/usr/bin/bash",)):
                self.assertFalse(
                    self.windows_installer(Path(directory)).channel_available("script")
                )
                self.assertTrue(bare_installer(Path(directory)).channel_available("script"))

    def test_windows_manual_note_never_suggests_a_bash_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.windows_installer(Path(directory))
            rtk_note = installer.manual_note(detector.registry.component_by_id("rtk"))
            gh_note = installer.manual_note(detector.registry.component_by_id("gh"))
            graphify_note = installer.manual_note(
                detector.registry.component_by_id("graphify")
            )
        self.assertNotIn("| bash", rtk_note)
        self.assertIn("rtk-ai/rtk", rtk_note)
        self.assertIn("winget install --id GitHub.cli", gh_note)
        self.assertNotIn("| bash", graphify_note)


    def test_refresh_process_path_only_extends_and_stays_idempotent(self) -> None:
        # winget / 官方 .ps1 装完后命令在注册表 PATH 里，进程内必须能立刻看到它。
        environ = {"PATH": "C:\\existing"}
        first = platform_layer.refresh_process_path(environ)
        if not platform_layer.is_windows():
            self.assertEqual(first, ())
            self.assertEqual(environ["PATH"], "C:\\existing")
            return
        self.assertTrue(environ["PATH"].startswith("C:\\existing"))
        self.assertTrue(first)
        # 第二次调用不应再新增，避免 PATH 无限增长。
        self.assertEqual(platform_layer.refresh_process_path(environ), ())

    def test_powershell_literal_escapes_single_quotes(self) -> None:
        # 提权脚本靠它拼参数，含引号的路径不能把命令拆散。
        self.assertEqual(platform_layer.powershell_literal("a'b"), "'a''b'")

    def test_elevation_is_refused_off_windows(self) -> None:
        # Windows 上真去提权会弹 UAC，只有其余平台有确定的拒绝语义。
        if platform_layer.is_windows():
            self.skipTest("Windows 上会真的请求提权")
        self.assertEqual(platform_layer.run_elevated(["winget", "uninstall"]), 127)


class ComponentVersionProbeTests(unittest.TestCase):
    def test_probe_resolves_a_bare_command_to_its_full_path(self) -> None:
        # Windows 上 npm 落地的是 lark-cli.cmd：裸命令名会抛 WinError 2，探测必须
        # 先把 argv[0] 换成 shutil.which 的结果，否则版本会被静默读成空串。
        completed = subprocess.CompletedProcess([], 0, stdout="1.0.95\n", stderr="")
        with patch.object(
            platform_layer, "resolve_exe", return_value="/npm/lark-cli.cmd"
        ), patch("subprocess.run", return_value=completed) as run:
            returncode, output, _error = detector.run_command_capture(
                ["lark-cli", "--version"]
            )
        self.assertEqual(returncode, 0)
        self.assertEqual(output, "1.0.95")
        self.assertEqual(run.call_args.args[0], ["/npm/lark-cli.cmd", "--version"])

    def test_cli_component_version_keeps_only_the_version_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = plugin_context(Path(directory))
            with patch.object(
                detector, "command_version", return_value="lark-cli version 1.0.95"
            ):
                version = detector.component_version("feishu_cli", context)
        self.assertEqual(version, "1.0.95")

    def test_cli_component_version_falls_back_to_the_raw_output(self) -> None:
        # 提取不到版本号时必须保留原文，不能把探测结果整条丢掉。
        with tempfile.TemporaryDirectory() as directory:
            context = plugin_context(Path(directory))
            with patch.object(
                detector, "command_version", return_value="lark-cli unknown"
            ):
                version = detector.component_version("feishu_cli", context)
        self.assertEqual(version, "lark-cli unknown")


class WingetChannelTests(unittest.TestCase):
    """winget 通道语义：与 brew 对齐，避免把"已最新"当失败而降级装出第二份。"""

    def test_upgrade_of_a_latest_package_counts_as_success(self) -> None:
        # 实测 winget 对"找不到可用升级"返回 43；判成失败会降级到 release 再装一份。
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=True), patch.object(
                installer, "winget_manages", return_value=True
            ), patch.object(installer, "run_command", return_value=RunResult(43)) as run:
                self.assertTrue(installer.install_winget_package("GitHub.cli", "gh"))
            run.assert_called_once()
            self.assertEqual(run.call_args[0][0][1], "upgrade")

    def test_package_outside_winget_lets_the_next_channel_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            with patch.object(installer, "command_exists", return_value=True), patch.object(
                installer, "winget_manages", return_value=False
            ), patch.object(installer, "run_command") as run:
                self.assertFalse(installer.install_winget_package("GitHub.cli", "gh"))
            run.assert_not_called()

    def test_missing_command_installs_then_rechecks_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            probes = {"count": 0}

            def command_exists(_command: str) -> bool:
                probes["count"] += 1
                return probes["count"] > 1

            with patch.object(installer, "command_exists", side_effect=command_exists), patch.object(
                installer, "run_command", return_value=RunResult(0)
            ) as run:
                self.assertTrue(installer.install_winget_package("GitHub.cli", "gh"))
            self.assertEqual(run.call_args[0][0][1], "install")


class UninstallWingetTests(unittest.TestCase):
    """卸载 winget 接管的包：机器级 MSI 需要管理员，只在必要时才提权。"""

    def test_user_scope_package_uninstalls_without_elevation(self) -> None:
        # glab 这类用户态包在普通会话里就卸得掉，不该平白弹一次 UAC。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "winget_manages", return_value=True), patch.object(
                platform_layer, "is_admin", return_value=False
            ), patch.object(uninstaller, "run_command", return_value=RunResult(0)) as run, patch.object(
                platform_layer, "run_elevated"
            ) as elevated:
                self.assertTrue(uninstaller.uninstall_winget_package("GLab.GLab"))
            self.assertTrue(run.call_args.kwargs["quiet"])
            elevated.assert_not_called()

    def test_machine_scope_msi_is_retried_elevated(self) -> None:
        # 实测 Event 11730 / Error 1730：非管理员卸不掉 gh，同一条命令要经 UAC 重跑。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "winget_manages", return_value=True), patch.object(
                platform_layer, "is_admin", return_value=False
            ), patch.object(uninstaller, "run_command", return_value=RunResult(2316632112)), patch.object(
                platform_layer, "run_elevated", return_value=0
            ) as elevated:
                self.assertTrue(uninstaller.uninstall_winget_package("GitHub.cli"))
            command = elevated.call_args[0][0]
            self.assertEqual(command[:3], ["winget", "uninstall", "--id"])
            self.assertIn("GitHub.cli", command)

    def test_admin_session_uninstalls_without_a_second_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "winget_manages", return_value=True), patch.object(
                platform_layer, "is_admin", return_value=True
            ), patch.object(uninstaller, "run_command", return_value=RunResult(0)) as run, patch.object(
                platform_layer, "run_elevated"
            ) as elevated:
                self.assertTrue(uninstaller.uninstall_winget_package("GitHub.cli"))
            self.assertFalse(run.call_args.kwargs.get("quiet", False))
            elevated.assert_not_called()

    def test_declined_elevation_reports_an_actionable_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "winget_manages", return_value=True), patch.object(
                platform_layer, "is_admin", return_value=False
            ), patch.object(uninstaller, "run_command", return_value=RunResult(2316632112)), patch.object(
                platform_layer, "run_elevated", return_value=platform_layer.ELEVATION_CANCELLED
            ):
                self.assertFalse(uninstaller.uninstall_winget_package("GitHub.cli"))
            self.assertIn("管理员终端", str(uninstaller.logger.error.call_args))

    def test_non_interactive_session_never_tries_to_elevate(self) -> None:
        # CI 里弹不出 UAC：直接说明需要管理员权限，不留下一个看不懂的退出码。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            uninstaller.platform.can_interact.return_value = False
            with patch.object(uninstaller, "winget_manages", return_value=True), patch.object(
                platform_layer, "is_admin", return_value=False
            ), patch.object(uninstaller, "run_command", return_value=RunResult(2316632112)), patch.object(
                platform_layer, "run_elevated"
            ) as elevated:
                self.assertFalse(uninstaller.uninstall_winget_package("GitHub.cli"))
            elevated.assert_not_called()
            self.assertIn("管理员权限", str(uninstaller.logger.error.call_args))

    def test_package_outside_winget_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "winget_manages", return_value=False), patch.object(
                uninstaller, "run_command"
            ) as run, patch.object(platform_layer, "run_elevated") as elevated:
                self.assertTrue(uninstaller.uninstall_winget_package("GitHub.cli"))
            run.assert_not_called()
            elevated.assert_not_called()


class UninstallLockedNpmPackageTests(unittest.TestCase):
    """npm 全局包被运行中进程占用时（Windows EBUSY），不擅自结束用户进程。

    gitnexus 包内带原生模块 ``lbugjs.node``，宿主 IDE 拉起的 ``gitnexus mcp`` 会一直
    映射它；Windows 上 npm 卸载要先给包目录改名，于是必定失败并只留下一个退出码。
    卸载器改为找出占用进程、经用户确认后终止再重试，非交互会话只报告。
    """

    LOCKED = [RunResult(4294963214, "npm error code EBUSY")]
    HOLDERS = [("4242", "node gitnexus/dist/cli/index.js mcp")]

    def build(self, directory: str) -> Uninstaller:
        uninstaller = bare_uninstaller(Path(directory))
        uninstaller.auto_yes = False
        return uninstaller

    def test_consented_termination_retries_and_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            with patch.object(
                uninstaller, "npm_package_process_holders", return_value=self.HOLDERS
            ), patch.object(uninstaller, "terminate_processes") as terminate, patch.object(
                uninstaller, "run_command"
            ) as retry, patch.object(uninstaller, "set_npm"), patch.object(
                uninstaller, "command_exists", return_value=False
            ), patch("builtins.input", return_value="y"):
                self.assertTrue(
                    uninstaller.resolve_unremoved_npm_package(
                        "gitnexus", "gitnexus", "gitnexus", self.LOCKED
                    )
                )
            terminate.assert_called_once_with(self.HOLDERS)
            retry.assert_called_once()
            self.assertIn("gitnexus", retry.call_args[0][0])

    def test_declining_termination_leaves_the_processes_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            with patch.object(
                uninstaller, "npm_package_process_holders", return_value=self.HOLDERS
            ), patch.object(uninstaller, "terminate_processes") as terminate, patch.object(
                uninstaller, "run_command"
            ) as retry, patch("builtins.input", return_value="n"):
                self.assertFalse(
                    uninstaller.resolve_unremoved_npm_package(
                        "gitnexus", "gitnexus", "gitnexus", self.LOCKED
                    )
                )
            terminate.assert_not_called()
            retry.assert_not_called()
            # 拒绝后给的是下一步怎么做，而不是一条红字错误。
            self.assertFalse(uninstaller.logger.error.called)
            self.assertIn("npm uninstall -g gitnexus", str(uninstaller.logger.info.call_args))

    def test_non_interactive_session_reports_instead_of_killing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            uninstaller.platform.can_interact.return_value = False
            with patch.object(
                uninstaller, "npm_package_process_holders", return_value=self.HOLDERS
            ), patch.object(uninstaller, "terminate_processes") as terminate, patch.object(
                uninstaller, "run_command"
            ) as retry:
                self.assertFalse(
                    uninstaller.resolve_unremoved_npm_package(
                        "gitnexus", "gitnexus", "gitnexus", self.LOCKED
                    )
                )
            terminate.assert_not_called()
            retry.assert_not_called()
            self.assertIn("npm uninstall -g gitnexus", str(uninstaller.logger.error.call_args))

    def test_auto_yes_session_never_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            uninstaller.auto_yes = True
            with patch.object(
                uninstaller, "npm_package_process_holders", return_value=self.HOLDERS
            ), patch.object(uninstaller, "terminate_processes") as terminate, patch(
                "builtins.input"
            ) as prompt:
                self.assertFalse(
                    uninstaller.resolve_unremoved_npm_package(
                        "gitnexus", "gitnexus", "gitnexus", self.LOCKED
                    )
                )
            prompt.assert_not_called()
            terminate.assert_not_called()

    def test_unlocked_failure_points_at_the_manual_command(self) -> None:
        # 不是占用（网络、registry 等）时不要去找进程，直接给手动命令。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            with patch.object(uninstaller, "npm_package_process_holders") as holders:
                self.assertFalse(
                    uninstaller.resolve_unremoved_npm_package(
                        "dingtalk_cli",
                        "dingtalk-workspace-cli",
                        "dws",
                        [RunResult(1, "npm error code E404")],
                    )
                )
            holders.assert_not_called()
            self.assertIn("npm uninstall -g dingtalk-workspace-cli", str(uninstaller.logger.error.call_args))

    def test_lock_detection_is_windows_only(self) -> None:
        # POSIX 上 unlink 不受文件占用影响，不存在这个失败模式。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            uninstaller.platform = platform_layer.platform_for("Linux")
            self.assertFalse(uninstaller.npm_package_locked(self.LOCKED))
            self.assertEqual(uninstaller.npm_package_process_holders("gitnexus"), [])

    def test_process_probe_targets_the_package_dir_and_skips_shells(self) -> None:
        # 探测脚本自身的命令行里带着这个片段：不排除 powershell 就会把自己列成占用者，
        # 那个 PID 事后复用时会误杀无关进程。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = self.build(directory)
            with patch.object(
                platform_layer, "resolve_exe", return_value="powershell.exe"
            ), patch.object(
                uninstaller,
                "run_capture",
                return_value=RunResult(0, "4242\tnode index.js mcp\nnot-a-pid\tjunk"),
            ) as capture:
                holders = uninstaller.npm_package_process_holders("@larksuite/cli")
            script = capture.call_args[0][0][-1]
            self.assertIn("-notin @('powershell.exe', 'pwsh.exe')", script)
            self.assertIn("node_modules" + os.sep + "@larksuite" + os.sep + "cli", script)
            # 曾经把 f-string 拆成两段，`}}` 没被还原，脚本多一个 `}` 直接解析失败；
            # 只有真实执行才会暴露，所以这里显式断言花括号配平。
            self.assertEqual(script.count("{"), script.count("}"))
            self.assertEqual(holders, [("4242", "node index.js mcp")])


class UninstallGitConfigTests(unittest.TestCase):
    """`git config --unset` 对不存在的键返回 5，那是"本来就干净"，不是失败。"""

    def test_missing_key_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "run_capture", return_value=RunResult(5)):
                uninstaller.unset_git_config("advice.addIgnoredFile")
            uninstaller.logger.warn.assert_not_called()

    def test_removed_key_is_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "run_capture", return_value=RunResult(0)):
                uninstaller.unset_git_config("push.autoSetupRemote")
            uninstaller.logger.warn.assert_not_called()

    def test_unexpected_exit_code_warns(self) -> None:
        # 例如 4（配置文件写不进去）：这是真问题，但没必要让整次卸载失败。
        with tempfile.TemporaryDirectory() as directory:
            uninstaller = bare_uninstaller(Path(directory))
            with patch.object(uninstaller, "run_capture", return_value=RunResult(4)):
                uninstaller.unset_git_config("push.autoSetupRemote")
            self.assertIn("push.autoSetupRemote", str(uninstaller.logger.warn.call_args))


class QoderEntryDetectionTests(unittest.TestCase):
    def test_windows_entry_detects_a_cmd_shim(self) -> None:
        # 官方安装器在 Windows 落地的是 qoder.cmd，没有无扩展名的入口文件。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry"
            entry.mkdir()
            shim = entry / "qoder.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            with patch.dict(os.environ, {"QODER_CONFIG_DIR": str(root)}, clear=False):
                with patch.object(qoder, "find_cli", return_value=("", "")):
                    with patch.object(qoder.platform, "system", return_value="Windows"):
                        found = qoder.cli_path("qoder", root)
        self.assertEqual(Path(found), shim)

    def test_windows_entry_ignores_a_non_executable_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry").mkdir()
            with patch.dict(os.environ, {"QODER_CONFIG_DIR": str(root)}, clear=False):
                with patch.object(qoder, "find_cli", return_value=("", "")):
                    with patch.object(qoder.platform, "system", return_value="Windows"):
                        self.assertEqual(qoder.cli_path("qoder", root), "")

    def test_dispatcher_wrapper_without_backend_is_not_installed(self) -> None:
        # 卸载 npm 包不会删掉 entry 目录里的转发器，而它退出 127：不能算已安装。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry"
            entry.mkdir()
            (entry / "qoder.cmd").write_text(
                '@echo off\npowershell.exe -File "%~dp0qoder-dispatcher.ps1" -- %*\n',
                encoding="utf-8",
            )
            environ = {"QODER_CONFIG_DIR": str(root), "PATH": ""}
            with patch.object(qoder, "find_cli", return_value=("", "")):
                with patch.object(qoder.shutil, "which", return_value=None):
                    with patch.object(qoder.platform, "system", return_value="Windows"):
                        self.assertEqual(qoder.cli_path("qoder", root, environ), "")

    def test_dispatcher_wrapper_reports_its_backend(self) -> None:
        # 转发器后面的真实 CLI 在 bin/<命令>/ 下；报后端路径才能探到版本号。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry"
            entry.mkdir()
            (entry / "qoder.cmd").write_text(
                '@echo off\npowershell.exe -File "%~dp0qoder-dispatcher.ps1" -- %*\n',
                encoding="utf-8",
            )
            backend = root / "bin" / "qodercli" / "qodercli.exe"
            backend.parent.mkdir(parents=True)
            backend.write_bytes(b"")
            environ = {"QODER_CONFIG_DIR": str(root), "PATH": ""}
            with patch.object(qoder, "find_cli", return_value=("", "")):
                with patch.object(qoder.shutil, "which", return_value=None):
                    with patch.object(qoder.platform, "system", return_value="Windows"):
                        self.assertEqual(qoder.cli_path("qoder", root, environ), str(backend))

    def test_wrapper_backend_env_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry"
            entry.mkdir()
            (entry / "qodercn.cmd").write_text(
                '@echo off\npowershell.exe -File "%~dp0qodercn-dispatcher.ps1" -- %*\n',
                encoding="utf-8",
            )
            override = root / "custom-qoderclicn.exe"
            override.write_bytes(b"")
            environ = {
                "QODERCN_CONFIG_DIR": str(root),
                "QODERCN_CLI_BIN": str(override),
                "PATH": "",
            }
            with patch.object(qoder, "find_cli", return_value=("", "")):
                with patch.object(qoder.shutil, "which", return_value=None):
                    with patch.object(qoder.platform, "system", return_value="Windows"):
                        self.assertEqual(
                            qoder.cli_path("qodercn", root, environ), str(override)
                        )

    def test_path_dispatcher_wrapper_without_backend_is_not_installed(self) -> None:
        # entry 目录被写进 PATH 时，PATH 命中本身就是同一个转发器。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "qoder.cmd"
            wrapper.write_text(
                '@echo off\npowershell.exe -File "%~dp0qoder-dispatcher.ps1" -- %*\n',
                encoding="utf-8",
            )
            environ = {"QODER_CONFIG_DIR": str(root), "PATH": ""}
            with patch.object(qoder, "find_cli", return_value=("qoder", str(wrapper))):
                with patch.object(qoder.shutil, "which", return_value=None):
                    with patch.object(qoder.platform, "system", return_value="Windows"):
                        self.assertEqual(qoder.cli_path("qoder", root, environ), "")


class FakeTerminalBackend:
    """脚本化后端：把预设按键序列喂给状态机，完全不碰真实终端。"""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)
        self.writes: list[str] = []

    def open(self) -> None:  # pragma: no cover - nothing to open
        pass

    def close(self) -> None:  # pragma: no cover - nothing to close
        pass

    def write(self, text: str) -> None:
        self.writes.append(text)

    def read_key(self) -> str:
        if not self._keys:
            raise EOFError
        return self._keys.pop(0)

    def size(self) -> tuple[int, int]:
        return 80, 40

    def signals(self) -> tuple[int, ...]:
        return ()


class SelectorBackendTests(unittest.TestCase):
    """选择器终端后端：两个平台归一化到同一组逻辑事件，状态机共用。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.selector = config.load_module(SELECTOR_SCRIPT, "codekit_component_selector")

    def components(self, count: int = 3):
        return [
            self.selector.Component(
                index=index,
                name=f"item-{index}",
                group="ai",
                group_title="AI",
                tag="",
                selected=False,
            )
            for index in range(1, count + 1)
        ]

    def windows_console(self, chars: list[str]):
        console = self.selector.WindowsConsole()

        class FakeMsvcrt:
            def __init__(self, values: list[str]) -> None:
                self._values = list(values)

            def getwch(self) -> str:
                return self._values.pop(0)

        console._msvcrt = FakeMsvcrt(chars)
        return console

    def test_windows_key_mapping_covers_arrows_and_control_keys(self) -> None:
        # Windows 的方向键是「前导字节 + 扫描码」两字节，与 POSIX 的 ANSI 序列不同，
        # 但必须归一化成同一组逻辑事件。
        cases = {
            ("\xe0", "P"): "down",
            ("\x00", "H"): "up",
            ("\xe0", "K"): "other",
            ("\r",): "confirm",
            (" ",): "toggle",
            ("a",): "all",
            ("n",): "none",
            ("p",): "path",
            ("q",): "quit",
            ("z",): "other",
        }
        for chars, expected in cases.items():
            with self.subTest(chars=chars):
                self.assertEqual(self.windows_console(list(chars)).read_key(), expected)

    def test_windows_ctrl_c_becomes_keyboard_interrupt(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.windows_console(["\x03"]).read_key()

    def test_state_machine_toggles_moves_and_confirms(self) -> None:
        backend = FakeTerminalBackend(["toggle", "down", "toggle", "confirm"])
        result = self.selector.interactive_select(
            self.components(), "install", False, "", backend
        )
        self.assertEqual(result, ([1, 2], False))
        # 画面必须显式回车换行：raw 模式与备用屏幕下没有 ONLCR 转换。
        self.assertTrue(backend.writes)
        self.assertTrue(all("\r\n" in frame for frame in backend.writes))
        self.assertTrue(backend.writes[0].startswith("\x1b[2J\x1b[H"))

    def test_state_machine_path_toggle_is_install_only(self) -> None:
        backend = FakeTerminalBackend(["path", "confirm"])
        self.assertEqual(
            self.selector.interactive_select(self.components(), "install", False, "", backend),
            ([], True),
        )
        backend = FakeTerminalBackend(["path", "confirm"])
        self.assertEqual(
            self.selector.interactive_select(self.components(), "uninstall", False, "", backend),
            ([], False),
        )

    def test_state_machine_quit_returns_none(self) -> None:
        backend = FakeTerminalBackend(["quit"])
        self.assertIsNone(
            self.selector.interactive_select(self.components(), "install", False, "", backend)
        )

    def test_open_backend_without_a_terminal_returns_none(self) -> None:
        # 无 /dev/tty 且非 Windows 时必须返回 None，让调用方显式降级。
        with patch.object(self.selector.platform_layer, "is_windows", return_value=False), patch.object(
            self.selector.os.path, "exists", return_value=False
        ):
            self.assertIsNone(self.selector.open_backend())


class SelectorDegradationTests(unittest.TestCase):
    def build(self, home: Path) -> Installer:
        installer = bare_installer(home)
        installer.auto_yes = False
        installer.setup_path_selected = False
        installer.states = {
            component.component_id: detector.ComponentState(component.component_id)
            for component in detector.registry.COMPONENTS
        }
        return installer

    def test_selector_without_a_terminal_degrades_to_defaults(self) -> None:
        # 选择器返回 2 表示拿不到终端；这必须显式降级而不是报错。
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))
            completed = subprocess.CompletedProcess([], 2, stdout="", stderr="no terminal")
            with patch.object(detector, "agent_cli_presence", return_value=""):
                with patch.object(platform_layer.Platform, "can_interact", return_value=True):
                    with patch("subprocess.run", return_value=completed):
                        installer.select_components()
        self.assertTrue(installer.setup_path_selected)
        self.assertIn("rtk", installer.selected)
        # 缺 CLI 的插件不默认勾选：自动安装 CLI 必须来自显式勾选。
        self.assertNotIn("claude_plugin", installer.selected)


class OpencodeConfigFileTests(unittest.TestCase):
    """安装器必须写 OpenCode 自己解析到的那份配置，否则同一插件会出现在两份文件里。"""

    def test_config_file_follows_the_opencode_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(config.opencode_config_file(root), root / "opencode.json")
            (root / "opencode.jsonc").write_text("{}", encoding="utf-8")
            self.assertEqual(config.opencode_config_file(root), root / "opencode.jsonc")
            (root / "opencode.json").write_text("{}", encoding="utf-8")
            self.assertEqual(config.opencode_config_file(root), root / "opencode.json")
            (root / "opencode.json").unlink()
            (root / "opencode.jsonc").unlink()
            (root / ".opencode").mkdir()
            (root / ".opencode" / "opencode.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                config.opencode_config_file(root), root / ".opencode" / "opencode.json"
            )

    def test_jsonc_comments_and_trailing_commas_are_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            path.write_text(
                '{\n'
                "  // 用户注释不能被当成语法错误\n"
                '  "$schema": "https://opencode.ai/config.json",\n'
                '  "plugin": [\n'
                '    "codekit@file:///C:/x", // 行尾注释\n'
                "  ],\n"
                "}\n",
                encoding="utf-8",
            )
            self.assertEqual(config.opencode_plugins(path), ["codekit@file:///C:/x"])

    def test_stripper_never_touches_comment_markers_inside_strings(self) -> None:
        text = '{"url": "http://example.com", "note": "/* not a comment */"}'
        self.assertEqual(json.loads(config.strip_jsonc(text)), json.loads(text))

    def test_add_and_remove_round_trip_on_a_jsonc_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.jsonc"
            path.write_text('{\n  // keep the schema\n  "$schema": "x"\n}\n', encoding="utf-8")
            config.add_opencode_plugin(path, "codekit@file:///C:/y")
            self.assertTrue(config.opencode_has_plugin(path))
            config.remove_opencode_plugins(path)
            self.assertFalse(config.opencode_has_plugin(path))
            # 与 opencode 自身 `plugin -g` 的行为一致：重写时按标准 JSON 落盘。
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"$schema": "x"})


class ShellPathConfigTests(unittest.TestCase):
    """PATH 必须同时写进 login 配置：agent 的 shell 不读 ~/.bashrc。"""

    def test_posix_writes_both_interactive_and_login_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            linux = platform_layer.platform_for("Linux")
            targets = dict(linux.shell_path_configs(home, {"SHELL": "/bin/bash"}))
            self.assertIn(home / ".bashrc", targets)
            # login / 非交互 bash 只读 ~/.profile，漏掉它 gitnexus 就不可见。
            self.assertIn(home / ".profile", targets)
            self.assertIn(".npm-global/bin", targets[home / ".profile"])

    def test_zsh_login_shell_gets_its_own_login_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            linux = platform_layer.platform_for("Linux")
            targets = dict(linux.shell_path_configs(home, {"SHELL": "/usr/bin/zsh"}))
            self.assertIn(home / ".zshrc", targets)
            self.assertIn(home / ".zprofile", targets)

    def test_bash_profile_is_only_appended_never_created(self) -> None:
        # 创建 ~/.bash_profile 会让 bash login shell 不再读 ~/.profile，
        # 反而丢掉 .bashrc 的加载，因此缺省时不能凭空创建。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            linux = platform_layer.platform_for("Linux")
            absent = dict(linux.shell_path_configs(home, {"SHELL": "/bin/bash"}))
            self.assertNotIn(home / ".bash_profile", absent)
            (home / ".bash_profile").write_text("# user\n", encoding="utf-8")
            present = dict(linux.shell_path_configs(home, {"SHELL": "/bin/bash"}))
            self.assertIn(home / ".bash_profile", present)

    def test_uninstall_cleans_every_install_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            linux = platform_layer.platform_for("Linux")
            installed = {path for path, _ in linux.shell_path_configs(home, {"SHELL": "/bin/zsh"})}
            self.assertLessEqual(installed, set(linux.shell_rc_files(home)))

    def test_setup_shell_path_writes_login_config_too_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = bare_installer(home)
            with patch.dict(os.environ, {"SHELL": "/bin/bash"}, clear=False):
                installer.setup_shell_path()
                installer.setup_shell_path()
                self.assertTrue(installer.path_configured())

            bashrc = (home / ".bashrc").read_text(encoding="utf-8")
            profile = (home / ".profile").read_text(encoding="utf-8")
            self.assertIn(".npm-global/bin", bashrc)
            # agent 的 login shell 只读 ~/.profile，这一份缺了工具就不可见。
            self.assertIn(".npm-global/bin", profile)
            self.assertEqual(1, bashrc.count("# Added by CodeKit Plugin"))
            self.assertEqual(1, profile.count("# Added by CodeKit Plugin"))
            # 凭空创建 ~/.bash_profile 会让 bash login shell 不再读 ~/.profile。
            self.assertFalse((home / ".bash_profile").exists())


class GitNexusQoderRegistrationTests(unittest.TestCase):
    """GitNexus 必须落到 Qoder 真正加载的位置。

    实测上游 `gitnexus setup` 把 Qoder 的 MCP 写进没有读者的 `~/.qoder.json`
    （`mcp list` 仍回答 "No MCP servers configured"），而它的 hook 目标表里只有
    claude/codex/antigravity，根本没有 Qoder。两端都得由 CodeKit 补齐，且补不上
    就等于没装，必须让组件失败。
    """

    def _home(self, directory: str) -> Path:
        home = Path(directory)
        (home / ".qoder-cn").mkdir()
        return home

    def _fake_install(self, home: Path) -> Path:
        """伪造一次 npm 全局安装：bin 目录里有 shim，包目录里有 hook 脚本。"""
        scripts = home / "npm" / "node_modules" / "gitnexus" / "hooks" / "claude"
        scripts.mkdir(parents=True)
        for name in qoder.GITNEXUS_HOOK_SCRIPTS:
            (scripts / name).write_text("// hook\n", encoding="utf-8")
        return home / "npm" / "gitnexus.cmd"

    def _installer(self, home: Path) -> Installer:
        installer = bare_installer(home)
        installer.install_via_channels = Mock(return_value=True)
        installer.run_command = Mock(return_value=RunResult(0))
        return installer

    def _resolving_gitnexus_to(self, shim: Path):
        """只把 ``gitnexus`` 的解析指向伪造 shim，其余命令照常解析。

        整体替换 ``resolve_exe`` 会连带打断 node 的解析（``command_version`` 也走
        它），于是 node 版本读成空、依赖门禁先失败，用例就测不到注册逻辑了。
        """
        real_resolve = platform_layer.resolve_exe

        def resolve(name: str):
            return str(shim) if name == "gitnexus" else real_resolve(name)

        return patch.object(platform_layer, "resolve_exe", side_effect=resolve)

    def test_install_registers_mcp_and_hooks_in_the_shared_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory)
            root = home / ".qoder-cn"
            shim = self._fake_install(home)
            installer = self._installer(home)
            with self._resolving_gitnexus_to(shim):
                self.assertTrue(installer.install_gitnexus())
            document = qoder.read_settings(root)
            self.assertEqual(
                document["mcpServers"]["gitnexus"],
                {"command": str(shim), "args": ["mcp"]},
            )
            commands = {
                entry["command"]
                for groups in document["hooks"].values()
                for group in groups
                for entry in group["hooks"]
            }
            self.assertEqual(len(commands), 1, "两个事件指向同一个脚本")
            self.assertTrue((qoder.hook_script_dir(root) / "gitnexus-hook.cjs").is_file())
            self.assertIn(str(qoder.settings_path(root)), installer.changed_paths)
            # 上游 setup 仍然是第一步，Qoder 注册只是追加。
            self.assertIn(
                ["gitnexus", "setup"],
                [entry.args[0] for entry in installer.run_command.call_args_list],
            )

    def test_dead_qoder_json_is_pruned_and_a_foreign_one_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory)
            shim = self._fake_install(home)
            dead = home / ".qoder.json"
            dead.write_text(
                json.dumps(
                    {"mcpServers": {"gitnexus": {"command": "gitnexus", "args": ["mcp"]}}}
                ),
                encoding="utf-8",
            )
            installer = self._installer(home)
            with self._resolving_gitnexus_to(shim):
                self.assertTrue(installer.install_gitnexus())
            self.assertFalse(dead.exists(), "Qoder 从不读取这个文件")

            dead.write_text(
                json.dumps({"mcpServers": {"gitnexus": {}, "other": {}}}),
                encoding="utf-8",
            )
            installer.prune_dead_qoder_mcp_file()
            self.assertTrue(dead.exists(), "别人的 server 不能被顺手删掉")

    def test_missing_hook_scripts_fail_the_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory)
            (home / "npm").mkdir()
            installer = self._installer(home)
            shim = home / "npm" / "gitnexus.cmd"
            with self._resolving_gitnexus_to(shim):
                self.assertFalse(installer.install_gitnexus())
            self.assertTrue(installer.logger.error.called)
            self.assertEqual(qoder.read_settings(home / ".qoder-cn"), {})

    def test_uninstall_removes_everything_install_wrote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = self._home(directory)
            root = home / ".qoder-cn"
            shim = self._fake_install(home)
            installer = self._installer(home)
            with self._resolving_gitnexus_to(shim):
                installer.install_gitnexus()
            # 安装期的两个落点：私有 npm 前缀副本，以及 bin_dir 里的转发垫片。
            private = installer.platform.npm_prefix_private(home)
            private.mkdir(parents=True, exist_ok=True)
            forwarded = installer.bin_dir / "gitnexus.cmd"
            forwarded.parent.mkdir(parents=True, exist_ok=True)
            forwarded.write_text("", encoding="utf-8")

            qoder.register_mcp_server(root, "other", {"command": "other"})
            uninstaller = bare_uninstaller(home)
            uninstaller.command_exists = Mock(return_value=False)
            uninstaller.uninstall_npm_component = Mock(return_value=True)
            self.assertTrue(uninstaller.uninstall_gitnexus())
            document = qoder.read_settings(root)
            self.assertEqual(document["mcpServers"], {"other": {"command": "other"}})
            self.assertNotIn("hooks", document)
            self.assertFalse(qoder.hook_script_dir(root).exists())
            self.assertFalse(private.exists(), "私有前缀副本必须一并清掉")
            self.assertFalse(
                forwarded.exists(), "转发垫片留在 PATH 上会让后续卸载误判成没卸干净"
            )


class GraphifyQoderVisibilityTests(unittest.TestCase):
    """graphify 技能必须出现在 Qoder 两端读取的目录里。"""

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("QODER_CONFIG_DIR", "QODERCN_CONFIG_DIR"):
            os.environ.pop(key, None)

    def _installer(self, home: Path) -> Installer:
        installer = bare_installer(home)
        installer.install_via_channels = Mock(return_value=True)
        installer.command_exists = Mock(return_value=False)
        installer.run_capture = Mock(return_value=RunResult(1))
        installer.run_command = Mock(return_value=RunResult(0))
        return installer

    def _fake_agents_skill(self, home: Path) -> Path:
        source = home / ".agents" / "skills" / "graphify"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("# graphify\n", encoding="utf-8")
        return source

    def test_agents_platform_is_registered_even_without_other_agents(self) -> None:
        # 三家智能体一个都没装时也要注册 agents 目标：它是 Qoder IDE 唯一的共享
        # 技能入口，而且既不写 CLAUDE.md 也不装 agent hook。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".qoder-cn").mkdir()
            installer = self._installer(home)
            self._fake_agents_skill(home)
            self.assertTrue(installer.install_graphify())
            platforms = [
                argv[3]
                for argv in (
                    entry.args[0] for entry in installer.run_command.call_args_list
                )
                if argv[:2] == ["graphify", "install"]
            ]
            self.assertIn("agents", platforms)

    def test_skill_is_mirrored_into_every_existing_qoder_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in (".qoder", ".qoder-cn"):
                (home / name).mkdir()
            installer = self._installer(home)
            source = self._fake_agents_skill(home)
            self.assertTrue(installer.mirror_graphify_skill())
            for name in (".qoder", ".qoder-cn"):
                mirrored = home / name / "skills" / "graphify" / "SKILL.md"
                self.assertTrue(mirrored.is_file(), name)
                self.assertEqual(
                    mirrored.read_text(encoding="utf-8"),
                    (source / "SKILL.md").read_text(encoding="utf-8"),
                )
            self.assertEqual(len(installer.changed_paths), 2)

    def test_missing_agents_output_is_reported_instead_of_a_quiet_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".qoder-cn").mkdir()
            installer = self._installer(home)
            self.assertFalse(installer.mirror_graphify_skill())
            self.assertTrue(installer.logger.error.called)


class CodexMultiAgentTests(unittest.TestCase):
    """Codex 子代理开关：以实测工具面为准，写用户配置必须有本次授权。"""

    CONFIG = (
        'model = "gpt-5"\n'
        "approval_policy = \"on-request\"\n"
        "\n"
        "[features]\n"
        "web_search_request = false\n"
        "multi_agent_v2 = false  # off until restart\n"
        "\n"
        "[mcp_servers.gitnexus]\n"
        'command = "gitnexus"\n'
    )
    TOOLS = "tools: spawn_agent list_agents wait_agent send_message interrupt_agent"

    def _installer(self, home: Path, selected: list[str] = ()) -> Installer:
        installer = bare_installer(home)
        installer.auto_yes = True
        installer.selected = list(selected)
        installer.agent_capabilities = []
        installer.logger = Mock()
        installer.logger.log_path = home / "install.log"
        return installer

    def test_probe_is_isolated_from_the_current_repository(self) -> None:
        """探测必须在空目录里跑：仓库正文里就有 ``spawn_agent`` 这个词。"""
        seen = []

        def fake_run_capture(instance, args, *, timeout=15, cwd=None):
            seen.append(Path(cwd) if cwd else None)
            return RunResult(0, self.TOOLS)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = self._installer(home)
            with patch.object(Installer, "run_capture", autospec=True, side_effect=fake_run_capture):
                verdict, _ = installer.codex_collab_verdict()
            self.assertEqual(codex.AVAILABLE, verdict)
            self.assertEqual(1, len(seen))
            self.assertIsNotNone(seen[0])
            self.assertTrue(seen[0].name.startswith("codekit-collab-probe-"))
            self.assertNotEqual(home.resolve(), seen[0].resolve())

    def test_single_tool_name_does_not_count_as_available(self) -> None:
        # 判定阈值来自共享实现，安装器只是转述：单命中算未暴露，据此才会去写开关。
        self.assertEqual(
            codex.classify_probe(0, "用 spawn_agent 派发子代理"), codex.UNAVAILABLE
        )

    def test_enable_keeps_the_change_once_the_surface_reappears(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_file = codex.config_path(home)
            config_file.parent.mkdir()
            config_file.write_text(self.CONFIG, encoding="utf-8")
            installer = self._installer(home)
            # 首次探测看不到工具面，写入后的复验看到了 —— 这才是 enabled 那条路。
            with patch.object(
                Installer, "codex_probe_runner", side_effect=[(0, ""), (0, self.TOOLS)]
            ):
                outcome, detail = installer.enable_codex_multi_agent()
            self.assertEqual(("enabled", ""), (outcome, detail))
            self.assertIn("multi_agent_v2 = true", config_file.read_text(encoding="utf-8"))
            self.assertIn(str(config_file), installer.changed_paths)

    def test_enable_rolls_back_byte_for_byte_when_reprobe_still_sees_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_file = codex.config_path(home)
            config_file.parent.mkdir()
            config_file.write_text(self.CONFIG, encoding="utf-8")
            before = config_file.read_bytes()
            installer = self._installer(home)
            # 首次探测与写后复验都看不到工具面：结论必须是已回滚，且文件逐字节还原。
            with patch.object(Installer, "codex_probe_runner", return_value=(0, "")):
                outcome, detail = installer.enable_codex_multi_agent()
            self.assertEqual(codex.ROLLED_BACK, outcome)
            self.assertTrue(detail)
            self.assertEqual(before, config_file.read_bytes())

    def test_selecting_codex_plugin_enables_it_without_a_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = codex.config_path(home)
            config.parent.mkdir()
            config.write_text(self.CONFIG, encoding="utf-8")
            installer = self._installer(home, ["codex_plugin"])
            with patch.object(Installer, "command_exists", return_value=True), patch.object(
                Installer,
                "codex_collab_verdict",
                side_effect=[(codex.UNAVAILABLE, ""), (codex.AVAILABLE, self.TOOLS)],
            ):
                installer.configure_agent_capabilities()
            self.assertEqual(installer.agent_capabilities[0]["outcome"], "enabled")
            self.assertIn("multi_agent_v2 = true", config.read_text(encoding="utf-8"))

    def test_probe_that_cannot_run_leaves_the_config_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = codex.config_path(home)
            config.parent.mkdir()
            config.write_text(self.CONFIG, encoding="utf-8")
            installer = self._installer(home, ["codex_plugin"])
            with patch.object(Installer, "command_exists", return_value=True), patch.object(
                Installer, "codex_collab_verdict", return_value=(codex.UNKNOWN, "")
            ), patch.object(codex, "enable") as enable:
                installer.configure_agent_capabilities()
            self.assertEqual(installer.agent_capabilities[0]["outcome"], "unknown")
            self.assertFalse(enable.called)
            self.assertEqual(config.read_text(encoding="utf-8"), self.CONFIG)

    def test_other_platforms_get_no_invented_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = self._installer(home, ["claude_plugin", "opencode_plugin", "qoder_plugin"])
            with patch.object(Installer, "enable_codex_multi_agent") as enable:
                installer.configure_agent_capabilities()
            self.assertFalse(enable.called)
            self.assertEqual(
                [entry["outcome"] for entry in installer.agent_capabilities],
                ["no-switch"] * 3,
            )
            self.assertFalse((home / ".codex").exists())

    def test_enabled_notice_is_one_line_of_at_most_ten_characters(self) -> None:
        # 静默开启：整条链路只留一行提示，不再有汇总框与三行重启说明。
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), ["codex_plugin", "claude_plugin"])
            with patch.object(Installer, "enable_codex_multi_agent", return_value=("enabled", "")):
                installer.configure_agent_capabilities()
            printed = "\n".join(call.args[0] for call in installer.logger.info.call_args_list)
            self.assertEqual(installer.MULTI_AGENT_NOTICE, printed)
            self.assertLessEqual(len(printed), 10)
            self.assertIn("重启", printed)
            self.assertFalse(installer.logger.warn.called)
            self.assertNotIn("无 multi_agent 开关", printed)

    def test_already_on_is_silent(self) -> None:
        # 已开启是每次重跑都会出现的常态结论：不需要用户做任何事，就不该占版面。
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), ["codex_plugin"])
            with patch.object(Installer, "enable_codex_multi_agent", return_value=("already-on", "")):
                installer.configure_agent_capabilities()
            self.assertFalse(installer.logger.info.called)
            self.assertFalse(installer.logger.warn.called)
            self.assertFalse(installer.logger.success.called)

    def test_unconfirmed_capability_warns_once_without_a_box(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), ["codex_plugin"])
            with patch.object(Installer, "enable_codex_multi_agent", return_value=("unknown", "")):
                installer.configure_agent_capabilities()
            self.assertFalse(installer.logger.info.called)
            self.assertEqual(1, installer.logger.warn.call_count)
            text = str(installer.logger.warn.call_args.args[0])
            self.assertIn("Codex", text)
            self.assertIn("未确认", text)

    def test_shared_reason_is_reported_instead_of_a_generic_line(self) -> None:
        """失败原因只有共享编排知道（哪一步、哪个路径），安装器转述它而不是重写一句。"""
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), ["codex_plugin"])
            reason = "写入 /tmp/x/config.toml 后复验仍看不到派发工具（unavailable），已回滚为原内容"
            with patch.object(
                Installer, "enable_codex_multi_agent", return_value=("rolled-back", reason)
            ):
                installer.configure_agent_capabilities()
            self.assertIn(reason, str(installer.logger.warn.call_args.args[0]))

    def test_nothing_printed_without_a_plugin_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), ["gitnexus"])
            installer.configure_agent_capabilities()
            self.assertEqual(installer.agent_capabilities, [])
            self.assertFalse(installer.logger.info.called)


class CodexDesktopDetectionTests(unittest.TestCase):
    """``[desktop]`` 段是区分桌面版与 CLI 的配置痕迹。"""

    def test_desktop_table_is_detected(self) -> None:
        text = 'model = "gpt"\n\n[desktop]\n  integratedTerminalShell = "powershell"\n'
        self.assertTrue(codex.desktop_app_present(text))

    def test_desktop_subtable_is_detected(self) -> None:
        self.assertTrue(codex.desktop_app_present("[desktop.window]\nsize = 1\n"))

    def test_config_without_the_desktop_section_is_a_cli_config(self) -> None:
        text = '[features]\n  multi_agent_v2 = true\n\n[mcp_servers.gitnexus]\n  command = "gitnexus"\n'
        self.assertFalse(codex.desktop_app_present(text))

    def test_commented_header_does_not_count(self) -> None:
        self.assertFalse(codex.desktop_app_present("# [desktop]\nmodel = 'gpt'\n"))


class ToolchainHealthTests(unittest.TestCase):
    """工具链落点的自检按清单实测状态报告，不再用「是否只在用户级 PATH」。

    那个旧判据已被实测证伪：受限宿主的 PATH 里用户级条目是全的，真正被拒绝的是对
    这些目录的文件系统访问，所以「把目录加进 PATH」永远解决不了问题。
    """

    def _installer(self, home: Path, tools: dict) -> Installer:
        installer = bare_installer(home)
        installer.selected = sorted(tools)
        installer.toolchain_manifest = {"tools": tools}
        return installer

    def test_healthy_entries_warn_about_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(
                Path(directory),
                {"gitnexus": {"state": "ok"}, "graphify": {"state": "ok"}},
            )
            installer.report_toolchain_health()
            self.assertEqual(
                [("gitnexus", "ok"), ("graphify", "ok")], installer.toolchain_health
            )
            self.assertFalse(installer.logger.warn.called)

    def test_not_readable_names_the_state_and_refuses_the_path_advice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(
                Path(directory), {"gitnexus": {"state": "not-readable"}}
            )
            installer.report_toolchain_health()
            self.assertEqual(1, installer.logger.warn.call_count)
            text = str(installer.logger.warn.call_args[0][0])
            self.assertIn("not-readable", text)
            # 提示必须点明「不是未安装」，并给出真正的出路。
            self.assertIn("不是未安装", text)
            self.assertIn("install.sh install", text)
            self.assertIn("只改 PATH 无效", text)

    def test_not_executable_points_at_the_venv_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(
                Path(directory), {"graphify": {"state": "not-executable"}}
            )
            installer.report_toolchain_health()
            text = str(installer.logger.warn.call_args[0][0])
            self.assertIn("基础解释器", text)
            self.assertIn("内置运行时", text)

    def test_absent_entry_asks_for_a_reinstall_with_that_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(
                Path(directory), {"gitnexus": {"state": "not-installed"}}
            )
            installer.report_toolchain_health()
            text = str(installer.logger.warn.call_args[0][0])
            self.assertIn("未安装", text)
            self.assertIn("install.sh install", text)

    def test_missing_manifest_entry_is_skipped_rather_than_blamed(self) -> None:
        # 清单里没有该组件，说明这次安装没接管它；报「未安装」是错的。
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), {})
            installer.selected = ["gitnexus"]
            installer.report_toolchain_health()
            self.assertEqual([], installer.toolchain_health)
            self.assertFalse(installer.logger.warn.called)


class UninstallerPurgeTests(unittest.TestCase):
    """``--purge`` 才移除工具链根：默认保留，因为同机多仓库共用那一份运行时。

    默认保留不是保守，是实测约束：根里是可读可执行的唯一一份内置运行时，删掉会让其它
    仓库的 ``codekit:initialize`` 立刻降级成 partial，而技能侧无权把它装回来。
    """

    def _uninstaller(self, home: Path, *, purge: bool) -> Uninstaller:
        uninstaller = bare_uninstaller(home)
        uninstaller.purge = purge
        return uninstaller

    def _seed_root(self, home: Path) -> Path:
        root = platform_layer.platform_for("Windows").toolchain_root(home)
        (root / "runtime" / "20260901" / "python").mkdir(parents=True)
        (root / "toolchain.json").write_text("{}", encoding="utf-8")
        return root

    def test_default_keeps_the_toolchain_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = self._seed_root(home)
            uninstaller = self._uninstaller(home, purge=False)
            uninstaller.purge_toolchain()
            self.assertTrue(root.is_dir())
            self.assertFalse(uninstaller.toolchain_purged)

    def test_purge_removes_the_toolchain_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = self._seed_root(home)
            uninstaller = self._uninstaller(home, purge=True)
            uninstaller.purge_toolchain()
            self.assertFalse(root.exists())
            self.assertTrue(uninstaller.toolchain_purged)

    def test_purging_an_absent_root_is_not_reported_as_a_removal(self) -> None:
        # 幂等卸载会重复跑：不能把「本来就没有」写成「已移除」。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            uninstaller = self._uninstaller(home, purge=True)
            uninstaller.purge_toolchain()
            self.assertFalse(uninstaller.toolchain_purged)
            self.assertFalse(uninstaller.logger.warn.called)

    def test_purge_flag_reaches_the_cli(self) -> None:
        self.assertTrue(uninstaller_module.parse_args(["--purge"]).purge)
        self.assertFalse(uninstaller_module.parse_args(["-y"]).purge)


class CodexRtkRegistrationTests(unittest.TestCase):
    """桌面版 shell 看不到用户级 rtk 时，不要写入做不到的前缀约束。"""

    def _installer(self, home: Path, config_text: "str | None") -> Installer:
        installer = bare_installer(home)
        if config_text is not None:
            path = codex.config_path(home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(config_text, encoding="utf-8")
        return installer

    def test_desktop_host_without_machine_visible_rtk_skips_the_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), "[desktop]\nwindow = 1\n")
            with patch.object(installer, "visible_without_user_path", return_value=False):
                self.assertFalse(installer.codex_can_run_rtk())

    def test_desktop_host_with_machine_visible_rtk_keeps_the_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), "[desktop]\nwindow = 1\n")
            with patch.object(installer, "visible_without_user_path", return_value=True):
                self.assertTrue(installer.codex_can_run_rtk())

    def test_cli_only_host_keeps_the_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), "[features]\n")
            with patch.object(installer, "visible_without_user_path", return_value=False):
                self.assertTrue(installer.codex_can_run_rtk())

    def test_missing_config_keeps_the_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer(Path(directory), None)
            with patch.object(installer, "visible_without_user_path", return_value=False):
                self.assertTrue(installer.codex_can_run_rtk())

    def test_configure_rtk_skips_only_the_codex_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = bare_installer(Path(directory))
            installer.command_exists = Mock(return_value=True)
            installer.codex_can_run_rtk = Mock(return_value=False)
            with patch.object(installer, "run_command", return_value=RunResult(0)) as run:
                self.assertTrue(installer.configure_rtk())
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["rtk", "init", "-g", "--auto-patch"], commands)
            self.assertNotIn(["rtk", "init", "-g", "--codex"], commands)
            self.assertIn(["rtk", "init", "-g", "--opencode", "--auto-patch"], commands)
            self.assertTrue(installer.logger.warn.called)

    def test_machine_path_entries_is_empty_off_windows(self) -> None:
        if os.name == "nt":
            self.skipTest("非 Windows 用例")
        self.assertEqual((), platform_layer.machine_path_entries())


if __name__ == "__main__":
    unittest.main()
