from __future__ import annotations

import importlib.util
import errno
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
import os
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))


def load_script(name: str):
    """按名字加载技能自带脚本，找不到时回退到 skills/_shared/scripts 的共享脚本。"""
    path = SKILL_DIR / "scripts" / name
    if not path.is_file():
        path = SKILL_DIR.parent / "_shared" / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


prepare = load_script("prepare.py")
reconcile = load_script("reconcile.py")
validator = load_script("validate-result.py")
controller = load_script("controller.py")
gitnexus_runner = load_script("run-gitnexus.py")
gitnexus_step = load_script("gitnexus-step.py")
graphify_step = load_script("graphify-step.py")
probe_collab = load_script("probe-collab.py")
common = sys.modules["_common"]

# Windows reports every writable file as 0o666: os.chmod only maps the read-only
# bit, so POSIX permission bits are not observable there.
POSIX_MODES = os.name != "nt"
POSIX_MODE_REASON = "os.chmod on Windows only maps the read-only bit"
# A clean Windows `python3` is the Microsoft Store alias stub: shutil.which finds
# it, but it exits non-zero, so fixtures name the interpreter that really runs.
VALIDATION_PYTHON = "py -3" if os.name == "nt" else "python3"


def validation_python_runs() -> bool:
    try:
        completed = subprocess.run(
            [*VALIDATION_PYTHON.split(), "-c", "pass"], capture_output=True, check=False
        )
    except OSError:
        return False
    return completed.returncode == 0


def worker_result(task: str, *, changed=None, allowed=None, artifacts=None, run_id: str = "test-run") -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "task": task,
        "status": "success",
        "required": True,
        "runner": task,
        "command": [task],
        "exit_code": 0,
        "changed_files": changed or [],
        "allowed_paths": allowed or [],
        "artifacts": artifacts or [],
        "warnings": [],
        "started_at": "2026-09-08T00:00:00Z",
        "finished_at": "2026-09-08T00:00:01Z",
        "duration_ms": 1000,
        "error": {"code": None, "message": None},
    }


@contextmanager
def denied_stat(prefix: Path):
    """让 ``prefix`` 下的路径在 ``stat`` 时抛 EACCES，复现沙箱的按路径拒绝。

    实测形态是**按路径拒绝文件系统访问**（``WinError 5``），而 ``PATH`` 里的用户级条目
    是全的。解析层因此按显式 ``stat`` 的 errno 分类，模拟也必须拦在 ``os.stat`` 上：
    只拦 ``Path.is_file``（旧实现的做法）已经测不到任何东西，因为 CPython 会把那里的
    ``OSError`` 吞掉返回 ``False``，于是「被拒绝」与「不存在」无法区分。
    """
    original = os.stat
    prefix_text = str(prefix)

    def guarded(path, *args, **kwargs):
        if str(path).startswith(prefix_text):
            raise PermissionError(errno.EACCES, "Access is denied")
        return original(path, *args, **kwargs)

    with patch.object(common.runtime.os, "stat", guarded):
        yield


def no_ambient_manifest():
    """隔离宿主机真实安装写出的 ``toolchain.json``，让解析只看到用例给定的输入。"""
    return patch.object(common.runtime, "read_manifest", return_value={})


def git_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    # gc --auto forks a writer inside .git, which races with cleanup.
    subprocess.run(["git", "-C", str(root), "config", "gc.auto", "0"], check=True)


def git_scratch_directory() -> tempfile.TemporaryDirectory:
    """Scratch repository root that tolerates asynchronous ``.git`` writers.

    Some git extensions in developer environments append to ``.git/`` right after
    a commit, which otherwise surfaces as ENOTEMPTY while tearing the fixture
    down and makes the suite flaky for reasons unrelated to the scripts.
    """
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def commit_count(root: Path) -> int:
    return int(subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        text=True, capture_output=True, check=True,
    ).stdout.strip())


def analyze_manifest(root: Path) -> dict:
    """Gate input for the ignored-scope tests: analyzer prefixes, no skill support."""
    return {
        "root": str(root),
        "baseline_status": {},
        "baseline_files": {},
        "worker_tree_roots": [".gitnexus", "graphify-out", ".claude/skills"],
        "worker_scope_baseline": {},
        "tools": {},
    }


def scan_context(root: Path, commands: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "root": str(root),
        "overview": "Test repository.",
        "structure": [{"path": "README.md", "description": "Fixture documentation"}],
        "commands": commands,
        "evidence": [{"path": "README.md"}],
        "warnings": [],
    }


def write_fake_gitnexus(root: Path, *, status: str) -> Path:
    """最小 GitNexus 替身：只回答 status，供「控制器执行 gitnexus」的流程用例使用。"""
    fake = root / "fake-gitnexus.py"
    fake.write_text(
        "import json, sys\n"
        "if sys.argv[1:2] == ['status']:\n"
        f"    print(json.dumps({{'status': {status!r}}}))\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return fake


GRAPHIFY_FAKE_HELP = (
    "  update <path>    re-extract code files (no LLM needed)\n"
    "  cluster-only <path>\n"
    "  extract <path>   [--backend B] [--force] [--max-workers N] "
    "[--code-only] [--no-viz]\n"
    "  hook install\n"
)


def write_fake_graphify(root: Path) -> Path:
    """Graphify 替身：把真实 argv 与并行开关落进 invoked.json 供断言。

    产物写到 ``<--out>/graphify-out/``，``.graphify_root`` 记录解析后的绝对路径，与真实
    CLI 的两个写入点保持一致。行为由 ``FAKE_GRAPHIFY_BEHAVIOR`` 选择：``ok``、
    ``nothing-to-update``（退出码 1 表达无增量）、``crash``、``no-artifacts``。
    """
    body = (
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "root = Path(os.getcwd())\n"
        "if args[:1] == ['--version']:\n"
        "    print('graphify 9.9.9')\n"
        "    raise SystemExit(0)\n"
        "if args[:1] == ['--help']:\n"
        f"    print({GRAPHIFY_FAKE_HELP!r})\n"
        "    raise SystemExit(0)\n"
        "if args[:2] == ['hook', 'install']:\n"
        "    print('post-commit: installed')\n"
        "    raise SystemExit(0)\n"
        "(root / 'invoked.json').write_text(json.dumps({\n"
        "    'args': args, 'workers': os.environ.get('GRAPHIFY_MAX_WORKERS'),\n"
        "}), encoding='utf-8')\n"
        "behavior = os.environ.get('FAKE_GRAPHIFY_BEHAVIOR', 'ok')\n"
        "if behavior == 'nothing-to-update':\n"
        "    print('Nothing to update or rebuild failed - check output above.',"
        " file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "if behavior == 'crash':\n"
        "    print('boom', file=sys.stderr)\n"
        "    raise SystemExit(4)\n"
        "if behavior == 'no-artifacts':\n"
        "    raise SystemExit(0)\n"
        "out = Path(args[args.index('--out') + 1]) if '--out' in args else Path(args[1])\n"
        "target = out / 'graphify-out'\n"
        "target.mkdir(parents=True, exist_ok=True)\n"
        "(target / 'graph.json').write_text(\n"
        "    json.dumps({'nodes': [{'id': 'n'}]}), encoding='utf-8')\n"
        "(target / 'manifest.json').write_text(json.dumps({'root': str(root)}), encoding='utf-8')\n"
        "(target / '.graphify_root').write_text(str(Path(args[1]).resolve()), encoding='utf-8')\n"
        "raise SystemExit(0)\n"
    )
    (root / "fake-graphify.py").write_text(body, encoding="utf-8")
    if os.name == "nt":
        # CreateProcess 起不了无扩展名的 POSIX 脚本：CODEKIT_GRAPHIFY_RUNNER 的第一个
        # token 既被 prepare 探测、又被控制器步骤直接执行，必须是真实可执行入口。
        launcher = root / "graphify-fake.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fake-graphify.py" %*\r\n', encoding="utf-8"
        )
        return launcher
    launcher = root / "graphify-fake"
    launcher.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


@contextmanager
def isolated_gitnexus_home(root: Path, *, registered: bool = True):
    """隔离 GitNexus 全局目录，并可预先登记 ``root``。

    ``list``/``query``/``context``/``impact``/``trace``/``cypher``/``check``/
    ``detect-changes`` 与 MCP 图查询只从 ``<GITNEXUS_HOME>/registry.json`` 解析仓库，
    因此任何对完整 gitnexus result 的断言都必须控制该注册表，而不是读取开发机真实
    全局目录。
    """
    with tempfile.TemporaryDirectory(prefix="gitnexus-home-") as home:
        entries = (
            [{"name": root.name, "path": str(root), "storagePath": str(root / ".gitnexus")}]
            if registered else []
        )
        (Path(home) / "registry.json").write_text(json.dumps(entries), encoding="utf-8")
        with patch.dict(os.environ, {"GITNEXUS_HOME": home}, clear=False):
            yield Path(home)


def drive_initialize_flow(
    root: Path, context: dict, *, run_id: str = "flow", commit: bool = False,
    before_finish=None, finish_args: list[str] | None = None,
    gitnexus_via_step: bool = False, graphify_via_step: bool = False,
    expect_reconcile_error: bool = False,
) -> tuple[int, dict]:
    """Run start, gate, reconcile and finish, returning the exit code and final report.

    ``gitnexus_via_step`` 用控制器 ``analyze`` 产出的 result 代替手写的 gitnexus
    result，验证新架构下整条链路（gate/reconcile/finish）仍然成立。

    ``graphify_via_step`` 同理，用 ``controller.py graphify`` 的真实产物代替手写的
    graphify result：这一步必须排在 analyze 之后，因此两个开关同时为真时，本函数就是
    「两次工具执行都落在主会话」的完整流程。

    ``expect_reconcile_error`` 在 gate 之后停在 reconcile，返回可恢复错误载荷，
    用于断言契约错误发生在写入任何托管文件之前、锁仍然持有。
    """
    git_repository(root)
    for name, body in {
        "README.md": "fixture\n",
        "AGENTS.md": "user agents\n",
        "CLAUDE.md": "user claude\n",
        "check_ok.py": "raise SystemExit(0)\n",
        "check_bad.py": "raise SystemExit(3)\n",
    }.items():
        (root / name).write_text(body, encoding="utf-8")
    fake = write_fake_gitnexus(root, status="current") if gitnexus_via_step else None
    graphify_fake = write_fake_graphify(root) if graphify_via_step else None
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    # 门禁的注册校验走 <GITNEXUS_HOME>/registry.json，所以整条流程都要在该临时全局
    # 目录下运行，并把 fixture 仓库登记进去。
    with tempfile.TemporaryDirectory(prefix="gitnexus-home-") as gitnexus_home:
        (Path(gitnexus_home) / "registry.json").write_text(json.dumps([
            {"name": root.name, "path": str(root), "storagePath": str(root / ".gitnexus")},
        ]), encoding="utf-8")
        with patch.dict(os.environ, {"GITNEXUS_HOME": gitnexus_home}, clear=False):
            with patch.object(
                prepare, "probe_graphify_ast_process_pool", return_value=True
            ):
                return _drive_initialize_flow(
                    root, context, run_id=run_id, commit=commit, before_finish=before_finish,
                    finish_args=finish_args, fake=fake, graphify_fake=graphify_fake,
                    expect_reconcile_error=expect_reconcile_error,
                )


