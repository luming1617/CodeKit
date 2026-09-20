from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
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
        self.assertEqual(snapshot["worktree_state"]["changed_files"], ["A:src/app.py"])
        self.assertIsNone(snapshot["committed_range"])
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
        # KW2-d: a Handoff now names the documents it claims are available, and
        # the controller checks they exist instead of taking the word for it.
        # CW2-a：快照这一整块按 `freeze-review` 落盘的形状现算，不手抄字面量——
        # 手抄的 fixture 只能证明"抄得像"，证明不了两侧真能机械比对。
        head_sha = controller._git(self.repo, "rev-parse", "HEAD").strip()
        return {
            "review_mode": "FULL",
            "repo_root": str(self.repo),
            "review_input": {"materials": [
                {"kind": "PRD", "access": "available",
                 "locator": str(self.feature / "product-doc.md")},
                {"kind": "TDD", "access": "available",
                 "locator": str(self.feature / "technical-doc.md")},
                {"kind": "RPD", "access": "available",
                 "locator": str(self.feature / "release-doc.md")},
            ]},
            "snapshot": {
                "schema_version": controller.SNAPSHOT_SCHEMA_VERSION,
                "produced_by": controller.PRODUCED_BY,
                "digest_scheme": controller._DIGEST_SCHEME,
                "committed_range": None,
                "target_id": controller._handoff_target_id(self.repo, head_sha),
                "worktree_state": {
                    "head_sha": head_sha,
                    "diff_digest": "0" * 64,
                    "changed_files": ["A:src/app.py"],
                },
            },
        }

    def test_validate_handoff_accepts_freeze_review_shape(self) -> None:
        # 门禁认的形状必须就是 `freeze-review` 落盘的那份，否则两边各写各的契约。
        state_path = controller.init_run(self.repo, self.feature, "run-hf-shape")
        payload = self._valid_handoff()
        payload["snapshot"] = controller.freeze_review(state_path)
        controller.validate_contract(payload, "handoff")  # must not raise

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

    def test_validate_handoff_rejects_locator_that_is_not_on_disk(self) -> None:
        """KW2-d: access=available is measured, not self-reported."""
        payload = self._valid_handoff()
        for m in payload["review_input"]["materials"]:
            if m["kind"] == "PRD":
                m["locator"] = str(self.feature / "never-written.md")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "handoff")
        self.assertIn("does not resolve to a file", str(ctx.exception))

    def test_validate_handoff_rejects_msys_rewritten_locator(self) -> None:
        """KW2-d: the Git Bash path-rewrite failure mode is named, not just missing."""
        payload = self._valid_handoff()
        for m in payload["review_input"]["materials"]:
            if m["kind"] == "PRD":
                m["locator"] = "C:/Program Files/Git/Users/test/prd/product-doc.md"
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "handoff")
        self.assertIn("Git Bash", str(ctx.exception))

    # ── KW1-f: requirement_trace is a real link, not a decorative list ───────

    def test_validate_task_dag_rejects_unknown_trace_source(self) -> None:
        payload = {
            "tasks": [{"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": []}],
            "requirement_trace": [
                {"source": "vibes", "requirement": "r", "covered_by": ["T1"]}
            ],
            "unresolved": [],
        }
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "task-dag")
        self.assertIn("source", str(ctx.exception))

    def test_validate_task_dag_rejects_trace_covering_nothing(self) -> None:
        payload = {
            "tasks": [{"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": []}],
            "requirement_trace": [{"source": "product_requirements", "requirement": "r", "covered_by": []}],
            "unresolved": [],
        }
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "task-dag")
        self.assertIn("covered_by", str(ctx.exception))

    def test_validate_task_dag_rejects_trace_covering_unknown_id(self) -> None:
        payload = {
            "tasks": [{"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": []}],
            "requirement_trace": [
                {"source": "product_requirements", "requirement": "r", "covered_by": ["T999"]}
            ],
            "unresolved": [],
        }
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.validate_contract(payload, "task-dag")
        self.assertIn("T999", str(ctx.exception))

    def test_validate_task_dag_accepts_human_acceptance_coverage(self) -> None:
        payload = {
            "tasks": [{"id": "T1", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": []}],
            "requirement_trace": [
                {"source": "release_plan", "requirement": "rollback drill", "covered_by": ["H01"]}
            ],
            "human_acceptance": [{"id": "H01", "requirement": "rollback drill"}],
            "unresolved": [],
        }
        controller.validate_contract(payload, "task-dag")  # must not raise

    # ── KW1-a: agent lifecycle is not a verdict channel ──────────────────────

    def test_record_agent_rejects_verdict_as_status(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1a")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state_path, "code-review", "code-review", "s-1", "d-1", "APPROVE"
            )
        self.assertIn("record-review", str(ctx.exception))
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["agents"], [])

    # ── KW1-b: validation evidence is on disk, not in the transcript ─────────

    def test_record_validation_pass_requires_exit_zero_and_evidence(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1b1")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_validation(
                state_path, "overall", "pytest", "pytest -q", str(self.repo),
                1, "PASS", evidence="3 passed",
            )
        self.assertIn("exit_code=1", str(ctx.exception))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_validation(
                state_path, "overall", "pytest", "pytest -q", str(self.repo),
                0, "PASS",
            )
        self.assertIn("without evidence", str(ctx.exception))
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["validations"], [])

    def test_record_validation_binds_evidence_to_the_working_tree(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1b2")
        record = controller.record_validation(
            state_path, "overall", "pytest", "pytest -q", str(self.repo),
            0, "PASS", evidence="12 passed in 3.1s",
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(state["validations"][0]["diff_digest"],
                         controller.dirty_snapshot(self.repo)["digest"])
        self.assertNotIn("task_id", state["validations"][0])
        # Evidence collected on this tree cannot describe a later edit.
        (self.repo / "after-tests.py").write_text("print(1)\n", encoding="utf-8")
        self.assertNotEqual(state["validations"][0]["diff_digest"],
                            controller.dirty_snapshot(self.repo)["digest"])

    def test_record_validation_not_run_requires_reason(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1b3")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_validation(
                state_path, "overall", "migration", "alembic upgrade head", str(self.repo),
                None, "NOT_RUN",
            )
        self.assertIn("not-run-reason", str(ctx.exception))
        record = controller.record_validation(
            state_path, "overall", "migration", "alembic upgrade head", str(self.repo),
            None, "NOT_RUN", not_run_reason="本机无数据库",
        )
        self.assertEqual(record["result"], "NOT_RUN")

    def test_record_validation_task_scope_requires_registered_task(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1b4")
        with self.assertRaises(controller.ControllerError):
            controller.record_validation(
                state_path, "task", "pytest", "pytest -q", str(self.repo),
                0, "PASS", task_id="T404", evidence="ok",
            )

    # ── KW1-c: a verdict is recorded against the frozen diff ─────────────────

    def test_record_review_requires_a_frozen_snapshot(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1c1")
        report = state_path.parent / "review-report.json"
        report.write_text('{"verdict": "APPROVE"}', encoding="utf-8")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_review(state_path, "APPROVE", str(report), 0)
        self.assertIn("freeze-review", str(ctx.exception))

    def test_record_review_verdict_is_bound_to_the_newest_freeze(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1c2")
        controller.record_task(state_path, "T01", ["late.py"])
        snapshot = controller.freeze_review(state_path)
        report = state_path.parent / "review-report.json"
        report.write_text('{"verdict": "APPROVE"}', encoding="utf-8")
        record = controller.record_review(state_path, "APPROVE", str(report), 0)
        self.assertEqual(record["diff_digest"], snapshot["worktree_state"]["diff_digest"][:12])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["reviews"][-1]["report_digest"][:7], "sha256:")
        # An edit after the verdict shows up as a different digest at gate time.
        (self.repo / "late.py").write_text("print(1)\n", encoding="utf-8")
        controller.freeze_review(state_path)
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(after["review_snapshots"][-1]["diff_digest"],
                            after["reviews"][-1]["diff_digest"])

    def test_record_review_rejects_unknown_verdict_and_missing_report(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw1c3")
        controller.freeze_review(state_path)
        with self.assertRaises(controller.ControllerError):
            controller.record_review(state_path, "LGTM", str(self.repo / "x.json"), 0)
        with self.assertRaises(controller.ControllerError):
            controller.record_review(state_path, "APPROVE", str(self.repo / "missing.json"), 0)

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
            {"path", "target_id", "head_sha", "diff_digest", "frozen_at"},
        )
        # CW2-a③：state.json 里的那条引用就是续跑时的连接键。`base_sha` 已经不在
        # code-impl 的字典里（它从不提交），留着只会让下游拿它去比 code-review 的
        # merge-base，比出来永远是 False。
        self.assertNotIn("base_sha", entry)
        self.assertTrue(entry["target_id"].endswith("@" + entry["head_sha"]))
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
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::AssertionError")
        # KW1-e: the retry budget is only mechanical if the failed attempt was
        # registered, so dispatch demands the record-failure first.
        controller.record_failure(
            state_path, "task", ["T01"], "CODE_OR_TEST", "pytest::t::AssertionError"
        )
        outcome = controller.dispatch_task(state_path, "T01", "session-2", "digest-2")
        self.assertEqual(outcome["status"], "RUNNING")
        self.assertEqual(outcome["attempts"], 2)

    def test_dispatch_task_retry_without_registered_failure_is_refused(self) -> None:
        """KW1-e: settle FAILED + dispatch again, ten times, was the bypass."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp2g")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::AssertionError")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.dispatch_task(state_path, "T01", "session-2", "digest-2")
        self.assertIn("no record-failure entry", str(ctx.exception))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["tasks"][0]["status"], "FAILED")
        self.assertEqual(state["tasks"][0]["attempts"], 1)

    def test_dispatch_task_prose_policy_restores_unregistered_retry(self) -> None:
        """KW1-e rollback switch: --retry-policy prose keeps the old behaviour."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp2p")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::AssertionError")
        outcome = controller.dispatch_task(
            state_path, "T01", "session-2", "digest-2", retry_policy="prose"
        )
        self.assertEqual(outcome["attempts"], 2)

    def test_dispatch_task_refuses_retry_escalated_to_human(self) -> None:
        """A failure whose disposition is human_intervention cannot be retried away."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp2h")
        controller.record_task(state_path, "T01", ["src/app.py"])
        fp = "pytest::t::AssertionError"
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        controller.settle_task(state_path, "T01", "FAILED", fp)
        for _ in range(controller.MAX_FINGERPRINT_ATTEMPTS):
            controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", fp)
            controller.dispatch_task(state_path, "T01", f"s-{_}", "d")
            controller.settle_task(state_path, "T01", "FAILED", fp)
        capped = controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", fp)
        self.assertEqual(capped["disposition"], "human_intervention")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.dispatch_task(state_path, "T01", "session-x", "d")
        self.assertIn("human intervention", str(ctx.exception))

    def test_dispatch_task_absolute_attempt_cap(self) -> None:
        """Even with a fresh fingerprint each time, no task dispatches forever."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp2c")
        controller.record_task(state_path, "T01", ["src/app.py"])
        for n in range(controller.MAX_TASK_ATTEMPTS):
            # Distinct failures by *letter*: a digit-varying fingerprint would be
            # folded by normalize_error_fingerprint and hit the per-fingerprint cap.
            fp = f"pytest::t::variant{chr(ord('a') + n)}"
            controller.dispatch_task(state_path, "T01", f"s-{n}", "d")
            controller.settle_task(state_path, "T01", "FAILED", fp)
            controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", fp)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.dispatch_task(state_path, "T01", "s-final", "d")
        self.assertIn("MAX_TASK_ATTEMPTS", str(ctx.exception))

    def test_prose_policy_does_not_lift_the_absolute_cap(self) -> None:
        """The rollback switch loosens the diagnosis-record rule, never the budget."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp2cap")
        controller.record_task(state_path, "T01", ["src/app.py"])
        for n in range(controller.MAX_TASK_ATTEMPTS):
            fp = f"pytest::t::variant{chr(ord('a') + n)}"
            controller.dispatch_task(state_path, "T01", f"s-{n}", "d", retry_policy="prose")
            controller.settle_task(state_path, "T01", "FAILED", fp)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.dispatch_task(state_path, "T01", "s-x", "d", retry_policy="prose")
        self.assertIn("MAX_TASK_ATTEMPTS", str(ctx.exception))

    def test_prose_retry_leaves_a_countable_trace(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-dp2trace")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "s-1", "d")
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::a")
        outcome = controller.dispatch_task(state_path, "T01", "s-2", "d", retry_policy="prose")
        self.assertEqual(outcome["retry_policy"], "prose")
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::b")
        controller.record_failure(state_path, "task", ["T01"], "CODE_OR_TEST", "pytest::t::b")
        gated = controller.dispatch_task(state_path, "T01", "s-3", "d")
        self.assertEqual(gated["retry_policy"], "gated")  # every retry discloses its policy
        state = json.loads(state_path.read_text(encoding="utf-8"))
        log = state["tasks"][0]["attempts_log"]
        self.assertEqual([entry.get("retry_policy") for entry in log], [None, "prose", "gated"])
        self.assertEqual(controller.show(state_path)["tasks"]["prose_retries"], 1)

    def test_settle_task_failed_requires_fingerprint(self) -> None:
        """KW1-e: without the key, the retry budget cannot count anything."""
        state_path = controller.init_run(self.repo, self.feature, "run-st0f")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.settle_task(state_path, "T01", "FAILED")
        self.assertIn("--error-fingerprint", str(ctx.exception))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # The verdict is refused before the transition: no fingerprint, no ledger row.
        self.assertEqual(state["tasks"][0]["status"], "RUNNING")

    def test_normalise_error_fingerprint_folds_volatile_parts(self) -> None:
        """KW1-e: the same failure with a new line number is still the same failure."""
        base = controller.normalize_error_fingerprint(
            "AssertionError in /repo/src/auth.py:412 for token 'abc123deadbeef'"
        )
        for variant in (
            "AssertionError in /repo/src/auth.py:517 for token 'ff9922113344'",
            "AssertionError in /repo/src/auth.py:12   for   token 'x'",
        ):
            self.assertEqual(controller.normalize_error_fingerprint(variant), base)
        self.assertNotEqual(
            base, controller.normalize_error_fingerprint("ConnectionRefusedError: port 5432")
        )

    def test_record_failure_counts_normalised_fingerprints(self) -> None:
        """KW1-e: re-wording the error no longer buys a fresh retry budget."""
        state_path = controller.init_run(self.repo, self.feature, "run-nfp")
        controller.record_task(state_path, "T01", ["src/a.py"])
        dispositions = []
        for n in range(1, controller.MAX_FINGERPRINT_ATTEMPTS + 2):
            event = controller.record_failure(
                state_path, "task", ["T01"], "CODE_OR_TEST",
                f"AssertionError at /repo/test_x.py:{100 + n}",
            )
            dispositions.append((event["fingerprint_occurrences"], event["disposition"]))
        self.assertEqual(dispositions[0][0], 1)
        self.assertEqual(dispositions[-1], (controller.MAX_FINGERPRINT_ATTEMPTS + 1, "human_intervention"))

    def test_dispatch_task_records_attempt_log(self) -> None:
        """KW2-a: the ledger can say when each attempt ran."""
        state_path = controller.init_run(self.repo, self.feature, "run-dp2l")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "session-1", "digest-1")
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::AssertionError")
        controller.record_failure(
            state_path, "task", ["T01"], "CODE_OR_TEST", "pytest::t::AssertionError"
        )
        controller.dispatch_task(state_path, "T01", "session-2", "digest-2")
        controller.settle_task(state_path, "T01", "PASS")
        task = json.loads(state_path.read_text(encoding="utf-8"))["tasks"][0]
        self.assertEqual([a["attempt"] for a in task["attempts_log"]], [1, 2])
        self.assertEqual(task["attempts_log"][0]["result"], "FAILED")
        self.assertEqual(task["attempts_log"][1]["result"], "PASS")
        self.assertTrue(task["attempts_log"][0]["started_at"])
        self.assertEqual(task["error_fingerprints"], ["pytest::t::AssertionError"])
        self.assertEqual(task["changed_files"], ["src/app.py"])

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
        controller.settle_task(state_path, "T01", "FAILED", "pytest::t::fail")
        with self.assertRaises(controller.ControllerError):
            controller.settle_task(state_path, "T01", "FAILED", "pytest::t::fail")  # terminal retry must re-dispatch

    # ── KW1-a: the delivery gate decides from recorded evidence ──────────────

    def _deliverable_run(self, run_id: str) -> Path:
        """Drive one task all the way to a defensible delivery."""
        state_path = controller.init_run(self.repo, self.feature, run_id)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "s-1", "d-1")
        controller.settle_task(state_path, "T01", "PASS")
        controller.record_validation(
            state_path, "task", "pytest-T01", "pytest -q tests/test_app.py", str(self.repo),
            0, "PASS", task_id="T01", evidence="1 passed in 0.31s",
        )
        controller.freeze_review(state_path)
        controller.record_validation(
            state_path, "overall", "全量测试", "pytest -q", str(self.repo),
            0, "PASS", evidence="142 passed in 9.4s",
        )
        report = state_path.parent / "review-report.json"
        report.write_text('{"verdict":"APPROVE"}\n', encoding="utf-8")
        controller.record_review(state_path, "APPROVE", str(report), 0)
        return state_path

    def test_gate_passes_only_on_a_fully_evidenced_run(self) -> None:
        report = controller.gate(self._deliverable_run("run-gate-ok"))
        self.assertEqual(report["status"], "PASS", report["failures"])
        self.assertEqual(report["failures"], [])

    def test_gate_blocks_delivery_without_validation_evidence(self) -> None:
        """A3: zero test evidence used to be freezable and deliverable."""
        state_path = controller.init_run(self.repo, self.feature, "run-gate-noev")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "s-1", "d-1")
        controller.settle_task(state_path, "T01", "PASS")
        controller.freeze_review(state_path)
        report = controller.gate(state_path)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("task-validations-recorded", report["failures"])
        self.assertIn("overall-validation-recorded", report["failures"])
        self.assertIn("review-verdict-recorded", report["failures"])

    def test_gate_blocks_unfinished_plan(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-gate-open")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.record_task(state_path, "T02", ["src/other.py"])
        controller.dispatch_task(state_path, "T01", "s-1", "d-1")
        controller.settle_task(state_path, "T01", "PASS")
        report = controller.gate(state_path)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("all-current-plan-tasks-pass", report["failures"])
        self.assertIn("T02=PENDING", str(report["checks"]))

    def test_gate_blocks_evidence_that_predates_the_frozen_diff(self) -> None:
        """Overall evidence must describe the diff that is actually delivered."""
        state_path = controller.init_run(self.repo, self.feature, "run-gate-stale")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "s-1", "d-1")
        controller.settle_task(state_path, "T01", "PASS")
        controller.record_validation(
            state_path, "task", "pytest-T01", "pytest -q", str(self.repo),
            0, "PASS", task_id="T01", evidence="1 passed",
        )
        controller.record_validation(
            state_path, "overall", "全量测试", "pytest -q", str(self.repo),
            0, "PASS", evidence="1 passed",
        )
        # A late edit makes the overall evidence describe a different tree.
        (self.repo / "src" / "app.py").write_text("print('changed after tests')\n", encoding="utf-8")
        controller.freeze_review(state_path)
        report = controller.gate(state_path)
        self.assertIn("overall-validation-covers-frozen-diff", report["failures"])

    def test_gate_blocks_verdict_from_an_older_freeze(self) -> None:
        """A4: a review of a superseded diff cannot certify the delivery."""
        state_path = self._deliverable_run("run-gate-old")
        (self.repo / "src" / "app.py").write_text("print('edited after review')\n", encoding="utf-8")
        controller.freeze_review(state_path)
        report = controller.gate(state_path)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("review-verdict-bound-to-frozen-diff", report["failures"])
        self.assertIn("new freeze and a fresh review", str(report["checks"]))

    def test_gate_blocks_request_changes_verdict(self) -> None:
        state_path = self._deliverable_run("run-gate-rc")
        report_path = state_path.parent / "review-report.json"
        report = controller.gate(state_path)
        self.assertEqual(report["status"], "PASS")
        controller.record_review(state_path, "REQUEST_CHANGES", str(report_path), 0)
        report = controller.gate(state_path)
        self.assertIn("review-verdict-passing", report["failures"])

    def test_gate_blocks_structurally_rejected_report(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-gate-val")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        controller.dispatch_task(state_path, "T01", "s-1", "d-1")
        controller.settle_task(state_path, "T01", "PASS")
        controller.record_validation(
            state_path, "task", "pytest-T01", "pytest -q", str(self.repo),
            0, "PASS", task_id="T01", evidence="1 passed",
        )
        controller.freeze_review(state_path)
        controller.record_validation(
            state_path, "overall", "全量测试", "pytest -q", str(self.repo),
            0, "PASS", evidence="1 passed",
        )
        report_file = state_path.parent / "review-report.json"
        report_file.write_text('{"verdict": "APPROVE"}\n', encoding="utf-8")
        controller.record_review(state_path, "APPROVE", str(report_file), 1)
        report = controller.gate(state_path)
        self.assertIn("review-validator-exited-0", report["failures"])

    def test_gate_blocks_writes_after_the_last_settle(self) -> None:
        state_path = self._deliverable_run("run-gate-scope")
        (self.repo / "stray.py").write_text("sneaked in\n", encoding="utf-8")
        report = controller.gate(state_path)
        self.assertIn("final-scope-clean", report["failures"])
        self.assertIn("stray.py", str(report["checks"]))

    # ── KW2-b/c/g: the ledger is persisted, state stays small ────────────────

    def test_scope_check_full_record_lives_outside_state(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw2c")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        controller.record_task(state_path, "T01", ["src/app.py"])
        full = controller.check_scope(state_path, "T01")
        self.assertIn("new_files", full)  # the Python view is still complete
        entry = json.loads(state_path.read_text(encoding="utf-8"))["scope_checks"][-1]
        self.assertNotIn("new_files", entry)
        self.assertEqual(entry["counts"]["new_files"], 1)
        artifact = Path(entry["path"])
        self.assertTrue(artifact.is_file())
        self.assertEqual(json.loads(artifact.read_text(encoding="utf-8"))["new_files"], ["src/app.py"])
        self.assertEqual(artifact.parent.name, "scope-checks")

    def test_state_does_not_grow_file_lists_per_check(self) -> None:
        """KW2-c: 20 checks must not multiply every registered file 20 times."""
        state_path = controller.init_run(self.repo, self.feature, "run-kw2c2")
        (self.repo / "src").mkdir()
        files = []
        for n in range(10):
            relative = f"src/f{n:02d}.py"
            (self.repo / relative).write_text(f"v{n}\n", encoding="utf-8")
            files.append(relative)
        controller.record_task(state_path, "T01", files)
        controller.check_scope(state_path, "T01")
        size_after_first = len(state_path.read_text(encoding="utf-8"))
        for _ in range(20):
            controller.check_scope(state_path, "T01")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # The tree never changed, so the same digest is one row with a repeat count.
        self.assertEqual(len(state["scope_checks"]), 1)
        self.assertEqual(state["scope_checks"][0]["repeat_count"], 21)
        self.assertEqual(state["scope_checks_total"], 21)
        raw = state_path.read_text(encoding="utf-8")
        self.assertLess(len(raw), size_after_first + 200)
        # The registered file list is stored once (the Task Ledger), not per check.
        self.assertEqual(raw.count("src/f00.py"), 1)
        # A changed tree does get its own row and artifact.
        (self.repo / "src" / "f00.py").write_text("changed\n", encoding="utf-8")
        controller.check_scope(state_path, "T01")
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(after["scope_checks"]), 2)
        self.assertEqual(after["scope_checks_total"], 22)

    def test_render_ledger_writes_managed_block_and_is_idempotent(self) -> None:
        state_path = self._deliverable_run("run-kw2b")
        first = controller.render_ledger(state_path)
        text = Path(first["path"]).read_text(encoding="utf-8")
        self.assertEqual(text.count(controller.LEDGER_START), 1)
        self.assertIn("| T01 | PASS |", text)
        second = controller.render_ledger(state_path)
        again = Path(second["path"]).read_text(encoding="utf-8")
        self.assertEqual(again.count(controller.LEDGER_START), 1)
        self.assertEqual(second["rows"], 1)
        self.assertEqual(second["passed"], 1)

    def test_render_ledger_preserves_surrounding_handwriting(self) -> None:
        state_path = self._deliverable_run("run-kw2b2")
        target = state_path.parent / "task-ledger.md"
        target.write_text("# 人工备注\n\n保留这段\n", encoding="utf-8")
        controller.render_ledger(state_path, target)
        text = target.read_text(encoding="utf-8")
        self.assertIn("保留这段", text)
        self.assertIn(controller.LEDGER_START, text)

    def test_show_reports_position_without_dumping_state(self) -> None:
        state_path = self._deliverable_run("run-kw2show")
        report = controller.show(state_path)
        self.assertEqual(report["run_id"], "run-kw2show")
        self.assertEqual(report["tasks"]["by_status"], {"PASS": 1})
        self.assertEqual(report["review"]["verdict"], "APPROVE")
        self.assertEqual(report["drifted_materials"], [])
        self.assertLess(len(json.dumps(report, ensure_ascii=False)), 4000)
        # A document edited after the freeze is visible without reading state.
        (self.feature / "product-doc.md").write_text(
            "# PRD\nThe product requirement changed after the freeze.\n", encoding="utf-8"
        )
        drifted = controller.show(state_path)
        self.assertEqual(drifted["drifted_materials"], ["product_requirements"])

    def test_record_tasks_from_dag_registers_in_one_call(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw2g")
        dag = state_path.parent / "task-dag.json"
        dag.write_text(json.dumps({
            "task_dag": {
                "plan_version": 1,
                "requirement_trace": [
                    {"source": "product_requirements", "requirement": "登录", "covered_by": ["T01", "T02"]}
                ],
                "tasks": [
                    {"id": "T01", "objective": "模型", "allowed_write_files": ["src/a.py"], "depends_on": []},
                    {"id": "T02", "objective": "接口", "allowed_write_files": ["src/b.py"], "depends_on": ["T01"]},
                ],
                "unresolved": [],
            }
        }), encoding="utf-8")
        result = controller.record_tasks_from_dag(state_path, dag)
        self.assertEqual(result["registered"], ["T01", "T02"])
        self.assertEqual(result["tasks_total"], 2)
        tasks = {item["task_id"]: item for item in
                 json.loads(state_path.read_text(encoding="utf-8"))["tasks"]}
        self.assertEqual(tasks["T01"]["allowed_files"], ["src/a.py"])
        self.assertEqual(tasks["T01"]["status"], "PENDING")
        # An empty depends_on is not stored: the DAG artifact remains the source of truth.
        self.assertNotIn("depends_on", tasks["T01"])
        self.assertEqual(tasks["T02"]["depends_on"], ["T01"])
        # Idempotent: re-running registers nothing new.
        again = controller.record_tasks_from_dag(state_path, dag)
        self.assertEqual(again["registered"], [])
        self.assertEqual(again["already_present"], ["T01", "T02"])
        self.assertEqual(again["tasks_total"], 2)

    def test_record_tasks_from_dag_refuses_a_bad_plan(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw2g2")
        dag = state_path.parent / "task-dag.json"
        dag.write_text(json.dumps({
            "tasks": [
                {"id": "T01", "objective": "x", "allowed_write_files": ["a.py"], "depends_on": ["T02"]},
                {"id": "T02", "objective": "y", "allowed_write_files": ["b.py"], "depends_on": ["T01"]},
            ],
            "requirement_trace": [{"source": "product_requirements", "requirement": "r", "covered_by": ["T01"]}],
            "unresolved": [],
        }), encoding="utf-8")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_tasks_from_dag(state_path, dag)
        self.assertIn("cycle", str(ctx.exception).lower())
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["tasks"], [])

    # ── KW2-d/e: the freeze speaks one shape and fails readably ──────────────

    def test_init_freezes_locators_and_self_validates_the_input_contract(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-kw2d")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        prd = state["materials"]["product_requirements"]
        self.assertNotIn("path", prd)
        self.assertEqual(Path(prd["locator"]).name, "product-doc.md")
        self.assertEqual(prd["revision"], "unversioned")
        self.assertTrue(prd["last_modified"])
        contract_path = state_path.parent / "input-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        # The freeze is representable in the language its own validator reads.
        controller.validate_contract(contract, "input")

    def test_non_utf8_document_reports_blocked_with_encoding(self) -> None:
        """C1: a GBK document used to crash the controller with exit 1."""
        feature = self.repo / "docs" / "gbk"
        feature.mkdir(parents=True)
        for name, marker in (
            ("product-doc.md", "PRD"), ("technical-doc.md", "TDD"), ("release-doc.md", "RPD"),
        ):
            (feature / name).write_bytes(
                f"# {marker}\n这是中文说明，{marker} 的内容需要足够长以通过实质内容检查。\n".encode("gbk")
            )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.init_run(self.repo, feature, "run-gbk")
        message = str(ctx.exception)
        self.assertIn("not valid UTF-8", message)
        self.assertIn("gb18030", message)
        self.assertIn("--allow-lossy-decoding", message)

    def test_allow_lossy_decoding_records_the_degradation(self) -> None:
        feature = self.repo / "docs" / "gbk-lossy"
        feature.mkdir(parents=True)
        for name, marker in (
            ("product-doc.md", "PRD"), ("technical-doc.md", "TDD"), ("release-doc.md", "RPD"),
        ):
            (feature / name).write_bytes(f"# {marker}\n中文内容 {marker} 需要足够长一些。\n".encode("gbk"))
        state_path = controller.init_run(self.repo, feature, "run-gbk-lossy", allow_lossy_decoding=True)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["materials"]["product_requirements"]["decoding"], "lossy:gb18030")
        self.assertEqual(controller.show(state_path)["materials"]["product_requirements"]["decoding"],
                         "lossy:gb18030")

    def test_utf8_bom_is_stripped_before_digesting(self) -> None:
        feature = self.repo / "docs" / "bom"
        feature.mkdir(parents=True)
        for name, marker in (
            ("product-doc.md", "PRD"), ("technical-doc.md", "TDD"), ("release-doc.md", "RPD"),
        ):
            (feature / name).write_text(
                f"\ufeff# {marker}\nThe {marker} body needs to be long enough to pass the check.\n",
                encoding="utf-8",
            )
        state_path = controller.init_run(self.repo, feature, "run-bom")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["materials"]["product_requirements"]["decoding"], "bom-stripped")
        self.assertEqual(controller.show(state_path)["drifted_materials"], [])

    # ── CLI surface: exit codes and bounded prints ───────────────────────────

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Run ``main()`` in-process; argparse exits via SystemExit, not a return."""
        buffer, error_buffer = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(error_buffer):
                code = controller.main(list(argv))
        except SystemExit as exit_exc:  # argparse rejects the flags before dispatch
            code = exit_exc.code if isinstance(exit_exc.code, int) else 2
        return code, buffer.getvalue(), error_buffer.getvalue()

    def test_cli_gate_exits_two_while_blocked(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-cli-gate")
        code, _, _ = self._run_cli("gate", "--state", str(state_path))
        self.assertEqual(code, 2)
        code, out, _ = self._run_cli("gate", "--state", str(state_path), "--mode", "warn-only")
        self.assertEqual(code, 0)
        self.assertIn("BLOCKED (warn-only)", out)

    def test_cli_check_scope_prints_the_bounded_view(self) -> None:
        """KW2-h: 6.1 KB per check went into the main context; it must not again."""
        state_path = controller.init_run(self.repo, self.feature, "run-cli-scope")
        (self.repo / "src").mkdir()
        files = []
        for n in range(30):
            relative = f"src/f{n:02d}.py"
            (self.repo / relative).write_text(f"v{n}\n", encoding="utf-8")
            files.append(relative)
        controller.record_task(state_path, "T01", files)
        code, out, _ = self._run_cli("check-scope", "--state", str(state_path), "--task-id", "T01")
        self.assertEqual(code, 0)
        self.assertLess(len(out), 600, out)
        payload = json.loads(out)
        self.assertEqual(payload["counts"]["allowed_files"], 30)
        self.assertNotIn("allowed_files_array", payload)

    def test_cli_freeze_review_does_not_echo_the_preexisting_diff(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-cli-freeze")
        for n in range(30):
            (self.repo / f"dirty{n}.py").write_text(f"v{n}\n", encoding="utf-8")
        # Re-init is not possible in-place; refresh the baseline by re-running on a new run.
        state_path = controller.init_run(self.repo, self.feature, "run-cli-freeze2")
        code, out, _ = self._run_cli("freeze-review", "--state", str(state_path))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertNotIn("preexisting_diff_snapshot", payload)
        self.assertIn("diff_digest", payload)
        self.assertLess(len(out), 6000, out[:200])

    def test_cli_record_validation_rejects_pass_with_nonzero_exit(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-cli-val")
        code, _, err = self._run_cli(
            "record-validation", "--state", str(state_path), "--scope", "overall",
            "--name", "pytest", "--command", "pytest -q", "--cwd", str(self.repo),
            "--exit-code", "1", "--result", "PASS", "--evidence", "2 failed",
        )
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED:", err)
        self.assertIn("exit_code=1", err)

    def test_cli_record_agent_rejects_a_verdict_status(self) -> None:
        state_path = controller.init_run(self.repo, self.feature, "run-cli-agent")
        code, _, err = self._run_cli(
            "record-agent", "--state", str(state_path), "--role", "code-review",
            "--name", "code-review", "--session-id", "s-1", "--input-digest", "d-1",
            "--status", "APPROVE",
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    # ── 文档与控制器命令面不得漂移 ────────────────────────────────────────────

    SKILL_DIR = MODULE_PATH.parent.parent
    DOCS = (SKILL_DIR / "SKILL.md", SKILL_DIR / "references" / "contracts.md")
    SOURCE = MODULE_PATH.read_text(encoding="utf-8")
    COMMANDS = set(re.findall(r'add_parser\(\s*"([\w-]+)"', SOURCE))
    FLAGS = set(re.findall(r'add_argument\(\s*"(--[\w-]+)"', SOURCE))

    def _doc_command_blocks(self) -> list[str]:
        text = "\n".join(path.read_text(encoding="utf-8") for path in self.DOCS)
        return [
            block
            for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.DOTALL)
            if "code_impl_controller.py" in block
        ]

    def test_docs_only_call_commands_and_flags_the_controller_registers(self) -> None:
        """A documented command that argparse does not know is an instruction the run cannot follow."""
        called_commands: set[str] = set()
        called_flags: set[str] = set()
        for block in self._doc_command_blocks():
            for line in block.splitlines():
                match = re.search(r'code_impl_controller\.py"?\s+(\S+)', line)
                if match:
                    called_commands.add(match.group(1).strip('"\\ '))
            called_flags.update(re.findall(r"(--[\w-]+)", block))
        self.assertTrue(called_commands)
        self.assertEqual(called_commands - self.COMMANDS, set())
        self.assertEqual(called_flags - self.FLAGS, set())

    def test_every_controller_command_is_documented(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8") for path in self.DOCS)
        undocumented = {name for name in self.COMMANDS if name not in text}
        self.assertEqual(undocumented, set())


if __name__ == "__main__":
    unittest.main()
