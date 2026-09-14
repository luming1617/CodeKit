#!/usr/bin/env python3
"""Tests for the CodeKit toolchain root: bundled runtime and toolchain.json.

Hermetic on purpose: the runtime is the one asset that otherwise needs a ~45 MB
download, so every network and process boundary is mocked here.  What is *not*
mocked is the archive handling and the manifest contract, because those are the
parts that silently produce an unusable toolchain.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# 技能侧共享层按路径加载：它随插件分发、安装器独立，因此两边各有一份实现而不是共用
# 模块；一致性由 SharedResolverParityTests 断言，而不是靠注释声称。
SHARED_SCRIPTS = SCRIPT_DIR.parent / "skills" / "_shared" / "scripts"
sys.path.insert(0, str(SHARED_SCRIPTS))

import installer_config as config  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_state as detector  # noqa: E402
import installer_toolchain as toolchain  # noqa: E402
import codekit_runtime as shared  # noqa: E402
from installer import TOOLCHAIN_COMPONENTS, Installer  # noqa: E402


def fake_installer(home: Path, *, platform: str = "Linux"):
    installer = Mock()
    installer.home = home
    installer.platform = platform_layer.platform_for(platform)
    installer.os_name = installer.platform.os_name
    installer.selected = []
    installer.changed_paths = []
    installer.logger = Mock()
    installer.command_version = Mock(return_value="v24.16.0")
    installer.plugin_version = "9.9.9"
    installer.track = Mock(side_effect=lambda path: installer.changed_paths.append(str(path)))
    installer.run_command = Mock(return_value=Mock(returncode=0))
    return installer


def publish_fake_runtime(installer, spec, *, version: str = "3.13.15") -> Path:
    """Lay out a runtime exactly the way ``_install_archive`` publishes one."""
    root = installer.platform.runtime_dir(installer.home, spec.tag)
    executable = installer.platform.runtime_python(root)
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    config.write_json(
        installer.platform.runtime_current(installer.home),
        {"schema_version": 1, "tag": spec.tag, "python": str(executable), "version": version},
    )
    return executable


def build_archive(destination: Path) -> Path:
    """A python-build-standalone-shaped archive: top-level ``python/``."""
    payload = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(destination, "w:gz") as package:
        for name in ("python/bin/python3", "python/lib/python313.zip"):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            package.addfile(info, io.BytesIO(payload))
    return destination


class RuntimeSpecTests(unittest.TestCase):
    def test_every_pinned_asset_carries_a_digest(self) -> None:
        # 未固定摘要的 pin 会被 --describe 提醒，但发布出去的必须是已校验的。
        for key, spec in toolchain.RUNTIME_SPECS.items():
            with self.subTest(key):
                self.assertRegex(spec.sha256, r"^[0-9a-f]{64}$")

    def test_spec_is_selected_per_platform_and_arch(self) -> None:
        windows = platform_layer.platform_for("Windows")
        spec = toolchain.runtime_spec(windows, "AMD64")
        self.assertIsNotNone(spec)
        self.assertIn("pc-windows-msvc", spec.asset)

    def test_unknown_architecture_is_reported_as_unsupported(self) -> None:
        linux = platform_layer.platform_for("Linux")
        self.assertIsNone(toolchain.runtime_spec(linux, "riscv64"))

    def test_asset_url_escapes_the_plus_in_the_asset_name(self) -> None:
        # 资产名里的 '+' 在 URL 里必须写成 %2B：这正是发布 API 报出来的形式。
        spec = toolchain.RUNTIME_SPECS[("linux", "x86_64")]
        url = toolchain.runtime_archive_url(spec)
        self.assertIn("%2B", url)
        self.assertNotIn("+", url.split("/")[-1])


class ClassifyStateTests(unittest.TestCase):
    def test_missing_path_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                "not-installed", toolchain.classify_state(Path(directory) / "absent")
            )

    def test_denied_stat_is_not_readable_not_absent(self) -> None:
        # 关键区分：is_file() 会把 EACCES 吞成 False，于是「读不到」被误报成「未安装」。
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "entry"
            target.write_text("x", encoding="utf-8")
            with patch.object(os, "stat", side_effect=PermissionError(13, "denied")):
                self.assertEqual("not-readable", toolchain.classify_state(target))

    def test_present_file_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "entry"
            target.write_text("x", encoding="utf-8")
            self.assertEqual("ok", toolchain.readable_state(target))

    def test_file_that_cannot_be_opened_is_not_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "entry"
            target.write_text("x", encoding="utf-8")
            with patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
                self.assertEqual("not-readable", toolchain.readable_state(target))


class VenvBaseStateTests(unittest.TestCase):
    def _venv(self, home: Path, home_value: str, executable_value: str = "") -> Path:
        venv = home / "venv"
        (venv / "Scripts").mkdir(parents=True)
        lines = [f"home = {home_value}"]
        if executable_value:
            lines.append(f"executable = {executable_value}")
        (venv / "pyvenv.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return venv

    def test_reachable_base_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / "runtime"
            base.mkdir()
            venv = self._venv(home, str(base))
            self.assertEqual("ok", toolchain.venv_base_state(venv)["state"])

    def test_missing_base_is_not_installed(self) -> None:
        # venv 在、基础解释器没了：报 not-executable 而不是「没装过」。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            venv = self._venv(home, str(home / "gone"))
            state = toolchain.venv_base_state(venv)
            self.assertEqual("not-installed", state["state"])
            self.assertIn("home", state["values"])

    def test_absent_pyvenv_cfg_is_reported_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            venv.mkdir()
            self.assertEqual("not-installed", toolchain.venv_base_state(venv)["state"])

    def test_cfg_without_any_base_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            venv.mkdir()
            (venv / "pyvenv.cfg").write_text("version = 3.13.15\n", encoding="utf-8")
            self.assertEqual("not-executable", toolchain.venv_base_state(venv)["state"])


class EnsureRuntimeTests(unittest.TestCase):
    def test_existing_runtime_is_reused_without_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            spec = toolchain.runtime_spec(installer.platform, "x86_64")
            publish_fake_runtime(installer, spec)
            installer.download_release = Mock(side_effect=AssertionError("must not download"))
            with patch.object(toolchain, "_probe_runtime", return_value=(True, "3.13.15")):
                state = toolchain.ensure_runtime(installer, arch="x86_64")
            self.assertTrue(state.usable)
            self.assertEqual("3.13.15", state.version)
            self.assertEqual(spec.tag, state.tag)

    def test_broken_existing_runtime_is_refetched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            spec = toolchain.runtime_spec(installer.platform, "x86_64")
            publish_fake_runtime(installer, spec)
            installer.download_release = Mock(return_value=None)
            with patch.object(toolchain, "_probe_runtime", return_value=(False, "")):
                state = toolchain.ensure_runtime(installer, arch="x86_64")
            # 下载失败且旧运行时也起不来 → 如实报 absent，而不是假装可用。
            self.assertFalse(state.usable)
            self.assertEqual(toolchain.RUNTIME_ABSENT, state.state)
            installer.download_release.assert_called_once()

    def test_download_failure_keeps_the_previous_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            spec = toolchain.runtime_spec(installer.platform, "x86_64")
            # 记录一个更早的 tag 作为「上一个可用版本」。
            previous = toolchain.RuntimeSpec(tag="20250101", asset="old.tar.gz", sha256="a" * 64)
            publish_fake_runtime(installer, previous)
            config.write_json(
                installer.platform.runtime_current(installer.home),
                {"schema_version": 1, "tag": previous.tag},
            )
            installer.download_release = Mock(return_value=None)
            with patch.object(
                toolchain, "_probe_runtime", return_value=(True, previous.tag)
            ):
                state = toolchain.ensure_runtime(installer, arch="x86_64")
            self.assertTrue(state.usable)
            self.assertEqual(previous.tag, state.tag)
            self.assertTrue(state.warnings)

    def test_download_verifies_with_the_pinned_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            spec = toolchain.runtime_spec(installer.platform, "x86_64")
            installer.download_release = Mock(return_value=None)
            state = toolchain.ensure_runtime(installer, arch="x86_64")
            installer.download_release.assert_called_once_with(
                toolchain.runtime_archive_url(spec), spec.sha256
            )
            self.assertFalse(state.usable)

    def test_archive_is_normalised_and_published_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = fake_installer(home)
            spec = toolchain.runtime_spec(installer.platform, "x86_64")
            archive = build_archive(home / "runtime.tar.gz")
            installer.download_release = Mock(return_value=archive)
            with patch.object(toolchain, "_probe_runtime", return_value=(True, "3.13.15")), patch.object(
                toolchain, "_venv_smoke_test", return_value=True
            ):
                state = toolchain.ensure_runtime(installer, arch="x86_64")
            self.assertTrue(state.usable)
            target = installer.platform.runtime_dir(home, spec.tag)
            self.assertTrue((target / "python" / "bin" / "python3").is_file())
            # 压缩包里的中间层 raw/ 不能留在发布目录里。
            self.assertFalse((target / "raw").exists())
            current = json.loads(
                installer.platform.runtime_current(home).read_text(encoding="utf-8")
            )
            self.assertEqual(spec.tag, current["tag"])
            # staging 目录必须清干净，否则每次安装都会在工具链根里堆垃圾。
            leftovers = [p for p in installer.platform.runtime_root(home).iterdir() if p.name.startswith(".staging-")]
            self.assertEqual([], leftovers)

    def test_failed_smoke_test_does_not_publish_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = fake_installer(home)
            spec = toolchain.runtime_spec(installer.platform, "x86_64")
            archive = build_archive(home / "runtime.tar.gz")
            installer.download_release = Mock(return_value=archive)
            with patch.object(toolchain, "_probe_runtime", return_value=(True, "3.13.15")), patch.object(
                toolchain, "_venv_smoke_test", return_value=False
            ):
                state = toolchain.ensure_runtime(installer, arch="x86_64")
            self.assertFalse(state.usable)
            self.assertEqual(toolchain.RUNTIME_INCOMPLETE, state.state)
            self.assertFalse(installer.platform.runtime_dir(home, spec.tag).exists())
            self.assertIn("venv", state.reason)

    def test_unsupported_platform_reports_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            state = toolchain.ensure_runtime(installer, arch="riscv64")
            self.assertFalse(state.usable)
            self.assertEqual(toolchain.RUNTIME_ABSENT, state.state)


def seed_gitnexus(installer, *, bin_name: str = "dist/cli/index.js") -> Path:
    """Create the private-prefix package tree npm would have produced."""
    root = installer.platform.npm_prefix_private(installer.home) / "node_modules" / "gitnexus"
    (root / Path(bin_name).parent).mkdir(parents=True, exist_ok=True)
    (root / bin_name).write_text("// cli\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"name": "gitnexus", "version": "1.6.12", "bin": {"gitnexus": bin_name}}),
        encoding="utf-8",
    )
    return root


class GitnexusEntryTests(unittest.TestCase):
    def test_runner_is_an_absolute_node_plus_script(self) -> None:
        # 裸名与 .cmd 垫片在受限宿主里都不可达，所以 runner 必须是绝对路径对。
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            root = seed_gitnexus(installer)
            with patch.object(platform_layer, "resolve_exe", return_value=r"C:\Program Files\nodejs\node.exe"):
                entry = toolchain.gitnexus_entry(installer)
            self.assertEqual("ok", entry["state"])
            self.assertEqual(
                [r"C:\Program Files\nodejs\node.exe", str(root / "dist" / "cli" / "index.js")],
                entry["runner"],
            )
            self.assertEqual("1.6.12", entry["version"])

    def test_missing_package_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            self.assertEqual("not-installed", toolchain.gitnexus_entry(installer)["state"])

    def test_package_without_node_is_not_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            seed_gitnexus(installer)
            with patch.object(platform_layer, "resolve_exe", return_value=None):
                self.assertEqual("not-executable", toolchain.gitnexus_entry(installer)["state"])


class GraphifyEntryTests(unittest.TestCase):
    def test_venv_whose_base_is_gone_is_reported_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            venv = installer.platform.venv_dir(installer.home, "graphify")
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "graphify").write_text("#!/bin/sh\n", encoding="utf-8")
            (venv / "pyvenv.cfg").write_text(
                f"home = {installer.home / 'gone'}\n", encoding="utf-8"
            )
            entry = toolchain.graphify_entry(installer)
            self.assertEqual("not-installed", entry["state"])
            self.assertEqual([], entry["runner"])

    def test_working_venv_is_recorded_with_its_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            base = installer.platform.runtime_dir(installer.home, "20260901") / "python"
            base.mkdir(parents=True)
            venv = installer.platform.venv_dir(installer.home, "graphify")
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "graphify").write_text("#!/bin/sh\n", encoding="utf-8")
            (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
            entry = toolchain.graphify_entry(installer)
            self.assertEqual("ok", entry["state"])
            self.assertEqual([str(venv / "bin" / "graphify")], entry["runner"])
            self.assertEqual(str(base), entry["pyvenv_cfg"]["home"])

    def test_installed_version_is_read_from_the_venv_metadata(self) -> None:
        # 清单里的 version 必须是实测值：留空会让报告把「装好了」显示成「版本未知」。
        with tempfile.TemporaryDirectory() as directory:
            installer = fake_installer(Path(directory))
            base = installer.platform.runtime_dir(installer.home, "20260901") / "python"
            base.mkdir(parents=True)
            venv = installer.platform.venv_dir(installer.home, "graphify")
            (venv / "bin").mkdir(parents=True)
            (venv / "bin" / "graphify").write_text("#!/bin/sh\n", encoding="utf-8")
            (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="0.9.61\n", stderr="")
            with patch.object(toolchain.subprocess, "run", return_value=completed):
                entry = toolchain.graphify_entry(installer)
            self.assertEqual("ok", entry["state"])
            self.assertEqual("0.9.61", entry["version"])


class VenvPackageVersionTests(unittest.TestCase):
    def test_missing_interpreter_probes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(toolchain.subprocess, "run") as run:
                self.assertEqual(
                    "",
                    toolchain._venv_package_version(Path(directory) / "python", "graphifyy"),
                )
            run.assert_not_called()

    def test_version_is_read_through_importlib_metadata(self) -> None:
        # 用元数据而不是 --version 的文本：CLI 输出措辞是第三方的自由。
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.write_text("", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="0.9.61\n", stderr="")
            with patch.object(toolchain.subprocess, "run", return_value=completed) as run:
                self.assertEqual("0.9.61", toolchain._venv_package_version(python, "graphifyy"))
            command = run.call_args.args[0]
            self.assertIn("importlib.metadata", command[2])
            self.assertIn("graphifyy", command[2])

    def test_failed_probe_is_empty_rather_than_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.write_text("", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
            with patch.object(toolchain.subprocess, "run", return_value=completed):
                self.assertEqual("", toolchain._venv_package_version(python, "graphifyy"))

    def test_unlaunchable_interpreter_is_empty_rather_than_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.write_text("", encoding="utf-8")
            with patch.object(
                toolchain.subprocess, "run", side_effect=OSError(101, "cannot create process")
            ):
                self.assertEqual("", toolchain._venv_package_version(python, "graphifyy"))


class ManifestTests(unittest.TestCase):
    def _write(self, home: Path, runtime, *, selected=("gitnexus", "graphify")) -> Path:
        installer = fake_installer(home)
        installer.selected = list(selected)
        return toolchain.write_manifest(installer, runtime, selected=selected)

    def test_manifest_round_trips_and_records_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = fake_installer(home)
            installer.platform.toolchain_root(home).mkdir(parents=True, exist_ok=True)
            seed_gitnexus(installer)
            runtime = toolchain.RuntimeState(
                state=toolchain.RUNTIME_OK,
                python=home / "runtime" / "20260901" / "python" / "bin" / "python3",
                version="3.13.15",
                tag="20260901",
                sha256="b" * 64,
            )
            with patch.object(platform_layer, "resolve_exe", return_value="/usr/bin/node"):
                path = self._write(home, runtime, selected=("gitnexus",))
            self.assertIsNotNone(path)
            document = toolchain.read_manifest(path)
            self.assertEqual(1, document["schema_version"])
            self.assertEqual("3.13.15", document["runtime"]["version"])
            self.assertTrue(document["runtime"]["venv_ok"])
            self.assertEqual("ok", document["tools"]["gitnexus"]["state"])
            # 未选中的组件不写条目：清单不描述 CodeKit 没接管的东西。
            self.assertNotIn("graphify", document["tools"])

    def test_manifest_requires_the_toolchain_root_to_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(self._write(Path(directory), toolchain.RuntimeState()))

    def test_manifest_is_read_only_to_its_owner(self) -> None:
        # 与 initialize 的 run-manifest 同一约定；Windows 没有 POSIX mode bits。
        if os.name == "nt":
            self.skipTest("Windows 不支持 POSIX mode bits")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = fake_installer(home)
            installer.platform.toolchain_root(home).mkdir(parents=True, exist_ok=True)
            path = self._write(home, toolchain.RuntimeState(), selected=())
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_wrong_schema_version_is_treated_as_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "toolchain.json"
            config.write_json(path, {"schema_version": 99, "tools": {"gitnexus": {"state": "ok"}}})
            self.assertEqual({}, toolchain.read_manifest(path))

    def test_invalid_json_degrades_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "toolchain.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual({}, toolchain.read_manifest(path))


class SharedResolverParityTests(unittest.TestCase):
    """安装器侧与插件侧对同一份契约的实现必须给出同一个答案。

    `skills/_shared/scripts/codekit_runtime.py` 与 `installer_platform` /
    `installer_toolchain` 各有一份实现——技能随插件分发、安装器独立，无法共用模块。
    所以一致性必须由这组用例保证：只靠注释声称「两边同源」是没人执行的承诺，一旦漂移，
    安装器写清单的判据与技能读清单的判据会给出相反的结论。
    """

    def test_toolchain_root_is_the_same_directory_on_both_sides(self) -> None:
        home = Path("/home/me")
        for system in ("Windows", "Darwin", "Linux"):
            with self.subTest(system):
                self.assertEqual(
                    platform_layer.platform_for(system).toolchain_root(home),
                    shared.toolchain_root(home),
                )

    def test_managed_tool_list_is_the_same_tuple(self) -> None:
        self.assertEqual(tuple(shared.KNOWN_TOOLS), tuple(TOOLCHAIN_COMPONENTS))

    def test_state_classification_agrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            present = root / "entry"
            present.write_text("x", encoding="utf-8")
            absent = root / "gone"
            for path in (present, absent, root):
                with self.subTest(str(path)):
                    self.assertEqual(
                        toolchain.classify_state(path), shared.classify_state(path)
                    )
            with patch.object(os, "stat", side_effect=PermissionError(13, "denied")):
                self.assertEqual(
                    toolchain.classify_state(present), shared.classify_state(present)
                )

    def test_readable_state_agrees_on_every_outcome(self) -> None:
        """含「stat 能过但 open 打不开且不是权限问题」这一分支。

        这一条曾被两侧写成不同的态（安装器 not-readable / 技能 not-executable）；既然
        两处注释都声称同源，就必须有用例真的走一遍，而不是只声明。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            present = root / "entry"
            present.write_text("x", encoding="utf-8")
            for path in (present, root, root / "gone"):
                with self.subTest(str(path)):
                    self.assertEqual(
                        toolchain.readable_state(path), shared.readable_state(path)
                    )
            for error in (PermissionError(13, "denied"), OSError(22, "invalid argument")):
                with self.subTest(error.__class__.__name__, errno=error.errno):
                    with patch.object(Path, "open", side_effect=error):
                        self.assertEqual(
                            toolchain.readable_state(present),
                            shared.readable_state(present),
                        )

    def test_venv_base_state_agrees_on_every_outcome(self) -> None:
        def both(venv: Path) -> tuple[dict, dict]:
            return toolchain.venv_base_state(venv), shared.venv_base_state(venv)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reachable = root / "runtime"
            reachable.mkdir()

            cases: dict[str, Path] = {}
            cases["reachable"] = self._venv(root / "ok", f"home = {reachable}")
            cases["gone base"] = self._venv(root / "gone-base", f"home = {root / 'nope'}")
            cases["no cfg"] = root / "no-cfg"
            cases["no base keys"] = self._venv(root / "no-keys", "version = 3.13.15")
            cfg_dir = root / "cfg-dir"
            (cfg_dir / "pyvenv.cfg").mkdir(parents=True)
            cases["cfg is a directory"] = cfg_dir
            cases["cfg unreadable"] = self._venv(root / "unreadable", f"home = {reachable}")

            for name, venv in cases.items():
                with self.subTest(name):
                    installed, shared_result = both(venv)
                    self.assertEqual(installed["state"], shared_result["state"])
                    self.assertEqual(installed["values"], shared_result["values"])

            # 目录不是目录、以及打不开的入口：两侧必须归到同一个态（这正是修复过的分叉）。
            with patch.object(Path, "read_text", side_effect=NotADirectoryError(20, "not a dir")):
                self.assertEqual(
                    toolchain.venv_base_state(cases["cfg unreadable"])["state"],
                    shared.venv_base_state(cases["cfg unreadable"])["state"],
                )
            with patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")):
                self.assertEqual(
                    toolchain.venv_base_state(cases["cfg unreadable"])["state"],
                    shared.venv_base_state(cases["cfg unreadable"])["state"],
                )

    @staticmethod
    def _venv(venv: Path, cfg_text: str, *, cfg: bool = True) -> Path:
        (venv / "bin").mkdir(parents=True)
        if cfg:
            (venv / "pyvenv.cfg").write_text(cfg_text + "\n", encoding="utf-8")
        return venv


