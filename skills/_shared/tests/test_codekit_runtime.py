#!/usr/bin/env python3
"""Tests for the shared toolchain resolver.

The behaviour under test is the part that used to be wrong in a way nobody could
see: the two-state resolver could not tell "not installed" from "installed but the
sandbox refuses to read it", so every hint it produced was a guess.  These cases
pin the four states and the resolution order.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SHARED_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SHARED_DIR))

import codekit_runtime as runtime  # noqa: E402


def write_manifest(home: Path, tools: dict, *, schema_version: int = 1) -> Path:
    root = runtime.toolchain_root(home)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "toolchain.json"
    path.write_text(
        json.dumps({"schema_version": schema_version, "tools": tools}),
        encoding="utf-8",
    )
    return path


class ClassifyStateTests(unittest.TestCase):
    def test_missing_path_is_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                runtime.NOT_INSTALLED,
                runtime.classify_state(Path(directory) / "absent"),
            )

    def test_present_file_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "entry"
            target.write_text("x", encoding="utf-8")
            self.assertEqual(runtime.OK, runtime.classify_state(target))

    def test_denied_stat_is_not_readable(self) -> None:
        # 这条就是旧实现丢掉的那一态：Path.is_file() 会把 EACCES 吞成 False。
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "entry"
            target.write_text("x", encoding="utf-8")
            with patch.object(os, "stat", side_effect=PermissionError(13, "denied")):
                self.assertEqual(runtime.NOT_READABLE, runtime.classify_state(target))

    def test_a_directory_is_ok_without_opening_it(self) -> None:
        # Windows 上 open(目录) 必然 EACCES；目录只看 stat，否则可用路径被标成不可读。
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(runtime.OK, runtime.readable_state(Path(directory)))

    def test_file_that_cannot_be_opened_is_not_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "entry"
            target.write_text("x", encoding="utf-8")
            with patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
                self.assertEqual(runtime.NOT_READABLE, runtime.readable_state(target))


class ManifestTests(unittest.TestCase):
    def test_missing_manifest_degrades_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual({}, runtime.read_manifest(Path(directory)))

    def test_invalid_json_degrades_to_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = runtime.toolchain_root(home)
            root.mkdir(parents=True, exist_ok=True)
            (root / "toolchain.json").write_text("{oops", encoding="utf-8")
            self.assertEqual({}, runtime.read_manifest(home))

    def test_future_schema_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_manifest(home, {"gitnexus": {"state": "ok", "runner": ["x"]}}, schema_version=99)
            self.assertEqual({}, runtime.read_manifest(home))

    def test_non_ok_entry_is_not_trusted(self) -> None:
        # 安装器写入的非 ok 条目是「实测不可用」，采信它等于把失败的安装当成功。
        document = {"tools": {"gitnexus": {"state": "not-readable", "runner": ["/x"]}}}
        self.assertEqual([], runtime.manifest_argv(document, "gitnexus"))

    def test_runner_must_be_a_list(self) -> None:
        document = {"tools": {"gitnexus": {"state": "ok", "runner": "gitnexus"}}}
        self.assertEqual([], runtime.manifest_argv(document, "gitnexus"))


class RunnerOverrideTests(unittest.TestCase):
    def test_windows_paths_survive_shlex(self) -> None:
        # posix=False 是必需的：POSIX 模式会把反斜杠当转义符，解析出一条不存在的路径。
        override = r"C:\Users\me\python.exe C:\x\run.cjs"
        argv = runtime.runner_override("gitnexus", {"CODEKIT_GITNEXUS_RUNNER": override})
        self.assertEqual([r"C:\Users\me\python.exe", r"C:\x\run.cjs"], argv)

    def test_quoted_paths_are_unquoted(self) -> None:
        argv = runtime.runner_override(
            "graphify", {"CODEKIT_GRAPHIFY_RUNNER": '"C:\\Program Files\\py.exe" -m graphify'}
        )
        self.assertEqual([r"C:\Program Files\py.exe", "-m", "graphify"], argv)

    def test_empty_override_is_ignored(self) -> None:
        self.assertEqual([], runtime.runner_override("gitnexus", {"CODEKIT_GITNEXUS_RUNNER": "  "}))


class VenvBaseStateTests(unittest.TestCase):
    def _venv(self, home: Path, home_value: str) -> Path:
        venv = home / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text(f"home = {home_value}\n", encoding="utf-8")
        return venv

    def test_reachable_base_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            base = home / "runtime"
            base.mkdir()
            self.assertEqual(runtime.OK, runtime.venv_base_state(self._venv(home, str(base)))["state"])

    def test_missing_base_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            state = runtime.venv_base_state(self._venv(home, str(home / "gone")))
            self.assertEqual(runtime.NOT_INSTALLED, state["state"])
            self.assertIn("home", state["values"])

    def test_absent_cfg_is_reported_as_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            venv.mkdir()
            self.assertEqual(runtime.NOT_INSTALLED, runtime.venv_base_state(venv)["state"])


class ClassifyArgvTests(unittest.TestCase):
    def test_missing_entry_element_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, _ = runtime.classify_argv([str(Path(directory) / "absent")])
            self.assertEqual(runtime.NOT_INSTALLED, state)

    def test_venv_with_a_gone_base_is_not_executable(self) -> None:
        # 实测形态：入口可读、能 stat，启动却失败（rc=101 Unable to create process）。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            venv = home / "venv"
            (venv / "bin").mkdir(parents=True)
            entry = venv / "bin" / "graphify"
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            (venv / "pyvenv.cfg").write_text(
                f"home = {home / 'gone'}\n", encoding="utf-8"
            )
            state, detail = runtime.classify_argv([str(entry)])
            self.assertEqual(runtime.NOT_EXECUTABLE, state)
            self.assertIn("基础解释器", detail)


class ResolveToolTests(unittest.TestCase):
    def test_manifest_outranks_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            tool = home / "toolchain" / "gitnexus.cmd"
            tool.parent.mkdir(parents=True)
            tool.write_text("", encoding="utf-8")
            write_manifest(home, {"gitnexus": {"state": "ok", "runner": [str(tool)]}})
            with patch.object(runtime.shutil, "which", return_value=r"C:\other\gitnexus.cmd"):
                resolution = runtime.resolve_tool("gitnexus", home=home)
            self.assertEqual("toolchain", resolution.source)
            self.assertEqual([str(tool)], resolution.argv)

    def test_stale_manifest_entry_falls_through_to_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            on_path = home / "bin" / "gitnexus"
            on_path.parent.mkdir(parents=True)
            on_path.write_text("", encoding="utf-8")
            write_manifest(
                home,
                {"gitnexus": {"state": "ok", "runner": [str(home / "gone" / "gitnexus.cmd")]}},
            )
            # 候选目录整体隔离：否则用例会命中宿主真实安装的 npm 目录，测不到回退顺序。
            with patch.object(runtime.shutil, "which", return_value=str(on_path)), patch.object(
                runtime, "toolchain_paths", return_value=[]
            ):
                resolution = runtime.resolve_tool("gitnexus", home=home)
            self.assertEqual("path", resolution.source)
            self.assertEqual(runtime.OK, resolution.state)

    def test_denied_candidates_report_not_readable(self) -> None:
        # 候选全被拒绝读取时必须报 not-readable：报 not-installed 会让用户去重装一个
        # 其实已经装好的工具。
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            denied = home / "AppData" / "Roaming" / "npm" / "gitnexus.cmd"
            with patch.object(
                runtime, "classify_state", return_value=runtime.NOT_READABLE
            ), patch.object(runtime.shutil, "which", return_value=None), patch.object(
                runtime, "toolchain_paths", return_value=[denied.parent]
            ):
                resolution = runtime.resolve_tool("gitnexus", home=home)
            self.assertEqual(runtime.NOT_READABLE, resolution.state)
            self.assertEqual("declared", resolution.source)
            self.assertFalse(resolution.resolved)

    def test_all_stages_failing_reports_the_first_specific_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            entry = home / "bin" / "gitnexus"
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")
            with patch.object(
                runtime, "classify_argv", return_value=(runtime.NOT_READABLE, "denied")
            ), patch.object(runtime.shutil, "which", return_value=None), patch.object(
                runtime, "toolchain_paths", return_value=[entry.parent]
            ):
                resolution = runtime.resolve_tool("gitnexus", home=home)
            self.assertEqual(runtime.NOT_READABLE, resolution.state)
            self.assertFalse(resolution.resolved)

    def test_unresolved_tool_is_not_installed_with_a_hint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(runtime.shutil, "which", return_value=None), patch.object(
                runtime, "toolchain_paths", return_value=[]
            ):
                resolution = runtime.resolve_tool("graphify", home=home)
            self.assertEqual(runtime.NOT_INSTALLED, resolution.state)
            self.assertEqual("unresolved", resolution.source)
            self.assertIn("未安装", runtime.state_hint("graphify", resolution.state, home=home))

    def test_override_wins_over_everything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            tool = home / "custom" / "gitnexus"
            tool.parent.mkdir(parents=True)
            tool.write_text("", encoding="utf-8")
            write_manifest(home, {"gitnexus": {"state": "ok", "runner": [str(home / "other")]}})
            resolution = runtime.resolve_tool(
                "gitnexus", home=home, environ={"CODEKIT_GITNEXUS_RUNNER": str(tool)}
            )
            self.assertEqual("override", resolution.source)
            self.assertEqual([str(tool)], resolution.argv)


class ExplainTests(unittest.TestCase):
    """``--explain`` 的逐阶段记录：故障归因要能指出是哪一层拒绝的。"""

    def test_stages_stop_at_the_winning_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            tool = home / "toolchain" / "gitnexus.cmd"
            tool.parent.mkdir(parents=True)
            tool.write_text("", encoding="utf-8")
            write_manifest(home, {"gitnexus": {"state": "ok", "runner": [str(tool)]}})
            with patch.object(runtime.shutil, "which", return_value=r"C:\other\gitnexus.cmd"):
                resolution = runtime.resolve_tool("gitnexus", home=home)
            # 清单命中即返回：PATH 那一层根本不该被采信，也不该出现在轨迹里。
            self.assertEqual(["toolchain"], [stage["source"] for stage in resolution.stages])
            self.assertEqual(runtime.OK, resolution.stages[-1]["state"])

    def test_explanation_names_the_refused_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            denied = home / "AppData" / "Roaming" / "npm" / "gitnexus.cmd"
            with patch.object(
                runtime, "classify_state", return_value=runtime.NOT_READABLE
            ), patch.object(runtime.shutil, "which", return_value=None), patch.object(
                runtime, "toolchain_paths", return_value=[denied.parent]
            ):
                resolution = runtime.resolve_tool("gitnexus", home=home)
            text = runtime.format_explanation(resolution, home=home)
            self.assertIn("declared: 不可用（not-readable）", text)
            self.assertIn("→", text)

    def test_explanation_says_so_when_no_source_produced_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(runtime.shutil, "which", return_value=None), patch.object(
                runtime, "toolchain_paths", return_value=[]
            ):
                resolution = runtime.resolve_tool("graphify", home=home)
            text = runtime.format_explanation(resolution, home=home)
            self.assertIn("graphify: not-installed", text)
            self.assertIn("没有任何候选来源", text)

    def test_json_carries_the_trace_only_when_asked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(runtime.shutil, "which", return_value=None), patch.object(
                runtime, "toolchain_paths", return_value=[]
            ):
                resolution = runtime.resolve_tool("graphify", home=home)
            self.assertNotIn("stages", resolution.to_json())
            self.assertIn("stages", resolution.to_json(explain=True))


class ResolveRunnerCliTests(unittest.TestCase):
    """CLI 契约：退出码按态区分，``--explain`` 才附带轨迹。"""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # CODEKIT_*_RUNNER 由本机环境变量清掉，否则用例会跟着开发者环境漂移。
        environ = {key: value for key, value in os.environ.items() if not key.startswith("CODEKIT_")}
        # -X utf8：Windows 上未设置时子进程按本地代码页（GBK）写 stdout，中文断言会碎。
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SHARED_DIR / "resolve-runner.py"), *args],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _home_with_manifest(self, home: Path) -> Path:
        tool = home / "gitnexus.cmd"
        tool.write_text("", encoding="utf-8")
        write_manifest(home, {"gitnexus": {"state": "ok", "runner": [str(tool)]}})
        return tool

    def test_explain_marks_the_winning_stage_and_keeps_the_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._home_with_manifest(home)
            completed = self._run("--tool", "gitnexus", "--explain", "--home", directory)
            self.assertEqual(0, completed.returncode)
            self.assertIn("toolchain: 命中（ok）", completed.stdout)

    def test_json_is_unchanged_without_explain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._home_with_manifest(home)
            completed = self._run("--tool", "gitnexus", "--json", "--home", directory)
            self.assertEqual(0, completed.returncode)
            self.assertNotIn("stages", json.loads(completed.stdout))

    def test_json_with_explain_carries_the_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self._home_with_manifest(home)
            completed = self._run("--tool", "gitnexus", "--json", "--explain", "--home", directory)
            payload = json.loads(completed.stdout)
            self.assertEqual(["toolchain"], [stage["source"] for stage in payload["stages"]])

    def test_exit_code_matches_the_reported_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = self._run(
                "--tool", "graphify", "--json", "--home", str(Path(directory) / "absent-home")
            )
            # 只断言「码与态一致」：这个 home 是空的，但候选目录包含宿主真实安装位置，
            # 具体落到哪一态取决于机器，钉死数值会让用例跟着环境漂移。
            payload = json.loads(completed.stdout)
            self.assertEqual(
                {"ok": 0, "not-installed": 3, "not-readable": 4, "not-executable": 5}[
                    payload["state"]
                ],
                completed.returncode,
            )


class ToolchainPathTests(unittest.TestCase):
    def test_toolchain_root_candidates_come_first(self) -> None:
        paths = runtime.toolchain_paths(Path("/tmp/missing-home"))
        self.assertEqual(runtime.toolchain_root(Path("/tmp/missing-home")) / "npm", paths[0])

    def test_candidates_are_deduplicated(self) -> None:
        paths = runtime.toolchain_paths(Path("/tmp/missing-home"))
        keys = [os.path.normcase(str(path)) for path in paths]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
