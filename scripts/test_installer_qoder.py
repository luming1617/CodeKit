#!/usr/bin/env python3
"""Regression tests for the Qoder / Qoder CN shared-storage platform layer."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_agents as agents  # noqa: E402
import installer_qoder as qoder  # noqa: E402
import installer_state as detector  # noqa: E402
from installer import Installer, InstallerError  # noqa: E402


# 宿主若导出了 QODER*_CONFIG_DIR，共享配置根会被覆盖，断言就会指向真实用户目录
# 而不是临时目录。整模块运行期间先移除它们，tearDown 时再恢复。
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


# A registry entry shaped the way the desktop IDE writes it: an install path with
# no version subdirectory, a nine-digit timestamp, and keys CodeKit never owns.
DESKTOP_FOREIGN_ENTRY = {
    "scope": "user",
    "installPath": "/opt/qoder/resources/qoder-context",
    "version": "0.4.2",
    "installedAt": "2026-01-02T03:04:05.123456789Z",
    "lastUpdated": "2026-01-03T03:04:05.12Z",
    "userVisible": True,
    "enabled": True,
}


def empty_environment() -> dict[str, str]:
    """Return an environment where no Qoder command can be resolved on PATH."""
    return {"PATH": str(Path(tempfile.mkdtemp(prefix="qoder-empty-bin-")))}


def make_stage(base: Path, version: str = "1.0.0") -> Path:
    """Build a staged plugin source shaped like installer_sources' qoder stage."""
    stage = base / "stage" / "codekit"
    manifest = stage / ".qoder-plugin"
    manifest.mkdir(parents=True)
    (manifest / "plugin.json").write_text(
        json.dumps({"name": qoder.PLUGIN_NAME, "version": version}), encoding="utf-8"
    )
    skill = stage / "skills" / "product-doc"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# product-doc\n", encoding="utf-8")
    return stage


class ConfigRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptor = qoder.PLATFORMS["qodercn"]

    def test_default_root_is_used_when_environment_is_unset(self) -> None:
        root = qoder.resolve_config_root(self.descriptor, Path("/home/u"), {})
        self.assertEqual(root, Path("/home/u/.qoder-cn"))

    def test_environment_override_wins_and_both_platforms_have_their_own_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "shared-qoder"
            custom.mkdir()
            environ = {self.descriptor.config_env: str(custom)}
            self.assertEqual(
                qoder.resolve_config_root(self.descriptor, Path("/home/u"), environ),
                custom,
            )
            self.assertEqual(
                qoder.resolve_config_root(
                    qoder.PLATFORMS["qoder"], Path("/home/u"), {"QODER_CONFIG_DIR": str(custom)}
                ),
                custom,
            )

    def test_environment_pointing_at_a_missing_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "typo-dir"
            with self.assertRaises(qoder.QoderError):
                qoder.resolve_config_root(self.descriptor, Path("/home/u"), {self.descriptor.config_env: str(missing)})
            self.assertFalse(missing.exists(), "a mistyped root must never be created")

    def test_home_relative_override_is_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "cfg").mkdir()
            # Windows 的 expanduser("~") 读 USERPROFILE 而不是 HOME，两个都要给。
            with patch.dict(os.environ, {"HOME": directory, "USERPROFILE": directory}, clear=False):
                root = qoder.resolve_config_root(
                    self.descriptor, Path(directory), {self.descriptor.config_env: "~/cfg"}
                )
            self.assertEqual(root, Path(directory) / "cfg")


class SharedStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.root = self.home / ".qoder-cn"
        self.root.mkdir()

    def write_registry(self, document: object) -> Path:
        """Store a registry document, or raw text when the document is a string."""
        payload = document if isinstance(document, str) else json.dumps(document)
        path = qoder.registry_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        return path

    def test_missing_registry_reads_as_an_empty_document(self) -> None:
        self.assertEqual(qoder.read_registry(self.root), {"version": 2, "plugins": {}})

    def test_corrupt_registry_raises_instead_of_being_overwritten(self) -> None:
        path = self.write_registry("{not json")
        with self.assertRaises(qoder.QoderError):
            qoder.read_registry(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_corrupt_settings_raises_instead_of_being_overwritten(self) -> None:
        path = qoder.settings_path(self.root)
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(qoder.QoderError):
            qoder.set_plugin_enabled(self.root, True)
        self.assertEqual(path.read_text(encoding="utf-8"), "{oops")

    def test_registry_rejects_a_non_object_document(self) -> None:
        self.write_registry([1, 2, 3])
        with self.assertRaises(qoder.QoderError):
            qoder.read_registry(self.root)

    def test_registering_keeps_foreign_plugins_and_unknown_keys(self) -> None:
        self.write_registry(
            {
                "version": 2,
                "schemaRevision": 7,
                "plugins": {"qoder-context@qoderapp-bundler": [DESKTOP_FOREIGN_ENTRY]},
            }
        )
        qoder.register_plugin(self.root, self.root / "plugins" / "cache" / "x", "1.0.0")
        document = json.loads(qoder.registry_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(document["schemaRevision"], 7)
        self.assertEqual(
            document["plugins"]["qoder-context@qoderapp-bundler"], [DESKTOP_FOREIGN_ENTRY]
        )
        entry = document["plugins"][qoder.PLUGIN_ID][0]
        self.assertEqual(entry["scope"], "user")
        self.assertEqual(entry["version"], "1.0.0")

    def test_enabled_flag_defaults_to_true_for_desktop_written_settings(self) -> None:
        self.assertTrue(qoder.plugin_enabled(self.root))
        (qoder.settings_path(self.root)).parent.mkdir(parents=True, exist_ok=True)
        qoder.set_plugin_enabled(self.root, False)
        self.assertFalse(qoder.plugin_enabled(self.root))
        qoder.set_plugin_enabled(self.root, True)
        self.assertTrue(qoder.plugin_enabled(self.root))
        self.assertTrue(qoder.set_plugin_enabled(self.root, True) is False)

    def test_settings_other_keys_survive_enable_and_disable(self) -> None:
        path = qoder.settings_path(self.root)
        path.write_text(
            json.dumps({"theme": "dark", "enabledPlugins": {"other@mp": True}}), encoding="utf-8"
        )
        qoder.set_plugin_enabled(self.root, True)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["theme"], "dark")
        self.assertEqual(document["enabledPlugins"], {"other@mp": True, qoder.PLUGIN_ID: True})
        qoder.set_plugin_enabled(self.root, None)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["enabledPlugins"], {"other@mp": True})

    def test_reject_non_object_enabled_plugins(self) -> None:
        qoder.settings_path(self.root).write_text(
            json.dumps({"enabledPlugins": ["codekit"]}), encoding="utf-8"
        )
        with self.assertRaises(qoder.QoderError):
            qoder.plugin_enabled(self.root)


class CapabilityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptor = qoder.PLATFORMS["qoder"]

    def probe(self, which_results: dict[str, str], responses: dict[str, tuple[int, str]]):
        environ = {"PATH": "/usr/bin"}

        def which(command: str, path: str | None = None) -> str | None:
            return which_results.get(command)

        calls: list[list[str]] = []

        def runner(args, timeout: float = 20.0) -> tuple[int, str]:
            calls.append([str(item) for item in args])
            key = " ".join(str(item) for item in args[1:])
            if key not in responses:
                return 1, ""
            returncode, output = responses[key]
            if returncode == -1:  # stand-in for a subprocess timeout
                raise AssertionError("timeout must be surfaced as a failure result")
            return returncode, output

        with patch.object(qoder.shutil, "which", side_effect=which):
            return qoder.capable_cli(self.descriptor, environ, runner), calls

    def test_absent_command_is_not_capable(self) -> None:
        cli, calls = self.probe({}, {})
        self.assertEqual(cli, "")
        self.assertEqual(calls, [])

    def test_desktop_launcher_sharing_the_command_name_is_not_capable(self) -> None:
        cli, calls = self.probe({"qoder": "/Applications/Qoder.app/Contents/Resources/qoder"}, {})
        self.assertEqual(cli, "")
        self.assertEqual(calls, [])

    def test_unusable_version_output_is_not_capable(self) -> None:
        cli, _ = self.probe(
            {"qoder": "/usr/bin/qoder"},
            {"--version": (1, "qoder: command unavailable")},
        )
        self.assertEqual(cli, "")

    def test_help_without_json_flag_is_not_capable(self) -> None:
        cli, _ = self.probe(
            {"qoder": "/usr/bin/qoder"},
            {
                "--version": (0, "qoder 1.1.48"),
                "plugins install --help": (0, "Usage: plugins install [path]"),
            },
        )
        self.assertEqual(cli, "")

    def test_help_without_scope_flag_is_not_capable(self) -> None:
        cli, _ = self.probe(
            {"qoder": "/usr/bin/qoder"},
            {
                "--version": (0, "qoder 1.1.48"),
                "plugins install --help": (0, "Usage: plugins install [path] [--json]"),
            },
        )
        self.assertEqual(cli, "")

    def test_cli_with_json_and_scope_is_capable(self) -> None:
        cli, calls = self.probe(
            {"qoder": "/usr/bin/qoder"},
            {
                "--version": (0, "qoder 1.1.48"),
                "plugins install --help": (0, "Usage: plugins install [path] [--scope user] [--json]"),
            },
        )
        self.assertEqual(cli, "/usr/bin/qoder")
        self.assertEqual(len(calls), 2)

    def test_command_timeout_degrades_to_an_unusable_probe_result(self) -> None:
        returncode, output = qoder.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertEqual((returncode, output), (1, ""))
        with patch.object(qoder.shutil, "which", return_value="/usr/bin/qoder"):
            self.assertEqual(
                qoder.capable_cli(
                    self.descriptor,
                    {"PATH": "/usr/bin"},
                    Mock(return_value=(1, "")),
                ),
                "",
            )

    def test_install_command_result_is_verified_from_json(self) -> None:
        responses = {
            "ok": (0, json.dumps({"ok": True, "pluginId": qoder.PLUGIN_ID, "version": "1.0.0"})),
            "rejected": (0, json.dumps({"ok": False, "error": "manifest missing name"})),
            "banner": (0, "Qoder 1.1.48\n" + json.dumps({"ok": True, "pluginId": qoder.PLUGIN_ID})),
            "noise": (1, "segmentation fault"),
        }

        for label, expected in (("ok", qoder.PLUGIN_ID), ("rejected", None), ("banner", qoder.PLUGIN_ID), ("noise", None)):
            runner = Mock(return_value=responses[label])
            if expected is None:
                with self.assertRaises(qoder.QoderError):
                    qoder.install_via_cli("qoder", Path("/root"), Path("/stage"), runner=runner)
            else:
                self.assertEqual(
                    qoder.install_via_cli("qoder", Path("/root"), Path("/stage"), runner=runner),
                    expected,
                )
            argv = [str(item) for item in runner.call_args[0][0]]
            self.assertIn("--config-dir", argv)
            self.assertIn("--scope", argv)
            self.assertIn("--json", argv)


class DirectInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.descriptor = qoder.PLATFORMS["qodercn"]
        self.root = self.home / ".qoder-cn"
        self.root.mkdir()
        self.environ = empty_environment()
        self.stage = make_stage(self.home)

    def state(self) -> qoder.InstallState:
        return qoder.inspect(self.descriptor, self.home, self.environ)

    def test_install_writes_cache_and_shared_registry_without_a_cli(self) -> None:
        target = qoder.install_direct(self.root, self.stage, "1.0.0", )
        self.assertEqual(target, qoder.plugin_cache_root(self.root) / "1.0.0")
        self.assertTrue((target / ".qoder-plugin" / "plugin.json").is_file())
        self.assertTrue((target / "skills" / "product-doc" / "SKILL.md").is_file())
        state = self.state()
        self.assertTrue(state.installed)
        self.assertTrue(state.enabled)
        self.assertEqual(state.version, "1.0.0")
        self.assertEqual(state.install_path, str(target))

    def test_reinstall_is_idempotent_and_replaces_the_cache(self) -> None:
        qoder.install_direct(self.root, self.stage, "1.0.0")
        first = self.state()
        first_entry = qoder.plugin_entries(qoder.read_registry(self.root))[0]
        installed_at = first_entry["installedAt"]
        self.assertNotIn("lastUpdated", first_entry)
        (self.stage / "skills" / "extra").mkdir()
        qoder.install_direct(self.root, self.stage, "1.0.0")
        second = self.state()
        entries = qoder.plugin_entries(qoder.read_registry(self.root))
        self.assertEqual(len(entries), 1)
        self.assertEqual(second.install_path, first.install_path)
        # 重装保留首次安装时间，并刷新 lastUpdated。
        self.assertEqual(entries[0]["installedAt"], installed_at)
        self.assertIn("lastUpdated", entries[0])
        self.assertTrue((Path(second.install_path) / "skills" / "extra").is_dir())

    def test_superseded_versions_and_staging_leftovers_are_removed(self) -> None:
        cache = qoder.plugin_cache_root(self.root)
        (cache / "0.9.0").mkdir(parents=True)
        (cache / "0.9.0" / "stale.txt").write_text("old", encoding="utf-8")
        (cache / ".1.0.0.codekit.1.deadbeef").mkdir()
        qoder.install_direct(self.root, self.stage, "1.0.0")
        self.assertEqual([path.name for path in cache.iterdir()], ["1.0.0"])

    def test_settings_and_registry_keep_their_foreign_content_after_install(self) -> None:
        qoder.registry_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        qoder.registry_path(self.root).write_text(
            json.dumps({"version": 2, "plugins": {"other@bundler": [DESKTOP_FOREIGN_ENTRY]}}),
            encoding="utf-8",
        )
        qoder.settings_path(self.root).write_text(
            json.dumps({"theme": "light", "enabledPlugins": {"other@bundler": True}}),
            encoding="utf-8",
        )
        qoder.install_direct(self.root, self.stage, "1.0.0")
        registry = json.loads(qoder.registry_path(self.root).read_text(encoding="utf-8"))
        settings = json.loads(qoder.settings_path(self.root).read_text(encoding="utf-8"))
        self.assertEqual(registry["plugins"]["other@bundler"], [DESKTOP_FOREIGN_ENTRY])
        self.assertEqual(settings["theme"], "light")
        self.assertEqual(settings["enabledPlugins"][qoder.PLUGIN_ID], True)

    def test_missing_install_directory_means_not_installed(self) -> None:
        qoder.install_direct(self.root, self.stage, "1.0.0")
        shutil.rmtree(qoder.plugin_cache_root(self.root))
        state = self.state()
        self.assertFalse(state.installed)
        self.assertEqual(state.version, "1.0.0")

    def test_disable_from_settings_is_reported(self) -> None:
        qoder.install_direct(self.root, self.stage, "1.0.0")
        qoder.set_plugin_enabled(self.root, False)
        state = self.state()
        self.assertTrue(state.installed)
        self.assertFalse(state.enabled)


class UninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.descriptor = qoder.PLATFORMS["qodercn"]
        self.root = self.home / ".qoder-cn"
        self.root.mkdir()
        self.environ = empty_environment()
        self.stage = make_stage(self.home)

    def test_uninstall_without_cli_clears_registry_cache_and_settings(self) -> None:
        target = qoder.install_direct(self.root, self.stage, "1.0.0")
        qoder.set_plugin_enabled(self.root, True)
        self.assertTrue(qoder.uninstall(self.descriptor, self.home, self.environ))
        self.assertFalse(target.exists())
        self.assertFalse(qoder.plugin_entries(qoder.read_registry(self.root)))
        self.assertNotIn(qoder.PLUGIN_ID, qoder.enabled_flags(qoder.read_settings(self.root), self.root))

    def test_foreign_plugins_survive_uninstall(self) -> None:
        qoder.registry_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        qoder.install_direct(self.root, self.stage, "1.0.0")
        registry = json.loads(qoder.registry_path(self.root).read_text(encoding="utf-8"))
        registry["plugins"]["other@bundler"] = [DESKTOP_FOREIGN_ENTRY]
        qoder.registry_path(self.root).write_text(json.dumps(registry), encoding="utf-8")
        qoder.uninstall(self.descriptor, self.home, self.environ)
        after = json.loads(qoder.registry_path(self.root).read_text(encoding="utf-8"))
        self.assertIn("other@bundler", after["plugins"])
        self.assertNotIn(qoder.PLUGIN_ID, after["plugins"])

    def test_install_path_outside_the_cache_is_never_deleted(self) -> None:
        outside = self.home / "important-work"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        qoder.registry_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        qoder.register_plugin(self.root, outside, "1.0.0")
        self.assertTrue(qoder.uninstall(self.descriptor, self.home, self.environ))
        self.assertTrue((outside / "keep.txt").is_file())
        self.assertFalse(qoder.plugin_entries(qoder.read_registry(self.root)))

    def test_install_path_equal_to_the_cache_root_is_never_deleted(self) -> None:
        cache = self.root / "plugins" / "cache"
        cache.mkdir(parents=True)
        qoder.register_plugin(self.root, cache, "1.0.0")
        qoder.uninstall(self.descriptor, self.home, self.environ)
        self.assertTrue(cache.is_dir())

    def test_uninstall_prefers_the_cli_but_always_cleans_the_shared_root(self) -> None:
        qoder.install_direct(self.root, self.stage, "1.0.0")
        calls: list[list[str]] = []

        def runner(args, timeout: float = 20.0) -> tuple[int, str]:
            argv = [str(item) for item in args]
            calls.append(argv)
            if argv[1:2] == ["--version"]:
                return 0, "qodercn 1.1.48"
            if "uninstall" in argv:
                return 0, json.dumps({"ok": True})
            return 0, "Usage: plugins install [path] [--scope user] [--json]"

        with patch.object(qoder.shutil, "which", return_value="/usr/bin/qodercn"):
            self.assertTrue(qoder.uninstall(self.descriptor, self.home, {"PATH": "/usr/bin"}, runner))
        self.assertTrue(any("uninstall" in argv for argv in calls))
        self.assertFalse(qoder.plugin_entries(qoder.read_registry(self.root)))


class PresenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.environ = empty_environment()

    def test_shared_root_alone_counts_as_presence_for_both_platforms(self) -> None:
        (self.home / ".qoder-cn").mkdir()
        (self.home / ".qoder").mkdir()
        with patch.dict(os.environ, self.environ, clear=False):
            self.assertEqual(
                qoder.agent_presence("qodercn", self.home, "Linux", self.environ),
                str(self.home / ".qoder-cn"),
            )
            self.assertEqual(
                detector.agent_presence("qoder", self.home, "Linux"), str(self.home / ".qoder")
            )

    def test_desktop_entry_takes_precedence_over_an_absent_root(self) -> None:
        launcher = self.home / ".local" / "share" / "applications" / "qoder-cn-ide.desktop"
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        environ = dict(self.environ, XDG_DATA_HOME=str(self.home / ".local" / "share"))
        source = qoder.agent_presence("qodercn", self.home, "Linux", environ)
        self.assertEqual(source, str(launcher))

    def test_no_trace_at_all_is_absence(self) -> None:
        self.assertEqual(qoder.agent_presence("qoder", self.home, "Linux", self.environ), "")

    def test_cli_wins_over_the_shared_root(self) -> None:
        root = self.home / ".qoder"
        root.mkdir()
        # Windows 的 shutil.which 依赖 PATHEXT，裸文件不会被当作命令；解析结果还会
        # 把后缀规范成大写（qoder.CMD），因此按文件身份比较而不是字符串。
        windows = os.name == "nt"
        executable = self.home / ("qoder.cmd" if windows else "qoder")
        executable.write_text("@echo off\r\n" if windows else "#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        environ = dict(self.environ, PATH=str(self.home))
        source = qoder.agent_presence("qoder", self.home, "Linux", environ)
        self.assertTrue(source.startswith("qoder ("), source)
        self.assertTrue(
            os.path.samefile(source.removeprefix("qoder (").removesuffix(")"), executable)
        )

    def test_agent_location_exposes_the_shared_agents_directory(self) -> None:
        root = self.home / ".qoder-cn"
        root.mkdir()
        flags, agents = qoder.agent_location("qodercn", self.home, self.environ)
        self.assertEqual(flags, ["--config-root", str(root)])
        self.assertEqual(agents, root / "agents")
        self.assertEqual(qoder.agent_location("claude", self.home, self.environ), ([], None))


REPO_DIR = SCRIPT_DIR.parent
REPO_VERSION = json.loads((REPO_DIR / "plugin-version.json").read_text(encoding="utf-8"))["version"]


class SharedStateTests(unittest.TestCase):
    """installer_state must judge Qoder from the shared root, never from a CLI."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.descriptor = qoder.PLATFORMS["qodercn"]
        self.root = self.home / ".qoder-cn"
        self.root.mkdir()
        self.stage = make_stage(self.home)
        self.target = qoder.install_direct(self.root, self.stage, "1.0.0")
        # 插件状态以 CLI 为前置，测试里放一个可执行的假 CLI，等价于已装 CLI。
        self.cli = self.root / "entry" / "qodercn"
        self.cli.parent.mkdir(parents=True, exist_ok=True)
        self.write_cli()
        self.context = detector.Context(
            home=self.home,
            marketplace_local=REPO_DIR,
            version_file=self.home / "installed_versions",
        )
        self.no_cli_env = empty_environment()

    def write_cli(self) -> None:
        """写入一个可执行的假 CLI，供只探测存在性的检测路径使用。"""
        self.cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.cli.chmod(0o755)

    def detect(self, agents_state: str = "up_to_date") -> detector.ComponentState:
        with patch.object(shutil, "which", return_value=None):
            with patch.object(detector, "platform_subagents_state", return_value=agents_state):
                with patch.dict(os.environ, self.no_cli_env, clear=False):
                    os.environ.pop(self.descriptor.config_env, None)
                    return detector.detect_component("qodercn_plugin", self.context, {})

    def test_plugin_state_requires_the_platform_cli(self) -> None:
        # 只装了桌面端、没有 CLI 时，插件不算已安装，选择器会默认勾选并自动补 CLI。
        self.cli.unlink()
        state = self.detect()
        self.assertEqual(state.status, "not_installed")
        self.write_cli()
        state = self.detect()
        self.assertEqual(state.status, "up_to_date")
        self.assertEqual(state.installed_version, "1.0.0")

    def test_registry_version_wins_over_the_repository_fallback(self) -> None:
        qoder.register_plugin(self.root, self.target, "0.9.9")
        with patch.object(shutil, "which", return_value=None):
            with patch.object(detector, "platform_subagents_state", return_value="up_to_date"):
                with patch.dict(os.environ, self.no_cli_env, clear=False):
                    os.environ.pop(self.descriptor.config_env, None)
                    version = detector.component_version("qodercn_plugin", self.context)
        self.assertEqual(version, "0.9.9")
        # 版本较旧的共享注册表必须提示可升级，重装即完成升级。
        self.assertEqual(self.detect().status, "installed")

    def test_stale_global_agents_downgrade_the_component_state(self) -> None:
        self.assertEqual(self.detect(agents_state="installed").status, "installed")

    def test_missing_agents_sync_means_absent(self) -> None:
        with patch.object(shutil, "which", return_value=None):
            with patch.object(detector, "platform_subagents_state", return_value="missing"):
                with patch.dict(os.environ, self.no_cli_env, clear=False):
                    os.environ.pop(self.descriptor.config_env, None)
                    self.assertFalse(detector.present("qodercn_plugin", self.context))

    def test_disabled_shared_plugin_is_not_present(self) -> None:
        qoder.set_plugin_enabled(self.root, False)
        self.assertFalse(self.detect().status == "up_to_date")
        with patch.object(shutil, "which", return_value=None):
            with patch.object(detector, "platform_subagents_state", return_value="up_to_date"):
                with patch.dict(os.environ, self.no_cli_env, clear=False):
                    os.environ.pop(self.descriptor.config_env, None)
                    self.assertFalse(detector.present("qodercn_plugin", self.context))

    def test_install_path_is_reported_from_the_shared_cache(self) -> None:
        with patch.dict(os.environ, self.no_cli_env, clear=False):
            os.environ.pop(self.descriptor.config_env, None)
            path = detector.component_install_path("qodercn_plugin", self.context)
        # 展示路径按宿主分隔符渲染，Windows 上是反斜杠。
        self.assertTrue(
            path.replace("\\", "/").endswith(".qoder-cn/plugins/cache/local/codekit")
        )

    def test_broken_shared_root_never_raises_out_of_detection(self) -> None:
        with patch.dict(os.environ, {self.descriptor.config_env: str(self.home / "gone")}, clear=False):
            with patch.object(detector, "platform_subagents_state", return_value="up_to_date"):
                self.assertFalse(detector.present("qodercn_plugin", self.context))
                self.assertEqual(
                    detector.component_version("qodercn_plugin", self.context),
                    detector.repository_plugin_version(self.context),
                )

    def test_claude_plugin_install_state_needs_the_cli(self) -> None:
        cache = self.home / ".claude" / "plugins" / "cache" / "co-daming" / "codekit"
        cache.mkdir(parents=True)
        context = detector.Context(
            home=self.home,
            marketplace_local=REPO_DIR,
            version_file=self.home / "installed_versions",
        )
        with patch.object(shutil, "which", return_value=None):
            with patch.object(detector, "platform_subagents_state", return_value="up_to_date"):
                # 缓存由两端共享写入，卸载复核仍认它；安装状态则必须另有 CLI。
                self.assertTrue(detector.present("claude_plugin", context))
                self.assertFalse(detector.plugin_ready("claude_plugin", context))
                self.assertEqual(
                    detector.detect_component("claude_plugin", context, {}).status,
                    "not_installed",
                )
                self.assertFalse(detector.present("codex_plugin", context))

    def test_claude_without_cli_or_cache_stays_absent(self) -> None:
        context = detector.Context(
            home=self.home,
            marketplace_local=REPO_DIR,
            version_file=self.home / "installed_versions",
        )
        with patch.object(shutil, "which", return_value=None):
            with patch.object(detector, "platform_subagents_state", return_value="up_to_date"):
                self.assertFalse(detector.present("claude_plugin", context))


class PreflightTests(unittest.TestCase):
    def build(self, home: Path) -> Installer:
        installer = Installer.__new__(Installer)
        installer.home = home
        installer.os_name = "Linux"
        installer.logger = Mock()
        return installer

    def test_shared_qoder_root_alone_does_not_satisfy_the_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".qoder").mkdir()
            installer = self.build(home)
            with patch.object(shutil, "which", return_value=None):
                self.assertTrue(installer.preflight_agents())
            # 只有共享配置根（桌面端痕迹）时按无 CLI 处理：提示但不阻断流程。
            installer.logger.warn.assert_called_once()
            installer.logger.success.assert_not_called()

    def test_qodercn_cli_alone_satisfies_the_agent_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))

            def which(command: str, path: str | None = None) -> str | None:
                return "/usr/bin/qodercn" if command == "qodercn" else None

            with patch.object(shutil, "which", side_effect=which):
                self.assertTrue(installer.preflight_agents())
            installer.logger.warn.assert_not_called()

    def test_preflight_message_names_every_supported_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))
            with patch.object(shutil, "which", return_value=None):
                # 没有任何 CLI 时不再结束流程，只提示后续由勾选的插件自动安装。
                self.assertTrue(installer.preflight_agents())
        message = "\n".join(str(call.args[0]) for call in installer.logger.info.call_args_list)
        for label in ("Claude Code", "Codex", "OpenCode", "Qoder", "Qoder CN"):
            self.assertIn(label, message)

    def test_detected_clis_are_tabulated_one_row_each_with_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self.build(Path(directory))

            def which(command: str, path: str | None = None) -> str | None:
                return f"/usr/bin/{command}"

            with patch.object(shutil, "which", side_effect=which):
                with patch.object(agents, "cli_version", return_value="1.2.3"):
                    self.assertTrue(installer.preflight_agents())

        installer.logger.warn.assert_not_called()
        message = "\n".join(str(call.args[0]) for call in installer.logger.info.call_args_list)
        # 表格边框行数 = 表头 1 行 + 每个已检测到的 CLI 1 行。
        rows = [line for line in message.splitlines() if line.lstrip().startswith("║")]
        self.assertEqual(len(rows), 1 + len(agents.AGENT_CLIS))
        for entry in agents.AGENT_CLIS:
            self.assertIn(entry.key, message)
        self.assertIn("1.2.3", message)


def build_installer(home: Path, stage: Path | None) -> Installer:
    """Assemble just enough installer state to drive one component handler."""
    installer = Installer.__new__(Installer)
    installer.home = home
    installer.os_name = "Linux"
    installer.state_dir = home / ".local" / "state" / "CodeKit"
    installer.state_dir.mkdir(parents=True, exist_ok=True)
    installer.logger = Mock()
    installer.marketplace_local = REPO_DIR
    installer.changed_paths = []
    installer.qoder_source_dir = stage
    return installer


class InstallFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.descriptor = qoder.PLATFORMS["qodercn"]
        self.root = self.home / ".qoder-cn"
        self.root.mkdir()
        self.stage = make_stage(self.home, REPO_VERSION)
        self.installer = build_installer(self.home, self.stage)
        self.environment = patch.dict(
            os.environ,
            {self.descriptor.config_env: str(self.root), **empty_environment()},
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        presence = patch.object(
            detector, "agent_cli_presence", return_value="qodercn (/usr/bin/qodercn)"
        )
        presence.start()
        self.addCleanup(presence.stop)
        foreign = patch.object(qoder, "foreign_os_roots", return_value=[])
        foreign.start()
        self.addCleanup(foreign.stop)

    def install(self, capable: str, agents_ok: bool = True) -> bool:
        with patch.object(qoder, "capable_cli", return_value=capable):
            with patch.object(self.installer, "install_platform_agents", return_value=agents_ok) as agents:
                self.calls = agents
                return self.installer.install_qoder_plugin("qodercn")

    def test_cli_without_plugin_support_writes_the_shared_registry(self) -> None:
        # CLI 存在但太旧、不支持 plugins install 时，安装器直接写共享注册表。
        self.assertTrue(self.install(""))
        state = qoder.inspect(self.descriptor, self.home)
        self.assertTrue(state.installed)
        self.assertEqual(state.version, REPO_VERSION)
        self.calls.assert_called_once_with("qodercn")

    def test_capable_cli_installs_once_for_both_surfaces(self) -> None:
        with patch.object(qoder, "install_via_cli", return_value=qoder.PLUGIN_ID) as via_cli:
            with patch.object(qoder, "install_direct") as direct:
                with patch.object(qoder, "inspect", return_value=qoder.InstallState(
                    root=self.root, installed=True, enabled=True, version=REPO_VERSION, install_path="x"
                )):
                    self.assertTrue(self.install("/usr/bin/qodercn"))
        via_cli.assert_called_once()
        self.assertEqual(via_cli.call_args[0][0], "/usr/bin/qodercn")
        direct.assert_not_called()

    def test_registry_without_the_plugin_is_a_failed_install(self) -> None:
        with patch.object(qoder, "install_direct", return_value=self.root):
            with patch.object(self.installer, "install_platform_agents", return_value=True):
                with patch.object(qoder, "capable_cli", return_value=""):
                    with self.assertRaises(InstallerError):
                        self.installer.install_qoder_plugin("qodercn")

    def test_unsynchronized_global_agents_abort_the_component(self) -> None:
        self.assertFalse(self.install("", agents_ok=False))
        # 全局 subagent 同步失败必须中止安装，且不得留下已写入的插件缓存与注册表。
        self.assertFalse(qoder.plugin_entries(qoder.read_registry(self.root)))
        self.assertFalse(qoder.plugin_cache_root(self.root).exists())

    def test_missing_staged_source_is_rejected_before_touching_the_root(self) -> None:
        installer = build_installer(self.home, None)
        self.assertFalse(installer.install_qoder_plugin("qodercn"))

    def test_platform_without_a_cli_is_skipped_with_a_warning(self) -> None:
        # 自动安装 CLI 失败（无 CLI）时不写共享注册表，也不继续装插件。
        with patch.object(self.installer, "ensure_agent_cli", return_value=False):
            self.assertFalse(self.installer.install_qoder_plugin("qodercn"))
        self.assertFalse(qoder.plugin_entries(qoder.read_registry(self.root)))

    def test_misconfigured_shared_root_raises_an_actionable_error(self) -> None:
        os.environ[self.descriptor.config_env] = str(self.home / "does-not-exist")
        with self.assertRaises(InstallerError):
            self.install("")

    def test_both_platforms_are_dispatchable(self) -> None:
        for key in ("qoder", "qodercn"):
            component = qoder.PLATFORMS[key].component_id
            with patch.object(self.installer, "install_qoder_plugin", return_value=True) as called:
                self.assertTrue(self.installer.install_component(component))
            called.assert_called_once_with(key)

    def test_dispatched_failures_stay_contained(self) -> None:
        with patch.object(self.installer, "install_qoder_plugin", side_effect=InstallerError("boom")):
            self.assertFalse(self.installer.install_component("qodercn_plugin"))
        self.installer.logger.error.assert_called_once()


class UninstallFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.descriptor = qoder.PLATFORMS["qodercn"]
        self.root = self.home / ".qoder-cn"
        self.root.mkdir()
        self.target = qoder.install_direct(self.root, make_stage(self.home), "1.0.0")
        from uninstaller import Uninstaller

        self.uninstaller = Uninstaller.__new__(Uninstaller)
        self.uninstaller.home = self.home
        self.uninstaller.os_name = "Linux"
        self.uninstaller.state_dir = self.home / ".state"
        self.uninstaller.state_dir.mkdir(parents=True)
        self.uninstaller.logger = Mock()
        self.uninstaller.changed_paths = []
        self.environment = patch.dict(os.environ, empty_environment(), clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_shared_root_and_global_agents_are_removed_together(self) -> None:
        with patch.object(self.uninstaller, "uninstall_platform_agents", return_value=True):
            self.assertTrue(self.uninstaller.uninstall_qoder_plugin("qodercn"))
        self.assertFalse(self.target.exists())
        self.assertFalse(qoder.plugin_entries(qoder.read_registry(self.root)))

    def test_leftover_agents_keep_the_component_failing(self) -> None:
        with patch.object(self.uninstaller, "uninstall_platform_agents", return_value=False):
            self.assertFalse(self.uninstaller.uninstall_qoder_plugin("qodercn"))

    def test_unusable_registry_is_reported_instead_of_raising(self) -> None:
        with patch.object(qoder, "uninstall", side_effect=qoder.QoderError("unreadable registry")):
            with patch.object(self.uninstaller, "uninstall_platform_agents", return_value=True):
                self.assertFalse(self.uninstaller.uninstall_qoder_plugin("qodercn"))
        self.uninstaller.logger.error.assert_called_once()

    def test_claude_cache_and_agents_are_cleaned_without_a_cli(self) -> None:
        # The Claude plugin cache is shared with the desktop app, so it must go
        # away even on a machine where the Claude CLI was never installed.
        cache = self.home / ".claude" / "plugins" / "cache" / "co-daming" / "codekit"
        cache.mkdir(parents=True)
        with patch.object(shutil, "which", return_value=None):
            with patch.object(self.uninstaller, "uninstall_platform_agents", return_value=True):
                self.assertTrue(self.uninstaller.uninstall_claude_plugin())
        self.assertFalse((self.home / ".claude" / "plugins" / "cache" / "co-daming").exists())