def toolchain_installer(home: Path, *, system: str = "Windows"):
    """An installer session with only what the toolchain decisions need."""
    installer = Installer.__new__(Installer)
    installer.home = home
    installer.platform = platform_layer.platform_for(system)
    installer.os_name = installer.platform.os_name
    installer.selected = []
    installer.states = {}
    installer.install_records = []
    installer.changed_paths = []
    installer.logger = Mock()
    installer.toolchain = toolchain.RuntimeState()
    installer.command_exists = Mock(return_value=False)
    return installer


def seed_local_gitnexus(installer) -> None:
    """A gitnexus already materialised inside the toolchain root."""
    root = installer.platform.npm_prefix_private(installer.home) / "node_modules" / "gitnexus"
    (root / "dist" / "cli").mkdir(parents=True)
    (root / "dist" / "cli" / "index.js").write_text("// cli\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {"name": "gitnexus", "version": "1.6.12", "bin": {"gitnexus": "dist/cli/index.js"}}
        ),
        encoding="utf-8",
    )


def seed_graphify_venv(installer, base: Path) -> None:
    venv = installer.platform.venv_dir(installer.home, "graphify")
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "graphify.exe").write_text("", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")


class MigrationTargetTests(unittest.TestCase):
    """已装、但落点在工具链根之外的组件必须被识别出来并自动迁移。

    这是最初问题的直接成因：用户重跑安装器时只勾了插件（gitnexus / graphify 已装，
    默认不勾），于是工具链根永远是空的，「重跑安装器」这句修复建议落不到实处。
    """

    def test_never_installed_component_is_not_a_migration_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            self.assertEqual([], installer.toolchain_migration_targets())

    def test_installed_elsewhere_is_a_migration_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            installer.command_exists = Mock(side_effect=lambda name: name == "gitnexus")
            self.assertIn("gitnexus", installer.toolchain_migration_targets())

    def test_already_local_component_is_not_migrated_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            installer.command_exists = Mock(side_effect=lambda name: name == "gitnexus")
            seed_local_gitnexus(installer)
            with patch.object(
                platform_layer, "resolve_exe", return_value=r"C:\nodejs\node.exe"
            ):
                self.assertEqual([], installer.toolchain_migration_targets())

    def test_graphify_venv_pointing_at_the_system_python_is_migrated(self) -> None:
        # 这正是本机上实测的旧状态：venv 在根里，但基础解释器在 %LOCALAPPDATA% 下。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = toolchain_installer(home)
            seed_graphify_venv(
                installer, home / "AppData" / "Local" / "Programs" / "Python" / "Python314"
            )
            self.assertIn("graphify", installer.toolchain_migration_targets())

    def test_graphify_venv_pointing_inside_the_root_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = toolchain_installer(home)
            base = installer.platform.runtime_dir(home, "20260901") / "python"
            base.mkdir(parents=True)
            seed_graphify_venv(installer, base)
            self.assertEqual([], installer.toolchain_migration_targets())

    def test_setup_toolchain_records_migration_and_fetches_the_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installer = toolchain_installer(home)
            seed_graphify_venv(installer, home / "system-python")
            runtime = toolchain.RuntimeState(
                state=toolchain.RUNTIME_OK,
                python=home / "rt" / "python.exe",
                version="3.13.15",
                tag="20260901",
            )
            with patch.object(toolchain, "ensure_runtime", return_value=runtime) as ensure:
                installer.setup_toolchain()
            self.assertEqual(["graphify"], installer.toolchain_migration)
            self.assertTrue(installer.needs_bundled_runtime())
            ensure.assert_called_once()
            self.assertTrue(installer.toolchain.usable)

    def test_migration_targets_are_installed_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            installer.toolchain_migration = ["graphify"]
            installer.install_component = Mock(return_value=True)
            installer.run_installs()
            installer.install_component.assert_called_once_with("graphify")
            self.assertEqual([("graphify", "迁移", "1")], installer.install_records)

    def test_toolchain_targets_merge_selection_and_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            installer.selected = ["gitnexus"]
            installer.toolchain_migration = ["graphify", "gitnexus"]
            self.assertEqual(["gitnexus", "graphify"], installer.toolchain_targets())


class ChannelOrderTests(unittest.TestCase):
    """工具链托管的组件：可达性优先，venv 通道必须排在 pipx 之前。

    pipx 的 shim 落在工具链根之外，受限宿主读不到——「不需要符号链接权限」这个理由
    已经让位给「装上了在会话里能不能跑起来」。
    """

    def _installer_with_runtime(self, home: Path):
        installer = toolchain_installer(home)
        installer.toolchain = toolchain.RuntimeState(
            state=toolchain.RUNTIME_OK, python=Path("python.exe"), version="3.13.15"
        )
        return installer

    def test_venv_precedes_pipx_for_a_toolchain_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer_with_runtime(Path(directory))
            order = installer.channel_order(detector.registry.component_by_id("graphify"))
            self.assertLess(order.index("venv"), order.index("pipx"))

    def test_order_is_untouched_without_a_usable_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            order = installer.channel_order(detector.registry.component_by_id("graphify"))
            self.assertLess(order.index("pipx"), order.index("venv"))

    def test_order_is_untouched_for_other_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer_with_runtime(Path(directory))
            order = installer.channel_order(detector.registry.component_by_id("gh"))
            self.assertLess(order.index("pipx"), order.index("venv"))

    def test_graphify_is_not_installed_outside_the_root_without_a_runtime(self) -> None:
        """运行时不可用时如实失败，不退到 pipx 产出一份宿主读不到的副本。"""
        with tempfile.TemporaryDirectory() as directory:
            installer = toolchain_installer(Path(directory))
            with patch.object(Installer, "install_channel") as channel:
                self.assertFalse(installer.install_via_channels("graphify"))
            channel.assert_not_called()

    def test_graphify_uses_the_venv_channel_when_the_runtime_is_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installer = self._installer_with_runtime(Path(directory))
            with patch.object(
                Installer, "channel_available", return_value=True
            ), patch.object(Installer, "install_channel", return_value=True) as channel:
                self.assertTrue(installer.install_via_channels("graphify"))
            self.assertEqual("venv", channel.call_args.args[1])


class RecordedInstallPathTests(unittest.TestCase):
    """收尾表格报的路径必须来自工具链清单，而不是按命令名猜出来的旧落点。"""

    def _context(self, home: Path) -> detector.Context:
        return detector.Context(
            home=home, marketplace_local=None, version_file=home / "installed_versions"
        )

    def test_manifest_entry_outranks_a_root_outside_shim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            platform = platform_layer.platform_for("Windows")
            package_root = platform.npm_prefix_private(home) / "node_modules" / "gitnexus"
            package_root.mkdir(parents=True)
            config.write_json(
                platform.toolchain_manifest(home),
                {
                    "schema_version": toolchain.MANIFEST_SCHEMA_VERSION,
                    "tools": {
                        "gitnexus": {
                            "kind": "node-cli",
                            "state": "ok",
                            "package_root": str(package_root),
                            "runner": [r"C:\nodejs\node.exe", str(package_root / "cli.js")],
                        }
                    },
                },
            )
            # 根外旧垫片存在也不采信：受限宿主读不到它。
            with patch.object(detector.shutil, "which", return_value=r"C:\npm\gitnexus.cmd"):
                reported = detector.component_install_path("gitnexus", self._context(home))
            self.assertIn("node_modules", reported.replace("\\", "/"))

    def test_non_ok_entry_falls_back_instead_of_being_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            platform = platform_layer.platform_for("Linux")
            config.write_json(
                platform.toolchain_manifest(home),
                {
                    "schema_version": toolchain.MANIFEST_SCHEMA_VERSION,
                    "tools": {"graphify": {"kind": "python-venv", "state": "not-readable"}},
                },
            )
            self.assertIsNone(detector.toolchain_recorded_path("graphify", self._context(home)))

    def test_missing_manifest_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertIsNone(
                detector.toolchain_recorded_path("gitnexus", self._context(home))
            )


if __name__ == "__main__":
    unittest.main()
