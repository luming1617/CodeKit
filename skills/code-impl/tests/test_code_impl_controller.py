from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

# os.chmod on Windows only maps the read-only bit, so st_mode never carries 0o600.
POSIX_MODES = os.name != "nt"


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "skills" / "code-impl" / "scripts" / "code_impl_controller.py"
SPEC = importlib.util.spec_from_file_location("code_impl_controller", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(
        f"cannot load code_impl_controller from {MODULE_PATH}: "
        "spec or loader is None (file missing or not importable)"
    )
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例都要在一个临时 git 仓库里跑。`gc --auto` 与部分 git 扩展会在 commit 后异步向
        # `.git/` 写入，撞上 `cleanup()` 就变成 ENOTEMPTY 假失败（与 code-impl 脚本无关），
        # 因此关闭自动 gc 并容忍清理错误，与 skills/initialize 的 fixture 保持一致。
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "CodeKit Test")
        git(self.repo, "config", "gc.auto", "0")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "base")
        self.feature = self.repo / "docs" / "feature"
        self.feature.mkdir(parents=True)
        for name, content in {
            "product-doc.md": "# PRD\nThe product requirement is explicit and testable.\n",
            "technical-doc.md": "# TDD\nThe technical design defines the implementation boundary.\n",
            "release-doc.md": "# RPD\nThe release plan defines validation and rollback steps.\n",
        }.items():
            (self.feature / name).write_text(content, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_init_freezes_all_documents_and_preexisting_changes(self) -> None:
        (self.repo / "README.md").write_text("preexisting\n", encoding="utf-8")
        state_path = controller.init_run(self.repo, self.feature, "run-1")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(set(state["materials"]), {"product_requirements", "technical_design", "release_plan"})
        self.assertEqual(state["preexisting_snapshot"]["files"][0]["path"], "README.md")

    def test_scope_blocks_preexisting_file_mutation_and_unapproved_file(self) -> None:
        (self.repo / "README.md").write_text("preexisting\n", encoding="utf-8")
        state_path = controller.init_run(self.repo, self.feature, "run-2")
        (self.repo / "README.md").write_text("overwritten\n", encoding="utf-8")
        (self.repo / "secret.txt").write_text("unexpected\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        result = controller.check_scope(state_path, "T01")
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(result["preexisting_modified"], ["README.md"])
        self.assertEqual(result["unauthorized_files"], ["secret.txt"])
        self.assertEqual(result["task_id"], "T01")

    def test_scope_passes_for_new_allowed_file(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-3")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        result = controller.check_scope(state_path, "T01")
        self.assertEqual(result["result"], "PASS")

    def test_feature_directory_cannot_escape_repository(self) -> None:
        with self.assertRaises(controller.ControllerError):
            controller.init_run(self.repo, self.repo.parent / "outside", "run-escape")

    def test_required_document_cannot_be_a_symlink(self) -> None:
        target = self.repo / "outside-prd.md"
        target.write_text("# PRD\nThis content is outside the feature directory.\n", encoding="utf-8")
        (self.feature / "product-doc.md").unlink()
        try:
            (self.feature / "product-doc.md").symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(controller.ControllerError):
            controller.init_run(self.repo, self.feature, "run-symlink")

    def test_overall_failure_without_mapping_is_blocked(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-4")
        event = controller.record_failure(state_path, "overall", [], "CODE_OR_TEST", "pytest::x::AssertionError")
        self.assertEqual(event["resolution"], "BLOCKED_MISSING_TASK_MAPPING")
        self.assertEqual(event["disposition"], "human_intervention")

    def test_overall_failure_with_multiple_tasks_requires_replan(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-5")
        controller.record_task(state_path, "T01", ["src/one.py"])
        controller.record_task(state_path, "T02", ["src/two.py"])
        event = controller.record_failure(state_path, "overall", ["T01", "T02"], "CODE_OR_TEST", "pytest::x::AssertionError")
        self.assertEqual(event["resolution"], "REPLAN_MULTI_TASK_IMPACT")
        self.assertEqual(event["disposition"], "replan")

    def test_agent_registration_and_review_freeze_use_recorded_scope(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-6")
        controller.record_agent(state_path, "impl-planner", "impl-planner", "session-1", "digest-1", "DONE")
        controller.record_agent(state_path, "impl-planner", "impl-planner", "session-1", "digest-1", "ARCHIVED")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["agents"][0]["status"], "ARCHIVED")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        self.assertEqual(controller.check_scope(state_path, "T01")["result"], "PASS")
        snapshot = controller.freeze_review(state_path)
        self.assertEqual(snapshot["changed_files"], ["src/app.py"])
        self.assertTrue((state_path.parent / "review-snapshot.json").is_file())

    def test_review_freeze_rejects_file_outside_recorded_task_scope(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-7")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.check_scope(state_path, "T01")
        (self.repo / "other.py").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaises(controller.ControllerError):
            controller.freeze_review(state_path)

    def test_input_contract_requires_prd_and_task_contract_requires_feature_directory(self) -> None:
        with self.assertRaises(controller.ControllerError):
            controller.validate_contract({"sources": {}}, "input")
        with self.assertRaises(controller.ControllerError):
            controller.validate_contract({"repo_root": str(self.repo)}, "task-contract")

    def test_failure_cannot_reference_unregistered_task(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-8")
        with self.assertRaises(controller.ControllerError):
            controller.record_failure(state_path, "task", ["T404"], "CODE_OR_TEST", "pytest::x::AssertionError")

    # ── P0-2: authorized preexisting-file changes must not block ─────────────

    def test_authorized_preexisting_file_change_not_blocked(self) -> None:
        """P0-2: when a preexisting dirty file is in allowed_files, scope PASSes."""
        (self.repo / "README.md").write_text("preexisting\n", encoding="utf-8")
        state_path = controller.init_run(self.repo, self.feature, "run-p02")
        (self.repo / "README.md").write_text("task-modified\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["README.md"])
        result = controller.check_scope(state_path, "T01")
        self.assertEqual(result["result"], "PASS")
        self.assertEqual(result["preexisting_modified"], [])
        self.assertIn("README.md", result["authorized_overlaps"])

    def test_unauthorized_preexisting_file_still_blocks(self) -> None:
        """P0-2 negative: out-of-scope preexisting modification still BLOCKED."""
        (self.repo / "README.md").write_text("preexisting\n", encoding="utf-8")
        state_path = controller.init_run(self.repo, self.feature, "run-p02neg")
        (self.repo / "README.md").write_text("overwritten\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])  # README NOT in scope
        result = controller.check_scope(state_path, "T01")
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(result["preexisting_modified"], ["README.md"])

    # ── P0-1: task state machine ─────────────────────────────────────────────

    def test_task_state_machine_valid_full_path(self) -> None:
        """P0-1: PENDING→READY→RUNNING→VALIDATING→PASS is accepted."""
        state_path = controller.init_run(self.repo, self.feature, "run-sm1")
        controller.record_task(state_path, "T01", ["src/app.py"])
        for status in ["READY", "RUNNING", "VALIDATING", "PASS"]:
            task = controller.update_task(state_path, "T01", status)
            self.assertEqual(task["status"], status)

    def test_task_state_machine_skip_intermediate_rejected(self) -> None:
        """P0-1: PENDING→PASS is rejected (skips intermediate states)."""
        state_path = controller.init_run(self.repo, self.feature, "run-sm2")
        controller.record_task(state_path, "T01", ["src/app.py"])
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.update_task(state_path, "T01", "PASS")
        self.assertIn("PENDING", str(ctx.exception))

    def test_task_state_machine_terminal_state_rejects_all(self) -> None:
        """P0-1: PASS is terminal; no further transitions allowed."""
        state_path = controller.init_run(self.repo, self.feature, "run-sm3")
        controller.record_task(state_path, "T01", ["src/app.py"])
        for s in ["READY", "RUNNING", "VALIDATING", "PASS"]:
            controller.update_task(state_path, "T01", s)
        with self.assertRaises(controller.ControllerError):
            controller.update_task(state_path, "T01", "RUNNING")

    def test_task_state_machine_failed_retry_allowed(self) -> None:
        """P0-1: FAILED→RUNNING (retry) is a valid transition."""
        state_path = controller.init_run(self.repo, self.feature, "run-sm4")
        controller.record_task(state_path, "T01", ["src/app.py"])
        for s in ["READY", "RUNNING", "FAILED"]:
            controller.update_task(state_path, "T01", s)
        task = controller.update_task(state_path, "T01", "RUNNING")
        self.assertEqual(task["status"], "RUNNING")

    # ── P1-3: replan ────────────────────────────────────────────────────────

    def test_replan_supersedes_pending_and_ready_tasks(self) -> None:
        """P1-3: replan moves PENDING/READY tasks to SUPERSEDED."""
        state_path = controller.init_run(self.repo, self.feature, "run-rp1")
        controller.record_task(state_path, "T01", ["src/a.py"], plan_version=1)
        controller.record_task(state_path, "T02", ["src/b.py"])  # default pv=1
        result = controller.replan(state_path, 2)
        self.assertIn("T01", result["superseded_tasks"])
        self.assertIn("T02", result["superseded_tasks"])
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(after["current_plan_version"], 2)

    def test_replan_preserves_pass_task_files_in_authorized(self) -> None:
        """P1-3: PASS tasks survive replan and their files stay authorized."""
        state_path = controller.init_run(self.repo, self.feature, "run-rp2")
        controller.record_task(state_path, "T01", ["src/passed.py"], plan_version=1)
        controller.record_task(state_path, "T02", ["src/pending.py"], plan_version=1)
        # Drive T01 to PASS
        for s in ["READY", "RUNNING", "VALIDATING", "PASS"]:
            controller.update_task(state_path, "T01", s)
        controller.replan(state_path, 2)
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIn("src/passed.py", after["authorized_files"])
        self.assertNotIn("src/pending.py", after["authorized_files"])

    def test_replan_rejects_non_increasing_version(self) -> None:
        """P1-3: replan to same or lower version is rejected."""
        state_path = controller.init_run(self.repo, self.feature, "run-rp3")
        with self.assertRaises(controller.ControllerError):
            controller.replan(state_path, 1)  # same as default

    # ── P1-1: validate_contract for task-dag ─────────────────────────────────

    def test_validate_task_dag_rejects_cycle(self) -> None:
        payload = {
            "tasks": [
                {"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": ["T2"]},
                {"id": "T2", "objective": "y", "allowed_write_files": ["b.py"], "depends_on": ["T1"]},
            ],
            "requirement_trace": [
                {"source": "product_requirements", "requirement": "r", "covered_by": ["T1"]}
            ],
            "unresolved": [],
        }
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "task-dag")
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_validate_task_dag_rejects_unknown_dep(self) -> None:
        payload = {
            "tasks": [
                {"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": ["T404"]},
            ],
            "requirement_trace": [
                {"source": "product_requirements", "requirement": "r", "covered_by": ["T1"]}
            ],
            "unresolved": [],
        }
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "task-dag")
        self.assertIn("T404", str(ctx.exception))

    def test_validate_task_dag_rejects_nonempty_unresolved(self) -> None:
        payload = {
            "tasks": [
                {"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": []},
            ],
            "requirement_trace": [
                {"source": "product_requirements", "requirement": "r", "covered_by": ["T1"]}
            ],
            "unresolved": ["missing build command"],
        }
        with self.assertRaises(controller.ControllerError):
            controller.validate_contract(payload, "task-dag")

    def test_validate_task_dag_accepts_valid_plan(self) -> None:
        payload = {
            "tasks": [
                {"id": "T1", "objective": "base", "allowed_write_files": ["a.py"], "depends_on": []},
                {"id": "T2", "objective": "dep", "allowed_write_files": ["b.py"], "depends_on": ["T1"]},
            ],
            "requirement_trace": [
                {"source": "product_requirements", "requirement": "r1", "covered_by": ["T1", "T2"]}
            ],
            "unresolved": [],
        }
        controller.validate_contract(payload, "task-dag")  # must not raise

    # ── P1-1: validate_contract for handoff ──────────────────────────────────

    def _valid_handoff(self) -> dict:
        return {
            "review_mode": "FULL",
            "review_input": {"materials": [
                {"kind": "PRD", "access": "available"},
                {"kind": "TDD", "access": "available"},
                {"kind": "RPD", "access": "available"},
            ]},
            "snapshot": {"base_sha": "abc", "head_sha": "def", "diff_digest": "ghi"},
        }

    def test_validate_handoff_rejects_non_full_mode(self) -> None:
        payload = self._valid_handoff()
        payload["review_mode"] = "PARTIAL"
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "handoff")
        self.assertIn("FULL", str(ctx.exception))

    def test_validate_handoff_requires_all_docs_available(self) -> None:
        payload = self._valid_handoff()
        for m in payload["review_input"]["materials"]:
            if m["kind"] == "TDD":
                m["access"] = "unavailable"
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "handoff")
        self.assertIn("TDD", str(ctx.exception))

    def test_validate_handoff_accepts_valid_snapshot(self) -> None:
        controller.validate_contract(self._valid_handoff(), "handoff")  # must not raise

    # ── P2-1: deleted files reported separately from new files ───────────────

    def test_scope_reports_deleted_files_separately(self) -> None:
        """P2-1: a deleted file appears in deleted_files, not new_files."""
        # Add a tracked file BEFORE init so it is NOT in preexisting_snapshot.
        (self.repo / "to-delete.py").write_text("content\n", encoding="utf-8")
        git(self.repo, "add", "to-delete.py")
        git(self.repo, "commit", "-qm", "add to-delete")
        state_path = controller.init_run(self.repo, self.feature, "run-del")
        (self.repo / "to-delete.py").unlink()
        (self.repo / "new_file.py").write_text("new\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["to-delete.py", "new_file.py"])
        result = controller.check_scope(state_path, "T01")
        self.assertEqual(result["result"], "PASS")
        self.assertIn("to-delete.py", result["deleted_files"])
        self.assertIn("new_file.py", result["new_files"])
        self.assertNotIn("to-delete.py", result["new_files"])

    # ── 2.1 quality fixes: error-message regression guards ─────────────────

    def test_git_failure_raises_controller_error_with_stderr(self) -> None:
        """_git wraps CalledProcessError into ControllerError with actionable msg."""
        with self.assertRaises(controller.ControllerError) as ctx:
            controller._git(self.repo, "rev-parse", "NONEXISTENT_REF_XYZ")
        message = str(ctx.exception)
        self.assertIn("git rev-parse", message)
        self.assertIn("exit", message)
        # stderr excerpt must appear (git prints something like "fatal: ...").
        self.assertTrue(
            "fatal" in message.lower() or "unknown" in message.lower() or "bad" in message.lower(),
            f"expected git stderr excerpt in message, got: {message}",
        )

    def test_require_repo_root_rejects_missing_directory(self) -> None:
        """_require_repo_root raises ControllerError on non-existent path."""
        missing = self.repo / "does-not-exist"
        with self.assertRaises(controller.ControllerError) as ctx:
            controller._require_repo_root(missing)
        self.assertIn("repository root is not a directory", str(ctx.exception))

    def test_require_repo_root_rejects_regular_file(self) -> None:
        """_require_repo_root raises when the path exists but is not a directory."""
        with self.assertRaises(controller.ControllerError):
            controller._require_repo_root(self.repo / "README.md")

    @unittest.skipUnless(POSIX_MODES, "os.chmod on Windows only maps the read-only bit")
    def test_atomic_json_writes_with_restricted_mode(self) -> None:
        """_atomic_json sets 0o600 by default so state files are private."""
        target = self.repo / ".codekit" / "probe.json"
        controller._atomic_json(target, {"hello": "world"})
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"expected 0o600, got {oct(mode)}")

    @unittest.skipUnless(POSIX_MODES, "os.chmod on Windows only maps the read-only bit")
    def test_atomic_json_honours_explicit_mode(self) -> None:
        target = self.repo / ".codekit" / "probe2.json"
        controller._atomic_json(target, {"a": 1}, mode=0o644)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    # ── HIGH-1: mechanical retry and review-freeze budgets ─────────────────────

    def test_fingerprint_attempt_cap_forces_human_intervention(self) -> None:
        """HIGH-1: 4 occurrences of a fingerprint still retry; the 5th is ATTEMPT_CAP."""
        state_path = controller.init_run(self.repo, self.feature, "run-cap1")
        controller.record_task(state_path, "T01", ["src/a.py"])
        fp = "pytest::t::AssertionError"
        for n in range(1, controller.MAX_FINGERPRINT_ATTEMPTS + 1):
            event = controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", fp)
            self.assertEqual(event["disposition"], "retry_implementation")
            self.assertEqual(event["fingerprint_occurrences"], n)
        capped = controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", fp)
        self.assertEqual(capped["disposition"], "human_intervention")
        self.assertEqual(capped["resolution"], "ATTEMPT_CAP")
        self.assertEqual(capped["fingerprint_occurrences"], controller.MAX_FINGERPRINT_ATTEMPTS + 1)

    def test_fingerprint_cap_is_per_fingerprint(self) -> None:
        """A new error fingerprint restarts the retry budget."""
        state_path = controller.init_run(self.repo, self.feature, "run-cap2")
        controller.record_task(state_path, "T01", ["src/a.py"])
        for _ in range(controller.MAX_FINGERPRINT_ATTEMPTS):
            controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", "pytest::old::fail")
        event = controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", "pytest::new::fail")
        self.assertEqual(event["disposition"], "retry_implementation")
        self.assertEqual(event["fingerprint_occurrences"], 1)

    def test_missing_task_mapping_precedes_attempt_cap(self) -> None:
        """Unmappable overall failures keep BLOCKED_MISSING_TASK_MAPPING precedence."""
        state_path = controller.init_run(self.repo, self.feature, "run-cap3")
        fp = "pytest::overall::fail"
        for _ in range(controller.MAX_FINGERPRINT_ATTEMPTS + 2):
            event = controller.record_failure(state_path, "overall", [], "CODE_OR_TEST", fp)
            self.assertEqual(event["resolution"], "BLOCKED_MISSING_TASK_MAPPING")

    def test_record_failure_rejects_empty_fingerprint(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-cap4")
        with self.assertRaises(controller.ControllerError):
            controller.record_failure(state_path, "overall", [], "CODE_OR_TEST", "   ")

    def test_freeze_review_capped_at_max_freezes(self) -> None:
        """HIGH-1: initial freeze + 2 repair rounds; the 4th freeze raises."""
        state_path = controller.init_run(self.repo, self.feature, "run-fzc")
        for _ in range(controller.MAX_REVIEW_FREEZES):
            controller.freeze_review(state_path)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.freeze_review(state_path)
        self.assertIn("review freeze limit", str(ctx.exception))
        self.assertIn("human intervention", str(ctx.exception))

    def test_freeze_review_state_keeps_compact_snapshot_reference(self) -> None:
        """state.json stores only a reference; the bulk stays in the artifact file."""
        state_path = controller.init_run(self.repo, self.feature, "run-fzs")
        controller.freeze_review(state_path)
        entry = json.loads(state_path.read_text(encoding="utf-8"))["review_snapshots"][-1]
        self.assertEqual(
            set(entry),
            {"path", "base_sha", "head_sha", "diff_digest", "frozen_at"},
        )
        artifact = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
        self.assertIn("preexisting_diff_snapshot", artifact)

    # ── HIGH-2: scope comes from the ledger, never from the caller ─────────────

    def test_check_scope_requires_registered_task(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-tb1")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.check_scope(state_path, "T404")
        self.assertIn("record-task", str(ctx.exception))

    def test_check_scope_no_longer_widens_authorized_files(self) -> None:
        """The old permanent-authorization side effect (HIGH-2) must be gone."""
        state_path = controller.init_run(self.repo, self.feature, "run-tb2")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        self.assertEqual(controller.check_scope(state_path, "T01")["result"], "PASS")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["authorized_files"], [])

    def test_check_scope_allows_files_from_earlier_pass_task(self) -> None:
        """Serial worktree: cumulative diff is fine once the owning task PASSED."""
        state_path = controller.init_run(self.repo, self.feature, "run-tb3")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("a\n", encoding="utf-8")
        (self.repo / "src" / "b.py").write_text("b\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/a.py"])
        controller.record_task(state_path, "T02", ["src/b.py"])
        for status in ["READY", "RUNNING", "VALIDATING", "PASS"]:
            controller.update_task(state_path, "T01", status)
        self.assertEqual(controller.check_scope(state_path, "T02")["result"], "PASS")

    def test_check_scope_blocks_file_owned_by_unrun_task(self) -> None:
        """Files registered to a not-yet-PASS task are still out of scope."""
        state_path = controller.init_run(self.repo, self.feature, "run-tb4")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("a\n", encoding="utf-8")
        (self.repo / "src" / "b.py").write_text("b\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/a.py"])
        controller.record_task(state_path, "T02", ["src/b.py"])
        result = controller.check_scope(state_path, "T01")
        self.assertEqual(result["result"], "BLOCKED")
        self.assertEqual(result["unauthorized_files"], ["src/b.py"])

    def test_expand_scope_widens_task_and_records_audit(self) -> None:
        """NEEDS_SCOPE flow: register the extra file with a reason, then PASS."""
        state_path = controller.init_run(self.repo, self.feature, "run-ex1")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.repo / "src" / "extra.py").write_text("extra\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.update_task(state_path, "T01", "READY")
        controller.update_task(state_path, "T01", "RUNNING")
        self.assertEqual(controller.check_scope(state_path, "T01")["result"], "BLOCKED")
        outcome = controller.expand_scope(state_path, "T01", ["src/extra.py"], "Implementer returned NEEDS_SCOPE")
        self.assertEqual(outcome["added_files"], ["src/extra.py"])
        self.assertEqual(controller.check_scope(state_path, "T01")["result"], "PASS")
        task = json.loads(state_path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual(task["scope_expansions"][0]["reason"], "Implementer returned NEEDS_SCOPE")
        self.assertEqual(task["scope_expansions"][0]["added_files"], ["src/extra.py"])

    def test_expand_scope_rejects_terminal_task(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-ex2")
        controller.record_task(state_path, "T01", ["src/a.py"])
        for status in ["READY", "RUNNING", "VALIDATING", "PASS"]:
            controller.update_task(state_path, "T01", status)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.expand_scope(state_path, "T01", ["src/b.py"], "too late")
        self.assertIn("terminal", str(ctx.exception))

    def test_expand_scope_requires_reason(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-ex3")
        controller.record_task(state_path, "T01", ["src/a.py"])
        with self.assertRaises(controller.ControllerError):
            controller.expand_scope(state_path, "T01", ["src/b.py"], "   ")

    # ── composite dispatch-task / settle-task commands ─────────────────────────

    def test_dispatch_task_pending_to_running_with_agent(self) -> None:
        """One call replaces PENDING→READY→RUNNING plus record-agent."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp1")
        controller.record_task(state_path, "T01", ["src/app.py"])
        outcome = controller.dispatch_task(state_path, "T01", "session-impl-1", "digest-t01")
        self.assertEqual(outcome["status"], "RUNNING")
        self.assertEqual(outcome["attempts"], 1)
        self.assertEqual(outcome["agent"], {
            "session_id": "session-impl-1", "role": "impl-implementer", "status": "RUNNING",
        })
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"][0]["status"], "RUNNING")
        self.assertEqual(state["agents"][0]["input_digest"], "digest-t01")

    def test_dispatch_task_retry_from_failed(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-dp2")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        controller.settle_task(state_path, "T01", "FAILED")
        outcome = controller.dispatch_task(state_path, "T01", "session-2", "digest-2")
        self.assertEqual(outcome["status"], "RUNNING")
        self.assertEqual(outcome["attempts"], 2)

    def test_dispatch_task_rejects_non_dispatchable_status(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-dp3")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.dispatch_task(state_path, "T01", "session-2", "digest-2")
        self.assertIn("PENDING or FAILED", str(ctx.exception))

    def test_dispatch_task_rejects_unregistered_task(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-dp4")
        with self.assertRaises(controller.ControllerError):
            controller.dispatch_task(state_path, "T404", "session-1", "digest-1")

    def test_settle_task_pass_blocked_by_scope_stays_validating(self) -> None:
        """An out-of-scope write can never be settled as PASS."""
        state_path = controller.init_run(self.repo, self.feature, "run-st1")
        controller.record_task(state_path, "T01", ["src/app.py"])
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.repo / "stray.py").write_text("stray\n", encoding="utf-8")
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.settle_task(state_path, "T01", "PASS")
        self.assertIn("scope check BLOCKED", str(ctx.exception))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"][0]["status"], "VALIDATING")
        self.assertEqual(state["scope_checks"][-1]["result"], "BLOCKED")
        # Recovering: FAILED is still settleable from VALIDATING.
        outcome = controller.settle_task(state_path, "T01", "FAILED", "pytest::t::fail")
        self.assertEqual(outcome["status"], "FAILED")
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final["scope_checks"][-1]["error_fingerprint"], "pytest::t::fail")

    def test_settle_task_full_pass_path(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-st2")
        controller.record_task(state_path, "T01", ["src/app.py"])
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        outcome = controller.settle_task(state_path, "T01", "PASS")
        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["scope_result"], "PASS")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"][0]["status"], "PASS")
        self.assertEqual(len(state["scope_checks"]), 1)

    def test_settle_task_rejects_unstarted_and_finished_tasks(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-st3")
        with self.assertRaises(controller.ControllerError):
            controller.settle_task(state_path, "T01", "PASS")  # unregistered
        controller.record_task(state_path, "T01", ["src/app.py"])
        with self.assertRaises(controller.ControllerError):
            controller.settle_task(state_path, "T01", "PASS")  # PENDING
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        controller.settle_task(state_path, "T01", "FAILED")
        with self.assertRaises(controller.ControllerError):
            controller.settle_task(state_path, "T01", "FAILED")  # terminal retry must re-dispatch


if __name__ == "__main__":
    unittest.main()
