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
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))


def load_script(name: str):
    path = SKILL_DIR / "scripts" / name
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
common = sys.modules["_common"]


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


def drive_initialize_flow(
    root: Path, context: dict, *, run_id: str = "flow", commit: bool = False,
    before_finish=None, finish_args: list[str] | None = None,
) -> tuple[int, dict]:
    """Run start, gate, reconcile and finish, returning the exit code and final report."""
    git_repository(root)
    for name, body in {
        "README.md": "fixture\n",
        "AGENTS.md": "user agents\n",
        "CLAUDE.md": "user claude\n",
        "check_ok.py": "raise SystemExit(0)\n",
        "check_bad.py": "raise SystemExit(3)\n",
    }.items():
        (root / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)

    start = ["start", "--root", str(root), "--language", "python", "--run-id", run_id]
    if commit:
        start.append("--commit")
    with redirect_stdout(StringIO()):
        assert controller.main(start) == 0
    manifest = root / ".codekit" / "initialize" / run_id / "run-manifest.json"

    (root / ".gitnexus").mkdir()
    (root / ".gitnexus/meta.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
    (root / "graphify-out").mkdir()
    (root / "graphify-out/graph.json").write_text(
        json.dumps({"nodes": [{"id": "n"}]}), encoding="utf-8",
    )
    (root / "graphify-out/manifest.json").write_text(json.dumps({"root": str(root)}), encoding="utf-8")
    (root / "graphify-out/.graphify_root").write_text(str(root), encoding="utf-8")

    results = [
        worker_result(
            "gitnexus", run_id=run_id,
            changed=[".gitnexus/meta.json"], allowed=[".gitnexus/"],
            artifacts=[{"path": ".gitnexus/meta.json", "validation": "json-valid"}],
        ),
        worker_result(
            "graphify", run_id=run_id,
            changed=["graphify-out/graph.json", "graphify-out/manifest.json", "graphify-out/.graphify_root"],
            allowed=["graphify-out/"],
            artifacts=[
                {"path": "graphify-out/graph.json", "validation": "json-nonempty-nodes"},
                {"path": "graphify-out/manifest.json", "validation": "json-valid"},
                {"path": "graphify-out/.graphify_root", "validation": "root-match"},
            ],
        ),
        worker_result("context-scanner", run_id=run_id),
    ]
    result_paths = []
    for result in results:
        path = manifest.parent / f"{result['task']}.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        result_paths.append(path)
    context_path = manifest.parent / "context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")

    argv = ["--manifest", str(manifest)] + list(finish_args or [])
    if before_finish is not None:
        argv += list(before_finish(manifest.parent))
    with redirect_stdout(StringIO()):
        assert controller.main([
            "gate", "--manifest", str(manifest),
            *[part for path in result_paths for part in ("--result", str(path))],
        ]) == 0
        assert controller.main([
            "reconcile", "--manifest", str(manifest), "--context", str(context_path),
            *[part for path in result_paths for part in ("--worker-result", str(path))],
        ]) == 0
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

    def test_gitnexus_skill_directories_are_added_to_managed_gitignore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / ".claude/skills/gitnexus-cli"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("generated\n", encoding="utf-8")
            ignore = root / ".gitignore"
            ignore.write_text(
                "user-rule\n"
                "# codekit:initialize:ignore:start\n"
                ".codekit/\n.gitnexus/\ngraphify-out/\n.graphifyignore\n"
                "# codekit:initialize:ignore:end\n",
                encoding="utf-8",
            )
            result = worker_result(
                "gitnexus",
                changed=[".claude/skills/gitnexus-cli/SKILL.md"],
                allowed=[".claude/skills/gitnexus-cli/"],
            )
            self.assertTrue(reconcile.update_gitignore_for_skills(root, result))
            updated = ignore.read_text(encoding="utf-8")
            self.assertIn(".claude/skills/gitnexus-cli/", updated)
            self.assertNotIn("\n.claude/\n", updated)
            self.assertNotIn("\n.agents/\n", updated)
            self.assertIn("user-rule\n", updated)
            self.assertFalse(reconcile.update_gitignore_for_skills(root, result))

    def test_language_validation_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(prepare.PrepareError):
                prepare.parse_languages("python,python", Path(directory))


class GraphifyCapabilityTests(unittest.TestCase):
    def test_graphify_capabilities_detect_flags_and_ast_process_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "graphify"
            runner.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "if sys.argv[1:] == ['--version']:\n"
                "    print('graphify 1.0')\n"
                "elif sys.argv[1:] == ['--help']:\n"
                "    print('extract --max-workers N')\n"
                "elif sys.argv[1:] == ['extract', '--help']:\n"
                "    print('--code-only --max-workers')\n",
                encoding="utf-8",
            )
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
            os.symlink(outside, root / ".standard", target_is_directory=True)
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
            os.symlink(outside, root / ".standard/02-stack/evil", target_is_directory=True)
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
            with redirect_stdout(StringIO()):
                self.assertEqual(0, controller.main([
                    "start", "--root", str(root), "--language", "python", "--run-id", "repairable"
                ]))
            manifest_path = root / ".codekit/initialize/repairable/run-manifest.json"
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
            with redirect_stdout(StringIO()):
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
        with git_scratch_directory() as directory:
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
            context_path.write_text(json.dumps({
                "schema_version": 1,
                "root": str(root),
                "overview": "Test repository.",
                "structure": [{"path": "README.md", "description": "Fixture documentation"}],
                "commands": [],
                "evidence": [{"path": "README.md"}],
                "warnings": [],
            }), encoding="utf-8")

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
            self.assertIn("user agents", (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertIn("codekit:initialize:project-context:start", (root / "AGENTS.md").read_text(encoding="utf-8"))

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
                "commands": [],
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


@unittest.skipIf(shutil.which("python3") is None, "python3 is an allowed validation program")
class ValidationExecutionTests(unittest.TestCase):
    """Commit evidence must come from commands the controller executed itself."""

    def test_controller_executes_required_validations(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(root, scan_context(root, [
                {"purpose": "Check", "command": "python3 check_ok.py",
                 "evidence": "check_ok.py", "required_for_initialize": True},
            ]))
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("executed", report["validation_source"])
            self.assertEqual("python3 check_ok.py", report["validations"][0]["command"])
            self.assertEqual(0, report["validations"][0]["exit_code"])
            self.assertEqual(1, commit_count(root))

    def test_successful_validations_allow_controlled_commit(self):
        with git_scratch_directory() as directory:
            root = Path(directory)
            code, report = drive_initialize_flow(root, scan_context(root, [
                {"purpose": "Check", "command": "python3 check_ok.py",
                 "evidence": "check_ok.py", "required_for_initialize": True},
            ]), commit=True)
            self.assertEqual(0, code, report["errors"])
            self.assertEqual("committed", report["commit_status"])
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
                {"purpose": "Check", "command": "python3 check_bad.py",
                 "evidence": "check_bad.py", "required_for_initialize": True},
            ]), commit=True)
            self.assertEqual(1, code)
            self.assertIn("repository validation failed: python3 check_bad.py", report["errors"])
            self.assertFalse(report["commit_allowed"])
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
            self.assertEqual(
                [shutil.which("python3"), "check_ok.py"],
                validator.resolve_validation_command(root, "python3 check_ok.py"),
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


class AtomicWriteModeTests(unittest.TestCase):
    """Managed files must not silently lose their permission bits."""

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
        with self.assertRaisesRegex(reconcile.ReconcileError, "evidence"):
            reconcile.validate_context(context, self.root)

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


if __name__ == "__main__":
    unittest.main()