def _drive_initialize_flow(
    root: Path, context: dict, *, run_id: str, commit: bool, before_finish,
    finish_args: list[str] | None, fake: Path | None, graphify_fake: Path | None,
    expect_reconcile_error: bool,
) -> tuple[int, dict]:
    start = ["start", "--root", str(root), "--language", "python", "--run-id", run_id]
    if commit:
        start.append("--commit")
    runner_env = {"CODEKIT_GITNEXUS_RUNNER": f"{sys.executable} {fake}"} if fake else {}
    if graphify_fake is not None:
        runner_env["CODEKIT_GRAPHIFY_RUNNER"] = str(graphify_fake)
    with patch.dict(os.environ, runner_env, clear=False):
        with redirect_stdout(StringIO()):
            assert controller.main(start) == 0
        manifest = root / ".codekit" / "initialize" / run_id / "run-manifest.json"

        (root / ".gitnexus").mkdir()
        (root / ".gitnexus/meta.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
        if graphify_fake is None:
            (root / "graphify-out").mkdir()
            (root / "graphify-out/graph.json").write_text(
                json.dumps({"nodes": [{"id": "n"}]}), encoding="utf-8",
            )
            (root / "graphify-out/manifest.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
            (root / "graphify-out/.graphify_root").write_text(str(root), encoding="utf-8")

        result_paths: list[Path] = []
        if fake is not None:
            with redirect_stdout(StringIO()):
                assert controller.main(["analyze", "--manifest", str(manifest)]) == 0
            result_paths.append(manifest.parent / "gitnexus-result.json")
        else:
            path = manifest.parent / "gitnexus.json"
            path.write_text(json.dumps(worker_result(
                "gitnexus", run_id=run_id,
                changed=[".gitnexus/meta.json"], allowed=[".gitnexus/"],
                artifacts=[{"path": ".gitnexus/meta.json", "validation": "json-valid"}],
            )), encoding="utf-8")
            result_paths.append(path)
        if graphify_fake is not None:
            with redirect_stdout(StringIO()):
                assert controller.main(["graphify", "--manifest", str(manifest)]) == 0
            result_paths.append(manifest.parent / "graphify-result.json")
        else:
            path = manifest.parent / "graphify.json"
            path.write_text(json.dumps(worker_result(
                "graphify", run_id=run_id,
                changed=["graphify-out/graph.json", "graphify-out/manifest.json", "graphify-out/.graphify_root"],
                allowed=["graphify-out/"],
                artifacts=[
                    {"path": "graphify-out/graph.json", "validation": "json-nonempty-nodes"},
                    {"path": "graphify-out/manifest.json", "validation": "json-valid"},
                    {"path": "graphify-out/.graphify_root", "validation": "root-match"},
                ],
            )), encoding="utf-8")
            result_paths.append(path)
        path = manifest.parent / "context-scanner.json"
        path.write_text(json.dumps(worker_result("context-scanner", run_id=run_id)), encoding="utf-8")
        result_paths.append(path)
        context_path = manifest.parent / "context.json"
        context_path.write_text(json.dumps(context), encoding="utf-8")

    argv = ["--manifest", str(manifest)] + list(finish_args or [])
    if before_finish is not None:
        argv += list(before_finish(manifest.parent))
    reconcile_argv = [
        "reconcile", "--manifest", str(manifest), "--context", str(context_path),
        *[part for path in result_paths for part in ("--worker-result", str(path))],
    ]
    with redirect_stdout(StringIO()):
        assert controller.main([
            "gate", "--manifest", str(manifest),
            *[part for path in result_paths for part in ("--result", str(path))],
        ]) == 0
        if expect_reconcile_error:
            with redirect_stderr(StringIO()) as context_error:
                code = controller.main(reconcile_argv)
            return code, json.loads(context_error.getvalue())
        assert controller.main(reconcile_argv) == 0
        output = StringIO()
        with redirect_stdout(output):
            code = controller.main(["finish", *argv])
    return code, json.loads(output.getvalue())


class ManagedBlockTests(unittest.TestCase):
    def test_preserves_user_content_and_is_idempotent(self):
        original = "user-rule\n"
        once = prepare.replace_managed_block(original, "# start", "# end", ["generated/"])
        twice = prepare.replace_managed_block(once, "# start", "# end", ["generated/"])
        self.assertEqual(once, twice)
        self.assertTrue(once.startswith("user-rule\n"))

    def test_rejects_broken_markers(self):
        with self.assertRaises(prepare.PrepareError):
            prepare.replace_managed_block("# start\n", "# start", "# end", [])

    def test_rejects_reversed_markers_in_both_callers(self):
        with self.assertRaises(prepare.PrepareError):
            prepare.replace_managed_block("# end\nbody\n# start\n", "# start", "# end", [])
        with self.assertRaises(reconcile.ReconcileError):
            reconcile.replace_block("# end\nbody\n# start\n", "# start", "# end", "body")

    def test_shared_primitives_and_contract_tables_are_singletons(self):
        self.assertIs(prepare.PrepareError, reconcile.ReconcileError)
        self.assertIs(reconcile.ReconcileError, validator.ValidationError)
        self.assertIs(prepare.replace_managed_block, reconcile.replace_block)
        self.assertIs(prepare.atomic_write, reconcile.atomic_write)
        self.assertIs(reconcile.load_json, validator.load_json)
        self.assertIs(validator.atomic_json, common.atomic_json)
        self.assertIs(prepare.snapshot_worker_scopes, validator.snapshot_worker_scopes)
        self.assertIs(reconcile.EXPECTED_TASKS, validator.EXPECTED_TASKS)
        self.assertIs(common.EXPECTED_TASKS, validator.EXPECTED_TASKS)
        self.assertIs(reconcile.resolve_validation_command, validator.resolve_validation_command)
        self.assertIs(reconcile.resolve_validation_command, common.resolve_validation_command)
        self.assertIs(validator.ValidationProgramMissing, common.ValidationProgramMissing)
        self.assertIs(sys.modules["prepare.py"], controller.prepare)
        self.assertIs(sys.modules["reconcile.py"], controller.reconcile)
        self.assertIs(sys.modules["validate_result.py"], controller.validator)

    def test_gitignore_managed_block_requires_graphifyignore_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".gitignore"
            path.write_text(
                "user-rule\n"
                "# codekit:initialize:ignore:start\n"
                ".codekit/\n"
                ".gitnexus/\n"
                "graphify-out/\n"
                "# codekit:initialize:ignore:end\n",
                encoding="utf-8",
            )
            errors = validator.validate_managed_gitignore(path)
        self.assertIn(".graphifyignore", " ".join(errors))

    def test_graphifyignore_managed_rules_preserve_user_content_and_are_idempotent(self):
        original = "user-graph-rule\n"
        rules = [
            ".codekit/", ".standard/", "AGENTS.md", "CLAUDE.md",
            ".gitnexus/", ".claude/", ".agents/", "graphify-out/",
        ]
        once = prepare.replace_managed_block(
            original, common.GRAPHIFY_START, common.GRAPHIFY_END, rules
        )
        twice = prepare.replace_managed_block(
            once, common.GRAPHIFY_START, common.GRAPHIFY_END, rules
        )
        self.assertEqual(once, twice)
        self.assertTrue(once.startswith(original))
        for rule in rules:
            self.assertIn(f"\n{rule}\n", once)

    def test_managed_gitignore_requires_the_frozen_gitnexus_mirror_globs(self):
        """The block is frozen: mirrors are ignored by glob, user skills are not."""
        self.assertIn(".claude/skills/gitnexus*/", common.MANAGED_GITIGNORE_ENTRIES)
        self.assertIn(".agents/skills/gitnexus*/", common.MANAGED_GITIGNORE_ENTRIES)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".gitignore"
            path.write_text(
                "user-rule\n" + prepare.replace_managed_block(
                    "", common.IGNORE_START, common.IGNORE_END,
                    list(common.MANAGED_GITIGNORE_ENTRIES),
                ),
                encoding="utf-8",
            )
            self.assertEqual([], validator.validate_managed_gitignore(path))
            path.write_text(
                path.read_text(encoding="utf-8").replace(".claude/skills/gitnexus*/\n", ""),
                encoding="utf-8",
            )
            errors = validator.validate_managed_gitignore(path)
        self.assertIn(".claude/skills/gitnexus*/", " ".join(errors))

    def test_language_validation_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(prepare.PrepareError):
                prepare.parse_languages("python,python", Path(directory))


class GraphifyCapabilityTests(unittest.TestCase):
    def test_graphify_capabilities_detect_flags_and_ast_process_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = (
                "import sys\n"
                "if sys.argv[1:] == ['--version']:\n"
                "    print('graphify 1.0')\n"
                "elif sys.argv[1:] == ['--help']:\n"
                "    print('extract --max-workers N')\n"
                "    print('hook install            install post-commit git hooks')\n"
                "elif sys.argv[1:] == ['extract', '--help']:\n"
                "    print('--code-only --max-workers')\n"
            )
            if os.name == "nt":
                # CreateProcess cannot exec an extension-less POSIX script.
                (root / "graphify.py").write_text(body, encoding="utf-8")
                runner = root / "graphify.cmd"
                runner.write_text(
                    f'@echo off\r\n"{sys.executable}" "%~dp0graphify.py" %*\r\n', encoding="utf-8"
                )
            else:
                runner = root / "graphify"
                runner.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
                runner.chmod(0o755)
            with patch.object(
                prepare.shutil,
                "which",
                side_effect=lambda name: str(runner) if name == "graphify" else None,
            ), patch.object(prepare, "probe_graphify_ast_process_pool", return_value=True):
                capabilities = prepare.tool_capabilities(root)

            graphify = capabilities["graphify"]
            self.assertTrue(graphify["supports"]["max_workers"])
            self.assertTrue(graphify["supports"]["code_only"])
            self.assertTrue(graphify["supports"]["ast_process_pool"])
            self.assertTrue(graphify["supports"]["git_hooks"])

    def test_shebang_probe_resolves_the_venv_python_behind_a_cmd_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "venvs" / "graphify" / "Scripts"
            scripts.mkdir(parents=True)
            (scripts / "python.exe").write_text("", encoding="utf-8")
            launcher = root / "graphify.cmd"
            launcher.write_text(
                "@echo off\r\n"
                f'"{scripts / "graphify.exe"}" %*\r\n'
                "exit /b %ERRORLEVEL%\r\n",
                encoding="utf-8",
            )
            self.assertEqual([str(scripts / "python.exe")], prepare._shebang_python(launcher))

    def test_shebang_probe_keeps_windows_backslashes(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "graphify"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            self.assertEqual([sys.executable], prepare._shebang_python(runner))

    def test_ast_process_pool_probe_uses_graphify_shebang_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "graphify"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(prepare.subprocess, "run", return_value=completed) as run_mock:
                self.assertTrue(prepare.probe_graphify_ast_process_pool(runner, root))

            command = run_mock.call_args.args[0]
            self.assertEqual(sys.executable, command[0])
            self.assertTrue(command[1].endswith("ast_process_pool_probe.py"))
            self.assertEqual(3, run_mock.call_args.kwargs["timeout"])

    def test_ast_process_pool_probe_fails_safe_on_eperm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "graphify"
            runner.write_text(f"#!{sys.executable}\n", encoding="utf-8")
            with patch.object(
                prepare.subprocess,
                "run",
                side_effect=OSError(errno.EPERM, "operation not permitted"),
            ):
                self.assertFalse(prepare.probe_graphify_ast_process_pool(runner, root))

    def test_prepare_gitnexus_skill_dirs_creates_both_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare.prepare_gitnexus_skill_dirs(root)
            self.assertEqual([".claude/skills", ".agents/skills"], prepared)
            self.assertTrue((root / ".claude/skills").is_dir())
            self.assertTrue((root / ".agents/skills").is_dir())
            self.assertEqual([], prepare.prepare_gitnexus_skill_dirs(root))

    def test_gitnexus_skill_mode_falls_back_on_read_only_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                prepare, "prepare_gitnexus_skill_dirs",
                side_effect=OSError(errno.EROFS, "read-only file system"),
            ):
                mode, warnings = prepare.gitnexus_skill_mode(root)
            self.assertEqual("community-only", mode)
            self.assertEqual(1, len(warnings))


class GraphifyGitHookTests(unittest.TestCase):
    """图谱钩子默认开：只在 initialize 期间刷新的图会在下一次提交后腐烂。"""

    def manifest(
        self,
        *,
        options=None,
        status="success",
        available=True,
        supports=None,
        runner="/usr/bin/graphify",
    ):
        merged_options = {"graph_git_hooks": True}
        merged_options.update(options or {})
        merged_supports = {"git_hooks": True}
        merged_supports.update(supports or {})
        return {
            "options": merged_options,
            "worker_results": {"graphify": {"status": status}},
            "tools": {
                "graphify": {
                    "available": available,
                    "runner": runner,
                    "supports": merged_supports,
                }
            },
        }

    def test_plan_installs_by_default_including_legacy_manifests(self):
        action, detail = validator.graphify_git_hook_plan(self.manifest())
        self.assertEqual(("install", "/usr/bin/graphify"), (action, detail))
        # 早期 run manifest 里没有这一位时按默认开启处理，不得静默跳过。
        legacy = self.manifest()
        legacy["options"] = {}
        self.assertEqual("install", validator.graphify_git_hook_plan(legacy)[0])

    def test_plan_skip_reasons(self):
        missing_receipt = self.manifest()
        missing_receipt.pop("worker_results")
        cases = (
            (self.manifest(options={"graph_git_hooks": False}), "disabled-by-flag"),
            (self.manifest(status="skipped"), "graphify-skipped"),
            (self.manifest(status="failed"), "graphify-failed"),
            (missing_receipt, "graphify-missing"),
            (self.manifest(available=False), "tool-missing"),
            (self.manifest(runner=None), "tool-missing"),
            (self.manifest(supports={"git_hooks": False}), "unsupported"),
        )
        for manifest, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    ("skip", reason), validator.graphify_git_hook_plan(manifest)
                )

    def test_partial_graph_still_gets_the_refresh_hook(self):
        self.assertEqual(
            "install", validator.graphify_git_hook_plan(self.manifest(status="partial"))[0]
        )

    def completed(self, returncode: int, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    def test_install_invokes_the_runner_in_this_repository(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            runner = str(root / "graphify")

            def fake_run(argv, **kwargs):
                # 钩子按仓库落盘：argv 必须是 <runner> hook install，cwd 必须是本仓库。
                self.assertEqual([runner, "hook", "install"], list(argv))
                self.assertEqual(root, Path(kwargs["cwd"]))
                (root / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\n", encoding="utf-8")
                return self.completed(0, "post-commit: installed")

            with patch.object(validator.subprocess, "run", side_effect=fake_run) as execute:
                record, warnings, degraded = validator.install_graphify_git_hooks(
                    root, self.manifest(runner=runner), 30
                )
            self.assertEqual(1, execute.call_count)
            self.assertEqual("installed", record["status"])
            self.assertEqual(0, record["exit_code"])
            self.assertIn("post-commit: installed", record["output_tail"])
            self.assertTrue((root / ".git" / "hooks" / "post-commit").is_file())
            self.assertEqual(([], False), (warnings, degraded))

    def test_failure_degrades_the_run_and_names_the_rollback(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            with patch.object(
                validator.subprocess, "run",
                return_value=self.completed(1, "", "No git repository found"),
            ):
                record, warnings, degraded = validator.install_graphify_git_hooks(
                    root, self.manifest(runner=str(root / "graphify")), 30
                )
            self.assertEqual("failed", record["status"])
            self.assertEqual(1, record["exit_code"])
            self.assertIn("graphify hook uninstall", record["rollback"])
            self.assertTrue(degraded)
            self.assertEqual(1, len(warnings))
            self.assertIn("graphify hook install", warnings[0])
            self.assertIn("--no-git-hooks", warnings[0])

    def test_unlaunchable_runner_degrades_instead_of_raising(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            with patch.object(
                validator.subprocess, "run", side_effect=OSError("no such file")
            ):
                record, warnings, degraded = validator.install_graphify_git_hooks(
                    root, self.manifest(runner=str(root / "graphify")), 30
                )
            self.assertEqual("failed", record["status"])
            self.assertIn("error", record)
            self.assertTrue(degraded)
            self.assertEqual(1, len(warnings))

    def test_skipped_step_never_touches_the_repository(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            hooks = root / ".git" / "hooks"
            before = sorted(path.name for path in hooks.iterdir())
            record, warnings, degraded = validator.install_graphify_git_hooks(
                root, self.manifest(options={"graph_git_hooks": False}), 30
            )
            self.assertEqual("skipped", record["status"])
            self.assertEqual("disabled-by-flag", record["reason"])
            self.assertEqual(([], False), (warnings, degraded))
            self.assertEqual(before, sorted(path.name for path in hooks.iterdir()))

    def test_unsupported_cli_warns_without_degrading(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            record, warnings, degraded = validator.install_graphify_git_hooks(
                root, self.manifest(supports={"git_hooks": False}), 30
            )
            self.assertEqual("skipped", record["status"])
            self.assertEqual("unsupported", record["reason"])
            self.assertFalse(degraded)
            self.assertEqual(1, len(warnings))

    def test_controller_forwards_the_flag_to_prepare(self):
        with patch.object(controller.prepare, "main", return_value=0) as main:
            controller.main(["start", "--root", "."])
        self.assertNotIn("--no-git-hooks", main.call_args.args[0])
        with patch.object(controller.prepare, "main", return_value=0) as main:
            controller.main(["start", "--root", ".", "--no-git-hooks"])
        self.assertIn("--no-git-hooks", main.call_args.args[0])


class GitNexusRunnerResolutionTests(unittest.TestCase):
    """runner 解析与模式判定：决定索引是否会每次全量重建。"""

    def test_runner_resolution_never_falls_back_to_repo_shim(self):
        # .gitnexus/run.cjs 按 gitnexus@latest 走 npx，会让分析器身份漂移。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/run.cjs").write_text("// shim\n", encoding="utf-8")
            # 候选目录与清单都清空，让用例与宿主机的真实安装无关。
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[]
            ), no_ambient_manifest(), patch.dict(
                os.environ, {"CODEKIT_GITNEXUS_RUNNER": ""}, clear=False
            ):
                command, runner = prepare.gitnexus_command()
            self.assertEqual([], command)
            self.assertEqual("unresolved", runner)

    def test_declared_install_directory_supplies_the_runner(self):
        # 受限宿主的沙箱按路径拒绝访问，安装器落在工具链根里的入口按名字找不到，
        # 但按绝对路径可读可执行，必须解析成功。
        with tempfile.TemporaryDirectory() as directory:
            declared = Path(directory)
            target = declared / common.executable_names("gitnexus")[0]
            target.write_text("", encoding="utf-8")
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[declared]
            ), no_ambient_manifest(), patch.dict(
                os.environ, {"CODEKIT_GITNEXUS_RUNNER": ""}, clear=False
            ):
                command, runner = prepare.gitnexus_command()
            self.assertEqual([str(target)], command)
            self.assertEqual("declared", runner)

    def test_manifest_runner_outranks_a_path_hit(self):
        # 清单优先于 PATH 是刻意的：即使会话里恰好有个能用的旧 shim，也按安装器写出的
        # 绝对 argv 执行，结果才跨宿主确定。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node = root / "node.exe"
            node.write_text("", encoding="utf-8")
            cli = root / "cli.js"
            cli.write_text("", encoding="utf-8")
            with patch.object(prepare.shutil, "which", return_value="/usr/bin/gitnexus"), patch.object(
                common.runtime, "read_manifest",
                return_value={"schema_version": 1, "tools": {"gitnexus": {"state": "ok", "runner": [str(node), str(cli)]}}},
            ):
                command, runner = prepare.gitnexus_command()
            self.assertEqual("toolchain", runner)
            self.assertEqual([str(node), str(cli)], command)

    def test_path_hit_still_wins_over_the_declared_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            on_path = Path(directory) / "bin"
            on_path.mkdir()
            hit = on_path / "gitnexus"
            hit.write_text("", encoding="utf-8")
            declared = Path(directory) / "declared"
            declared.mkdir()
            (declared / "gitnexus").write_text("", encoding="utf-8")
            with patch.object(
                prepare.shutil, "which", return_value=str(hit)
            ), patch.object(common.runtime, "toolchain_paths", return_value=[declared]), no_ambient_manifest():
                command, runner = prepare.gitnexus_command()
            self.assertEqual([str(hit)], command)
            self.assertEqual("path", runner)

    def test_cmd_shim_is_folded_into_the_real_cli_entry(self):
        # npm 的 gitnexus.cmd 只有一句 node "%dp0%\node_modules\...\index.js"：
        # 不折叠成真实入口就会误判「分析器身份不稳定 + 缺 embedding 栈」。
        with tempfile.TemporaryDirectory() as directory:
            npm = Path(directory)
            package = npm / "node_modules" / "gitnexus"
            cli = package / "dist" / "cli" / "index.js"
            cli.parent.mkdir(parents=True)
            cli.write_text("// cli\n", encoding="utf-8")
            (package / "package.json").write_text('{"name": "gitnexus"}', encoding="utf-8")
            (package / "node_modules" / "onnxruntime-node").mkdir(parents=True)
            (package / "node_modules" / "@huggingface" / "transformers").mkdir(parents=True)
            shim = npm / "gitnexus.cmd"
            shim.write_text(
                "@ECHO off\r\n"
                'node "%dp0%\\node_modules\\gitnexus\\dist\\cli\\index.js" %*\r\n',
                encoding="utf-8",
            )
            resolved = prepare.gitnexus_cli_path([str(shim)])
            self.assertEqual(cli.resolve(), resolved)
            self.assertIsNotNone(prepare.gitnexus_install_root(resolved))
            self.assertTrue(prepare.gitnexus_local_embeddings(resolved))

    def test_shim_without_a_resolvable_entry_keeps_its_own_path(self):
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "gitnexus.cmd"
            shim.write_text("@echo off\r\nnode missing.js %*\r\n", encoding="utf-8")
            self.assertEqual(shim.resolve(), prepare.gitnexus_cli_path([str(shim)]))

    def test_explicit_runner_override_wins_over_path(self):
        with patch.object(
            prepare.shutil, "which", return_value="/usr/bin/gitnexus"
        ), patch.dict(
            os.environ, {"CODEKIT_GITNEXUS_RUNNER": "node /opt/gn/run.cjs"}, clear=False
        ):
            command, runner = prepare.gitnexus_command()
        self.assertEqual(["node", "/opt/gn/run.cjs"], command)
        self.assertEqual("override", runner)

    def test_local_embedding_stack_is_detected_from_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            pkg = Path(directory) / "node_modules" / "gitnexus"
            cli = pkg / "dist" / "cli" / "index.js"
            cli.parent.mkdir(parents=True)
            (pkg / "package.json").write_text('{"name": "gitnexus"}', encoding="utf-8")
            self.assertFalse(prepare.gitnexus_local_embeddings(cli))
            (pkg / "node_modules" / "onnxruntime-node").mkdir(parents=True)
            (pkg / "node_modules" / "@huggingface" / "transformers").mkdir(parents=True)
            self.assertTrue(prepare.gitnexus_local_embeddings(cli))

    def test_index_modes_read_pdg_and_embeddings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                {"present": False, "pdg": None, "embeddings": 0, "branch": None},
                prepare.gitnexus_index_modes(root),
            )
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text(
                json.dumps({"pdg": {"x": 1}, "branch": "main", "stats": {"embeddings": 12}}),
                encoding="utf-8",
            )
            modes = prepare.gitnexus_index_modes(root)
            self.assertTrue(modes["pdg"])
            self.assertEqual(12, modes["embeddings"])
            self.assertEqual("main", modes["branch"])

    def test_embedding_plan_degrades_instead_of_failing(self):
        index = {"present": True, "pdg": True, "embeddings": 1341, "branch": "main"}
        mode, warnings = prepare.gitnexus_embedding_plan(index, False, False, False)
        self.assertEqual("drop", mode)
        self.assertTrue(warnings)
        self.assertEqual(
            ("preserve", []), prepare.gitnexus_embedding_plan(index, True, False, False)
        )
        mode, warnings = prepare.gitnexus_embedding_plan(index, False, False, True)
        self.assertEqual("install-authorized", mode)
        self.assertTrue(warnings)
        self.assertEqual(
            ("preserve", []),
            prepare.gitnexus_embedding_plan({"embeddings": 0}, False, False, False),
        )

    def test_requested_embeddings_require_authorized_install(self):
        with self.assertRaises(prepare.PrepareError):
            prepare.gitnexus_embedding_plan({"embeddings": 0}, False, True, False)
        mode, _ = prepare.gitnexus_embedding_plan({"embeddings": 0}, False, True, True)
        self.assertEqual("install-authorized", mode)

    def test_authorized_run_writes_the_real_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "gitnexus-home"
            home.mkdir()
            fake = root / "fake-gitnexus.py"
            fake.write_text(
                "import os\n"
                "from pathlib import Path\n"
                "path = Path(os.environ['GITNEXUS_HOME']) / 'registry.json'\n"
                "path.write_text('{}', encoding='utf-8')\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts/run-gitnexus.py"),
                 "--root", str(root), "--use-global-registry", "--",
                 sys.executable, str(fake)],
                text=True, capture_output=True, check=False,
                env={**os.environ, "GITNEXUS_HOME": str(home)},
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue((home / "registry.json").is_file())
            self.assertNotIn("disposable GITNEXUS_HOME", completed.stderr)


class GitNexusRegistrationTests(unittest.TestCase):
    """GitNexus 只在全局 registry 里解析仓库：缺注册时索引有效但查询全废。"""

    def fixture(self, root: Path) -> Path:
        git_repository(root)
        return write_fake_gitnexus(root, status="current")

    def run_prepare(self, root: Path, fake: Path, *extra: str):
        # runner 覆盖串要带引号：shlex.split 会吃掉未加引号的 Windows 路径分隔符。
        with patch.dict(
            os.environ,
            {"CODEKIT_GITNEXUS_RUNNER": f'"{sys.executable}" "{fake}"'},
            clear=False,
        ):
            with redirect_stdout(StringIO()):
                return prepare.main([
                    "--root", str(root), "--language", "python", "--run-id", "registry",
                    *extra,
                ])

    def manifest(self, root: Path) -> dict:
        return json.loads(
            (root / ".codekit" / "initialize" / "registry" / "run-manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def test_unregistered_repository_requires_a_global_write_decision(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            fake = self.fixture(root)
            with isolated_gitnexus_home(root, registered=False):
                with self.assertRaises(prepare.PrepareError) as caught:
                    self.run_prepare(root, fake)
            self.assertIn("registry.json", str(caught.exception))
            # 判定与 embedding 前提一样发生在任何写入之前。
            self.assertFalse((root / ".codekit").exists())

    def test_allow_partial_accepts_the_degraded_registration(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            fake = self.fixture(root)
            with isolated_gitnexus_home(root, registered=False):
                self.assertEqual(0, self.run_prepare(root, fake, "--allow-partial"))
                manifest = self.manifest(root)
            self.assertFalse(manifest["gitnexus_registry"]["registered"])
            self.assertTrue(
                any("registry-missing" in warning for warning in manifest["warnings"])
            )

    def test_allow_global_writes_authorizes_registration(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            fake = self.fixture(root)
            with isolated_gitnexus_home(root, registered=False):
                self.assertEqual(0, self.run_prepare(root, fake, "--allow-global-writes"))
                manifest = self.manifest(root)
            self.assertTrue(manifest["options"]["allow_global_writes"])
            self.assertEqual([], manifest["warnings"])

    def test_registered_repository_needs_no_decision(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            fake = self.fixture(root)
            with isolated_gitnexus_home(root):
                self.assertEqual(0, self.run_prepare(root, fake))
                manifest = self.manifest(root)
            self.assertTrue(manifest["gitnexus_registry"]["registered"])
            self.assertEqual([], manifest["warnings"])

    def test_unrunnable_runner_needs_no_registration_decision(self):
        """入口解析到了但跑不起来时，问用户要一次全局写入授权只是白问。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            broken = root / "broken-gitnexus.py"
            broken.write_text("raise SystemExit(3)\n", encoding="utf-8")
            with isolated_gitnexus_home(root, registered=False):
                self.assertEqual(0, self.run_prepare(root, broken))
                manifest = self.manifest(root)
            tool = manifest["tools"]["gitnexus"]
            self.assertFalse(tool["available"])
            self.assertFalse(manifest["gitnexus_registry"]["registered"])
            self.assertEqual([], manifest["warnings"])
            self.assertEqual("preserve", manifest["gitnexus_embedding_mode"])
            self.assertIn("跑不起来", tool["resolution_hint"])

    def test_complete_result_requires_registration(self):
        """门禁自己重测注册：只校验 .gitnexus/meta.json 会放过死索引。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text("{}", encoding="utf-8")
            manifest = {"run_id": "test-run", "tools": {}}
            result = worker_result(
                "gitnexus", allowed=[".gitnexus/"],
                artifacts=[{"path": ".gitnexus/meta.json", "validation": "json-valid"}],
            )
            with isolated_gitnexus_home(root, registered=False):
                errors = validator.validate_worker(result, manifest, root)
            self.assertIn("not registered", " ".join(errors))
            with isolated_gitnexus_home(root):
                errors = validator.validate_worker(result, manifest, root)
            self.assertEqual([], errors)


class GitNexusStepTests(unittest.TestCase):
    """controller.py analyze：gitnexus 由确定性步骤执行，产物必须能过 gate。"""

    def step_manifest(self, root: Path, tool: dict, **overrides) -> tuple[dict, Path]:
        run_dir = root / ".codekit" / "initialize" / "step-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "run-manifest.json"
        lock = run_dir / "initialize.lock"
        manifest = {
            "schema_version": 1,
            "run_id": "step-run",
            "root": str(root),
            "state_run_dir": str(run_dir),
            "manifest_path": str(path),
            "lock_path": str(lock),
            "phase": "prepared",
            "options": {
                "embeddings": False, "force": False, "allow_partial": False,
                "commit": False, "allow_global_writes": False,
            },
            "tools": {"gitnexus": tool},
            "gitnexus_index": {"present": False, "pdg": None, "embeddings": 0, "branch": None},
            "gitnexus_embedding_mode": "preserve",
            "gitnexus_skill_mode": "full",
            "baseline_status": {},
            "baseline_files": {},
            "worker_tree_roots": [".gitnexus", ".claude/skills", ".agents/skills"],
            "worker_scope_baseline": {},
        }
        manifest.update(overrides)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        # 控制器按锁校验运行身份，夹具必须提供同 run 的锁。
        lock.write_text(json.dumps({
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "root": str(root),
            "manifest": str(path),
            "created_at": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")
        return manifest, path

    def fake_runner(self, root: Path, *, status: str) -> Path:
        """最小 GitNexus 替身：status 报 status，analyze/index 落 meta 与全局注册。

        注册写到 ``GITNEXUS_HOME``：真实 CLI 也用同一处 registry 解析仓库，所以
        「授权 → 落在真实目录」「未授权 → 落进一次性目录后被删除」都能被复现。
        """
        fake = root / "fake-gitnexus.py"
        register = (
            "    home = os.environ.get('GITNEXUS_HOME')\n"
            "    if home:\n"
            "        registry = Path(home)\n"
            "        registry.mkdir(parents=True, exist_ok=True)\n"
            "        (registry / 'registry.json').write_text(\n"
            "            json.dumps([{'name': Path(os.getcwd()).name, 'path': os.getcwd()}]),\n"
            "            encoding='utf-8')\n"
        )
        fake.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['status']:\n"
            f"    print(json.dumps({{'status': {status!r}}}))\n"
            "    raise SystemExit(0)\n"
            "if args[:1] == ['analyze']:\n"
            "    target = Path(os.getcwd()) / '.gitnexus'\n"
            "    target.mkdir(exist_ok=True)\n"
            "    (target / 'meta.json').write_text(json.dumps({'root': os.getcwd()}), encoding='utf-8')\n"
            "    (Path(os.getcwd()) / 'analyze-called').write_text('1', encoding='utf-8')\n"
            + register +
            "    raise SystemExit(0)\n"
            "if args[:1] == ['index']:\n"
            "    (Path(os.getcwd()) / 'index-called').write_text('1', encoding='utf-8')\n"
            + register +
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        return fake

    def run_step(self, root: Path, manifest_path: Path) -> dict:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(0, controller.main(["analyze", "--manifest", str(manifest_path)]))
        return json.loads((manifest_path.parent / "gitnexus-result.json").read_text(encoding="utf-8"))

    def test_missing_runner_skips_without_falling_back(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            tool = {
                "available": False, "runner": "unresolved", "command": [],
                "supports": {}, "resolution_hint": "请把 npm 前缀/bin 加入 PATH",
            }
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(root, path)

            self.assertEqual("skipped", result["status"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual("tool-missing", result["error"]["code"])
            self.assertIn("请把 npm 前缀/bin 加入 PATH", result["warnings"])
            self.assertEqual([], validator.validate_worker(result, manifest, root))

    def test_unrunnable_runner_skips_as_tool_missing(self):
        """入口解析到了但探测失败：这是本机跑不起来，不是工具坏了。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            broken = root / "broken-gitnexus.py"
            broken.write_text("raise SystemExit(3)\n", encoding="utf-8")
            tool = {
                "available": False, "runner": "installed",
                "command": [sys.executable, str(broken)],
                "supports": {}, "resolution_hint": "请以已授权/提权（非沙箱）的会话重跑",
            }
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(root, path)

            self.assertEqual("skipped", result["status"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual("tool-missing", result["error"]["code"])
            self.assertIn("请以已授权/提权（非沙箱）的会话重跑", result["warnings"])
            self.assertEqual([], validator.validate_worker(result, manifest, root))
            # 没有真的去跑 status/analyze。
            self.assertFalse((root / "analyze-called").exists())

    def test_current_index_reuses_without_running_analyze(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root, status="current")
            # status 说 current 就意味着已有索引，artifact 必须存在。
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text("{}", encoding="utf-8")
            tool = {
                "available": True, "runner": "override", "command": [sys.executable, str(fake)],
                "supports": {"skills": False}, "identity_stable": True, "local_embeddings": False,
            }
            manifest, path = self.step_manifest(root, tool)
            with isolated_gitnexus_home(root):
                result = self.run_step(root, path)
                self.assertEqual([], validator.validate_worker(result, manifest, root))

            self.assertEqual("skipped", result["status"])
            self.assertEqual("index-current", result["error"]["code"])
            self.assertEqual(
                [{"path": ".gitnexus/meta.json", "validation": "json-valid"}], result["artifacts"]
            )
            self.assertTrue(result["registry"]["registered"])
            # 复用索引就必须真的不跑 analyze，也已注册时不必补注册。
            self.assertFalse((root / "analyze-called").exists())
            self.assertFalse((root / "index-called").exists())

    def test_unregistered_reuse_registers_the_index_when_authorized(self):
        """复用路径最容易漏注册：analyze 不跑，注册必须在 analyze 步骤里补。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root, status="current")
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text("{}", encoding="utf-8")
            tool = {
                "available": True, "runner": "override", "command": [sys.executable, str(fake)],
                "supports": {"skills": False}, "identity_stable": True, "local_embeddings": False,
            }
            manifest, path = self.step_manifest(root, tool, options={
                "embeddings": False, "force": False, "allow_partial": False,
                "commit": False, "allow_global_writes": True,
            })
            with isolated_gitnexus_home(root, registered=False):
                result = self.run_step(root, path)
                self.assertEqual([], validator.validate_worker(result, manifest, root))
                self.assertTrue(common.gitnexus_registry_state(root)["registered"])

            self.assertEqual("skipped", result["status"])
            self.assertEqual("index-current", result["error"]["code"])
            self.assertEqual("1", (root / "index-called").read_text(encoding="utf-8"))
            self.assertFalse((root / "analyze-called").exists())
            self.assertTrue(any("补注册" in warning for warning in result["warnings"]))

    def test_unregistered_reuse_without_authorization_is_partial(self):
        """未授权时不写全局注册表，但绝不能把「查不到的索引」记成 success。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root, status="current")
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text("{}", encoding="utf-8")
            tool = {
                "available": True, "runner": "override", "command": [sys.executable, str(fake)],
                "supports": {"skills": False}, "identity_stable": True, "local_embeddings": False,
            }
            manifest, path = self.step_manifest(root, tool)
            with isolated_gitnexus_home(root, registered=False):
                result = self.run_step(root, path)
                self.assertEqual([], validator.validate_worker(result, manifest, root))

            self.assertEqual("partial", result["status"])
            self.assertEqual("registry-missing", result["error"]["code"])
            self.assertEqual(0, result["exit_code"])
            self.assertFalse(result["registry"]["registered"])
            self.assertEqual("partial", validator.aggregate_status([result]))
            self.assertFalse((root / "index-called").exists())
            self.assertTrue(any("No indexed repositories" in warning for warning in result["warnings"]))

    def test_authorized_registration_that_does_not_land_fails(self):
        """已授权却仍没注册时必须失败：否则等于把「索引有效但查询全废」记成成功。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = root / "fake-gitnexus.py"
            fake.write_text(
                "import json, sys\n"
                "if sys.argv[1:2] == ['status']:\n"
                "    print(json.dumps({'status': 'current'}))\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(3)\n",
                encoding="utf-8",
            )
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text("{}", encoding="utf-8")
            tool = {
                "available": True, "runner": "override", "command": [sys.executable, str(fake)],
                "supports": {"skills": False}, "identity_stable": True, "local_embeddings": False,
            }
            manifest, path = self.step_manifest(root, tool, options={
                "embeddings": False, "force": False, "allow_partial": False,
                "commit": False, "allow_global_writes": True,
            })
            with isolated_gitnexus_home(root, registered=False):
                result = self.run_step(root, path)

            self.assertEqual("failed", result["status"])
            self.assertEqual(3, result["exit_code"])
            self.assertEqual("execution-failed", result["error"]["code"])
            self.assertFalse(result["registry"]["registered"])
            self.assertEqual("failed", validator.aggregate_status([result]))

    def test_analyze_declares_writes_and_passes_the_gate(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root, status="stale")
            tool = {
                "available": True, "runner": "override", "command": [sys.executable, str(fake)],
                "supports": {"skills": False}, "identity_stable": True, "local_embeddings": False,
            }
            manifest, path = self.step_manifest(root, tool, options={
                "embeddings": False, "force": False, "allow_partial": False,
                "commit": False, "allow_global_writes": True,
            })
            with isolated_gitnexus_home(root, registered=False):
                result = self.run_step(root, path)
                self.assertTrue(common.gitnexus_registry_state(root)["registered"])
                self.assertEqual([], validator.validate_worker(result, manifest, root))

            self.assertEqual("success", result["status"])
            self.assertEqual(0, result["exit_code"])
            self.assertEqual(
                [{"path": ".gitnexus/meta.json", "validation": "json-valid"}], result["artifacts"]
            )
            self.assertTrue(result["changed_files"])
            for changed in result["changed_files"]:
                self.assertTrue(
                    validator.path_allowed(changed, result["allowed_paths"]), changed
                )
            # 步骤不得自行推进阶段：推进由 gate 负责。
            self.assertEqual("prepared", json.loads(path.read_text(encoding="utf-8"))["phase"])

    def test_analyze_argv_mirrors_the_recorded_index_modes(self):
        manifest = {
            "tools": {"gitnexus": {"command": ["gitnexus"], "supports": {"skip_skills": True}}},
            "gitnexus_index": {"present": True, "pdg": True, "embeddings": 5, "branch": "main"},
            "options": {"embeddings": True, "force": True, "allow_global_writes": False},
            "gitnexus_skill_mode": "community-only",
        }
        argv, warnings = gitnexus_step.analyze_argv(manifest, "preserve")
        self.assertEqual(["gitnexus", "analyze", "--skills", "--skip-agents-md", "--verbose"], argv[:5])
        self.assertIn("--pdg", argv)
        self.assertIn("--embeddings", argv)
        self.assertIn("--force", argv)
        self.assertIn("--skip-skills", argv)
        self.assertTrue(warnings)

        fresh = {"tools": {"gitnexus": {"command": ["gitnexus"], "supports": {}}},
                 "gitnexus_index": {"present": False, "pdg": None},
                 "options": {}, "gitnexus_skill_mode": "full"}
        argv, warnings = gitnexus_step.analyze_argv(fresh, "preserve")
        # 新索引不得主动引入 --pdg/--embeddings，否则会改变默认模式。
        for flag in ("--pdg", "--embeddings", "--force", "--drop-embeddings", "--skip-skills"):
            self.assertNotIn(flag, argv)
        self.assertEqual([], warnings)

    def test_drop_embeddings_mode_is_declared(self):
        manifest = {
            "tools": {"gitnexus": {"command": ["gitnexus"], "supports": {}}},
            "gitnexus_index": {"present": True, "pdg": False, "embeddings": 9},
            "options": {}, "gitnexus_skill_mode": "full",
        }
        argv, warnings = gitnexus_step.analyze_argv(manifest, "drop")
        self.assertIn("--drop-embeddings", argv)
        self.assertTrue(any("drop-embeddings" in warning for warning in warnings))


class GraphifyStepTests(unittest.TestCase):
    """controller.py graphify：Graphify 由确定性步骤执行，产物必须能过 gate。"""

    def fake_runner(self, root: Path) -> Path:
        return write_fake_graphify(root)

    def step_manifest(self, root: Path, tool: dict, **overrides) -> tuple[dict, Path]:
        run_dir = root / ".codekit" / "initialize" / "gf-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "run-manifest.json"
        lock = run_dir / "initialize.lock"
        manifest = {
            "schema_version": 1,
            "run_id": "gf-run",
            "root": str(root),
            "state_run_dir": str(run_dir),
            "manifest_path": str(path),
            "lock_path": str(lock),
            "phase": "prepared",
            "options": {
                "embeddings": False, "force": False, "allow_partial": False,
                "commit": False, "allow_global_writes": False,
            },
            "tools": {"graphify": tool},
            "baseline_status": {},
            "baseline_files": {},
            "worker_tree_roots": ["graphify-out"],
            "worker_scope_baseline": {},
        }
        manifest.update(overrides)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        lock.write_text(json.dumps({
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "root": str(root),
            "manifest": str(path),
            "created_at": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")
        # 控制器要求 Graphify 排在 GitNexus 之后；本类的用例只关心 Graphify 这一步。
        (run_dir / "gitnexus-result.json").write_text(json.dumps({}), encoding="utf-8")
        return manifest, path

    def tool_snapshot(self, fake: Path, **supports) -> dict:
        flags = {
            "update": True, "cluster_only": True, "no_viz": True, "force": True,
            "code_only": True, "backend_selection": True, "max_workers": True,
            "git_hooks": True, "ast_process_pool": True,
        }
        flags.update(supports)
        return {
            "available": True,
            "runner": str(fake),
            "runner_type": "override",
            "command": [str(fake)],
            "version": ["graphify 9.9.9"],
            "supports": flags,
        }

    def run_step(self, manifest_path: Path, *, behavior: str = "ok") -> dict:
        with patch.dict(os.environ, {"FAKE_GRAPHIFY_BEHAVIOR": behavior}, clear=False):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(
                    0, controller.main(["graphify", "--manifest", str(manifest_path)])
                )
        return json.loads((manifest_path.parent / "graphify-result.json").read_text(encoding="utf-8"))

    def seed_valid_cache(self, root: Path, tool: dict) -> None:
        """按 tool-adapters 的四条判据铺一份可复用的缓存。"""
        (root / ".graphifyignore").write_text(
            common.replace_managed_block(
                "", common.GRAPHIFY_START, common.GRAPHIFY_END, ["graphify-out/"]
            ),
            encoding="utf-8",
        )
        out = root / "graphify-out"
        out.mkdir(exist_ok=True)
        (out / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "n"}]}), encoding="utf-8",
        )
        (out / "manifest.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
        (out / ".graphify_root").write_text(str(root), encoding="utf-8")
        (out / ".codekit-state.json").write_text(json.dumps({
            "schema_version": 1,
            "root": str(root),
            "graphify_version": " ".join(tool["version"]),
            "ignore_digest": common.graphify_ignore_digest(root),
            "mode": graphify_step.MODE,
            "last_status": "success",
        }), encoding="utf-8")

    def test_graphify_requires_analyze_first(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            manifest, path = self.step_manifest(root, self.tool_snapshot(fake))
            (path.parent / "gitnexus-result.json").unlink()

            with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as error:
                code = controller.main(["graphify", "--manifest", str(path)])

            payload = json.loads(error.getvalue())
            self.assertEqual(2, code)
            self.assertTrue(payload["recoverable"])
            self.assertIn("gitnexus-result.json", payload["message"])
            # 顺序写错不是真实失败：run 留在 prepared、锁仍持有，也不得已经跑过 runner。
            self.assertEqual("prepared", json.loads(path.read_text(encoding="utf-8"))["phase"])
            self.assertTrue((path.parent / "initialize.lock").exists())
            self.assertFalse((root / "invoked.json").exists())

    def test_unavailable_runner_skips_as_tool_missing(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            tool = {
                "available": False, "runner": "unresolved", "command": [],
                "supports": {}, "resolution_hint": "请重跑安装器并勾选 Graphify 组件",
            }
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path)

            self.assertEqual("skipped", result["status"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual("tool-missing", result["error"]["code"])
            self.assertIn("请重跑安装器并勾选 Graphify 组件", result["warnings"])
            self.assertEqual([], validator.validate_worker(result, manifest, root))

    def test_full_extract_produces_gate_valid_result(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake)
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path)

            self.assertEqual("success", result["status"])
            self.assertEqual(0, result["exit_code"])
            self.assertEqual(graphify_step.MODE, result["mode"])
            self.assertEqual("graphify 9.9.9", result["version"])
            # 路径参数必须是仓库根绝对路径：update 不接受 --out，root-match 也不能依赖 cwd。
            self.assertIn(str(root), result["command"])
            self.assertNotIn(".", result["command"])
            self.assertEqual(3, len(result["artifacts"]))
            self.assertTrue(
                all(item.startswith("graphify-out/") for item in result["changed_files"])
            )
            self.assertEqual([], validator.validate_worker(result, manifest, root))

    def test_valid_cache_reuses_with_update_subcommand(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake)
            manifest, path = self.step_manifest(root, tool)
            self.seed_valid_cache(root, tool)
            result = self.run_step(path)

            self.assertEqual("success", result["status"])
            self.assertEqual([str(fake), "update", str(root)], result["command"])
            self.assertNotIn("--code-only", result["command"])
            self.assertNotIn("--out", result["command"])

    def test_stale_sidecar_mode_forces_full_extract(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake)
            manifest, path = self.step_manifest(root, tool)
            self.seed_valid_cache(root, tool)
            sidecar = root / "graphify-out" / ".codekit-state.json"
            state = json.loads(sidecar.read_text(encoding="utf-8"))
            state["mode"] = "default"
            sidecar.write_text(json.dumps(state), encoding="utf-8")
            result = self.run_step(path)

            self.assertEqual("success", result["status"])
            self.assertIn("extract", result["command"])
            self.assertTrue(any("mode" in warning for warning in result["warnings"]))

    def test_update_without_increment_is_a_complete_skip(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake)
            manifest, path = self.step_manifest(root, tool)
            self.seed_valid_cache(root, tool)
            result = self.run_step(path, behavior="nothing-to-update")

            # graphify update 用退出码 1 表达「无增量」；它是完整复用，不是失败。
            self.assertEqual("skipped", result["status"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual("no-change", result["error"]["code"])
            self.assertEqual([], validator.validate_worker(result, manifest, root))
            self.assertNotIn("failed", validator.aggregate_status([result]))

    def test_real_failure_is_reported_with_exit_code(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake)
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path, behavior="crash")

            self.assertEqual("failed", result["status"])
            self.assertEqual(4, result["exit_code"])
            self.assertEqual("execution-failed", result["error"]["code"])
            self.assertIn("boom", result["error"]["message"])

    def test_zero_exit_without_artifacts_fails_the_step(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake)
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path, behavior="no-artifacts")

            self.assertEqual("failed", result["status"])
            self.assertIsNone(result["exit_code"])
            self.assertEqual("artifact-invalid", result["error"]["code"])
            self.assertIn("退出码 0", result["error"]["message"])

    def test_ast_pool_unsupported_caps_workers_through_env(self):
        """并行收敛走环境变量：--max-workers 只有 extract 接受，update 会 exit 2。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake, ast_process_pool=False)
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path)

            invoked = json.loads((root / "invoked.json").read_text(encoding="utf-8"))
            self.assertEqual("1", invoked["workers"])
            self.assertNotIn("--max-workers", result["command"])
            self.assertTrue(any("GRAPHIFY_MAX_WORKERS" in warning for warning in result["warnings"]))

    def test_no_pool_and_no_cap_skips_without_invoking(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake, ast_process_pool=False, max_workers=False)
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path)

            self.assertEqual("skipped", result["status"])
            self.assertEqual("tool-missing", result["error"]["code"])
            self.assertFalse((root / "invoked.json").exists())

    def test_missing_code_only_never_falls_back_to_semantic(self):
        """少了 --code-only 就退化成需要 API key 的语义抽取：宁可不跑。"""
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            fake = self.fake_runner(root)
            tool = self.tool_snapshot(fake, code_only=False)
            manifest, path = self.step_manifest(root, tool)
            result = self.run_step(path)

            self.assertEqual("skipped", result["status"])
            self.assertEqual("tool-missing", result["error"]["code"])
            self.assertFalse((root / "invoked.json").exists())


class WorkerContractTests(unittest.TestCase):
    def test_gitnexus_adapter_isolates_global_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fake-gitnexus.py"
            fake.write_text(
                "import os\n"
                "from pathlib import Path\n"
                "path = Path(os.environ['GITNEXUS_HOME']) / 'registry.json'\n"
                "path.parent.mkdir(parents=True, exist_ok=True)\n"
                "path.write_text('{}', encoding='utf-8')\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts/run-gitnexus.py"),
                 "--root", str(root), "--", sys.executable, str(fake)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("disposable GITNEXUS_HOME", completed.stderr)

    def test_path_boundary_does_not_accept_sibling_prefix(self):
        self.assertTrue(validator.path_allowed("graphify-out/graph.json", ["graphify-out/"]))
        self.assertFalse(validator.path_allowed("graphify-outside/graph.json", ["graphify-out/"]))

    def test_gitnexus_skill_path_requires_probed_skill_capability(self):
        unavailable = {"tools": {"gitnexus": {"supports": {"skills": False}}}}
        available = {"tools": {"gitnexus": {"supports": {"skills": True}}}}
        path = ".claude/skills/gitnexus/SKILL.md"
        self.assertFalse(validator.path_allowed(path, validator.authoritative_paths("gitnexus", unavailable)))
        self.assertTrue(validator.path_allowed(path, validator.authoritative_paths("gitnexus", available)))

    def test_gitnexus_sibling_skill_paths_are_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".claude/skills/gitnexus-cli").mkdir(parents=True)
            manifest = {
                "root": str(root),
                "tools": {"gitnexus": {"supports": {"skills": True}}},
            }
            paths = validator.authoritative_paths("gitnexus", manifest)
            self.assertTrue(validator.path_allowed(
                ".claude/skills/gitnexus-cli/SKILL.md", paths
            ))
            self.assertFalse(validator.path_allowed(
                ".claude/skills/unrelated/SKILL.md", paths
            ))

    def test_worker_status_cannot_hide_nonzero_exit(self):
        manifest = {"run_id": "run", "tools": {}}
        result = {
            "schema_version": 1,
            "run_id": "run",
            "task": "graphify",
            "status": "success",
            "required": True,
            "runner": "graphify",
            "command": ["graphify", "update"],
            "exit_code": 1,
            "changed_files": [],
            "allowed_paths": ["graphify-out/"],
            "artifacts": [],
            "warnings": [],
            "started_at": "2026-09-08T00:00:00Z",
            "finished_at": "2026-09-08T00:00:01Z",
            "duration_ms": 1000,
            "error": {"code": None, "message": None},
        }
        self.assertIn("success requires exit_code 0", " ".join(validator.validate_worker(result, manifest, Path.cwd())))

    def test_partial_worker_may_have_successful_process_exit(self):
        result = worker_result("graphify")
        result["status"] = "partial"
        result["error"] = {"code": "chunk-failures", "message": "one chunk failed"}
        errors = validator.validate_worker(
            result, {"run_id": "test-run", "tools": {}}, Path.cwd()
        )
        self.assertNotIn("cannot use exit_code 0", " ".join(errors))

    def test_worker_cannot_expand_authoritative_paths(self):
        result = worker_result("graphify", changed=["AGENTS.md"], allowed=["AGENTS.md"])
        errors = validator.validate_worker(
            result, {"run_id": "test-run", "tools": {}}, Path.cwd()
        )
        self.assertIn("unauthorized allowed_path claim", " ".join(errors))

    def test_graphify_success_requires_graph_and_manifest_artifacts(self):
        result = worker_result("graphify", allowed=["graphify-out/"])
        errors = validator.validate_worker(
            result, {"run_id": "test-run", "tools": {}}, Path.cwd()
        )
        self.assertIn("graphify-out/graph.json", " ".join(errors))
        self.assertIn("graphify-out/manifest.json", " ".join(errors))

    def test_required_artifact_validation_cannot_be_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "graphify-out"
            output.mkdir()
            (output / "graph.json").write_text("not-json\n", encoding="utf-8")
            (output / "manifest.json").write_text("not-json\n", encoding="utf-8")
            (output / ".graphify_root").write_text("/wrong/root\n", encoding="utf-8")
            artifacts = [
                {"path": f"graphify-out/{name}", "validation": "exists"}
                for name in ("graph.json", "manifest.json", ".graphify_root")
            ]
            result = worker_result(
                "graphify",
                changed=[artifact["path"] for artifact in artifacts],
                allowed=["graphify-out/"],
                artifacts=artifacts,
            )
            errors = validator.validate_worker(
                result, {"run_id": "test-run", "tools": {}}, root
            )
            self.assertIn("artifact is not valid JSON", " ".join(errors))
            self.assertIn("artifact root mismatch", " ".join(errors))

    def test_gitnexus_success_requires_meta_artifact(self):
        result = worker_result("gitnexus", allowed=[".gitnexus/"])
        errors = validator.validate_worker(
            result, {"run_id": "test-run", "tools": {}}, Path.cwd()
        )
        self.assertIn(".gitnexus/meta.json", " ".join(errors))

    def test_current_index_skip_is_complete_but_missing_tool_is_partial(self):
        current = {"status": "skipped", "error": {"code": "index-current"}}
        missing = {"status": "skipped", "error": {"code": "tool-missing"}}
        self.assertEqual("success", validator.aggregate_status([current]))
        self.assertEqual("partial", validator.aggregate_status([missing]))

    def test_collaboration_unavailable_is_blocked_and_allow_partial_cannot_release_it(self):
        result = worker_result("graphify")
        result["status"] = "skipped"
        result["exit_code"] = None
        result["error"] = {"code": "collab-unavailable", "message": "worker unavailable"}
        self.assertEqual([], validator.validate_worker(
            result, {"run_id": "test-run", "tools": {}}, Path.cwd()
        ))
        self.assertEqual("blocked", validator.aggregate_status([result]))

    def test_unknown_worker_error_code_is_rejected(self):
        result = worker_result("graphify")
        result["error"] = {"code": "tool-missng", "message": "typo"}
        errors = validator.validate_worker(
            result, {"run_id": "test-run", "tools": {}}, Path.cwd()
        )
        self.assertIn("unknown error code", " ".join(errors))

    def test_graphify_partial_remains_partial(self):
        graphify = {"status": "partial", "error": {"code": "chunk-failures"}}
        self.assertEqual("partial", validator.aggregate_status([graphify]))

    def test_old_gitnexus_help_is_detected_without_assuming_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if os.name == "nt":
                # CreateProcess cannot exec an extension-less POSIX script.
                runner = root / "gitnexus-old.cmd"
                runner.write_text(
                    "@echo off\r\n"
                    'if "%~1"=="--version" goto version\r\n'
                    "echo usage: gitnexus analyze --skills\r\n"
                    "exit /b 0\r\n"
                    ":version\r\n"
                    "echo gitnexus 1.0\r\n"
                    "exit /b 0\r\n",
                    encoding="utf-8",
                )
            else:
                runner = root / "gitnexus-old"
                runner.write_text(
                    "#!/bin/sh\n"
                    "if [ \"$1\" = \"--version\" ]; then echo 'gitnexus 1.0'; exit 0; fi\n"
                    "echo 'usage: gitnexus analyze --skills'\n",
                    encoding="utf-8",
                )
                runner.chmod(0o755)
            capability = prepare.probe([str(runner)], root)
            self.assertFalse(capability["supports"]["skip_agents_md"])
            self.assertFalse(capability["supports"]["index_only"])


class CommitGateTests(unittest.TestCase):
    def test_dirty_managed_file_is_recorded_in_baseline(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            agents = root / "AGENTS.md"
            agents.write_text("initial\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "AGENTS.md"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
            agents.write_text("user edit\n", encoding="utf-8")
            status = prepare.status_entries(root)
            self.assertIn("AGENTS.md", status)

    def test_prepare_and_validator_git_status_handle_renames_without_empty_paths(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            (root / "old.txt").write_text("same content\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "old.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
            subprocess.run(["git", "-C", str(root), "mv", "old.txt", "new.txt"], check=True)

            prepared = prepare.status_entries(root)
            validated = validator.git_status(root)
            for status in (prepared, validated):
                self.assertNotIn("", status)
                self.assertEqual("R ", status["old.txt"])
                self.assertEqual("R ", status["new.txt"])
            self.assertEqual(prepared, validated)

    def test_exact_commit_does_not_include_unrelated_staged_changes(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            git_repository(root)
            (root / "managed.txt").write_text("old\n", encoding="utf-8")
            (root / "unrelated.txt").write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
            (root / "managed.txt").write_text("new\n", encoding="utf-8")
            (root / "unrelated.txt").write_text("new\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "unrelated.txt"], check=True)
            validator.commit_exact_paths(root, ["managed.txt"])
            committed = subprocess.run(
                ["git", "-C", str(root), "show", "--pretty=format:", "--name-only", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.split()
            self.assertEqual(["managed.txt"], committed)
            staged = subprocess.run(
                ["git", "-C", str(root), "diff", "--cached", "--name-only"],
                text=True, capture_output=True, check=True,
            ).stdout.split()
            self.assertEqual(["unrelated.txt"], staged)


class ReconcileTests(unittest.TestCase):
    def test_context_managed_blocks_are_repeatable(self):
        source = "user content\n"
        once = reconcile.replace_block(source, "<!-- start -->", "<!-- end -->", "generated")
        twice = reconcile.replace_block(once, "<!-- start -->", "<!-- end -->", "generated")
        self.assertEqual(once, twice)

    def test_reconcile_rejects_missing_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest = root / "manifest.json"
            context = root / "context.json"
            manifest.write_text(json.dumps({"root": str(root), "options": {}}), encoding="utf-8")
            context.write_text(json.dumps({
                "schema_version": 1,
                "root": str(root),
                "overview": "fixture",
                "structure": [],
                "commands": [],
                "evidence": [],
                "warnings": [],
            }), encoding="utf-8")
            with self.assertRaises(reconcile.ReconcileError):
                reconcile.main(["--manifest", str(manifest), "--context", str(context)])


class RepositoryBoundaryTests(unittest.TestCase):
    def test_run_id_collision_is_not_reported_as_lock_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / ".codekit/initialize/collision"
            run_dir.mkdir(parents=True)

            with self.assertRaisesRegex(prepare.PrepareError, "run directory already exists"):
                prepare.acquire_state_lock(root, "collision")

            self.assertTrue(run_dir.is_dir())
            self.assertFalse((root / ".codekit/initialize.lock").exists())

    def test_eroFS_downgrades_state_with_an_explicit_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_open = os.open

            def fail_project_lock(path, flags, mode=0o777):
                if Path(path) == root / ".codekit/initialize.lock":
                    raise OSError(errno.EROFS, "read-only filesystem")
                return real_open(path, flags, mode)

            with patch.object(prepare.os, "open", side_effect=fail_project_lock):
                with redirect_stderr(StringIO()) as warning_output:
                    state_root, lock, run_dir, temporary, warnings, descriptor = prepare.acquire_state_lock(root, "eroFS")
            os.close(descriptor)
            shutil.rmtree(state_root)
            self.assertTrue(temporary)
            self.assertEqual(lock.parent, state_root)
            self.assertEqual(run_dir.name, "eroFS")
            self.assertEqual(1, len(warnings))
            self.assertIn("downgraded to temporary state", warning_output.getvalue())

    def test_prepare_rejects_symlinked_standard_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            try:
                os.symlink(outside, root / ".standard", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            with self.assertRaises(prepare.PrepareError):
                prepare.main(["--root", str(root), "--language", "python", "--run-id", "escape"])
            self.assertEqual([], list(outside.rglob("*.md")))

    def test_prepare_rejects_symlinked_stack_child(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            outside = base / "outside"
            (root / ".standard/02-stack").mkdir(parents=True)
            outside.mkdir()
            (outside / "rule.md").write_text("external\n", encoding="utf-8")
            try:
                os.symlink(outside, root / ".standard/02-stack/evil", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            with self.assertRaises(prepare.PrepareError):
                prepare.main(["--root", str(root), "--language", "python", "--run-id", "escape"])
            self.assertFalse((outside / "00-instructions.md").exists())

    def test_missing_required_standards_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = validator.validate_standard_references(root, {
                "required_standard_paths": [
                    ".standard/01-global/security.md",
                    ".standard/01-global/00-instructions.md",
                ]
            })
            self.assertIn("required standard file is missing", " ".join(errors))


class EndToEndTests(unittest.TestCase):
    def test_recoverable_report_reflects_actual_or_unknown_lock_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "run-manifest.json"
            lock_path = root / "initialize.lock"
            manifest_path.write_text(json.dumps({
                "phase": "prepared",
                "state_run_dir": str(root),
                "lock_path": str(lock_path),
            }), encoding="utf-8")

            lock_path.write_text("{}", encoding="utf-8")
            with redirect_stderr(StringIO()) as held_output:
                self.assertEqual(2, controller.report_recoverable_input_error(
                    manifest_path, ValueError("bad input")
                ))
            held_payload = json.loads(held_output.getvalue())
            self.assertEqual("held", held_payload["lock"])
            self.assertIn("retry this same run", held_payload["hint"])

            lock_path.unlink()
            with redirect_stderr(StringIO()) as absent_output:
                self.assertEqual(2, controller.report_recoverable_input_error(
                    manifest_path, ValueError("bad input")
                ))
            absent_payload = json.loads(absent_output.getvalue())
            self.assertEqual("absent", absent_payload["lock"])
            self.assertNotIn("retry this same run", absent_payload["hint"])

            manifest_path.write_text("not-json", encoding="utf-8")
            with redirect_stderr(StringIO()) as unknown_output:
                self.assertEqual(2, controller.report_recoverable_input_error(
                    manifest_path, ValueError("bad input")
                ))
            unknown_payload = json.loads(unknown_output.getvalue())
            self.assertEqual("unknown", unknown_payload["lock"])
            self.assertNotIn("retry this same run", unknown_payload["hint"])

    def test_barrier_input_error_is_recoverable_and_keeps_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            with isolated_gitnexus_home(root), redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "start", "--root", str(root), "--language", "python", "--run-id", "repairable"
                ]))
            manifest_path = root / ".codekit/initialize/repairable/run-manifest.json"
            if POSIX_MODES:
                self.assertEqual(0o600, stat.S_IMODE(manifest_path.stat().st_mode))
            outside_result = root.parent / "repairable-result.json"
            outside_result.write_text("{}", encoding="utf-8")
            with redirect_stderr(StringIO()) as error_output:
                self.assertEqual(2, controller.main([
                    "gate", "--manifest", str(manifest_path), "--result", str(outside_result)
                ]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("prepared", manifest["phase"])
            self.assertTrue((root / ".codekit/initialize.lock").exists())
            self.assertIn("retry this same run", error_output.getvalue())

    def test_controller_aborts_and_releases_lock_when_gate_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            # Phase 0 需要 GitNexus 注册前提的明确取舍；该用例只验证锁的释放。
            with isolated_gitnexus_home(root), redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "start", "--root", str(root), "--language", "python", "--run-id", "test-run"
                ]))
            git_dir = Path(subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
                text=True, capture_output=True, check=True,
            ).stdout.strip())
            manifest_path = root / ".codekit/initialize/test-run/run-manifest.json"
            self.assertTrue((root / ".codekit/initialize.lock").exists())
            self.assertFalse((git_dir / "codekit-initialize.lock").exists())
            scanner_path = manifest_path.parent / "context-scanner.json"
            scanner_path.write_text(json.dumps(worker_result("context-scanner")), encoding="utf-8")
            with redirect_stdout(StringIO()) as gate_output:
                self.assertEqual(1, controller.main([
                    "gate", "--manifest", str(manifest_path), "--result", str(scanner_path)
                ]))
            payload = json.loads(gate_output.getvalue())
            self.assertEqual("aborted", payload["phase"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("aborted", manifest["phase"])
            self.assertFalse((git_dir / "codekit-initialize.lock").exists())

    def test_controller_runs_complete_first_build_and_is_idempotent(self):
        with git_scratch_directory() as directory, isolated_gitnexus_home(Path(directory)):
            root = Path(directory)
            git_repository(root)
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("user agents\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("user claude\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)

            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main(["start", "--root", str(root), "--language", "python", "--run-id", "test-run"]))
            git_dir = Path(subprocess.run(["git", "-C", str(root), "rev-parse", "--absolute-git-dir"], text=True, capture_output=True, check=True).stdout.strip())
            manifest_path = root / ".codekit/initialize/test-run/run-manifest.json"
            (root / ".gitnexus").mkdir()
            (root / ".gitnexus/meta.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
            (root / "graphify-out").mkdir()
            (root / "graphify-out/graph.json").write_text(json.dumps({"nodes": [{"id": "n"}]}), encoding="utf-8")
            (root / "graphify-out/manifest.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
            (root / "graphify-out/.graphify_root").write_text(str(root), encoding="utf-8")
            results = {
                "gitnexus": worker_result(
                    "gitnexus",
                    changed=[".gitnexus/meta.json"],
                    allowed=[".gitnexus/"],
                    artifacts=[{"path": ".gitnexus/meta.json", "validation": "json-valid"}],
                ),
                "graphify": worker_result(
                    "graphify",
                    changed=["graphify-out/graph.json", "graphify-out/manifest.json", "graphify-out/.graphify_root"],
                    allowed=["graphify-out/"],
                    artifacts=[
                        {"path": "graphify-out/graph.json", "validation": "json-nonempty-nodes"},
                        {"path": "graphify-out/manifest.json", "validation": "json-valid"},
                        {"path": "graphify-out/.graphify_root", "validation": "root-match"},
                    ],
                ),
                "context-scanner": worker_result("context-scanner"),
            }
            result_paths = []
            for task, result in results.items():
                path = manifest_path.parent / f"{task}.json"
                path.write_text(json.dumps(result), encoding="utf-8")
                result_paths.append(path)
            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "gate", "--manifest", str(manifest_path),
                    *[part for path in result_paths for part in ("--result", str(path))],
                ]))
            context_path = manifest_path.parent / "context.json"
            context = {
                "schema_version": 1,
                "root": str(root),
                "overview": "Test repository.",
                "structure": [{"path": "README.md", "description": "Fixture documentation"}],
                "commands": [{
                    "purpose": "Check",
                    "command": f"{VALIDATION_PYTHON} -c pass",
                    "evidence": "missing.py",
                    "required_for_initialize": True,
                }],
                "evidence": [{"path": "README.md"}],
                "warnings": [],
            }
            context_path.write_text(json.dumps(context), encoding="utf-8")

            managed_before_reconcile = {
                name: (root / name).read_bytes()
                for name in ("AGENTS.md", "CLAUDE.md")
                if (root / name).exists()
            }
            with redirect_stderr(StringIO()) as context_error:
                self.assertEqual(2, controller.main([
                    "reconcile", "--manifest", str(manifest_path), "--context", str(context_path),
                    *[part for path in result_paths for part in ("--worker-result", str(path))],
                ]))
            context_payload = json.loads(context_error.getvalue())
            self.assertEqual("input-error", context_payload["status"])
            self.assertTrue(context_payload["recoverable"])
            self.assertEqual("gated", context_payload["phase"])
            self.assertEqual("held", context_payload["lock"])
            self.assertTrue((root / ".codekit/initialize.lock").exists())
            self.assertEqual(
                managed_before_reconcile,
                {
                    name: (root / name).read_bytes()
                    for name in managed_before_reconcile
                },
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotEqual("reconciled", manifest.get("phase"))
            self.assertNotIn("context_status", manifest)

            context["commands"][0]["evidence"] = "README.md"
            context_path.write_text(json.dumps(context), encoding="utf-8")

            with redirect_stderr(StringIO()) as reconcile_error:
                self.assertEqual(2, controller.main([
                    "reconcile", "--manifest", str(manifest_path), "--context", str(context_path),
                    *[part for path in result_paths[:2] for part in ("--worker-result", str(path))],
                ]))
            error_payload = json.loads(reconcile_error.getvalue())
            self.assertEqual("input-error", error_payload["status"])
            self.assertTrue(error_payload["recoverable"])
            self.assertEqual("gated", error_payload["phase"])
            self.assertEqual("held", error_payload["lock"])
            self.assertTrue((root / ".codekit/initialize.lock").exists())

            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "reconcile", "--manifest", str(manifest_path), "--context", str(context_path),
                    *[part for path in result_paths for part in ("--worker-result", str(path))],
                ]))
            with redirect_stdout(StringIO()) as finish_output:
                finish_code = controller.main(["finish", "--manifest", str(manifest_path)])
            self.assertEqual(0, finish_code, finish_output.getvalue())
            self.assertFalse((git_dir / "codekit-initialize.lock").exists())
            self.assertFalse((root / ".codekit").exists())
            agents_text = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("user agents", agents_text)
            self.assertIn("codekit:initialize:project-context:start", agents_text)
            # 仓库级回复语言锚点：目标仓库里只用 @skill 触发、本轮没有自然语言时，
            # 这是唯一会被加载的语言信号。
            self.assertIn("回复语言", agents_text)
            self.assertIn("回复语言", (root / "CLAUDE.md").read_text(encoding="utf-8"))
            self.assertIn(
                "回复语言",
                (root / ".standard/01-global/00-instructions.md").read_text(encoding="utf-8"),
            )

            managed_before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".git" not in path.parts
            }
            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main(["start", "--root", str(root), "--language", "python", "--run-id", "test-run-2"]))
            manifest_path_2 = root / ".codekit/initialize/test-run-2/run-manifest.json"
            manifest_2 = json.loads(manifest_path_2.read_text(encoding="utf-8"))
            self.assertEqual([], manifest_2["prepared_changed_files"])
            result_paths_2 = []
            for task in ("gitnexus", "graphify"):
                if task == "gitnexus":
                    result = worker_result(
                        task,
                        allowed=[".gitnexus/"],
                        artifacts=[{"path": ".gitnexus/meta.json", "validation": "json-valid"}],
                    )
                else:
                    result = worker_result(
                        task,
                        allowed=["graphify-out/"],
                        artifacts=[
                            {"path": "graphify-out/graph.json", "validation": "json-nonempty-nodes"},
                            {"path": "graphify-out/manifest.json", "validation": "json-valid"},
                            {"path": "graphify-out/.graphify_root", "validation": "root-match"},
                        ],
                    )
                result["run_id"] = "test-run-2"
                result["status"] = "skipped"
                result["exit_code"] = None
                result["error"] = {"code": "no-change", "message": None}
                path = manifest_path_2.parent / f"{task}.json"
                path.write_text(json.dumps(result), encoding="utf-8")
                result_paths_2.append(path)
            scanner = worker_result("context-scanner")
            scanner["run_id"] = "test-run-2"
            scanner_path = manifest_path_2.parent / "context-scanner.json"
            scanner_path.write_text(json.dumps(scanner), encoding="utf-8")
            result_paths_2.append(scanner_path)
            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "gate", "--manifest", str(manifest_path_2),
                    *[part for path in result_paths_2 for part in ("--result", str(path))],
                ]))
            context_path_2 = manifest_path_2.parent / "context.json"
            context_path_2.write_text(json.dumps({
                "schema_version": 1,
                "root": str(root),
                "overview": "Test repository.",
                "structure": [{"path": "README.md", "description": "Fixture documentation"}],
                "commands": [{
                    "purpose": "Check",
                    "command": f"{VALIDATION_PYTHON} -c pass",
                    "evidence": "README.md",
                    "required_for_initialize": True,
                }],
                "evidence": [{"path": "README.md"}],
                "warnings": [],
            }), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "reconcile", "--manifest", str(manifest_path_2), "--context", str(context_path_2),
                    *[part for path in result_paths_2 for part in ("--worker-result", str(path))],
                ]))
            with redirect_stdout(StringIO()) as finish_output_2:
                finish_code_2 = controller.main(["finish", "--manifest", str(manifest_path_2)])
            self.assertEqual(0, finish_code_2, finish_output_2.getvalue())
            managed_after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and ".git" not in path.parts
            }
            self.assertEqual(managed_before, managed_after)


class RepeatInitializeTests(unittest.TestCase):
    """同一仓库重复 initialize：遗留锁可回收，仍可能活跃的 run 不被抢占。"""

    def held_lock(self, root: Path, run_id: str, manifest: dict) -> tuple[Path, Path]:
        manifest_path = root / ".codekit" / "initialize" / run_id / "run-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        lock_path = root / ".codekit" / "initialize.lock"
        lock_path.write_text(json.dumps({
            "schema_version": 1,
            "run_id": run_id,
            "root": str(root),
            "manifest": str(manifest_path),
            "created_at": "2026-09-08T00:00:00Z",
        }), encoding="utf-8")
        return manifest_path, lock_path

    def start(self, root: Path, run_id: str) -> int:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return controller.main([
                "start", "--root", str(root), "--language", "python", "--run-id", run_id
            ])

    def test_leftover_lock_from_a_terminal_run_is_reclaimed(self):
        with git_scratch_directory() as directory, isolated_gitnexus_home(Path(directory)):
            root = Path(directory)
            git_repository(root)
            _, lock_path = self.held_lock(
                root, "stale", {"phase": "aborted", "started_at": "2026-09-08T00:00:00Z"}
            )
            self.assertEqual(0, self.start(root, "repeat"))
            self.assertEqual("repeat", json.loads(lock_path.read_text(encoding="utf-8"))["run_id"])
            manifest = json.loads(
                (root / ".codekit/initialize/repeat/run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                any("reclaimed the stale initialize lock" in warning for warning in manifest["warnings"]),
                manifest["warnings"],
            )
            # 回收只针对锁本身，遗留 run 的目录仍是失败证据。
            self.assertTrue((root / ".codekit/initialize/stale").is_dir())

    def test_unreadable_lock_is_reclaimed(self):
        with git_scratch_directory() as directory, isolated_gitnexus_home(Path(directory)):
            root = Path(directory)
            git_repository(root)
            (root / ".codekit").mkdir()
            lock_path = root / ".codekit" / "initialize.lock"
            lock_path.write_text("not json", encoding="utf-8")
            self.assertEqual(0, self.start(root, "repeat"))
            self.assertEqual("repeat", json.loads(lock_path.read_text(encoding="utf-8"))["run_id"])

    def test_possibly_live_lock_stops_with_the_reclaim_command(self):
        with git_scratch_directory() as directory, isolated_gitnexus_home(Path(directory)):
            root = Path(directory)
            git_repository(root)
            manifest_path, lock_path = self.held_lock(
                root, "live", {"phase": "prepared", "started_at": "2026-09-08T00:00:00Z"}
            )
            with self.assertRaises(prepare.PrepareError) as caught:
                self.start(root, "intruder")
            message = str(caught.exception)
            self.assertIn("may still be active", message)
            self.assertIn("abort --manifest", message)
            self.assertIn(str(manifest_path), message)
            self.assertEqual("live", json.loads(lock_path.read_text(encoding="utf-8"))["run_id"])
            self.assertFalse((root / ".codekit/initialize/intruder").exists())


class IgnoredScopeTests(unittest.TestCase):
    """Ignored analyzer areas are compared by authorized prefix, not per file."""

    def setUp(self):
        self.directory = git_scratch_directory()
        self.root = Path(self.directory.name)
        git_repository(self.root)
        (self.root / ".gitignore").write_text(
            ".codekit/\n.gitnexus/\ngraphify-out/\n.claude/\n", encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "base"], check=True)
        (self.root / ".gitnexus").mkdir()
        self.manifest = analyze_manifest(self.root)
        self.manifest["worker_scope_baseline"] = validator.snapshot_worker_scopes(
            self.root, self.manifest["worker_tree_roots"],
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_snapshot_records_top_level_entries_only(self):
        for index in range(50):
            (self.root / ".gitnexus" / f"{index}.json").write_text("{}", encoding="utf-8")
        self.assertEqual(50, len(validator.snapshot_worker_scopes(self.root, [".gitnexus"])))
        cache = self.root / ".gitnexus/parse-cache"
        cache.mkdir()
        for index in range(80):
            (cache / f"{index}.json").write_text("{}", encoding="utf-8")
        snapshot = validator.snapshot_worker_scopes(self.root, [".gitnexus"])
        self.assertEqual(51, len(snapshot))
        self.assertEqual(80, snapshot[".gitnexus/parse-cache"]["entries"])

    def test_prepare_and_validator_snapshots_agree(self):
        for index in range(5):
            (self.root / ".gitnexus" / f"{index}.json").write_text("{}", encoding="utf-8")
        (self.root / "graphify-out/cache").mkdir(parents=True)
        roots = self.manifest["worker_tree_roots"]
        self.assertEqual(
            prepare.snapshot_worker_scopes(self.root, roots),
            validator.snapshot_worker_scopes(self.root, roots),
        )

    def test_cache_churn_inside_authorized_scope_is_not_a_change(self):
        cache = self.root / ".gitnexus/parse-cache"
        cache.mkdir(parents=True)
        for index in range(200):
            (cache / f"{index}.json").write_text("{}", encoding="utf-8")
        os.utime(self.root / ".gitnexus", ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(set(), validator.changed_since_baseline(self.manifest, self.root))

    def test_unexpected_new_directory_still_trips(self):
        (self.root / ".claude/skills/unrelated").mkdir(parents=True)
        (self.root / ".claude/skills/unrelated/SKILL.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(
            {".claude/skills/unrelated"},
            validator.changed_since_baseline(self.manifest, self.root),
        )

    def test_probed_skill_directories_are_authorized_by_prefix(self):
        (self.root / ".claude/skills/gitnexus-cli").mkdir(parents=True)
        (self.root / ".claude/skills/gitnexus-cli/SKILL.md").write_text("x\n", encoding="utf-8")
        self.manifest["tools"] = {"gitnexus": {"supports": {"skills": True}}}
        self.assertEqual(set(), validator.changed_since_baseline(self.manifest, self.root))


class BaselineVisibilityTests(unittest.TestCase):
    """Phase 0 rewriting its own ignore file must not look like a foreign write."""

    def test_prepare_rewrite_keeps_mirror_visibility_unchanged(self):
        """An older run's concrete per-skill entries must not create a false write.

        The pre-run block carries concrete ``gitnexus-*`` entries instead of the
        mirror glob, so the mirrors are ignored at baseline.  Rewriting the block
        down to the canonical list must not un-ignore them; when it did, the
        barrier reported every mirror file as an unreported change while all
        three workers had done nothing to them.  ``prepare.py`` re-stripped those
        entries on every run, so the failure returned every other run.
        """
        directory = git_scratch_directory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        git_repository(root)
        (root / "README.md").write_text("fixture\n", encoding="utf-8")
        mirrors = {
            ".agents/skills/gitnexus-area-action/SKILL.md": ".agents/skills/gitnexus-area-action/",
            ".claude/skills/gitnexus-cli/SKILL.md": ".claude/skills/gitnexus-cli/",
        }
        for relative in mirrors:
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text("generated\n", encoding="utf-8")
        (root / ".gitignore").write_text(
            "# codekit:initialize:ignore:start\n"
            + "".join(f"{entry}\n" for entry in (
                ".codekit/", ".gitnexus/", "graphify-out/", ".graphifyignore",
                *mirrors.values(),
            ))
            + "# codekit:initialize:ignore:end\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)

        with isolated_gitnexus_home(root), redirect_stdout(StringIO()):
            self.assertEqual(0, prepare.main([
                "--root", str(root), "--language", "python", "--run-id", "converge",
            ]))
        manifest = json.loads(
            (root / ".codekit/initialize/converge/run-manifest.json").read_text(encoding="utf-8")
        )
        # The frozen block keeps the mirrors ignored, so nothing becomes visible.
        delta = manifest["controller_visibility_delta"]
        for relative in mirrors:
            self.assertNotIn(relative, delta)
        for relative in mirrors:
            self.assertTrue(validator.ignored_by_git(root, relative))
        block = (root / ".gitignore").read_text(encoding="utf-8")
        for stale_entry in mirrors.values():
            self.assertNotIn(stale_entry, block)
        # A tracked file Phase 0 really wrote stays a change, so ``--commit``
        # still picks up the rewritten managed block.
        self.assertIn(".gitignore", validator.changed_since_baseline(manifest, root))
        self.assertEqual(
            set(),
            validator.uncovered_paths(
                validator.changed_since_baseline(manifest, root),
                set(manifest["prepared_changed_files"]),
            ),
        )

    def test_controller_visibility_delta_is_not_reported_as_a_change(self):
        """Only the paths Phase 0 un-ignored are excused; other writes still trip."""
        directory = git_scratch_directory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        git_repository(root)
        (root / "controller-unignored.txt").write_text("x\n", encoding="utf-8")
        (root / "stray.txt").write_text("x\n", encoding="utf-8")
        manifest = analyze_manifest(root)
        self.assertEqual(
            {"controller-unignored.txt", "stray.txt"},
            validator.changed_since_baseline(manifest, root),
        )
        manifest["controller_visibility_delta"] = ["controller-unignored.txt"]
        self.assertEqual(
            {"stray.txt"},
            validator.changed_since_baseline(manifest, root),
        )

    def test_directory_declaration_covers_the_files_git_reports(self):
        """A declared directory covers its descendants, never a sibling prefix."""
        self.assertEqual(
            set(),
            validator.uncovered_paths(
                {".agents/skills/gitnexus-area-action/SKILL.md"},
                {".agents/skills/gitnexus-area-action"},
            ),
        )
        self.assertEqual(
            {".agents/skills/gitnexus-area-action-old/SKILL.md"},
            validator.uncovered_paths(
                {".agents/skills/gitnexus-area-action-old/SKILL.md"},
                {".agents/skills/gitnexus-area-action"},
            ),
        )
        self.assertEqual(
            {".gitnexus/meta.json.bak"},
            validator.uncovered_paths({".gitnexus/meta.json.bak"}, {".gitnexus/meta.json"}),
        )


class WorkerPathClaimTests(unittest.TestCase):
    """Malformed Worker path claims are contract errors, not gate crashes."""

    def test_absolute_paths_are_reported_and_never_read(self):
        manifest = {"run_id": "run", "tools": {}}
        result = worker_result(
            "graphify", run_id="run",
            changed=["/etc/hostname"],
            allowed=["graphify-out/"],
            artifacts=[{"path": "/etc/hostname", "validation": "json-valid"}],
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            validator, "validate_artifact"
        ) as inspect:
            errors = validator.validate_worker(result, manifest, Path(directory))
        inspect.assert_not_called()
        self.assertEqual(
            2, sum("unsafe repository-relative path" in message for message in errors),
        )

    def test_parent_traversal_in_allowed_paths_is_reported(self):
        manifest = {"run_id": "run", "tools": {}}
        result = worker_result("graphify", run_id="run", allowed=["graphify-out/../../outside"])
        with tempfile.TemporaryDirectory() as directory:
            errors = validator.validate_worker(result, manifest, Path(directory))
        self.assertTrue(any("unsafe repository-relative path" in message for message in errors))


@unittest.skipUnless(
    validation_python_runs(), f"{VALIDATION_PYTHON} must run as an allowed validation program"
)
class ValidationExecutionTests(unittest.TestCase):
    """Commit evidence must come from commands the controller executed itself."""

    def test_controller_produced_gitnexus_result_drives_the_whole_flow(self):
        # gitnexus 不再由 subagent 产出后，控制器写的 result 必须仍能通过
        # gate/reconcile/finish，并且按 index-current 记为完整复用。
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(
                root, scan_context(root, []), gitnexus_via_step=True,
            )
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("success", report["status"])

    def test_both_tool_steps_drive_the_whole_flow_in_order(self):
        """analyze → graphify 都在主会话执行时，整条链路必须仍然成立。

        这一步专门盯顺序带来的风险：GitNexus 会先写出 ``.claude/skills`` 与
        ``.agents/skills`` 镜像，Graphify 排在其后，门禁的越界检查不能把这些新产物
        算成 Graphify 那一路的未申报写入。
        """
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(
                root, scan_context(root, []),
                gitnexus_via_step=True, graphify_via_step=True,
            )
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("success", report["status"])
            self.assertFalse(
                any("unreported paths" in error for error in report["errors"]),
                report["errors"],
            )
            # sidecar 由 reconcile 按步骤 result 的 mode/version 落盘，finish 再复核一次。
            sidecar = json.loads(
                (root / "graphify-out" / ".codekit-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual("code-only", sidecar["mode"])
            self.assertEqual("graphify 9.9.9", sidecar["graphify_version"])
            self.assertEqual("success", sidecar["last_status"])

    def test_controller_executes_required_validations(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(root, scan_context(root, [
                {"purpose": "Check", "command": f"{VALIDATION_PYTHON} check_ok.py",
                 "evidence": "check_ok.py", "required_for_initialize": True},
            ]))
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("executed", report["validation_source"])
            self.assertEqual(
                f"{VALIDATION_PYTHON} check_ok.py", report["validations"][0]["command"]
            )
            self.assertEqual(0, report["validations"][0]["exit_code"])
            self.assertEqual(1, commit_count(root))

    def test_successful_validations_allow_controlled_commit(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(root, scan_context(root, [
                {"purpose": "Check", "command": f"{VALIDATION_PYTHON} check_ok.py",
                 "evidence": "check_ok.py", "required_for_initialize": True},
            ]), commit=True)
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("committed", report["commit_status"])
            self.assertTrue(report["restart_required"])
            self.assertIn("需要重启 Agent 应用以生效", report["restart_notice"])
            self.assertEqual(2, commit_count(root))
            committed = subprocess.run(
                ["git", "-C", str(root), "show", "--pretty=format:", "--name-only", "HEAD"],
                text=True, capture_output=True, check=True,
            ).stdout.split()
            self.assertIn("AGENTS.md", committed)
            self.assertNotIn(".gitnexus/meta.json", committed)

    def test_failing_validation_blocks_commit(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(root, scan_context(root, [
                {"purpose": "Check", "command": f"{VALIDATION_PYTHON} check_bad.py",
                 "evidence": "check_bad.py", "required_for_initialize": True},
            ]), commit=True)
            self.assertEqual(1, code)
            self.assertIn(
                f"repository validation failed: {VALIDATION_PYTHON} check_bad.py", report["errors"]
            )
            self.assertFalse(report["commit_allowed"])
            self.assertFalse(report["restart_required"])
            self.assertIsNone(report["restart_notice"])
            self.assertEqual(1, commit_count(root))
            manifest = json.loads(
                (root / ".codekit/initialize/flow/run-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual("aborted", manifest["phase"])
            self.assertEqual("failed", manifest["final_report"]["status"])

    def test_self_attested_success_cannot_unlock_commit(self):
        def attest(run_dir: Path) -> list[str]:
            payload = {"command": "python3 check_bad.py", "status": "success", "exit_code": 0}
            path = run_dir / "attested.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return ["--validation-result", str(path)]

        context = scan_context(Path(""), [
            {"purpose": "Check", "command": "python3 check_bad.py",
             "evidence": "check_bad.py", "required_for_initialize": True},
        ])
        with git_scratch_directory() as directory:
            root = Path(directory)
            context["root"] = str(root)
            code, report = drive_initialize_flow(root, context, commit=True,
                                                 before_finish=attest, run_id="unflagged")
            self.assertEqual(1, code)
            self.assertIn("--allow-attested-validations", " ".join(report["errors"]))
            self.assertEqual(1, commit_count(root))

        with git_scratch_directory() as directory:
            root = Path(directory)
            context["root"] = str(root)
            code, report = drive_initialize_flow(
                root, context, commit=True, before_finish=attest, run_id="attested",
                finish_args=["--allow-attested-validations"],
            )
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("attested", report["validation_source"])
            self.assertFalse(report["commit_allowed"])
            self.assertIsNone(report["commit_sha"])
            self.assertEqual("blocked-unverified-validations", report["commit_status"])
            self.assertEqual(1, commit_count(root))

    def test_validation_commands_are_constrained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = "python" if os.name == "nt" else "python3"
            self.assertEqual(
                [shutil.which(program), "check_ok.py"],
                validator.resolve_validation_command(root, f"{program} check_ok.py"),
            )
            cases = {
                'python3 -c "print(1)"': "forbidden shell syntax",
                "git status && rm -rf /": "forbidden shell syntax",
                "curl http://example.invalid": "not allowed",
                "./missing.py": "script is missing",
            }
            for command, expectation in cases.items():
                with self.subTest(command=command):
                    with self.assertRaisesRegex(validator.ValidationError, expectation):
                        validator.resolve_validation_command(root, command)

    def test_uninstalled_allow_listed_program_raises_a_dedicated_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(shutil, "which", lambda program: None):
                with self.assertRaises(validator.ValidationProgramMissing):
                    validator.resolve_validation_command(root, "gradle build")

    def test_windows_python_launcher_is_allow_listed(self):
        # Windows 只能用 `py -3`（干净 Windows 的 python3 是商店别名桩）；POSIX 上
        # 通常不存在 py，会走 skipped 而不是被拒。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "Launcher"
            launcher.mkdir()
            py = launcher / "py.exe"
            py.write_text("", encoding="utf-8")

            def which(program: str) -> str | None:
                return str(py) if program == "py" else None

            # 候选目录清空，只留 PATH 这一条通路，用例才与宿主机的真实安装无关。
            with patch.object(shutil, "which", which), patch.object(
                common.runtime, "toolchain_paths", return_value=[]
            ), no_ambient_manifest():
                self.assertEqual(
                    [str(py), "-3", "-m", "pytest"],
                    validator.resolve_validation_command(root, "py -3 -m pytest"),
                )
                with self.assertRaises(validator.ValidationProgramMissing):
                    validator.resolve_validation_command(root, "python3 -m pytest")

    def test_flow_skips_uninstalled_allow_listed_validation_and_blocks_commit(self):
        # 白名单内但本机未安装（Java 仓库的 gradle/mvn 常见）不得让已完成的运行失败。
        with git_scratch_directory() as directory:
            root = Path(directory)
            real_which = shutil.which
            with patch.object(
                shutil, "which",
                lambda program: None if program == "gradle" else real_which(program),
            ):
                code, report = drive_initialize_flow(root, scan_context(root, [
                    {"purpose": "Check", "command": "gradle build",
                     "evidence": "README.md", "required_for_initialize": True},
                ]), commit=True)
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("partial", report["status"])
            self.assertEqual("skipped", report["validations"][0]["status"])
            self.assertEqual("tool-missing", report["validations"][0]["reason"])
            self.assertTrue(report["validation_warnings"])
            self.assertFalse(report["commit_allowed"])
            self.assertEqual("blocked-skipped-validations", report["commit_status"])
            self.assertTrue(report["restart_required"])
            self.assertEqual(1, commit_count(root))

    def test_flow_rejects_non_allow_listed_required_command_before_writing(self):
        # Scanner 可能把宿主工具当成仓库验证命令（例如它自己跑过的 git status）。
        # 这类命令必须在写任何托管文件之前以可恢复契约错误被拒绝，并在修复
        # context 后允许重试同一个 run。
        with git_scratch_directory() as directory:
            root = Path(directory)
            context = scan_context(root, [{
                "purpose": "Check", "command": "git status --porcelain",
                "evidence": "README.md", "required_for_initialize": True,
            }])
            code, payload = drive_initialize_flow(
                root, context, run_id="allow-list", expect_reconcile_error=True,
            )
            self.assertEqual(2, code)
            self.assertEqual("input-error", payload["status"])
            self.assertTrue(payload["recoverable"])
            self.assertEqual("held", payload["lock"])
            self.assertIn("not allowed: git", payload["message"])
            self.assertEqual("user agents\n", (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertEqual("user claude\n", (root / "CLAUDE.md").read_text(encoding="utf-8"))
            run_dir = root / ".codekit" / "initialize" / "allow-list"
            manifest_path = run_dir / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("gated", manifest["phase"])
            self.assertNotIn("validation_commands", manifest)

            context["commands"][0]["command"] = f"{VALIDATION_PYTHON} check_ok.py"
            context["commands"][0]["evidence"] = "check_ok.py"
            (run_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "reconcile", "--manifest", str(manifest_path),
                    "--context", str(run_dir / "context.json"),
                    *[part for name in ("gitnexus", "graphify", "context-scanner")
                      for part in ("--worker-result", str(run_dir / f"{name}.json"))],
                ]))
                output = StringIO()
                with redirect_stdout(output):
                    code = controller.main(["finish", "--manifest", str(manifest_path)])
            report = json.loads(output.getvalue())
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("success", report["status"])
            self.assertEqual("executed", report["validation_source"])


class AtomicWriteModeTests(unittest.TestCase):
    """Managed files must not silently lose their permission bits."""

    @unittest.skipUnless(POSIX_MODES, POSIX_MODE_REASON)
    def test_existing_mode_is_preserved_and_new_files_are_readable(self):
        for module in (prepare, reconcile, validator):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "AGENTS.md"
                target.write_text("old\n", encoding="utf-8")
                os.chmod(target, 0o664)
                if module is validator:
                    module.atomic_json(target, {"value": "new"})
                    self.assertEqual(0o600, stat.S_IMODE(target.stat().st_mode))
                    continue
                self.assertTrue(module.atomic_write(target, "new\n"))
                self.assertEqual(0o664, stat.S_IMODE(target.stat().st_mode))
                created = target.parent / "created.md"
                self.assertTrue(module.atomic_write(created, "content\n"))
                self.assertEqual(0o644, stat.S_IMODE(created.stat().st_mode))

    def test_atomic_json_uses_unique_sibling_temp_names(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            with patch.object(common.tempfile, "mkstemp", wraps=common.tempfile.mkstemp) as mkstemp:
                validator.atomic_json(target, {"value": "new"})
            self.assertTrue(mkstemp.call_args.kwargs["prefix"].startswith(".manifest.json."))
            self.assertFalse((target.parent / ".manifest.json.tmp").exists())


class ContextContractTests(unittest.TestCase):
    """Scanner output is model output: validate it before it reaches AGENTS.md."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def test_structure_entry_without_description_is_a_contract_error(self):
        context = scan_context(self.root, [])
        context["structure"] = [{"path": "README.md"}]
        with self.assertRaisesRegex(reconcile.ReconcileError, "description"):
            reconcile.validate_context(context, self.root)

    def test_nonexistent_structure_path_is_rejected(self):
        context = scan_context(self.root, [])
        context["structure"] = [{"path": "ghost.md", "description": "absent"}]
        with self.assertRaisesRegex(reconcile.ReconcileError, "does not exist"):
            reconcile.validate_context(context, self.root)

    def test_required_command_without_evidence_is_rejected(self):
        context = scan_context(self.root, [
            {"purpose": "Check", "command": "python3 check.py", "required_for_initialize": True},
        ])
        with self.assertRaisesRegex(reconcile.ContextContractError, "evidence"):
            reconcile.validate_context(context, self.root)

    def test_required_command_outside_the_allow_list_is_rejected(self):
        context = scan_context(self.root, [{
            "purpose": "Check", "command": "git status --porcelain",
            "evidence": "README.md", "required_for_initialize": True,
        }])
        with self.assertRaisesRegex(reconcile.ContextContractError, "not allowed: git"):
            reconcile.validate_context(context, self.root)

    def test_required_command_with_uninstalled_program_is_tolerated(self):
        # 白名单内但未安装由 finish 记为 skipped，不是契约错误。
        context = scan_context(self.root, [{
            "purpose": "Check", "command": "gradle build",
            "evidence": "README.md", "required_for_initialize": True,
        }])
        with patch.object(shutil, "which", lambda program: None):
            reconcile.validate_context(context, self.root)

    def test_documented_allow_list_matches_the_controller(self):
        text = (SKILL_DIR / "references" / "context-files.md").read_text(encoding="utf-8")
        parts = text.split("```")
        documented: set[str] | None = None
        for index in range(1, len(parts), 2):
            block = parts[index]
            if "VALIDATION_PROGRAMS" not in block:
                continue
            # ``split("```")`` keeps the info string joined to the first content line.
            lines = block.splitlines()[1:]
            documented = {
                line.strip() for line in lines
                if line.strip() and not line.strip().startswith("#")
            }
            break
        self.assertIsNotNone(
            documented, "context-files.md must publish the validation allow-list"
        )
        self.assertEqual(set(common.VALIDATION_PROGRAMS), documented)
        scanner = (SKILL_DIR / "agents" / "initialize-context-scanner.md").read_text(encoding="utf-8")
        self.assertIn("VALIDATION_PROGRAMS", scanner)
        self.assertIn("context-files.md", scanner)

    def test_scanner_role_lists_every_gate_required_field(self):
        """角色正文的字段清单必须与 gate 的 REQUIRED_FIELDS 逐项相等。

        正文原先只以散文列举字段，漏掉 `runner`：Scanner 严格照正文写就必然缺键，
        而 gate 对三路 Worker 使用同一份字段集合，缺任何一个键即整轮 `aborted` 并释放锁，
        不属于可恢复的输入错误。夹具对所有 task 都硬写 `runner`，所以只有这条守卫能发现漂移。
        """
        text = (SKILL_DIR / "agents" / "initialize-context-scanner.md").read_text(encoding="utf-8")
        documented: set[str] = set()
        blocks = text.split("```")
        for index in range(1, len(blocks), 2):
            # ``split("```")`` keeps the info string joined to the first content line.
            lines = blocks[index].splitlines()[1:]
            keys = {
                line.split(":", 1)[0].strip() for line in lines
                if line.strip() and not line[0].isspace() and ":" in line
            }
            if "schema_version" in keys:
                documented = keys
                break
        self.assertTrue(documented, "角色正文必须内联 Worker result 的字段清单")
        self.assertEqual(set(validator.REQUIRED_FIELDS), documented)

    def test_single_string_evidence_is_accepted(self):
        context = scan_context(self.root, [
            {"purpose": "Check", "command": "python3 check.py", "evidence": "README.md"},
        ])
        self.assertIsNone(reconcile.validate_context(context, self.root))

    def test_item_overflow_is_trimmed_with_warnings(self):
        context = scan_context(self.root, [])
        context["structure"] = [
            {"path": "README.md", "description": f"entry {index}"} for index in range(25)
        ]
        with patch.object(reconcile, "CONTEXT_ITEM_LIMIT", 4):
            text, warnings = reconcile.render_project_context(context)
        self.assertEqual(4, text.count("- `README.md`:"))
        self.assertIn("context structure truncated from 25 to 4 entries", warnings)

    def test_line_budget_overflow_is_trimmed_with_warnings(self):
        context = scan_context(self.root, [])
        context["structure"] = [
            {"path": "README.md", "description": f"entry {index}"} for index in range(10)
        ]
        with patch.object(reconcile, "CONTEXT_LINE_BUDGET", 9):
            text, warnings = reconcile.render_project_context(context)
        self.assertLessEqual(len(text.splitlines()), 9)
        self.assertTrue(any("line budget" in warning for warning in warnings))


class CollabProbeTests(unittest.TestCase):
    """Phase 0 协作能力探测：只有实测到空工具面才允许阻断，探测本身不落盘。"""

    TOOLS = "… spawn_agent list_agents wait_agent send_message interrupt_agent …"

    def _runner(self, returncode, output):
        calls = []

        def runner(argv, timeout):
            calls.append(list(argv))
            return returncode, output

        return runner, calls

    def test_measured_tool_surface_is_available(self):
        runner, calls = self._runner(0, self.TOOLS)
        result = probe_collab.run_probe("codex", runner, Path("/nonexistent-home"))
        self.assertEqual(result["collab"], probe_collab.AVAILABLE)
        self.assertIn("spawn_agent", result["evidence"]["tools"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fix", result)

    def test_empty_surface_is_the_only_blocking_verdict(self):
        runner, _ = self._runner(0, "no agent tools in this prompt")
        result = probe_collab.run_probe("codex", runner, Path("/nonexistent-home"))
        self.assertEqual(result["collab"], probe_collab.UNAVAILABLE)
        self.assertIn("multi_agent_v2", result["fix"])
        self.assertTrue(result["restart_required"])

    def test_failed_probe_is_unknown_not_unavailable(self):
        runner, _ = self._runner(1, "")
        self.assertEqual(
            probe_collab.run_probe("codex", runner, Path("/h"))["collab"],
            probe_collab.UNKNOWN,
        )

    def test_hosts_without_a_measured_probe_are_never_blocked(self):
        runner, calls = self._runner(0, self.TOOLS)
        result = probe_collab.run_probe("claude", runner, Path("/h"))
        self.assertEqual(result["collab"], probe_collab.UNKNOWN)
        self.assertEqual(result["evidence"]["reason"], "no-measured-probe-for-host")
        self.assertEqual(calls, [])

    def test_missing_host_cli_is_unknown(self):
        runner, calls = self._runner(0, self.TOOLS)
        with patch.object(probe_collab, "resolve", return_value=None):
            result = probe_collab.run_probe("codex", runner, Path("/h"))
        self.assertEqual(result["collab"], probe_collab.UNKNOWN)
        self.assertEqual(result["evidence"]["reason"], "host-cli-not-on-path")
        self.assertEqual(calls, [])

    def test_flag_already_true_points_at_version_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text("[features]\nmulti_agent_v2 = true\n", encoding="utf-8")
            runner, _ = self._runner(0, "nothing")
            result = probe_collab.run_probe("codex", runner, home)
            self.assertEqual(result["codex_multi_agent_v2"], "true")
            self.assertEqual(result["collab"], probe_collab.UNAVAILABLE)
            self.assertIn("flag", result["fix"])

    def test_flag_reader_ignores_other_tables_and_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text(
                "[a.features]\nmulti_agent_v2 = 1\n\n[features]\n# multi_agent_v2 = 9\n"
                "other = true\nmulti_agent_v2 = false\n",
                encoding="utf-8",
            )
            self.assertEqual(probe_collab.multi_agent_flag(home), "false")
            self.assertIsNone(probe_collab.multi_agent_flag(home / "missing"))

    def test_host_detection_from_environment(self):
        self.assertEqual(probe_collab.detect_host({"CODEX_HOME": "/x"}), "codex")
        self.assertEqual(probe_collab.detect_host({"CLAUDECODE": "1"}), "claude")
        self.assertEqual(probe_collab.detect_host({}), "unknown")

    def test_enable_writes_then_keeps_the_change_when_tools_reappear(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text('[features]\nother = true\nmulti_agent_v2 = false\n', encoding="utf-8")
            runner, _ = self._runner(0, self.TOOLS)
            with patch.object(probe_collab, "resolve", return_value=["codex"]):
                outcome = probe_collab.enable("codex", home, runner)
            self.assertEqual(outcome["action"], probe_collab.ENABLED)
            written = config.read_text(encoding="utf-8")
            self.assertIn("multi_agent_v2 = true", written)
            self.assertIn("other = true", written)

    def test_enable_rolls_back_byte_for_byte_when_verification_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text('[features]\nmulti_agent_v2 = false\n', encoding="utf-8")
            before = config.read_bytes()
            runner, _ = self._runner(0, "nothing")
            with patch.object(probe_collab, "resolve", return_value=["codex"]):
                outcome = probe_collab.enable("codex", home, runner)
            self.assertEqual(outcome["action"], probe_collab.ROLLED_BACK)
            self.assertEqual(config.read_bytes(), before)

    def test_enable_refuses_hosts_without_a_measured_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for host in ("claude", "opencode", "qoder", "qodercn"):
                with self.subTest(host):
                    runner, calls = self._runner(0, self.TOOLS)
                    outcome = probe_collab.enable(host, home, runner)
                    self.assertEqual(outcome["action"], "refused")
                    self.assertEqual(calls, [])
            self.assertEqual(list(home.iterdir()), [])

    def test_enable_does_not_guess_a_new_flag_name_when_the_key_is_already_true(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".codex" / "config.toml"
            config.parent.mkdir()
            config.write_text("[features]\nmulti_agent_v2 = true\n", encoding="utf-8")
            before = config.read_bytes()
            runner, _ = self._runner(0, "nothing")
            with patch.object(probe_collab, "resolve", return_value=["codex"]):
                outcome = probe_collab.enable("codex", home, runner)
            self.assertEqual(outcome["action"], "noop")
            self.assertEqual(config.read_bytes(), before)

    def test_main_reports_enable_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(probe_collab, "enable", return_value={"action": "refused", "reason": "x"}):
                with redirect_stdout(StringIO()):
                    self.assertEqual(
                        probe_collab.main(["--host", "claude", "--enable", "--home", directory]), 0
                    )
            with patch.object(probe_collab, "enable", return_value={"action": probe_collab.ROLLED_BACK}):
                with redirect_stdout(StringIO()):
                    self.assertEqual(
                        probe_collab.main(["--host", "codex", "--enable", "--home", directory]), 2
                    )

    def test_main_exit_codes_and_no_writes(self):
        for verdict, output, expected in (
            (probe_collab.AVAILABLE, "spawn_agent list_agents wait_agent", 0),
            (probe_collab.UNAVAILABLE, "nothing", 1),
            # 单个名字命中不算工具面：技能正文里就会出现这个词，据此判定会静默跳过写开关。
            (probe_collab.UNAVAILABLE, "用 spawn_agent 派发子代理", 1),
        ):
            with self.subTest(verdict):
                with tempfile.TemporaryDirectory() as directory:
                    home = Path(directory)
                    cwd = home / "cwd"
                    cwd.mkdir()
                    runner, _ = self._runner(0, output)
                    previous = os.getcwd()
                    os.chdir(cwd)
                    try:
                        with patch.object(probe_collab, "subprocess_runner", runner), \
                                redirect_stdout(StringIO()):
                            self.assertEqual(
                                probe_collab.main(["--host", "codex", "--home", str(home / "nope")]),
                                expected,
                            )
                    finally:
                        os.chdir(previous)
                    self.assertEqual(list(cwd.iterdir()), [])


class GraphifyRunnerResolutionTests(unittest.TestCase):
    """Graphify runner 与 GitNexus 对称：覆盖变量 → PATH → 已知安装目录。"""

    def test_runner_override_wins_over_path(self):
        with patch.dict(
            os.environ, {"CODEKIT_GRAPHIFY_RUNNER": '"C:\\venv\\graphify.exe"'}, clear=False
        ), patch.object(prepare.shutil, "which", return_value="/usr/bin/graphify"):
            command, runner = prepare.graphify_command()
        self.assertEqual(["C:\\venv\\graphify.exe"], command)
        self.assertEqual("override", runner)

    def test_declared_install_directory_supplies_the_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            declared = Path(directory)
            target = declared / common.executable_names("graphify")[0]
            target.write_text("", encoding="utf-8")
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[declared]
            ), no_ambient_manifest(), patch.dict(
                os.environ, {"CODEKIT_GRAPHIFY_RUNNER": ""}, clear=False
            ):
                command, runner = prepare.graphify_command()
            self.assertEqual([str(target)], command)
            self.assertEqual("declared", runner)

    def test_capabilities_record_the_declared_runner_and_its_support_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declared = root / "npm"
            declared.mkdir()
            runner = declared / common.executable_names("graphify")[0]
            runner.write_text("", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [], 0, stdout="--max-workers\n--code-only\nhook install\n", stderr=""
            )
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[declared]
            ), patch.object(prepare, "run", return_value=completed), patch.object(
                prepare, "probe_graphify_ast_process_pool", return_value=True
            ), no_ambient_manifest(), patch.dict(
                os.environ, {"CODEKIT_GRAPHIFY_RUNNER": ""}, clear=False
            ):
                graphify = prepare.tool_capabilities(root)["graphify"]
            self.assertTrue(graphify["available"])
            self.assertEqual(str(runner), graphify["runner"])
            self.assertEqual("declared", graphify["runner_type"])
            self.assertEqual([str(runner)], graphify["command"])
            self.assertTrue(graphify["supports"]["max_workers"])
            self.assertTrue(graphify["supports"]["git_hooks"])

    def test_unresolved_runner_carries_an_actionable_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[]
            ), no_ambient_manifest(), patch.dict(
                os.environ, {"CODEKIT_GRAPHIFY_RUNNER": ""}, clear=False
            ):
                capabilities = prepare.tool_capabilities(root)
            graphify = capabilities["graphify"]
            self.assertFalse(graphify["available"])
            self.assertEqual("unresolved", graphify["runner_type"])
            self.assertIn("CODEKIT_GRAPHIFY_RUNNER", graphify["resolution_hint"])
            self.assertIn("CODEKIT_GITNEXUS_RUNNER", capabilities["gitnexus"]["resolution_hint"])


class KnownToolDirectoryTests(unittest.TestCase):
    """已知安装目录与入口文件名：宿主丢掉用户级 PATH 时的兜底来源。"""

    def test_windows_entry_names_prefer_directly_executable_files(self):
        if os.name != "nt":
            self.skipTest("Windows 入口命名")
        self.assertEqual(
            ["rtk.exe", "rtk.cmd", "rtk.bat", "rtk"], common.executable_names("rtk")
        )

    def test_windows_candidates_cover_the_npm_and_python_directories(self):
        if os.name != "nt":
            self.skipTest("Windows 目录布局")
        appdata = r"C:\Users\me\AppData\Roaming"
        local = r"C:\Users\me\AppData\Local"
        with patch.dict(
            os.environ, {"APPDATA": appdata, "LOCALAPPDATA": local}, clear=False
        ):
            directories = [str(path) for path in common.known_tool_dirs()]
        self.assertIn(r"C:\Users\me\AppData\Roaming\npm", directories)
        self.assertIn(r"C:\Users\me\AppData\Local\Programs\Python\Launcher", directories)

    def test_toolchain_root_candidates_come_first(self):
        # 正是不再让历史落点排在前面，受限宿主里才会优先命中工具链根内的副本。
        directories = common.known_tool_dirs()
        self.assertEqual(common.runtime.toolchain_root() / "npm", directories[0])

    def test_which_with_fallback_prefers_a_path_hit(self):
        # 新解析层会校验候选真的可读：PATH 命中一个不存在的路径不再算解析成功。
        with tempfile.TemporaryDirectory() as directory:
            on_path = Path(directory) / "python3"
            on_path.write_text("#!/bin/sh\n", encoding="utf-8")
            with patch.object(
                shutil, "which", return_value=str(on_path)
            ), patch.object(common.runtime, "toolchain_paths", return_value=[Path("/nope")]):
                self.assertEqual(str(on_path), common.which_with_fallback("python3"))

    def test_store_alias_stub_is_not_treated_as_usable(self):
        # %LOCALAPPDATA%\Microsoft\WindowsApps\python3.EXE 这类商店别名桩 stat 能过、
        # open 抛 EINVAL，是「存在但不可用」，既不是「未安装」也不是「被拒绝读取」。
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "python3"
            stub.write_text("", encoding="utf-8")
            with patch.object(
                Path, "open", side_effect=OSError(22, "Invalid argument")
            ), patch.object(shutil, "which", return_value=str(stub)), patch.object(
                common.runtime, "toolchain_paths", return_value=[]
            ):
                self.assertIsNone(common.which_with_fallback("python3"))

    def test_candidate_scan_survives_a_denied_enumeration(self):
        with patch.object(
            Path, "iterdir", side_effect=PermissionError(5, "Access is denied")
        ):
            directories = common.known_tool_dirs()
        self.assertTrue(directories)
        if os.name == "nt":
            # 写死路径的候选不依赖列目录能力，仍然可用。
            appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            self.assertIn(Path(appdata) / "npm", directories)
            local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            self.assertIn(local / "Programs" / "Python" / "Launcher", directories)
        else:
            self.assertIn(Path.home() / ".local" / "bin", directories)

    def test_empty_candidate_scan_degrades_instead_of_raising(self):
        # 候选目录扫不出任何东西只能退化成「解析失败」，不能变成内部错误。
        with patch.object(shutil, "which", return_value=None), patch.object(
            common.runtime, "toolchain_paths", return_value=[]
        ), no_ambient_manifest():
            self.assertIsNone(common.which_with_fallback("gitnexus"))
            self.assertIsNone(common.declared_tool_path("graphify"))

    def test_empty_candidate_scan_still_maps_to_a_recorded_skip(self):
        # 同上：白名单内但解析不到的程序记为可恢复的 skipped，而不是把运行拖进失败。
        with patch.object(shutil, "which", return_value=None), patch.object(
            common.runtime, "toolchain_paths", return_value=[]
        ), no_ambient_manifest():
            with self.assertRaises(common.ValidationProgramMissing):
                common.resolve_validation_command(Path("."), "python -m pytest")

    def test_hint_survives_a_denied_stat(self):
        with patch.object(
            Path, "is_dir", side_effect=PermissionError(5, "Access is denied")
        ):
            self.assertEqual("本机没有已存在的候选目录", prepare.known_tool_dir_hint())

    def test_capabilities_survive_an_empty_candidate_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[root / "absent"]
            ), no_ambient_manifest(), patch.dict(
                os.environ,
                {"CODEKIT_GITNEXUS_RUNNER": "", "CODEKIT_GRAPHIFY_RUNNER": ""},
                clear=False,
            ):
                capabilities = prepare.tool_capabilities(root)
            self.assertFalse(capabilities["gitnexus"]["available"])
            self.assertEqual("unresolved", capabilities["gitnexus"]["runner"])
            self.assertEqual(common.runtime.NOT_INSTALLED, capabilities["gitnexus"]["state"])
            self.assertFalse(capabilities["graphify"]["available"])
            self.assertEqual("unresolved", capabilities["graphify"]["runner_type"])

    def test_candidate_scan_continues_past_a_denied_candidate(self):
        # 一个候选撞到 EACCES 不能中断整轮扫描：Codex 沙箱拒绝 %APPDATA%\npm，
        # 但同机的 venv 目录可读，可读的那个必须还能被发现。
        with tempfile.TemporaryDirectory() as directory:
            denied_root = Path(directory) / "denied"
            readable = Path(directory) / "readable"
            readable.mkdir()
            target = readable / common.executable_names("graphify")[0]
            target.write_text("", encoding="utf-8")
            with denied_stat(denied_root), patch.object(
                common.runtime, "toolchain_paths", return_value=[denied_root, readable]
            ):
                path, state = common.declared_tool_scan("graphify")
                self.assertEqual(str(target), common.declared_tool_path("graphify"))
            self.assertEqual(str(target), path)
            self.assertEqual(common.runtime.OK, state)

    def test_all_absent_candidates_report_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                common.runtime, "toolchain_paths", return_value=[Path(directory)]
            ):
                self.assertEqual(
                    (None, common.runtime.NOT_INSTALLED),
                    common.declared_tool_scan("graphify"),
                )

    def test_unresolved_hint_says_installed_but_unreadable_when_denied(self):
        # 沙箱拒绝读取候选目录时，结论必须是「已安装但读不到」而不是「未安装」：
        # 后者会让用户去重装一个其实装好的工具。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            absent = root / "npm"
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[absent]
            ), denied_stat(absent), no_ambient_manifest(), patch.dict(
                os.environ,
                {"CODEKIT_GITNEXUS_RUNNER": "", "CODEKIT_GRAPHIFY_RUNNER": ""},
                clear=False,
            ):
                capabilities = prepare.tool_capabilities(root)
            for tool in ("gitnexus", "graphify"):
                hint = capabilities[tool]["resolution_hint"]
                self.assertIn("可能已安装但当前会话读不到", hint)
                self.assertIn("用安装器重跑并勾选该组件", hint)
                self.assertNotIn("确认不存在", hint)

    def test_unresolved_hint_says_absent_when_nothing_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[root / "absent"]
            ), no_ambient_manifest(), patch.dict(
                os.environ,
                {"CODEKIT_GITNEXUS_RUNNER": "", "CODEKIT_GRAPHIFY_RUNNER": ""},
                clear=False,
            ):
                capabilities = prepare.tool_capabilities(root)
            for tool in ("gitnexus", "graphify"):
                hint = capabilities[tool]["resolution_hint"]
                self.assertIn("确认不存在", hint)
                self.assertNotIn("可能已安装但当前会话读不到", hint)

    def test_resolved_but_unrunnable_runner_reports_a_hint(self):
        # 能 stat 到入口但跑不起来（沙箱拒绝读取）不是「未安装」：提示要指向重装到
        # 工具链根，且 available=False 让 gitnexus 阶段如实按 tool-missing 跳过。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broken = root / "broken-gitnexus.py"
            broken.write_text("raise SystemExit(3)\n", encoding="utf-8")
            with patch.object(prepare.shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[root]
            ), no_ambient_manifest(), patch.dict(
                os.environ,
                {
                    "CODEKIT_GITNEXUS_RUNNER": f'"{sys.executable}" "{broken}"',
                    "CODEKIT_GRAPHIFY_RUNNER": "",
                },
                clear=False,
            ):
                capabilities = prepare.tool_capabilities(root)
            gitnexus = capabilities["gitnexus"]
            self.assertFalse(gitnexus["available"])
            self.assertEqual("override", gitnexus["runner"])
            self.assertEqual(common.runtime.NOT_EXECUTABLE, gitnexus["state"])
            self.assertFalse(common.tool_runnable(gitnexus))
            self.assertIn("跑不起来", gitnexus["resolution_hint"])
            self.assertNotIn("未在 CODEKIT_GITNEXUS_RUNNER", gitnexus["resolution_hint"])

    def test_runnable_snapshot_requires_a_successful_probe(self):
        self.assertFalse(common.tool_runnable({"runner": "unresolved", "command": []}))
        self.assertFalse(common.tool_runnable({"runner": "declared", "command": [], "available": False}))
        self.assertFalse(common.tool_runnable({
            "runner": "declared", "command": ["C:/npm/gitnexus.cmd"], "available": False,
        }))
        self.assertTrue(common.tool_runnable({
            "runner": "declared", "command": ["C:/npm/gitnexus.cmd"], "available": True,
        }))
        # 早期 manifest 没有 state 这一位时按可用处理，不得因此把工具判死。
        self.assertTrue(common.tool_runnable({"runner": "override", "command": ["gitnexus"]}))
        # 实测状态非 ok 时一律不可用，哪怕 available 因为旧字段仍是真。
        self.assertFalse(common.tool_runnable({
            "runner": "declared", "command": ["C:/npm/gitnexus.cmd"],
            "available": True, "state": common.runtime.NOT_READABLE,
        }))


class ValidationCommandFallbackTests(unittest.TestCase):
    """仓库验证命令的程序解析：PATH 之外还要查已知安装目录。"""

    def test_program_is_found_in_a_declared_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            declared = Path(directory)
            program = "py" if os.name == "nt" else "python3"
            target = declared / common.executable_names(program)[0]
            target.write_text("", encoding="utf-8")
            with patch.object(shutil, "which", return_value=None), patch.object(
                common.runtime, "toolchain_paths", return_value=[declared]
            ):
                argv = common.resolve_validation_command(
                    Path(directory), f"{program} -m pytest"
                )
            self.assertEqual([str(target), "-m", "pytest"], argv)

    def test_program_missing_everywhere_still_records_a_skip(self):
        with patch.object(shutil, "which", return_value=None), patch.object(
            common.runtime, "toolchain_paths", return_value=[]
        ), no_ambient_manifest():
            with self.assertRaises(common.ValidationProgramMissing):
                common.resolve_validation_command(Path("."), "python -m pytest")


def drive_gate(
    root: Path,
    *,
    gitnexus: dict,
    graphify: dict | None = None,
    start_args: list[str] | None = None,
) -> tuple[int, dict]:
    """跑到 gate 为止，返回 ``(退出码, manifest)``。

    只驱动 start + gate：降级语义是门禁的职责，把 reconcile/finish 一起拉进来只会让
    断言淹没在无关步骤里。

    整个流程在「两个工具都解析不到」的环境下运行——那正是要复现的场景，也顺带让
    start 不再被 GitNexus 的注册硬前提拦住（本机真装了 gitnexus 时会要求先授权）。
    """
    git_repository(root)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    run_id = "gate"
    tools_absent = (
        patch.object(shutil, "which", return_value=None),
        patch.object(common.runtime, "toolchain_paths", return_value=[]),
        patch.object(common.runtime, "read_manifest", return_value={}),
        patch.dict(
            os.environ,
            {"CODEKIT_GITNEXUS_RUNNER": "", "CODEKIT_GRAPHIFY_RUNNER": ""},
            clear=False,
        ),
    )
    with tools_absent[0], tools_absent[1], tools_absent[2], tools_absent[3]:
        with redirect_stdout(StringIO()):
            assert controller.main([
                "start", "--root", str(root), "--language", "python", "--run-id", run_id,
                *(start_args or []),
            ]) == 0
        manifest_path = root / ".codekit" / "initialize" / run_id / "run-manifest.json"
        (root / ".gitnexus").mkdir(exist_ok=True)
        (root / ".gitnexus/meta.json").write_text(
            json.dumps({"root": str(root)}), encoding="utf-8"
        )
        (root / "graphify-out").mkdir(exist_ok=True)
        (root / "graphify-out/graph.json").write_text(
            json.dumps({"nodes": [{"id": "n"}]}), encoding="utf-8",
        )
        (root / "graphify-out/manifest.json").write_text(
            json.dumps({"root": str(root)}), encoding="utf-8",
        )
        (root / "graphify-out/.graphify_root").write_text(str(root), encoding="utf-8")

        paths: list[Path] = []
        gitnexus = dict(gitnexus)
        gitnexus.setdefault("changed_files", [".gitnexus/meta.json"])
        gitnexus.setdefault("allowed_paths", [".gitnexus/"])
        gitnexus.setdefault(
            "artifacts", [{"path": ".gitnexus/meta.json", "validation": "json-valid"}]
        )
        gitnexus.setdefault("run_id", run_id)
        for payload, name in (
            (gitnexus, "gitnexus.json"),
            (
                graphify
                or worker_result(
                    "graphify", run_id=run_id,
                    changed=[
                        "graphify-out/graph.json",
                        "graphify-out/manifest.json",
                        "graphify-out/.graphify_root",
                    ],
                    allowed=["graphify-out/"],
                    artifacts=[
                        {"path": "graphify-out/graph.json", "validation": "json-nonempty-nodes"},
                        {"path": "graphify-out/manifest.json", "validation": "json-valid"},
                        {"path": "graphify-out/.graphify_root", "validation": "root-match"},
                    ],
                ),
                "graphify.json",
            ),
            (worker_result("context-scanner", run_id=run_id), "context-scanner.json"),
        ):
            path = manifest_path.parent / name
            path.write_text(json.dumps(payload), encoding="utf-8")
            paths.append(path)
        with redirect_stdout(StringIO()):
            code = controller.main([
                "gate", "--manifest", str(manifest_path),
                *[part for path in paths for part in ("--result", str(path))],
            ])
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    return code, document


def skipped_tool_result(task: str, *, code: str = "tool-missing", hint: str = "分辨率提示") -> dict:
    """一个工具不可用导致的 skipped 结果，形状与控制器实际写出的完全一致。"""
    result = worker_result(task, run_id="gate")
    result.update(
        {
            "status": "skipped",
            "command": [],
            "exit_code": None,
            "warnings": [hint],
            "error": {"code": code, "message": "runner 不可用"},
        }
    )
    return result


class ToolDegradationGateTests(unittest.TestCase):
    """工具不可用造成的 partial：自动降级跑完，不再要求 --allow-partial 二次运行。

    这是用户反复重跑的直接来源：barrier 判 partial → 未传 --allow-partial 就 abort 释放
    锁 → 只能重跑一次。工具不可用是环境事实，不是需要用户授权的降级。
    """

    def test_tool_missing_keeps_the_run_gated_instead_of_aborting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest = drive_gate(
                root,
                gitnexus=skipped_tool_result("gitnexus"),
                graphify=skipped_tool_result("graphify", hint="graphify 分辨率提示"),
            )
            self.assertEqual(0, code, manifest.get("worker_gate"))
            self.assertEqual("partial", manifest["worker_gate"]["status"])
            self.assertEqual([], manifest["worker_gate"]["errors"])
            self.assertEqual("gated", manifest["phase"])
            self.assertTrue(manifest["worker_gate"]["tool_only_degradation"])
            self.assertEqual(
                {"gitnexus", "graphify"},
                {item["task"] for item in manifest["worker_gate"]["degradations"]},
            )
            # 报告要能逐条点名，附上原文提示。
            hint_by_task = {
                item["task"]: item["hint"] for item in manifest["worker_gate"]["degradations"]
            }
            self.assertEqual("graphify 分辨率提示", hint_by_task["graphify"])
            # 锁必须保留：降级运行要继续走 reconcile/finish，释放锁才是逼用户重跑的那一步。
            self.assertTrue(Path(manifest["lock_path"]).is_file())

    def test_registry_missing_alone_also_degrades(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gitnexus = skipped_tool_result("gitnexus", code="registry-missing")
            gitnexus["status"] = "partial"
            code, manifest = drive_gate(root, gitnexus=gitnexus)
            self.assertEqual(0, code, manifest.get("worker_gate"))
            self.assertEqual("gated", manifest["phase"])
            self.assertEqual("partial", manifest["worker_gate"]["status"])

    def test_a_real_failure_alongside_a_degradation_still_needs_consent(self):
        # Graphify chunk 失败是真实失败，不能被「同时有个工具缺失」赦免。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graphify = worker_result(
                "graphify", run_id="gate",
                changed=[
                    "graphify-out/graph.json",
                    "graphify-out/manifest.json",
                    "graphify-out/.graphify_root",
                ],
                allowed=["graphify-out/"],
                artifacts=[
                    {"path": "graphify-out/graph.json", "validation": "json-nonempty-nodes"},
                    {"path": "graphify-out/manifest.json", "validation": "json-valid"},
                    {"path": "graphify-out/.graphify_root", "validation": "root-match"},
                ],
            )
            graphify.update(
                {
                    "status": "partial",
                    "error": {"code": "chunk-failures", "message": "过半 chunk 失败"},
                }
            )
            code, manifest = drive_gate(
                root, gitnexus=skipped_tool_result("gitnexus"), graphify=graphify
            )
            self.assertNotEqual(0, code)
            self.assertEqual("aborted", manifest["phase"])
            self.assertFalse(manifest["worker_gate"]["tool_only_degradation"])
            self.assertIn("--allow-partial", " ".join(manifest["worker_gate"]["errors"]))

    def test_strict_tools_restores_the_fail_closed_behaviour(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest = drive_gate(
                root,
                gitnexus=skipped_tool_result("gitnexus"),
                start_args=["--strict-tools"],
            )
            self.assertNotEqual(0, code)
            self.assertEqual("aborted", manifest["phase"])
            self.assertIn("--allow-partial", " ".join(manifest["worker_gate"]["errors"]))


if __name__ == "__main__":
    unittest.main()
