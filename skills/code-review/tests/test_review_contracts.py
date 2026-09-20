from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from freeze_review_snapshot import (  # noqa: E402
    CHANGE_LIST_INLINE_LIMIT,
    CHANGE_LIST_PREVIEW,
    COVERAGE_DIMENSIONS,
    COMMITTED_RANGE_FIELDS,
    DIGEST_SCHEME,
    GENERATOR_VERSION,
    HANDOFF_LIMITATIONS,
    MANIFEST_SCHEMA_VERSION,
    MAX_REFREEZE,
    PRODUCED_BY,
    RELEASE_CHECK_ITEMS,
    REVIEW_SCALE_FIELDS,
    RETRY_POLICIES,
    SCALE_TIERS,
    SCALE_TIER_LIMITS,
    SNAPSHOT_REPORT_FIELDS,
    STEP_ARTIFACTS,
    SnapshotError,
    TARGET_ID_UNDECLARED,
    TargetBlocked,
    WORKTREE_STATE_FIELDS,
    build_report_skeleton,
    build_snapshot,
    canonical_json,
    parse_name_status,
    resolve_auto_branch_target,
    resolve_handoff_check,
    review_scale,
    run_dir_for,
    run_git,
    sha256,
    sign_manifest,
    snapshot_field,
    step_artifact_paths,
    target_id_for,
    untracked_manifest,
)
from record_review_dispatch import MAX_DISPATCHES  # noqa: E402
from validate_review_report import ReportValidator  # noqa: E402


DIGEST = "d" * 64
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
DEFAULT_OWNER = "SP-01"
# 夹具里的 /repo 不是真实目录；target_id 用脚本同一个函数算出，校验器重算的那一份
# 才会与报告里抄的那一份相等（换成字面量会在 Windows 上因 normcase/resolve 而假失败）。
TARGET_ID = target_id_for("/repo", HEAD_SHA)
# 骨架、manifest 与报告三方都比对 review_scale，用同一份脚本产出而不是各写一份字面量。
SCALE = review_scale(1, 0)
DIMENSIONS = [
    "correctness",
    "security_access",
    "data_transaction",
    "distributed_reliability",
    "compatibility_contract",
    "performance_capacity",
    "test_adequacy",
    "observability",
    "release_rollback",
]


def valid_report() -> dict:
    return {
        "schema_version": "1.0",
        "review_context": {"review_mode": "FULL", "documents_status": "available"},
        "verdict": "APPROVE",
        "verdict_basis": ["all required evidence is present"],
        "blockers": [],
        "scope": {
            "target_type": "local_diff",
            "target_locator": "test",
            "target_id": TARGET_ID,
            "included": ["src"],
            "excluded": [],
        },
        "snapshot": {
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "target_id": TARGET_ID,
            "diff_digest": DIGEST,
            "changed_files": ["M:src/app.py"],
            "staged_digest": "1" * 64,
            "unstaged_digest": "2" * 64,
            "untracked_digest": "3" * 64,
            "preexisting_changes": [],
            "documents": [],
            "review_scale": SCALE,
            "manifest_digest": "4" * 64,
        },
        "summary": "review complete",
        "findings": [],
        "open_questions": [],
        "traceability": [
            {
                "id": "REQ-001",
                "kind": "requirement",
                "source": "prd#1",
                "statement": "the behavior is preserved",
                "links": {"requirements": [], "designs": [], "code": ["src/app.py:1"], "tests": [], "releases": []},
                "status": "COVERED",
                "evidence": ["src/app.py:1"],
                "gap": None,
            }
        ],
        "coverage": [
            {"dimension": dimension, "required": True, "status": "REVIEWED", "owner": DEFAULT_OWNER, "evidence": ["checked"], "reason": "checked"}
            for dimension in DIMENSIONS
        ],
        "validation": [
            {
                "id": "VAL-001",
                "target_claim": "tests pass",
                "command": "python -m unittest",
                "cwd": ".",
                "diff_digest": DIGEST,
                "started_at": "2026-09-09T00:00:00Z",
                "finished_at": "2026-09-09T00:00:01Z",
                "exit_code": 0,
                "status": "PASS",
                "evidence": "exit code 0",
                "not_run_reason": None,
            }
        ],
        "release_gate": release_gate("NOT_APPLICABLE", {item: "NOT_APPLICABLE" for item in RELEASE_CHECK_ITEMS}),
        "rejected_candidates": [],
        "limitations": [],
        "residual_risks": [],
    }


def valid_finding(**overrides: object) -> dict:
    finding = {
        "id": "F-001",
        "candidate_id": "CF-001",
        "title": "missing authorization check",
        "dimension": "security_access",
        "severity": "HIGH",
        "blocking": True,
        "blocking_reason": "unauthenticated callers can read other tenants",
        "confidence": 90,
        "evidence_level": "E2",
        "locations": ["src/app.py:1"],
        "causal_chain": ["the change removed the tenant guard"],
        "trigger_scenario": "call /data without a token",
        "impact": "cross-tenant data leak",
        "affected_scope": ["all tenants"],
        "evidence": ["src/app.py:1 no longer checks tenant id"],
        "remediation": "restore the tenant guard",
        "regression_test": "assert 401 when token is absent",
    }
    finding.update(overrides)
    return finding


def release_gate(
    status: str,
    overrides: dict[str, str] | None = None,
    criticality: dict[str, str] | None = None,
) -> dict:
    overrides = overrides or {}
    criticality = criticality or {}
    checks = []
    for item in RELEASE_CHECK_ITEMS:
        item_status = overrides.get(item, "PASS")
        gap = None if item_status == "PASS" else "follow-up required"
        checks.append(
            {
                "item": item,
                "status": item_status,
                "criticality": criticality.get(item, "critical"),
                "evidence": ["checked"],
                "condition_or_gap": gap,
            }
        )
    return {
        "status": status,
        "checks": checks,
        "blocking_reasons": ["release blocker"] if status in {"NOT_READY", "NOT_VERIFIED"} else [],
        "conditions": ["follow-up required"] if status == "READY_WITH_CONDITIONS" else [],
    }


def signed_manifest(**fields: object) -> dict:
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "repository_root": "/repo",
        "target_type": "local_diff",
        "target_locator": "test",
        "digest_scheme": DIGEST_SCHEME,
        "target_id": TARGET_ID,
        "committed_range": {
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "range_digest": "6" * 64,
        },
        "worktree_state": {
            "staged_digest": "1" * 64,
            "unstaged_digest": "2" * 64,
            "untracked_digest": "3" * 64,
            "diff_digest": DIGEST,
        },
        "changed_files": ["M:src/app.py"],
        "preexisting_changes": [],
        "documents": [],
        "review_scale": SCALE,
        "freeze_index": 1,
        "retry_policy": "gated",
        "produced_by": PRODUCED_BY,
        "generator_version": GENERATOR_VERSION,
        "git_probe_digest": "5" * 64,
    }
    for key, value in fields.items():
        if "." in key:
            group, member = key.split(".", 1)
            target = manifest.setdefault(group, {})
            if value is None:
                target.pop(member, None)
            else:
                target[member] = value
        elif value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    manifest.setdefault(
        "changed_files_digest", sha256(canonical_json(manifest["changed_files"]))
    )
    sign_manifest(manifest)
    return manifest


def copy_snapshot_fields(report: dict, manifest: dict) -> dict:
    """按脚本那张路径表把 manifest 抄进报告：manifest 侧已分组，报告侧保持扁平。"""
    for field, path in SNAPSHOT_REPORT_FIELDS.items():
        report["snapshot"][field] = snapshot_field(manifest, *path)
    report["scope"]["target_type"] = manifest["target_type"]
    report["scope"]["target_locator"] = manifest["target_locator"]
    report["scope"]["target_id"] = manifest["target_id"]
    return report


def ledger_record(step: int, role: str, instance_id: str, **fields: object) -> dict:
    record = {
        "step": step,
        "role": role,
        "instance_id": instance_id,
        "input_diff_digest": DIGEST,
        "returned_status": "COMPLETED",
        "started_at": f"2026-09-09T00:00:{step:02d}Z",
        "ended_at": f"2026-09-09T00:00:{step:02d}Z",
    }
    record.update(fields)
    return record


def init_repo(repo: Path) -> str:
    """建一个基线干净的 git 仓库，返回 base sha；跨 SKILL 的夹具共用这一份实现。"""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "gc.auto", "0"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    # 归一化初始分支名，避免宿主 init.defaultBranch 差异影响基线优先级断言。
    subprocess.run(["git", "branch", "-M", "master"], cwd=repo, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def git_in(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def freeze_cli(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SKILL_DIR / "scripts" / "freeze_review_snapshot.py"), *extra],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )


def load_skill_module(name: str, path: Path):
    """按路径 import 另一个 SKILL 的脚本：跨 SKILL 的夹具用真产物，不手抄形状。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # bugfix_state 会 import 同目录的 validate_bugfix_proposal，兄弟目录不在 sys.path 上，
    # 所以按路径加载时临时把它的 home 顶进去（与直接把脚本当 CLI 跑的语义一致）。
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


def report_for_manifest(manifest: dict) -> dict:
    report = copy_snapshot_fields(valid_report(), manifest)
    report["validation"][0]["diff_digest"] = snapshot_field(manifest, "worktree_state", "diff_digest")
    return report


def valid_ledger() -> list[dict]:
    return [
        ledger_record(8, "review-specialist", DEFAULT_OWNER),
        ledger_record(10, "review-evidence", "EV-01"),
        ledger_record(18, "review-evidence", "EV-02", started_at="2026-09-09T00:01:00Z", ended_at="2026-09-09T00:02:00Z"),
    ]


class ReportContractTests(unittest.TestCase):
    def test_complete_approve_requires_traceability_and_validation(self) -> None:
        report = valid_report()
        self.assertEqual(ReportValidator().validate(report), [])

        report["traceability"] = []
        report["validation"] = []
        errors = ReportValidator().validate(report)
        self.assertTrue(any("$.traceability" in error for error in errors))
        self.assertTrue(any("$.validation" in error for error in errors))

    def test_missing_documents_cannot_be_approved(self) -> None:
        report = valid_report()
        report["review_context"] = {"review_mode": "LITE", "documents_status": "NOT_AVAILABLE"}
        errors = ReportValidator().validate(report)
        self.assertTrue(any("cannot be APPROVE" in error for error in errors))

    def test_waived_review_is_blocked(self) -> None:
        report = valid_report()
        report["review_context"] = {"review_mode": "WAIVED", "documents_status": "available"}
        report["verdict"] = "BLOCKED"
        report["blockers"] = ["system review was waived"]
        self.assertEqual(ReportValidator().validate(report), [])

    def test_release_gate_fail_cannot_be_ready_with_conditions(self) -> None:
        report = valid_report()
        report["verdict"] = "APPROVE_WITH_FOLLOW_UPS"
        report["release_gate"] = release_gate(
            "READY_WITH_CONDITIONS", {"rollback": "FAIL", "monitoring": "CONDITIONAL"}
        )
        report["release_gate"]["conditions"] = ["add alert"]
        errors = ReportValidator().validate(report)
        self.assertTrue(any("any FAIL check requires NOT_READY" in error for error in errors))

    def test_snapshot_manifest_is_bound_to_report(self) -> None:
        report = valid_report()
        manifest = signed_manifest()
        report["snapshot"]["manifest_digest"] = manifest["manifest_digest"]
        self.assertEqual(ReportValidator(snapshot_manifest=manifest).validate(report), [])

        report["snapshot"]["head_sha"] = "c" * 40
        self.assertTrue(ReportValidator(snapshot_manifest=manifest).validate(report))

    def test_cli_validates_against_manifest_file(self) -> None:
        report = valid_report()
        manifest = signed_manifest()
        copy_snapshot_fields(report, manifest)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            manifest_path = root / "snapshot.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "validate_review_report.py"), "--snapshot-manifest", str(manifest_path), str(report_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_formal_finding_requires_e2_or_e3(self) -> None:
        report = valid_report()
        report["verdict"] = "REQUEST_CHANGES"
        report["findings"] = [valid_finding()]
        self.assertEqual(ReportValidator().validate(report), [])

        for weak in ("E1", "E0"):
            with self.subTest(evidence_level=weak):
                candidate = valid_report()
                candidate["verdict"] = "REQUEST_CHANGES"
                candidate["findings"] = [valid_finding(evidence_level=weak)]
                errors = ReportValidator().validate(candidate)
                self.assertTrue(any("formal findings require E2 or E3" in error for error in errors))

    def test_lite_mode_requires_missing_documents(self) -> None:
        report = valid_report()
        report["review_context"] = {"review_mode": "LITE", "documents_status": "available"}
        errors = ReportValidator().validate(report)
        self.assertTrue(any("LITE requires documents_status" in error for error in errors))

    def test_release_gate_ready_rejects_conditional_check(self) -> None:
        report = valid_report()
        report["release_gate"] = release_gate("READY", {"monitoring": "CONDITIONAL"})
        errors = ReportValidator().validate(report)
        self.assertTrue(any("READY permits only PASS" in error for error in errors))

    def test_release_gate_not_ready_requires_fail_check(self) -> None:
        report = valid_report()
        report["release_gate"] = release_gate("NOT_READY")
        errors = ReportValidator().validate(report)
        self.assertTrue(any("NOT_READY requires a FAIL check" in error for error in errors))

    def test_release_gate_not_verified_requires_blockers(self) -> None:
        report = valid_report()
        gate = release_gate("NOT_VERIFIED", {"rollback": "NOT_VERIFIED"})
        gate["blocking_reasons"] = []
        report["release_gate"] = gate
        errors = ReportValidator().validate(report)
        self.assertTrue(any("NOT_VERIFIED requires a NOT_VERIFIED check" in error for error in errors))

    def test_release_gate_fail_takes_precedence_over_lower_status_checks(self) -> None:
        # release-gate.md 允许混合严重度：FAIL 强制 NOT_READY 时，不得再因
        # 同时存在 CONDITIONAL / NOT_VERIFIED 项而报互斥错误（历史死锁回归）。
        cases = (
            ("NOT_READY", {"rollback": "FAIL", "monitoring": "CONDITIONAL"}),
            ("NOT_READY", {"rollback": "FAIL", "monitoring": "NOT_VERIFIED"}),
            ("NOT_VERIFIED", {"rollback": "NOT_VERIFIED", "monitoring": "CONDITIONAL"}),
        )
        for status, overrides in cases:
            with self.subTest(status=status, overrides=tuple(sorted(overrides))):
                report = valid_report()
                report["verdict"] = "REQUEST_CHANGES"
                report["release_gate"] = release_gate(status, overrides)
                errors = [error for error in ReportValidator().validate(report) if error.startswith("$.release_gate")]
                self.assertEqual(errors, [])

    def test_release_gate_precedence_does_not_relax_wrong_status(self) -> None:
        # FAIL + CONDITIONAL 仍不得伪装成 READY_WITH_CONDITIONS。
        report = valid_report()
        report["verdict"] = "REQUEST_CHANGES"
        report["release_gate"] = release_gate("READY_WITH_CONDITIONS", {"rollback": "FAIL", "monitoring": "CONDITIONAL"})
        errors = ReportValidator().validate(report)
        self.assertTrue(any("any FAIL check requires NOT_READY" in error for error in errors))

    def test_forged_manifest_digest_is_rejected(self) -> None:
        report = valid_report()
        manifest = signed_manifest()
        forged = "e" * 64
        manifest["manifest_digest"] = forged
        report["snapshot"]["manifest_digest"] = forged
        errors = ReportValidator(snapshot_manifest=manifest).validate(report)
        self.assertTrue(any("does not match manifest contents" in error for error in errors))

    def test_validation_record_must_match_snapshot_digest(self) -> None:
        report = valid_report()
        report["validation"][0]["diff_digest"] = "9" * 64
        errors = ReportValidator().validate(report)
        self.assertTrue(any("must match $.snapshot.diff_digest" in error for error in errors))

    def _validate_cli(
        self, root: Path, report: dict, manifest: dict, *extra: str
    ) -> subprocess.CompletedProcess:
        report_path = root / "report.json"
        manifest_path = root / "snapshot.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(SKILL_DIR / "scripts" / "validate_review_report.py"),
                "--snapshot-manifest",
                str(manifest_path),
                *extra,
                str(report_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def _cli_report(self) -> tuple[dict, dict]:
        report = valid_report()
        manifest = signed_manifest()
        copy_snapshot_fields(report, manifest)
        return report, manifest

    def _cli_broken_report(self) -> tuple[dict, dict]:
        # 两处已知路径的错误：$.summary 与 $.coverage，用来验证 --only 只切视图不切检查。
        report, manifest = self._cli_report()
        report["summary"] = ""
        report["coverage"] = []
        return report, manifest

    def test_errors_json_holds_the_full_machine_readable_error_list(self) -> None:
        report, manifest = self._cli_broken_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out_path = root / "errors.json"
            result = self._validate_cli(root, report, manifest, "--errors-json", str(out_path))
            self.assertEqual(result.returncode, 1, result.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["valid"])
            self.assertTrue(payload["gate_result"])
            self.assertIsNone(payload["filtered_by"])
            self.assertEqual(payload["total_errors"], 2)
            self.assertEqual(payload["matched_errors"], 2)
            self.assertEqual([item["path"] for item in payload["errors"]], ["$.summary", "$.coverage"])
            self.assertEqual(payload["errors"][0]["message"], "must be a non-empty string")
            # $.coverage 的消息本身带 ": "，切分只能切第一处。
            self.assertTrue(payload["errors"][1]["message"].startswith("missing dimensions: "))
            self.assertIn("- $.summary: must be a non-empty string", result.stderr)

    def test_only_filters_the_human_view_but_never_the_checks(self) -> None:
        report, manifest = self._cli_broken_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out_path = root / "errors.json"
            result = self._validate_cli(
                root, report, manifest, "--errors-json", str(out_path), "--only", "$.summary"
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("FILTERED: 1 of 2 error(s) match --only", result.stderr)
            self.assertIn("- $.summary: must be a non-empty string", result.stderr)
            self.assertNotIn("$.coverage", result.stderr)
            self.assertNotIn("VALID:", result.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["gate_result"])
            self.assertEqual(payload["filtered_by"], ["$.summary"])
            self.assertEqual(payload["total_errors"], 2)
            self.assertEqual(payload["matched_errors"], 1)
            self.assertEqual(len(payload["errors"]), 2)

    def test_a_filtered_run_on_a_clean_report_is_not_printed_as_valid(self) -> None:
        report, manifest = self._cli_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out_path = root / "errors.json"
            result = self._validate_cli(
                root, report, manifest, "--errors-json", str(out_path), "--only", "$.findings"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("FILTERED: 0 of 0 error(s)", result.stderr)
            self.assertNotIn("VALID:", result.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["valid"])
            self.assertFalse(payload["gate_result"])

    def test_only_rejects_a_prefix_that_is_not_a_json_path(self) -> None:
        report, manifest = self._cli_broken_report()
        with tempfile.TemporaryDirectory() as directory:
            result = self._validate_cli(Path(directory), report, manifest, "--only", "findings")
            self.assertEqual(result.returncode, 2)
            self.assertIn("--only takes a JSON path", result.stderr)

    def test_a_cp936_console_still_gets_the_utf8_error_output(self) -> None:
        # CW4-d（C3）：owner 等字段由模型书写，可能含 cp936 码表外的字符；校验器必须把报错
        # 如实打出来，而不是崩在 print 里。子进程强制以 cp936 启动，复现 Windows 控制台口径。
        report, manifest = self._cli_report()
        report["coverage"][0]["owner"] = "评审𠮷"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            manifest_path = root / "snapshot.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "dispatch-ledger.jsonl").write_text(
                "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in valid_ledger()),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "validate_review_report.py"),
                    "--snapshot-manifest",
                    str(manifest_path),
                    str(report_path),
                ],
                capture_output=True,
                check=False,
                env={**os.environ, "PYTHONIOENCODING": "cp936", "PYTHONUTF8": "0"},
            )
            self.assertNotIn(b"Traceback", result.stderr)
            self.assertEqual(result.returncode, 1, result.stderr.decode("utf-8", "replace"))
            # 严格解码：子进程若按 GBK 写出这两个汉字，这里就抛错而不是静默通过。
            stderr = result.stderr.decode("utf-8")
            self.assertIn("never appears in the dispatch ledger", stderr)
            self.assertIn("评审𠮷", stderr)


class ReleaseGateCriticalityTests(unittest.TestCase):
    """CW1-e：如实标注非关键缺口不再比改标 NOT_APPLICABLE 更贵。"""

    def test_non_critical_not_verified_allows_approve_with_follow_ups(self) -> None:
        report = valid_report()
        report["verdict"] = "APPROVE_WITH_FOLLOW_UPS"
        report["release_gate"] = release_gate(
            "READY_WITH_CONDITIONS",
            {"observation_window": "NOT_VERIFIED"},
            {"observation_window": "non_critical"},
        )
        self.assertEqual(ReportValidator().validate(report), [])

    def test_critical_not_verified_still_blocks(self) -> None:
        report = valid_report()
        report["verdict"] = "APPROVE_WITH_FOLLOW_UPS"
        report["release_gate"] = release_gate("NOT_VERIFIED", {"rollback": "NOT_VERIFIED"})
        errors = ReportValidator().validate(report)
        self.assertTrue(any("must be BLOCKED" in error for error in errors))

        # 关键项未核实也不能靠改 Gate 状态降级成"有条件放行"。
        report = valid_report()
        report["verdict"] = "APPROVE_WITH_FOLLOW_UPS"
        report["release_gate"] = release_gate("READY_WITH_CONDITIONS", {"rollback": "NOT_VERIFIED"})
        errors = ReportValidator().validate(report)
        self.assertTrue(any("critical NOT_VERIFIED checks" in error for error in errors))

    def test_not_applicable_check_requires_reason(self) -> None:
        report = valid_report()
        gate = release_gate("READY_WITH_CONDITIONS", {"data_repair": "NOT_APPLICABLE"}, {"data_repair": "non_critical"})
        gate["conditions"] = ["confirm no schema change"]
        gate["checks"] = [
            {**check, "condition_or_gap": None} if check["item"] == "data_repair" else check
            for check in gate["checks"]
        ]
        report["release_gate"] = gate
        errors = ReportValidator().validate(report)
        self.assertTrue(any("condition_or_gap: must be a non-empty string" in error for error in errors))

    def test_gate_level_not_applicable_must_still_list_every_check(self) -> None:
        # 把整道 Gate 标成 NOT_APPLICABLE 不能再用"什么都不查"来兑现。
        report = valid_report()
        report["release_gate"] = {"status": "NOT_APPLICABLE", "checks": [], "blocking_reasons": [], "conditions": []}
        errors = ReportValidator().validate(report)
        self.assertTrue(any("must contain all release checks" in error for error in errors))

        report = valid_report()
        gate = release_gate("NOT_APPLICABLE", {item: "PASS" for item in RELEASE_CHECK_ITEMS})
        report["release_gate"] = gate
        errors = ReportValidator().validate(report)
        self.assertTrue(any("NOT_APPLICABLE requires every release check" in error for error in errors))

    def test_release_check_requires_criticality(self) -> None:
        report = valid_report()
        gate = release_gate("READY")
        gate["checks"] = [{k: v for k, v in check.items() if k != "criticality"} for check in gate["checks"]]
        report["release_gate"] = gate
        errors = ReportValidator().validate(report)
        self.assertTrue(any(".criticality: is required" in error for error in errors))


class DispatchLedgerTests(unittest.TestCase):
    """CW1-f：覆盖矩阵的 owner 必须是台账里真实派发过的实例。"""

    def test_ledger_backed_coverage_owner_passes(self) -> None:
        report = valid_report()
        self.assertEqual(ReportValidator(dispatch_ledger=valid_ledger()).validate(report), [])

    def test_coverage_owner_missing_from_ledger(self) -> None:
        report = valid_report()
        ledger = valid_ledger()
        ledger[0]["instance_id"] = "SP-99"
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any("never appears in the dispatch ledger" in error for error in errors))

    def test_re_review_must_use_a_fresh_verifier(self) -> None:
        report = valid_report()
        ledger = valid_ledger()
        ledger[2]["instance_id"] = "EV-01"  # 步骤 18 复用步骤 10 的 Verifier
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any("fresh Evidence Verifier" in error for error in errors))

    def test_dispatch_order_is_enforced(self) -> None:
        report = valid_report()
        ledger = valid_ledger()
        ledger[2]["started_at"] = "2026-09-09T00:00:00Z"  # 早于步骤 10 的派发
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any("step 18 re-review dispatch starts before" in error for error in errors))

    def test_dispatch_must_target_the_frozen_diff(self) -> None:
        report = valid_report()
        ledger = valid_ledger()
        ledger[0]["input_diff_digest"] = "9" * 64
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any("not run against the frozen snapshot" in error for error in errors))

    def test_empty_ledger_is_rejected(self) -> None:
        report = valid_report()
        errors = ReportValidator(dispatch_ledger=[]).validate(report)
        self.assertTrue(any("ledger is empty" in error for error in errors))

    def test_duplicate_instance_is_rejected(self) -> None:
        report = valid_report()
        ledger = valid_ledger() + [ledger_record(8, "review-specialist", DEFAULT_OWNER)]
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any("is already dispatched earlier" in error for error in errors))

    def test_declared_dispatch_inputs_must_be_well_formed(self) -> None:
        # CW2-c：历史台账没有 inputs 键仍然合法；一旦声明就必须是绝对路径 + 64-hex。
        report = valid_report()
        self.assertEqual(ReportValidator(dispatch_ledger=valid_ledger()).validate(report), [])
        ledger = valid_ledger()
        ledger[0]["inputs"] = []
        self.assertEqual(ReportValidator(dispatch_ledger=ledger).validate(report), [])
        ledger[0]["inputs"] = [
            {"path": "steps/03-context-contract.json", "digest": DIGEST},
            {"path": "/run/steps/03-context-contract.json", "digest": "nope"},
            {"path": "/run/steps/03-context-contract.json", "digest": DIGEST},
            {"path": "/run/steps/07-impact-surface.json"},
            "not-an-object",
        ]
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any("inputs[0].path" in error and "absolute" in error for error in errors))
        self.assertTrue(any("inputs[1].digest" in error for error in errors))
        self.assertTrue(any("duplicate input" in error for error in errors))
        self.assertTrue(any("inputs[3].digest" in error and "is required" in error for error in errors))
        self.assertTrue(any("inputs[4]" in error and "must be an object" in error for error in errors))
        broken = valid_ledger()
        broken[0]["inputs"] = "steps/03-context-contract.json"
        errors = ReportValidator(dispatch_ledger=broken).validate(report)
        self.assertTrue(any("inputs" in error and "list of" in error for error in errors))

    def test_cli_auto_discovers_ledger_next_to_manifest(self) -> None:
        report = valid_report()
        manifest = signed_manifest()
        report["snapshot"]["manifest_digest"] = manifest["manifest_digest"]
        ledger = valid_ledger()
        ledger[0]["instance_id"] = "SP-99"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            manifest_path = root / "snapshot.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "dispatch-ledger.jsonl").write_text(
                "".join(json.dumps(entry) + "\n" for entry in ledger), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "validate_review_report.py"),
                    "--snapshot-manifest",
                    str(manifest_path),
                    str(report_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("never appears in the dispatch ledger", result.stderr)

    def test_record_dispatch_cli_appends_and_refuses_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "nested" / "dispatch-ledger.jsonl"
            base = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "record_review_dispatch.py"),
                "--ledger",
                str(ledger_path),
                "--step",
                "08",
                "--role",
                "review-specialist",
                "--instance-id",
                "SP-01",
                "--input-diff-digest",
                DIGEST,
                "--returned-status",
                "COMPLETED",
            ]
            first = subprocess.run(base, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(ledger_path.exists())
            record = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["step"], 8)
            self.assertEqual(record["instance_id"], "SP-01")
            reused = subprocess.run(base, capture_output=True, text=True, check=False)
            self.assertEqual(reused.returncode, 2)
            self.assertIn("already in", reused.stderr)
            bad_digest = list(base)
            bad_digest[bad_digest.index("--input-diff-digest") + 1] = "not-a-digest"
            result = subprocess.run(bad_digest, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("SHA-256", result.stderr)
            self.assertEqual(len(ledger_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_ledger_above_the_dispatch_budget_is_rejected(self) -> None:
        # CW4-a：派发预算同时在门禁侧核对，绕开登记脚本手搓台账也不能超。
        report = valid_report()
        ledger = [
            ledger_record(8, "review-specialist", f"SP-{index:02d}")
            for index in range(MAX_DISPATCHES + 1)
        ]
        errors = ReportValidator(dispatch_ledger=ledger).validate(report)
        self.assertTrue(any(f"MAX_DISPATCHES={MAX_DISPATCHES}" in error for error in errors))

    def test_record_dispatch_cli_refuses_the_dispatch_over_budget(self) -> None:
        # 写入侧第 15 条直接拒绝，并把出路写成步骤 22 的 BLOCKED 兜底。
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "dispatch-ledger.jsonl"
            ledger_path.write_text(
                "".join(
                    json.dumps(ledger_record(8, "review-specialist", f"SP-{index:02d}")) + "\n"
                    for index in range(MAX_DISPATCHES)
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SKILL_DIR / "scripts" / "record_review_dispatch.py"),
                    "--ledger", str(ledger_path),
                    "--step", "08", "--role", "review-specialist", "--instance-id", "SP-99",
                    "--input-diff-digest", DIGEST, "--returned-status", "COMPLETED",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(f"MAX_DISPATCHES={MAX_DISPATCHES}", result.stderr)
            self.assertIn("BLOCKED", result.stderr)
            self.assertEqual(len(ledger_path.read_text(encoding="utf-8").splitlines()), MAX_DISPATCHES)


class SnapshotScriptTests(unittest.TestCase):
    def _init_repo(self, repo: Path) -> str:
        return init_repo(repo)

    def test_snapshot_is_deterministic_and_covers_dirty_layers(self) -> None:
        # Same .git rmtree race as below: this case commits, so tolerate cleanup failures
        # and keep ``gc --auto`` from forking a writer into .git.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "gc.auto", "0"], cwd=repo, check=True)
            tracked = repo / "app.py"
            tracked.write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            tracked.write_text("print('changed')\n", encoding="utf-8")
            (repo / "new.py").write_text("print('new')\n", encoding="utf-8")
            args = Namespace(
                repo=repo,
                base_sha=base,
                head_sha=base,
                target_type="local_diff",
                target_locator="test",
                preexisting_change=[],
            )
            first = build_snapshot(args)
            second = build_snapshot(args)
            self.assertEqual(first, second)
            self.assertIn("M:app.py", first["changed_files"])
            self.assertIn("A:new.py", first["changed_files"])
            self.assertEqual(len(first["worktree_state"]["diff_digest"]), 64)
            self.assertEqual(len(first["committed_range"]["range_digest"]), 64)
            self.assertEqual(first["digest_scheme"], DIGEST_SCHEME)
            self.assertEqual(first["target_id"], target_id_for(repo, first["committed_range"]["head_sha"]))
            self.assertEqual(len(first["manifest_digest"]), 64)

    def test_parse_name_status_handles_renames(self) -> None:
        raw = b"M\0app.py\0R100\0old.py\0new.py\0A\0fresh.py\0"
        self.assertEqual(
            parse_name_status(raw),
            ["M:app.py", "R:old.py->new.py", "A:fresh.py"],
        )

    def test_untracked_manifest_classifies_files_and_symlinks(self) -> None:
        # ignore_cleanup_errors tolerates the .git rmtree race seen on WSL/9p mounts.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "regular.py").write_text("print('x')\n", encoding="utf-8")
            try:
                os.symlink("regular.py", repo / "link.py")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {exc}")
            paths, digest, lines = untracked_manifest(repo)
            self.assertEqual(paths, ["link.py", "regular.py"])
            self.assertEqual(len(digest), 64)
            self.assertEqual(lines, 1)  # 只有 regular.py 有行可数，symlink 记 0

    def test_run_git_raises_snapshot_error_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SnapshotError):
                run_git(Path(directory), "rev-parse", "--verify", "deadbeef^{commit}")

    def test_auto_target_resolves_merge_base_and_prefers_master(self) -> None:
        # ignore_cleanup_errors tolerates the .git rmtree race seen on WSL/9p mounts.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            # main 与 master 同时存在时，locator 必须记录实际选择的 master 基线。
            subprocess.run(["git", "branch", "main"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
            (repo / "new.py").write_text("print('feat')\n", encoding="utf-8")
            subprocess.run(["git", "add", "new.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            resolved = resolve_auto_branch_target(repo)
            self.assertEqual(resolved["target_type"], "branch")
            self.assertEqual(resolved["target_locator"], "master...feature")
            self.assertEqual(resolved["base_sha"], base)
            self.assertEqual(resolved["head_sha"], head)

    def test_auto_target_blocks_baseline_branch_and_detached_head(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            self._init_repo(repo)
            with self.assertRaises(TargetBlocked):
                resolve_auto_branch_target(repo)  # 当前分支即 master，无分支差异
            subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
            subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True)
            with self.assertRaises(TargetBlocked):
                resolve_auto_branch_target(repo)  # detached HEAD

    def test_untracked_manifest_excludes_codekit_runtime_dir(self) -> None:
        # 上一轮审查产物不得计入 untracked_digest（观测者效应固定排除）。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "regular.py").write_text("print('x')\n", encoding="utf-8")
            artifact = repo / ".codekit" / "code-review" / "run-1"
            artifact.mkdir(parents=True)
            (artifact / "review-snapshot.json").write_text("{}", encoding="utf-8")
            paths, _, _ = untracked_manifest(repo)
            self.assertEqual(paths, ["regular.py"])

    def test_report_skeleton_prefills_only_manifest_bound_fields(self) -> None:
        manifest = signed_manifest()
        skeleton = build_report_skeleton(manifest)
        for field, path in SNAPSHOT_REPORT_FIELDS.items():
            self.assertEqual(skeleton["snapshot"][field], snapshot_field(manifest, *path))
        self.assertEqual(skeleton["scope"]["target_locator"], manifest["target_locator"])
        self.assertEqual(skeleton["scope"]["target_id"], manifest["target_id"])
        self.assertEqual({entry["dimension"] for entry in skeleton["coverage"]}, set(DIMENSIONS))
        errors = ReportValidator(snapshot_manifest=manifest).validate(skeleton)
        # 机械字段已绑定 manifest；判断字段留空必须让校验失败，骨架不可被直接交付。
        self.assertFalse([error for error in errors if error.startswith(("$.snapshot", "$.scope.target"))])
        self.assertTrue(any(error.startswith("$.verdict") for error in errors))

    def test_skeleton_prefills_every_release_check_item(self) -> None:
        # CW1-e 的配套：十项检查由骨架给出，漏项必须靠删行伪装而不是少写。
        skeleton = build_report_skeleton(signed_manifest())
        checks = skeleton["release_gate"]["checks"]
        self.assertEqual([entry["item"] for entry in checks], list(RELEASE_CHECK_ITEMS))
        self.assertTrue(all(entry["status"] is None and entry["criticality"] is None for entry in checks))

    def _freeze_cli(self, repo: Path, *extra: str) -> subprocess.CompletedProcess:
        return freeze_cli(repo, *extra)

    def test_emit_manifest_creates_missing_run_dir(self) -> None:
        # CW1-a：步骤 02 不再依赖 shell 重定向，父目录不存在也能落盘。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            manifest_path = repo / ".codekit" / "code-review" / "run-1" / "review-snapshot.json"
            result = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(manifest_path),
                "--emit-skeleton", str(manifest_path.with_name("review-report.json")),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["manifest_path"], str(manifest_path))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA_VERSION)
            # CW1-c：来源指纹三项齐全，且 git_probe_digest 覆盖脚本真实执行的 git 序列。
            self.assertEqual(manifest["produced_by"], PRODUCED_BY)
            self.assertEqual(manifest["generator_version"], GENERATOR_VERSION)
            self.assertEqual(len(manifest["git_probe_digest"]), 64)

    def test_emit_skeleton_refuses_to_clobber_a_filled_report(self) -> None:
        # CW1-b / A6：重入步骤 02 不得把已填完的报告静默压回骨架。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            run_dir = repo / "run"
            manifest_path = run_dir / "review-snapshot.json"
            report_path = run_dir / "review-report.json"
            first = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(manifest_path), "--emit-skeleton", str(report_path),
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            filled = json.loads(report_path.read_text(encoding="utf-8"))
            filled["summary"] = "written by step 20"
            report_path.write_text(json.dumps(filled), encoding="utf-8")

            again = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(run_dir / "second.json"), "--emit-skeleton", str(report_path),
            )
            self.assertEqual(again.returncode, 2)
            self.assertIn("refusing to overwrite", again.stderr)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["summary"], "written by step 20")
            self.assertFalse((run_dir / "second.json").exists())

            forced = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(run_dir / "third.json"),
                "--emit-skeleton", str(report_path), "--force-skeleton",
            )
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertNotIn("written by step 20", report_path.read_text(encoding="utf-8"))

    def _report_for_manifest(self, manifest: dict) -> dict:
        return report_for_manifest(manifest)

    def test_handwritten_manifest_without_provenance_is_rejected(self) -> None:
        # A1 回放：digest 算得出来，但缺来源指纹的 manifest 必须过不了关。
        manifest = signed_manifest()
        stripped = {key: value for key, value in manifest.items() if key not in {"produced_by", "generator_version", "git_probe_digest"}}
        # 手写 manifest 的典型形状：1.0 时代的键集合 + 自己算的 self-digest。
        stripped["schema_version"] = "1.0"
        stripped.pop("manifest_digest", None)
        stripped["manifest_digest"] = sha256(canonical_json(stripped))
        report = self._report_for_manifest(stripped)
        errors = ReportValidator(snapshot_manifest=stripped).validate(report)
        self.assertTrue(any("produced_by" in error for error in errors))
        self.assertTrue(any("git_probe_digest" in error for error in errors))
        self.assertTrue(any("schema_version" in error for error in errors))

    def test_check_worktree_flag_detects_drift_after_the_freeze(self) -> None:
        # A2 回放：冻结后又改了工作树——默认仍放行（保护历史报告复校验），
        # 带 --check-worktree 时重放四类 digest 并报 STALE_SNAPSHOT。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            manifest_path = repo / "snapshot.json"
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(manifest_path),
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report_path = repo / "report.json"
            report_path.write_text(
                json.dumps(self._report_for_manifest(manifest)), encoding="utf-8"
            )
            validator = str(SKILL_DIR / "scripts" / "validate_review_report.py")
            quiet = subprocess.run(
                [sys.executable, validator, "--snapshot-manifest", str(manifest_path), str(report_path)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(quiet.returncode, 0, quiet.stderr)

            (repo / "app.py").write_text("print('drifted after the freeze')\n", encoding="utf-8")
            checked = subprocess.run(
                [sys.executable, validator, "--snapshot-manifest", str(manifest_path), str(report_path), "--check-worktree"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(checked.returncode, 1)
            self.assertIn("STALE_SNAPSHOT", checked.stderr)
            self.assertIn("diff_digest", checked.stderr)
            untracked_later = repo / "late.py"
            untracked_later.write_text("print('late')\n", encoding="utf-8")
            still_stale = subprocess.run(
                [sys.executable, validator, "--snapshot-manifest", str(manifest_path), str(report_path), "--check-worktree"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(still_stale.returncode, 1)
            self.assertIn("untracked_digest", still_stale.stderr)

    def _repo_with_every_layer(self, repo: Path) -> tuple[str, str]:
        """造出四层同时有内容的仓库，返回 (base_sha, head_sha)。"""
        base = self._init_repo(repo)
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
        (repo / "committed.py").write_text("print('committed')\n", encoding="utf-8")
        subprocess.run(["git", "add", "committed.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)
        (repo / "pkg").mkdir()
        (repo / "pkg" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "pkg/mod.py"], cwd=repo, check=True)
        (repo / "app.py").write_text("print('unstaged')\n", encoding="utf-8")
        (repo / "loose.txt").write_text("untracked body\n", encoding="utf-8")
        head = str(run_git(repo, "rev-parse", "HEAD", text=True)).strip()
        return base, head

    def test_emit_diff_binds_one_artifact_to_the_manifest(self) -> None:
        # CW3-a：四段差异一次落盘，manifest 记录其摘要并覆盖在签名之内。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base, head = self._repo_with_every_layer(repo)
            diff_path = repo / ".codekit" / "code-review" / "run-1" / "review.diff"
            manifest_path = diff_path.with_name("review-snapshot.json")
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", head,
                "--target-type", "branch", "--target-locator", "master...feature",
                "--emit-manifest", str(manifest_path),
                "--emit-diff", str(diff_path), "--diff-shard", "by-directory",
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact = manifest["diff_artifact"]
            self.assertEqual(json.loads(freeze.stdout)["diff_artifact"]["digest"], artifact["digest"])
            data = diff_path.read_bytes()
            self.assertEqual(Path(artifact["path"]), diff_path.resolve())
            self.assertEqual(artifact["bytes"], len(data))
            self.assertEqual(sha256(data), artifact["digest"])
            self.assertEqual(artifact["sections"], ["committed", "staged", "unstaged", "untracked"])
            # 摘要登记后必须重签，否则换掉整段 diff_artifact 都不影响 manifest_digest。
            unsigned = dict(manifest)
            unsigned.pop("manifest_digest")
            self.assertEqual(sha256(canonical_json(unsigned)), manifest["manifest_digest"])
            text = data.decode("utf-8")
            self.assertEqual(text.count("##### section: "), 4)
            for entry in manifest["changed_files"]:
                path = entry.split(":", 1)[1].split("->")[-1]
                self.assertIn(path, text, f"{entry} 没有出现在 diff 读物里")
            self.assertIn("untracked body", text)

    def test_diff_shards_cover_every_chunk_and_self_verify(self) -> None:
        # 角色按分片读取，所以分片必须无遗漏、且每片摘要可自证。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base, head = self._repo_with_every_layer(repo)
            diff_path = repo / ".codekit" / "r" / "review.diff"
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", head,
                "--target-type", "branch", "--target-locator", "master...feature",
                "--emit-diff", str(diff_path), "--diff-shard", "by-directory",
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            artifact = json.loads(freeze.stdout)["diff_artifact"]
            index = json.loads(Path(artifact["shard_index"]).read_text(encoding="utf-8"))
            self.assertEqual(Path(index["diff_path"]), diff_path.resolve())
            corpus = ""
            for shard in index["shards"]:
                shard_path = Path(shard["shard_path"])
                self.assertTrue(shard_path.is_file(), shard_path)
                corpus += shard_path.read_text(encoding="utf-8")
                self.assertEqual(sha256(shard_path.read_bytes()), shard["digest"])
            self.assertEqual({shard["top"] for shard in index["shards"]}, {"_root", "pkg"})
            text = diff_path.read_text(encoding="utf-8")
            self.assertEqual(corpus.count("diff --git "), text.count("diff --git "))
            self.assertIn("### untracked file: loose.txt", corpus)
            self.assertLess(len(corpus), len(text), "分片只切正文，段头留在总读物里")

    def test_diff_shard_flag_requires_emit_diff(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            result = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--diff-shard", "by-directory",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--diff-shard requires --emit-diff", result.stderr)

    def test_check_worktree_detects_a_swapped_diff_artifact(self) -> None:
        # 派发只传路径之后，读物被中途改过必须是机器判据而不是靠角色自觉。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base, head = self._repo_with_every_layer(repo)
            manifest_path = repo / ".codekit" / "r" / "review-snapshot.json"
            diff_path = manifest_path.with_name("review.diff")
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", head,
                "--target-type", "branch", "--target-locator", "master...feature",
                "--emit-manifest", str(manifest_path), "--emit-diff", str(diff_path),
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIsNone(manifest["diff_artifact"]["shard_index"])
            report_path = manifest_path.with_name("review-report.json")
            report_path.write_text(json.dumps(self._report_for_manifest(manifest)), encoding="utf-8")
            validator = str(SKILL_DIR / "scripts" / "validate_review_report.py")
            argv = [
                sys.executable, validator, str(report_path),
                "--snapshot-manifest", str(manifest_path), "--check-worktree",
            ]
            clean = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            with diff_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("##### section: invented digest=" + "0" * 64 + "\n")
            tampered = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(tampered.returncode, 1)
            self.assertIn("diff_artifact digest changed", tampered.stderr)

    def test_declared_diff_artifact_must_be_well_formed(self) -> None:
        manifest = signed_manifest()
        errors = ReportValidator(snapshot_manifest=manifest).validate(self._report_for_manifest(manifest))
        self.assertEqual([error for error in errors if "diff_artifact" in error], [])
        broken = signed_manifest(
            diff_artifact={"path": "  ", "bytes": -1, "digest": "nope", "sections": ["committed"]}
        )
        errors = ReportValidator(snapshot_manifest=broken).validate(self._report_for_manifest(broken))
        for fragment in ("diff_artifact.path", "diff_artifact.digest", "diff_artifact.bytes",
                         "diff_artifact.sections", "diff_artifact.shard_index"):
            self.assertTrue(
                any(f"diff_artifact.{fragment.split('.')[-1]}" in error for error in errors),
                f"{fragment} 缺少形状校验：{errors}",
            )

    def test_large_change_list_is_compacted_in_the_skeleton(self) -> None:
        # CW3-c：大 MR 的骨架不再抄全量清单，但 count + 摘要 + 前缀必须能核回 manifest。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            total = CHANGE_LIST_INLINE_LIMIT + 5
            for index in range(total):
                (repo / f"extra_{index:03d}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
            run_dir = repo / ".codekit" / "code-review" / "run-big"
            manifest_path = run_dir / "review-snapshot.json"
            report_path = run_dir / "review-report.json"
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(manifest_path), "--emit-skeleton", str(report_path),
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            changed = manifest["changed_files"]
            self.assertGreater(len(changed), CHANGE_LIST_INLINE_LIMIT)
            compacted = json.loads(report_path.read_text(encoding="utf-8"))["snapshot"]["changed_files"]
            self.assertEqual(compacted["count"], len(changed))
            self.assertEqual(compacted["first"], changed[:CHANGE_LIST_PREVIEW])
            self.assertEqual(compacted["digest"], manifest["changed_files_digest"])
            list_path = Path(compacted["full_list_path"])
            self.assertEqual(list_path, (run_dir / "changed-files.txt").resolve())
            self.assertEqual(json.loads(freeze.stdout)["changed_files_path"], str(list_path))
            self.assertEqual(list_path.read_text(encoding="utf-8").splitlines(), changed)

            def report_with(fragment: dict) -> list[str]:
                report = self._report_for_manifest(manifest)
                report["snapshot"]["changed_files"] = json.loads(json.dumps(fragment))
                return ReportValidator(snapshot_manifest=manifest).validate(report)

            self.assertEqual(report_with(compacted), [])
            shrunk = dict(compacted, count=len(changed) - 1)
            self.assertTrue(any("$.snapshot.changed_files.count" in e for e in report_with(shrunk)))
            padded = dict(compacted, first=changed[:CHANGE_LIST_PREVIEW] + ["A:outside-the-scope.py"])
            self.assertTrue(any("$.snapshot.changed_files.first" in e for e in report_with(padded)))
            stale = dict(compacted, digest="0" * 64)
            self.assertTrue(any("$.snapshot.changed_files.digest" in e for e in report_with(stale)))
            # 清单不超阈值时改用摘要形状本身就是错——不给随手截断留口子。
            small = signed_manifest()
            small_report = self._report_for_manifest(small)
            small_report["snapshot"]["changed_files"] = compacted
            errors = ReportValidator(snapshot_manifest=small).validate(small_report)
            self.assertTrue(any("copied verbatim" in error for error in errors))

    def test_documents_are_frozen_into_the_signed_manifest(self) -> None:
        # CW2-b：步骤 01 解析到的 PRD/TDD/RPD 以内容摘要进入签名 manifest，
        # 报告侧照抄后即成为可核对的冻结输入，而不是模型自称读过的路径。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            base = self._init_repo(repo)
            (repo / "docs").mkdir()
            (repo / "docs" / "PRD.md").write_text("# PRD v1\n", encoding="utf-8")
            tdd = root / "TDD.md"
            tdd.write_text("# TDD v1\n", encoding="utf-8")
            run_dir = repo / ".codekit" / "code-review" / "run-doc"
            manifest_path = run_dir / "review-snapshot.json"
            report_path = run_dir / "review-report.json"
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(manifest_path), "--emit-skeleton", str(report_path),
                "--documents", "docs/PRD.md",
                "--documents", str(tdd),
                "--document-revision", f"{tdd}=2026-09-20",
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            documents = manifest["documents"]
            self.assertEqual([entry["locator"] for entry in documents], sorted(
                entry["locator"] for entry in documents
            ))
            self.assertEqual({str(repo / "docs" / "PRD.md"), str(tdd.resolve())},
                             {entry["locator"] for entry in documents})
            by_name = {Path(entry["locator"]).name: entry for entry in documents}
            self.assertEqual(
                set(by_name["PRD.md"]),
                {"locator", "revision", "content_digest"},
            )
            # 未给版本标签时，冻结摘要自身充当版本；给了标签则标签与摘要并存。
            prd_bytes = sha256((repo / "docs" / "PRD.md").read_bytes())
            self.assertEqual(by_name["PRD.md"]["content_digest"], prd_bytes)
            self.assertEqual(by_name["PRD.md"]["revision"], prd_bytes)
            self.assertEqual(by_name["TDD.md"]["revision"], "2026-09-20")
            self.assertEqual(by_name["TDD.md"]["content_digest"], sha256(tdd.read_bytes()))
            self.assertEqual(json.loads(freeze.stdout)["documents"], documents)
            # documents 必须在签名之内：换掉整段清单都会让 manifest_digest 对不上。
            unsigned = dict(manifest)
            unsigned.pop("manifest_digest")
            self.assertEqual(sha256(canonical_json(unsigned)), manifest["manifest_digest"])
            swapped = dict(manifest, documents=[])
            self.assertNotEqual(
                sha256(canonical_json({k: v for k, v in swapped.items() if k != "manifest_digest"})),
                manifest["manifest_digest"],
            )
            skeleton = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(skeleton["snapshot"]["documents"], documents)
            # 报告侧少抄一条即视为与冻结输入不符。
            report = self._report_for_manifest(manifest)
            self.assertEqual(ReportValidator(snapshot_manifest=manifest).validate(report), [])
            report["snapshot"]["documents"] = [documents[1]]
            errors = ReportValidator(snapshot_manifest=manifest).validate(report)
            self.assertTrue(any("$.snapshot.documents" in error for error in errors))

    def test_document_arguments_fail_before_signing(self) -> None:
        # 读不到的文档与没有配对路径的版本标签都在 rc=2 侧失败，不留半份 manifest。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            common = (
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
            )
            missing = self._freeze_cli(repo, *common, "--documents", "docs/absent.md")
            self.assertEqual(missing.returncode, 2)
            self.assertIn("unable to read frozen document", missing.stderr)
            unpaired = self._freeze_cli(
                repo, *common, "--documents", "app.py",
                "--document-revision", "docs/absent.md=v1",
            )
            self.assertEqual(unpaired.returncode, 2)
            self.assertIn("without a matching --documents entry", unpaired.stderr)
            malformed = self._freeze_cli(
                repo, *common, "--documents", "app.py", "--document-revision", "app.py"
            )
            self.assertEqual(malformed.returncode, 2)
            self.assertIn("PATH=REVISION", malformed.stderr)
            duplicated = self._freeze_cli(
                repo, *common, "--documents", "app.py", "--documents", "./app.py"
            )
            self.assertEqual(duplicated.returncode, 2)
            self.assertIn("duplicate --documents entry", duplicated.stderr)

    def test_frozen_document_entries_must_be_well_formed(self) -> None:
        good = {
            "locator": "/docs/PRD.md",
            "revision": "v1",
            "content_digest": DIGEST,
        }
        manifest = signed_manifest(documents=[good])
        self.assertEqual(
            [error for error in ReportValidator(snapshot_manifest=manifest).validate(
                self._report_for_manifest(manifest)) if "documents" in error],
            [],
        )
        broken = signed_manifest(documents=[
            {k: v for k, v in good.items() if k != "revision"},
            dict(good, locator="/docs/TDD.md", content_digest="nope"),
            dict(good),
        ])
        errors = ReportValidator(snapshot_manifest=broken).validate(self._report_for_manifest(broken))
        self.assertTrue(any("documents[0].revision" in error for error in errors))
        self.assertTrue(any("documents[1].content_digest" in error for error in errors))
        self.assertTrue(any("duplicate document entry" in error for error in errors))
        # 缺 documents 键 = 不是当前版本快照脚本的产物；空清单才是"无文档"的合法表达。
        legacy = signed_manifest()
        report = self._report_for_manifest(legacy)
        legacy.pop("documents")
        sign_manifest(legacy)
        errors = ReportValidator(snapshot_manifest=legacy).validate(report)
        self.assertTrue(any("$.snapshot.manifest.documents" in error for error in errors))

    def test_check_worktree_detects_an_edited_frozen_document(self) -> None:
        # 冻结后文档被改：工作树四段差异都没动，只有 documents 能暴露漂移。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            base = self._init_repo(repo)
            document = root / "PRD.md"
            document.write_text("# PRD v1\n", encoding="utf-8")
            run_dir = repo / ".codekit" / "code-review" / "run-check"
            manifest_path = run_dir / "review-snapshot.json"
            report_path = run_dir / "review-report.json"
            freeze = self._freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(manifest_path), "--documents", str(document),
            )
            self.assertEqual(freeze.returncode, 0, freeze.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report_path.write_text(
                json.dumps(self._report_for_manifest(manifest)), encoding="utf-8"
            )
            validator = str(SKILL_DIR / "scripts" / "validate_review_report.py")
            argv = [
                sys.executable, validator, "--snapshot-manifest", str(manifest_path),
                str(report_path), "--check-worktree",
            ]
            quiet = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            document.write_text("# PRD v1 edited after the freeze\n", encoding="utf-8")
            drifted = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(drifted.returncode, 1)
            self.assertIn("STALE_SNAPSHOT", drifted.stderr)
            self.assertIn("documents", drifted.stderr)
            self.assertNotIn("diff_digest", drifted.stderr)
            document.unlink()
            gone = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(gone.returncode, 1)
            self.assertIn("unable to replay snapshot", gone.stderr)

    def _frozen_run(self, repo: Path, run_id: str = "2026-09-20-demo") -> Path:
        base = self._init_repo(repo)
        run_dir = run_dir_for(repo, run_id)
        freeze = self._freeze_cli(
            repo,
            "--repo", str(repo), "--base-sha", base, "--head-sha", base,
            "--target-type", "local_diff", "--target-locator", "test",
            "--emit-manifest", str(run_dir / "review-snapshot.json"),
            "--emit-skeleton", str(run_dir / "review-report.json"),
        )
        self.assertEqual(freeze.returncode, 0, freeze.stderr)
        return run_dir

    def test_resume_lists_completed_and_missing_step_contracts(self) -> None:
        # CW2-d：中断后按磁盘现状续跑——缺哪份契约说哪一份，不靠模型回忆进度。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            run_dir = self._frozen_run(repo)
            resume = self._freeze_cli(repo, "--repo", str(repo), "--resume", "2026-09-20-demo")
            self.assertEqual(resume.returncode, 0, resume.stderr)
            state = json.loads(resume.stdout)
            self.assertTrue(state["manifest_frozen"])
            self.assertEqual(state["run_dir"], str(run_dir.resolve()))
            self.assertIsNotNone(state["diff_digest"])
            # CW4-a：续跑时就能看到冻结预算还剩几次，不必等到第 4 次被拒。
            self.assertEqual(state["freeze_index"], 1)
            self.assertEqual(state["freeze_budget"], MAX_REFREEZE)
            self.assertEqual(state["steps_completed"], [])
            self.assertEqual(
                [entry["step"] for entry in state["steps_missing"]],
                [step for step, _ in STEP_ARTIFACTS],
            )
            self.assertIn("步骤 03", state["next_action"])

            paths = dict(step_artifact_paths(run_dir.resolve()))
            for step in (3, 5):
                paths[step].parent.mkdir(parents=True, exist_ok=True)
                paths[step].write_text(json.dumps({"step": step}), encoding="utf-8")
            # 空文件不算已完成：写坏一半的契约不能骗过续跑判据。
            paths[7].parent.mkdir(parents=True, exist_ok=True)
            paths[7].write_text("", encoding="utf-8")
            state = json.loads(
                self._freeze_cli(repo, "--repo", str(repo), "--resume", "2026-09-20-demo").stdout
            )
            self.assertEqual([entry["step"] for entry in state["steps_completed"]], [3, 5])
            self.assertEqual(
                state["steps_completed"][0]["digest"], sha256(paths[3].read_bytes())
            )
            self.assertEqual(
                [entry["step"] for entry in state["steps_missing"]], [7, 9, 11, 16, 17]
            )
            self.assertIn("07-impact-surface.json", state["next_action"])
            for step in (7, 9, 11, 16, 17):
                paths[step].write_text(json.dumps({"step": step}), encoding="utf-8")
            state = json.loads(
                self._freeze_cli(repo, "--repo", str(repo), "--resume", "2026-09-20-demo").stdout
            )
            self.assertEqual(state["steps_missing"], [])
            self.assertIn("步骤 18", state["next_action"])
            (run_dir / "review-report.json").write_text(
                json.dumps({"verdict": "APPROVE"}), encoding="utf-8"
            )
            state = json.loads(
                self._freeze_cli(repo, "--repo", str(repo), "--resume", "2026-09-20-demo").stdout
            )
            self.assertTrue(state["report_filled"])
            self.assertIn("步骤 21", state["next_action"])

    def test_resume_rejects_unknown_run_and_freeze_flags(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            missing = self._freeze_cli(repo, "--repo", str(repo), "--resume", "no-such-run")
            self.assertEqual(missing.returncode, 2)
            self.assertIn("no run directory to resume", missing.stderr)
            run_dir = self._frozen_run(repo, "run-2")
            self.assertTrue((run_dir / "review-snapshot.json").is_file())
            conflicting = self._freeze_cli(
                repo,
                "--repo", str(repo), "--resume", "run-2",
                "--emit-manifest", str(run_dir / "other.json"),
            )
            self.assertEqual(conflicting.returncode, 2)
            self.assertIn("only inspects an existing run", conflicting.stderr)
            self.assertFalse((run_dir / "other.json").exists())

    def test_dispatch_record_hashes_the_contracts_it_handed_over(self) -> None:
        # CW2-c：交出去哪几份契约由脚本读文件算摘要，路径写错当场失败。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = root / "nested" / "dispatch-ledger.jsonl"
            contract = root / "steps" / "03-context-contract.json"
            contract.parent.mkdir()
            contract.write_text('{"modules": []}\n', encoding="utf-8")
            base = [
                sys.executable,
                str(SKILL_DIR / "scripts" / "record_review_dispatch.py"),
                "--ledger", str(ledger_path),
                "--step", "08", "--role", "review-specialist", "--instance-id", "SP-01",
                "--input-diff-digest", DIGEST, "--returned-status", "COMPLETED",
            ]
            recorded = subprocess.run(
                base + ["--input", str(contract), "--input", "./steps/../steps/03-context-contract.json"],
                capture_output=True, text=True, check=False, cwd=root,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            record = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(
                record["inputs"],
                [{"path": str(contract.resolve()), "digest": sha256(contract.read_bytes())}],
            )
            self.assertEqual(json.loads(recorded.stdout)["inputs"], record["inputs"])
            absent = subprocess.run(
                base + ["--instance-id", "SP-02", "--input", str(root / "gone.json")],
                capture_output=True, text=True, check=False, cwd=root,
            )
            self.assertEqual(absent.returncode, 2)
            self.assertIn("must name an existing file", absent.stderr)
            self.assertEqual(len(ledger_path.read_text(encoding="utf-8").splitlines()), 1)
            bare = subprocess.run(base[:4] + [
                "--step", "03", "--role", "review-context", "--instance-id", "CA-01",
                "--input-diff-digest", DIGEST, "--returned-status", "COMPLETED",
            ], capture_output=True, text=True, check=False, cwd=root)
            self.assertEqual(bare.returncode, 0, bare.stderr)
            self.assertEqual(json.loads(bare.stdout)["inputs"], [])

    def test_check_worktree_rehashes_declared_dispatch_inputs(self) -> None:
        # 派发后契约被改写：台账里的"我给了角色这份契约"必须当场失效。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            run_dir = self._frozen_run(repo, "run-3")
            manifest = json.loads((run_dir / "review-snapshot.json").read_text(encoding="utf-8"))
            contract = run_dir / "steps" / "03-context-contract.json"
            contract.parent.mkdir(parents=True, exist_ok=True)
            contract.write_text('{"modules": ["a"]}\n', encoding="utf-8")
            ledger_path = run_dir / "dispatch-ledger.jsonl"
            record = subprocess.run(
                [
                    sys.executable, str(SKILL_DIR / "scripts" / "record_review_dispatch.py"),
                    "--ledger", str(ledger_path),
                    "--step", "08", "--role", "review-specialist", "--instance-id", "SP-01",
                    "--input-diff-digest", manifest["worktree_state"]["diff_digest"],
                    "--returned-status", "COMPLETED", "--input", str(contract),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(record.returncode, 0, record.stderr)
            report_path = run_dir / "review-report.json"
            report_path.write_text(
                json.dumps(self._report_for_manifest(manifest)), encoding="utf-8"
            )
            argv = [
                sys.executable, str(SKILL_DIR / "scripts" / "validate_review_report.py"),
                "--snapshot-manifest", str(run_dir / "review-snapshot.json"),
                str(report_path), "--check-worktree",
            ]
            clean = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            contract.write_text('{"modules": ["a", "b"]}\n', encoding="utf-8")
            drifted = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(drifted.returncode, 1)
            self.assertIn("STALE_SNAPSHOT", drifted.stderr)
            self.assertIn("03-context-contract.json", drifted.stderr)
            self.assertIn("digest changed", drifted.stderr)
            contract.unlink()
            gone = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(gone.returncode, 1)
            self.assertIn("is unreadable", gone.stderr)

    def test_refreeze_budget_stops_the_fourth_frozen_snapshot(self) -> None:
        # CW4-a：E1 的放大路径是"改标签→重校验→再重冻结"，步骤 22 的 3 轮上限管不到它。
        # 同一审查目标冻结到第 4 份快照必须由脚本拒绝，并指向 BLOCKED 兜底。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)

            def freeze(run_id: str) -> subprocess.CompletedProcess:
                target = run_dir_for(repo, run_id)
                return self._freeze_cli(
                    repo,
                    "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                    "--target-type", "local_diff", "--target-locator", "test",
                    "--emit-manifest", str(target / "review-snapshot.json"),
                )

            def recorded_index(run_id: str) -> int:
                path = run_dir_for(repo, run_id) / "review-snapshot.json"
                return json.loads(path.read_text(encoding="utf-8"))["freeze_index"]

            def mutate(marker: str) -> None:
                (repo / "app.py").write_text(
                    f"print('base')\nprint({marker!r})\n", encoding="utf-8"
                )

            mutate("first")
            self.assertEqual(freeze("run-1").returncode, 0)
            self.assertEqual(recorded_index("run-1"), 1)
            # 同一份差异重复冻结不烧预算：重跑步骤 02 拿到的还是第 1 号快照。
            self.assertEqual(freeze("run-1").returncode, 0)
            self.assertEqual(recorded_index("run-1"), 1)

            mutate("second")
            self.assertEqual(freeze("run-1").returncode, 0)  # 原地覆盖也算新的一次冻结
            self.assertEqual(recorded_index("run-1"), 2)
            mutate("third")
            self.assertEqual(freeze("run-2").returncode, 0)  # 换 run-id 逃不开按目标计的谱系
            self.assertEqual(recorded_index("run-2"), 3)

            mutate("fourth")
            fourth = freeze("run-3")
            self.assertEqual(fourth.returncode, 2)
            self.assertIn(f"MAX_REFREEZE={MAX_REFREEZE}", fourth.stderr)
            self.assertIn("BLOCKED", fourth.stderr)
            self.assertFalse((run_dir_for(repo, "run-3") / "review-snapshot.json").exists())

    def test_prose_policy_writes_the_over_budget_snapshot_with_a_warning(self) -> None:
        # CW4-e：回滚开关把第 4 次冻结的拒绝降成告警，但放行凭据必须签进 manifest，
        # 而不是让模型事后叙述"这次是人工放行的"。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            manifest_path = run_dir_for(repo, "run-1") / "review-snapshot.json"

            def freeze(marker: str, *extra: str) -> subprocess.CompletedProcess:
                (repo / "app.py").write_text(f"print('base')\nprint({marker!r})\n", encoding="utf-8")
                return self._freeze_cli(
                    repo,
                    "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                    "--target-type", "local_diff", "--target-locator", "test",
                    "--emit-manifest", str(manifest_path), *extra,
                )

            for marker in ("one", "two", "three"):
                self.assertEqual(freeze(marker).returncode, 0)
            self.assertEqual(freeze("four").returncode, 2)

            allowed = freeze("five", "--retry-policy", "prose")
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertIn("WARN:", allowed.stderr)
            self.assertIn(f"MAX_REFREEZE={MAX_REFREEZE}", allowed.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["retry_policy"], "prose")
            self.assertGreater(manifest["freeze_index"], MAX_REFREEZE)
            self.assertEqual(json.loads(allowed.stdout)["retry_policy"], "prose")
            # 取值仍由 argparse 白名单把关。
            self.assertEqual(freeze("six", "--retry-policy", "always").returncode, 2)

    def test_manifest_retry_policy_must_be_a_known_value(self) -> None:
        for forged in ("gated-or-prose", None):
            with self.subTest(retry_policy=forged):
                manifest = signed_manifest(retry_policy=forged) if forged else {
                    key: value for key, value in signed_manifest().items() if key != "retry_policy"
                }
                report = self._report_for_manifest(manifest)
                errors = ReportValidator(snapshot_manifest=manifest).validate(report)
                self.assertTrue(
                    any(".retry_policy: must be one of" in error for error in errors), errors
                )

    def test_prose_rollback_still_needs_a_verbatim_disclosure(self) -> None:
        # 放行不等于静默：报告必须在 limitations 里带上 MAX_REFREEZE=<编号> 这个标记。
        manifest = signed_manifest(freeze_index=MAX_REFREEZE + 1, retry_policy="prose")
        report = self._report_for_manifest(manifest)
        errors = ReportValidator(snapshot_manifest=manifest).validate(report)
        self.assertTrue(any("$.limitations" in error and "retry_policy=prose" in error for error in errors), errors)
        self.assertFalse(any("freeze budget" in error for error in errors), errors)

        # 披露之后仍要如实降级结论：limitations 非空 ⇒ APPROVE_WITH_FOLLOW_UPS，
        # 所以走回滚开关这次审查不可能同时拿到干净的 APPROVE。
        report["verdict"] = "APPROVE_WITH_FOLLOW_UPS"
        report["limitations"] = [f"re-froze past the budget (MAX_REFREEZE={MAX_REFREEZE + 1}) under prose policy"]
        self.assertEqual(ReportValidator(snapshot_manifest=manifest).validate(report), [])

        # 没超预算时不该逼报告写一句无关披露。
        within = signed_manifest(retry_policy="prose")
        self.assertEqual(
            ReportValidator(snapshot_manifest=within).validate(self._report_for_manifest(within)), []
        )

    def test_the_dispatch_budget_stays_hard_under_the_prose_policy(self) -> None:
        # MAX_DISPATCHES 对应 code-impl 的 policy-independent 绝对预算：prose 不放松它。
        manifest = signed_manifest(retry_policy="prose")
        report = self._report_for_manifest(manifest)
        errors = ReportValidator(
            snapshot_manifest=manifest,
            dispatch_ledger=[
                ledger_record(8, "review-specialist", f"SP-{index:02d}")
                for index in range(MAX_DISPATCHES + 1)
            ],
        ).validate(report)
        self.assertTrue(any(f"MAX_DISPATCHES={MAX_DISPATCHES}" in error for error in errors), errors)
        self.assertFalse(any(".retry_policy" in error for error in errors), errors)

    def test_manifest_above_the_refreeze_budget_is_rejected(self) -> None:
        # 门禁侧同一判据：手搓一份编号超预算的 manifest 也过不了步骤 21。
        manifest = signed_manifest(freeze_index=MAX_REFREEZE + 1)
        report = self._report_for_manifest(manifest)
        errors = ReportValidator(snapshot_manifest=manifest).validate(report)
        self.assertTrue(any("freeze budget" in error for error in errors))

    def test_review_scale_tiers_use_the_documented_limits(self) -> None:
        # CW4-b：阈值由脚本单点定义，边界逐个钉住——改阈值必须同时改这里与文档。
        small_files, small_lines = SCALE_TIER_LIMITS[0]
        self.assertEqual(review_scale(small_files, small_lines)["tier"], "S")
        self.assertEqual(review_scale(small_files + 1, small_lines)["tier"], "M")
        self.assertEqual(review_scale(small_files, small_lines + 1)["tier"], "M")
        last_files, last_lines = SCALE_TIER_LIMITS[-1]
        self.assertEqual(review_scale(last_files, last_lines)["tier"], "L")
        self.assertEqual(review_scale(last_files + 1, 0)["tier"], "XL")
        self.assertEqual(review_scale(0, last_lines + 1)["tier"], "XL")
        self.assertEqual(set(review_scale(0, 0)), set(REVIEW_SCALE_FIELDS))

    def test_untracked_only_change_is_not_tiered_as_small(self) -> None:
        # 全新增文件的改动在 tracked numstat 里是 0 行；不补未跟踪行数就会把大改动读成小改动。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo = Path(directory)
            base = self._init_repo(repo)
            for index in range(12):
                (repo / f"pkg{index}.py").write_text(
                    "\n".join(f"print({index})" for _ in range(30)) + "\n", encoding="utf-8"
                )
            manifest = build_snapshot(
                Namespace(
                    repo=repo, base_sha=base, head_sha=base,
                    target_type="local_diff", target_locator="test", preexisting_change=[],
                )
            )
            scale = manifest["review_scale"]
            self.assertEqual(scale["changed_files"], 12)
            self.assertEqual(scale["changed_lines"], 12 * 30)
            self.assertEqual(scale["tier"], "M")

    def test_relaxing_the_frozen_scale_tier_is_a_validation_error(self) -> None:
        # 档位是路由输入：报告侧改标（XL→S 以少派角色）必须被步骤 21 抓出。
        manifest = signed_manifest(review_scale=review_scale(200, 99_999))
        report = self._report_for_manifest(manifest)
        self.assertEqual(report["snapshot"]["review_scale"]["tier"], "XL")
        self.assertEqual(ReportValidator(snapshot_manifest=manifest).validate(report), [])
        report["snapshot"]["review_scale"] = SCALE
        errors = ReportValidator(snapshot_manifest=manifest).validate(report)
        self.assertTrue(
            any(error.startswith("$.snapshot.review_scale") for error in errors), errors
        )

    def test_manifest_review_scale_must_be_well_formed(self) -> None:
        manifest = signed_manifest(review_scale={"tier": "XS"})
        errors = ReportValidator(snapshot_manifest=manifest).validate(
            self._report_for_manifest(manifest)
        )
        self.assertTrue(any("review_scale.tier" in error for error in errors))
        self.assertTrue(any("review_scale.changed_files" in error for error in errors))
        tampered = dict(SCALE)
        tampered["tier_limits"] = [[1, 1]]
        errors = ReportValidator(snapshot_manifest=signed_manifest(review_scale=tampered)).validate(
            valid_report()
        )
        self.assertTrue(any("tier_limits" in error for error in errors))


class SkillCommandDriftTests(unittest.TestCase):
    """SKILL.md 里抄的命令块必须和脚本 argparse 同步（C1 类回归的看守）。"""

    ROLE_PAIRS = {
        "review-context": "context-analyst-prompt.md",
        "review-traceability": "traceability-analyst-prompt.md",
        "review-specialist": "specialist-reviewer-prompt.md",
        "review-evidence": "evidence-verifier-prompt.md",
        "review-failure": "failure-analyst-prompt.md",
    }

    def _layout_gate(self):
        path = SKILL_DIR.parents[1] / "scripts" / "validate-skill-layout.py"
        spec = importlib.util.spec_from_file_location("validate_skill_layout", path)
        self.assertTrue(spec is not None and spec.loader is not None, f"无法加载 {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_role_body_lives_in_exactly_one_file(self) -> None:
        # CW3-d：角色正文只写在 agents/*.md，prompt 模板只带本步输入与特有约束。
        # 仓库级门禁（scripts/validate-skill-layout.py）允许偶合一句，本 Skill 收紧到 0 句。
        layout = self._layout_gate()
        for agent_name, prompt_name in sorted(self.ROLE_PAIRS.items()):
            with self.subTest(role=agent_name):
                card = layout.role_body_lines(SKILL_DIR / "agents" / f"{agent_name}.md")
                prompt = layout.role_body_lines(SKILL_DIR / "references" / prompt_name)
                self.assertTrue(card, f"agents/{agent_name}.md 没有正文句子")
                self.assertEqual(
                    sorted(card & prompt), [], f"{agent_name} 与 {prompt_name} 又出现了双份角色正文"
                )
                text = (SKILL_DIR / "references" / prompt_name).read_text(encoding="utf-8")
                self.assertIn(f"$SKILL_DIR/agents/{agent_name}.md", text)

    def _help_options(self, script: str) -> set[str]:
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / script), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return set(re.findall(r"--[a-z][a-z0-9-]+", result.stdout))

    def test_step_contract_paths_match_the_script(self) -> None:
        # CW2-c：SKILL.md 与 5 份 prompt 点名的中间契约，必须是脚本 STEP_ARTIFACTS 里那几份。
        # 文档里造一个新落点或漏写一份，都会在这里断掉，而不是等到运行时才发现无人在读。
        expected = {f"{step:02d}-{slug}.json" for step, slug in STEP_ARTIFACTS}
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(set(re.findall(r"steps/(\d{2}-[a-z-]+\.json)", skill_text)), expected)
        for step, _ in STEP_ARTIFACTS:
            self.assertIn(f"## {step:02d} ", skill_text, f"步骤 {step:02d} 不存在，契约无处可写")
            self.assertIn(f"$RUN_DIR/steps/{step:02d}-", skill_text)
        documented: set[str] = set()
        for prompt in sorted((SKILL_DIR / "references").glob("*-prompt.md")):
            documented |= set(re.findall(r"steps/(\d{2}-[a-z-]+\.json)", prompt.read_text(encoding="utf-8")))
        self.assertTrue(documented <= expected, f"prompt 引用了未登记的契约：{documented - expected}")
        self.assertTrue(documented, "角色模板里没有任何契约落点，派发又退回转述")

    def test_snapshot_field_count_claims_match_the_code(self) -> None:
        # "snapshot N 个字段"是最容易腐烂的文档计数：加一个签名字段却漏改计数，
        # 读者就会以为骨架预填比实际少。计数与字段名都绑回 SNAPSHOT_REPORT_FIELDS。
        count = len(SNAPSHOT_REPORT_FIELDS)
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"`snapshot` {count} 个字段", skill_text)
        contracts_text = (SKILL_DIR / "references" / "contracts.md").read_text(encoding="utf-8")
        line = next(item for item in contracts_text.splitlines() if "恰好复制其中" in item)
        self.assertIn(f"恰好复制其中 {count} 个字段", line)
        for field in SNAPSHOT_REPORT_FIELDS:
            self.assertIn(f"`{field}`", line, f"contracts.md 的复制清单漏了 {field}")
        for tier in SCALE_TIERS:
            self.assertIn(f"`{tier}`", skill_text, f"SKILL.md 没有给 {tier} 档分派规则")

    def test_contracts_md_snapshot_block_versions_and_enums_match_the_script(self) -> None:
        # 抬 manifest 版本或改策略取值时漏改 contracts.md，文档就写着没人产得出的 manifest。
        # 上一段的计数看守只管报告侧字段，这里把 manifest 侧的版本字面量与枚举绑回常量。
        contracts_text = (SKILL_DIR / "references" / "contracts.md").read_text(encoding="utf-8")
        block = contracts_text.split("```yaml\nreview_snapshot:", 1)[1].split("```", 1)[0]
        lines = {
            raw.strip().split(":", 1)[0]: raw.split("#", 1)[0].strip()
            for raw in block.splitlines()
            if ":" in raw
        }
        self.assertEqual(
            lines["schema_version"], f'schema_version: "{MANIFEST_SCHEMA_VERSION}"'
        )
        self.assertEqual(
            lines["generator_version"], f'generator_version: "{GENERATOR_VERSION}"'
        )
        self.assertEqual(lines["retry_policy"], f"retry_policy: {' | '.join(RETRY_POLICIES)}")
        self.assertEqual(lines["freeze_index"], "freeze_index: integer")
        self.assertIn(f"MAX_REFREEZE={MAX_REFREEZE}", block)

    def test_documented_flags_exist_in_argparse(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
        self.assertGreaterEqual(len(blocks), 3, "SKILL.md 应至少含步骤 02/08/21 三条命令块")
        documented: dict[str, set[str]] = {}
        for block in blocks:
            scripts = re.findall(r"scripts/([a-z_]+\.py)", block)
            self.assertTrue(scripts, f"命令块缺少脚本引用：{block[:80]}")
            for script in set(scripts):
                documented.setdefault(script, set()).update(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]+)", block))
        self.assertTrue(documented, "SKILL.md 未解析到任何脚本命令块")
        for script, flags in sorted(documented.items()):
            with self.subTest(script=script):
                self.assertTrue((SKILL_DIR / "scripts" / script).exists(), f"{script} 不存在")
                self.assertEqual(
                    flags - self._help_options(script), set(),
                    f"{script} 的命令块写了 argparse 不认识的参数",
                )

    def test_snapshot_manifest_command_needs_no_shell_redirect(self) -> None:
        # 步骤 02 曾把 manifest 重定向进未创建的 $RUN_DIR，全新仓库上必然失败。
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        step02 = text.split("## 02 冻结审查快照", 1)[1].split("## 03", 1)[0]
        self.assertIn("--emit-manifest", step02)
        # CW3-a：Diff 读物必须由步骤 02 一条命令顺手落盘，否则角色又会回到粘贴正文。
        self.assertIn("--emit-diff", step02)
        self.assertIn("--diff-shard", step02)
        self.assertNotRegex(step02, r"freeze_review_snapshot\.py[^\n]*\\\n[^\n]*>\s*\"?\$RUN_DIR")

    def test_prompt_templates_reference_the_diff_artifact_not_its_body(self) -> None:
        # CW3-a：五个角色模板都要按"路径 + 摘要"接收 Diff，不能残留粘贴正文的写法。
        for name in (
            "context-analyst-prompt.md",
            "traceability-analyst-prompt.md",
            "specialist-reviewer-prompt.md",
            "evidence-verifier-prompt.md",
            "failure-analyst-prompt.md",
        ):
            with self.subTest(prompt=name):
                text = (SKILL_DIR / "references" / name).read_text(encoding="utf-8")
                self.assertIn("review.diff", text)
                self.assertIn("diff_artifact.digest", text)
                self.assertNotIn("<完整 Diff", text)
                self.assertNotIn("<目标 Diff>\n", text)


class ContractAnchorDriftTests(unittest.TestCase):
    """CW3-b：契约节锚点、角色表与 prompt 三方必须互相对得上（D1 的看守）。"""

    PROMPT_ROLE = {
        "context-analyst-prompt.md": "Context Analyst",
        "traceability-analyst-prompt.md": "Traceability Analyst",
        "specialist-reviewer-prompt.md": "Specialist Reviewer",
        "evidence-verifier-prompt.md": "Evidence Verifier",
        "failure-analyst-prompt.md": "Failure Analyst",
    }

    def _contracts(self) -> str:
        return (SKILL_DIR / "references" / "contracts.md").read_text(encoding="utf-8")

    def _defined(self, text: str) -> list[str]:
        ids = re.findall(r"^<!-- SECTION: ([a-z0-9-]+) -->$", text, re.M)
        self.assertEqual(len(ids), len(set(ids)), "锚点 id 必须唯一")
        # 每个锚点紧跟一个契约节标题，且不能被 Markdown 模板代码块里的假标题带偏。
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if line.startswith("<!-- SECTION: "):
                self.assertTrue(lines[index - 1].startswith("## "), line)
        return ids

    def _used(self, text: str, defined: set[str]) -> set[str]:
        return {anchor for anchor in defined if f"`{anchor}`" in text}

    def test_every_anchor_is_routed_and_no_route_is_invented(self) -> None:
        text = self._contracts()
        defined = self._defined(text)
        self.assertEqual(len(defined), 16)
        table = text.split("## 节锚点与按角色读取", 1)[1].split("\n## ", 1)[0]
        routed: set[str] = set()
        for row in re.findall(r"^\| ([^|]+) \| ([^|]+) \|$", table, re.M):
            routed |= self._used(row[1], set(defined))
        self.assertEqual(routed, set(defined), "有契约节没被任何角色路由，等于没人读它")
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(self._used(skill, set(defined)) - set(defined) == set())
        self.assertTrue(self._used(skill, set(defined)), "SKILL.md 必须点名主 Agent 自己读哪几节")

    def test_prompts_name_exactly_the_anchors_the_table_routes(self) -> None:
        text = self._contracts()
        defined = set(self._defined(text))
        table = text.split("## 节锚点与按角色读取", 1)[1].split("\n## ", 1)[0]
        rows: dict[str, set[str]] = {}
        for label, cells in re.findall(r"^\| ([^|]+) \| ([^|]+) \|$", table, re.M):
            rows.setdefault(label.strip(), set()).update(self._used(cells, defined))
        for prompt, role in self.PROMPT_ROLE.items():
            with self.subTest(prompt=prompt):
                body = (SKILL_DIR / "references" / prompt).read_text(encoding="utf-8")
                expected: set[str] = set()
                for label, anchors in rows.items():
                    if label.startswith(role):
                        expected |= anchors
                self.assertTrue(expected, f"角色表里没有 {role} 这一行")
                self.assertEqual(self._used(body, defined), expected)
                self.assertIn("$SKILL_DIR/references/contracts.md", body)


class CoverageDimensionSourceTests(unittest.TestCase):
    """CW3-d：9 个审查维度名在文档侧的三处书写必须与脚本常量一致（D4 的看守）。"""

    def test_routing_table_lists_exactly_the_canonical_dimensions(self) -> None:
        text = (SKILL_DIR / "references" / "specialist-reviewer-prompt.md").read_text(encoding="utf-8")
        rows = set(re.findall(r"^\| `([a-z_]+)` \|", text, re.M))
        self.assertEqual(rows, set(COVERAGE_DIMENSIONS), "维度路由表与 freeze 脚本的常量表不一致")

    def test_specialist_card_lists_exactly_the_canonical_dimensions(self) -> None:
        text = (SKILL_DIR / "agents" / "review-specialist.md").read_text(encoding="utf-8")
        listed = re.search(r"维度：([^。]+)。", text)
        self.assertIsNotNone(listed, "review-specialist.md 不再内联维度清单，请同步本测试")
        names = [token.strip().removesuffix(" 中的一个或多个") for token in listed.group(1).split("、")]
        self.assertEqual(names, list(COVERAGE_DIMENSIONS))

    def test_contracts_md_dimension_enumeration_matches_the_constant(self) -> None:
        text = (SKILL_DIR / "references" / "contracts.md").read_text(encoding="utf-8")
        line = next(
            (raw.strip() for raw in text.splitlines() if raw.strip().startswith("- dimension:")), ""
        )
        self.assertTrue(line, "contracts.md 的 Coverage Matrix 不再内联维度枚举，请同步本测试")
        names = [token.strip() for token in line.split(":", 1)[1].split("|")]
        self.assertEqual(names, list(COVERAGE_DIMENSIONS))

    def test_skill_step_08_covers_every_dimension(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        step08 = text.split("## 08", 1)[1].split("## 09", 1)[0]
        missing = [name for name in COVERAGE_DIMENSIONS if name not in step08]
        self.assertEqual(missing, [], "SKILL.md 步骤 08 的维度清单缺少维度")


class SharedScriptHelperTests(unittest.TestCase):
    """CW4-d：跨脚本共用的实现只允许在 freeze 脚本书写一处，其余 import（D4 的看守）。"""

    HELPERS = ("_reconfigure_streams", "canonical_json", "sha256", "write_text_atomic")

    def test_each_helper_is_defined_in_exactly_one_script(self) -> None:
        scripts = sorted((SKILL_DIR / "scripts").glob("*.py"))
        self.assertEqual([path.name for path in scripts], [
            "freeze_review_snapshot.py",
            "record_review_dispatch.py",
            "validate_review_report.py",
        ])
        for name in self.HELPERS:
            with self.subTest(helper=name):
                defined = [
                    path.name
                    for path in scripts
                    if re.search(rf"^def {re.escape(name)}\(", path.read_text(encoding="utf-8"), re.M)
                ]
                self.assertEqual(
                    defined,
                    ["freeze_review_snapshot.py"],
                    f"{name} 又被抄了一份：另外两个脚本必须 import，而不是各自演化",
                )


class CrossSkillHandoffTests(unittest.TestCase):
    """CW2-a 验收：同一工作树两侧冻结可机械比对——A5 的三个 `False` 各自变成有语义的等式。

    这里刻意 import 真正的上游产物生成器（code-impl 控制器、bugfix 状态机），而不是手抄
    一份看起来像的 JSON：手抄的夹具只能证明"抄得像"，证明不了两侧真能对上。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.impl = load_skill_module(
            "code_impl_controller_under_review_test",
            SKILL_DIR.parent / "code-impl" / "scripts" / "code_impl_controller.py",
        )
        cls.bugfix = load_skill_module(
            "bugfix_state_under_review_test",
            SKILL_DIR.parent / "bugfix" / "scripts" / "bugfix_state.py",
        )

    def _code_impl_artifact(self, repo: Path) -> tuple[str, str, dict]:
        """让 code-impl 真的冻结一次，返回 (base_sha, head_sha, 它落盘的快照)。"""
        base = init_repo(repo)
        subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
        (repo / "committed.py").write_text("print('committed')\n", encoding="utf-8")
        subprocess.run(["git", "add", "committed.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "feat"], cwd=repo, check=True)
        head = git_in(repo, "rev-parse", "HEAD")
        feature = repo / "docs" / "feature"
        feature.mkdir(parents=True)
        for name, body in (
            ("product-doc.md", "# PRD\nThe product requirement is explicit and testable.\n"),
            ("technical-doc.md", "# TDD\nThe technical design defines the implementation boundary.\n"),
            ("release-doc.md", "# RPD\nThe release plan defines validation and rollback steps.\n"),
        ):
            (feature / name).write_text(body, encoding="utf-8")
        state_path = self.impl.init_run(repo, feature, "cw2a")
        (repo / "app.py").write_text("print('implemented')\n", encoding="utf-8")
        self.impl.record_task(state_path, "T01", ["app.py"])
        return base, head, self.impl.freeze_review(state_path)

    def _repo_and_run(self, directory: str) -> tuple[Path, Path]:
        """run 目录必须落在仓库外：写进仓库的 manifest 本身就是一个未跟踪文件，
        会把"同一份工作树"的两次冻结隔成两个 untracked_digest。"""
        repo = Path(directory) / "repo"
        run = Path(directory) / "run"
        repo.mkdir(parents=True)
        run.mkdir(parents=True)
        return repo, run

    def test_code_impl_freeze_hands_this_freeze_a_comparable_target(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo, _ = self._repo_and_run(directory)
            base, head, upstream = self._code_impl_artifact(repo)
            manifest_path = repo / ".codekit" / "code-review" / "cw2a" / "review-snapshot.json"
            frozen = freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", head,
                "--target-type", "branch", "--target-locator", "master...feature",
                "--handoff", upstream["path"], "--emit-manifest", str(manifest_path),
            )
            # 旧规则（"三个字段不一致即 BLOCKED"）在这里必然失败：上游从不提交，
            # 既没有 committed_range，diff_digest 的 material 也与本技能不同。
            self.assertEqual(frozen.returncode, 0, frozen.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            block = manifest["handoff"]
            producer = self.impl.PRODUCED_BY
            self.assertEqual(block["sources"], [producer])
            self.assertTrue(block["target_id_matched"])
            self.assertEqual(block["compared_fields"], ["head_sha"])
            # A5 的第二个 False：两个 diff_digest 本就不是同一份 material，现在它是
            # 一条有名有姓的"不可比"记录，而不是一次假冲突。
            self.assertEqual(
                block["uncomparable_digests"],
                [f"{producer}:{upstream['worktree_state']['diff_digest']}"],
            )
            self.assertEqual(
                block["limitations"], [f"SCOPE_SUPERSET: {producer} carries no committed_range"]
            )
            # A5 的第一个 False（base_sha 0b30... vs 7ed1...）现在由 target_id 承载：
            # 两侧同一表达式，不再拿语义不同的端点互比。
            self.assertEqual(upstream["target_id"], manifest["target_id"])
            # A5 的第三个 False（44 项 `A:scale/svc_00.py` vs []）：同一记法，可作集合比。
            theirs = upstream["worktree_state"]["changed_files"]
            self.assertEqual(theirs, ["M:app.py"])
            self.assertTrue(set(theirs) <= set(manifest["changed_files"]))
            self.assertTrue(all(re.fullmatch(r"[A-Z]:[^\n]+", item) for item in manifest["changed_files"]))

    def test_handoff_limitation_must_be_disclosed_and_caps_the_verdict(self) -> None:
        # 限制不是缺陷，但"读者以为上游全看过了"是缺陷：凭据在签名里，措辞在模型侧。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo, run = self._repo_and_run(directory)
            base, head, upstream = self._code_impl_artifact(repo)
            manifest_path = run / "snapshot.json"
            frozen = freeze_cli(
                repo,
                "--repo", str(repo), "--base-sha", base, "--head-sha", head,
                "--target-type", "branch", "--target-locator", "master...feature",
                "--handoff", upstream["path"], "--emit-manifest", str(manifest_path),
            )
            self.assertEqual(frozen.returncode, 0, frozen.stderr)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            marker = manifest["handoff"]["limitations"][0].split(":", 1)[0]
            report = report_for_manifest(manifest)
            self.assertEqual(report["verdict"], "APPROVE")
            undisclosed = ReportValidator(snapshot_manifest=manifest).validate(report)
            self.assertTrue(
                any(marker in error for error in undisclosed),
                f"未披露的 SCOPE_SUPERSET 竟被放过：{undisclosed}",
            )
            disclosed = report_for_manifest(manifest)
            disclosed["limitations"] = [marker + ": 上游快照不含已提交范围，本 run 自行审查该范围"]
            disclosed["verdict"] = "APPROVE_WITH_FOLLOW_UPS"
            self.assertEqual(ReportValidator(snapshot_manifest=manifest).validate(disclosed), [])

    def test_refreezing_against_our_own_snapshot_compares_every_shared_field(self) -> None:
        # 同一 producer、同一 material：共有字段全部可比，且干净的复用不产任何限制。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo, run = self._repo_and_run(directory)
            base = init_repo(repo)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            first_path = run / "first.json"
            args = ("--repo", str(repo), "--base-sha", base, "--head-sha", base,
                    "--target-type", "local_diff", "--target-locator", "test")
            self.assertEqual(freeze_cli(repo, *args, "--emit-manifest", str(first_path)).returncode, 0)
            first = json.loads(first_path.read_text(encoding="utf-8"))
            second_path = run / "second.json"
            again = freeze_cli(repo, *args, "--emit-manifest", str(second_path), "--handoff", str(first_path))
            self.assertEqual(again.returncode, 0, again.stderr)
            block = json.loads(second_path.read_text(encoding="utf-8"))["handoff"]
            self.assertEqual(
                block["compared_fields"],
                sorted(["head_sha", "committed_range.base_sha", "staged_digest", "unstaged_digest",
                        "untracked_digest", "diff_digest"]),
            )
            self.assertEqual(block["uncomparable_digests"], [])
            self.assertEqual(block["limitations"], [])
            second = json.loads(second_path.read_text(encoding="utf-8"))
            # A5 的对比在这一行成立：同一工作树两侧冻结，比对的是同一 material 的等式。
            self.assertEqual(first["worktree_state"]["diff_digest"], second["worktree_state"]["diff_digest"])

    def test_worktree_drift_after_the_upstream_freeze_still_blocks(self) -> None:
        # 三类真阻塞之一：工作树在上游冻结后又动了，比对结果不能只是一条限制。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo, run = self._repo_and_run(directory)
            base = init_repo(repo)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            first_path = run / "first.json"
            args = ("--repo", str(repo), "--base-sha", base, "--head-sha", base,
                    "--target-type", "local_diff", "--target-locator", "test")
            self.assertEqual(freeze_cli(repo, *args, "--emit-manifest", str(first_path)).returncode, 0)
            (repo / "app.py").write_text("print('changed again')\n", encoding="utf-8")
            drifted = freeze_cli(repo, *args, "--emit-manifest", str(run / "second.json"),
                                 "--handoff", str(first_path))
            self.assertEqual(drifted.returncode, 3)
            self.assertTrue(drifted.stderr.startswith("BLOCKED:"), drifted.stderr)
            self.assertIn("unstaged_digest", drifted.stderr)

    def test_a_handoff_for_another_target_blocks_before_anything_is_compared(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            repo, run = self._repo_and_run(directory)
            base = init_repo(repo)
            (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
            handoff_path = run / "other-target.json"
            handoff_path.write_text(
                json.dumps({"target_id": "somewhere/else@" + "b" * 40}), encoding="utf-8"
            )
            result = freeze_cli(
                repo, "--repo", str(repo), "--base-sha", base, "--head-sha", base,
                "--target-type", "local_diff", "--target-locator", "test",
                "--emit-manifest", str(run / "snapshot.json"), "--handoff", str(handoff_path),
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("target_id", result.stderr)
            self.assertFalse((run / "snapshot.json").exists(), "被拒绝的冻结不得留下可用的 manifest")

    def test_the_three_skills_state_one_encoding_and_one_join_key(self) -> None:
        # 三家没有共享 import 路径（各自独立分发），所以这里绑的是字面量与表达式。
        self.assertEqual(self.impl._DIGEST_SCHEME, DIGEST_SCHEME)
        self.assertEqual(self.bugfix.DIGEST_SCHEME, DIGEST_SCHEME)
        self.assertEqual(self.impl.SNAPSHOT_SCHEMA_VERSION, self.bugfix.SNAPSHOT_SCHEMA_VERSION)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            head = "b" * 40
            self.assertEqual(target_id_for(root, head), self.impl._handoff_target_id(root, head))
            self.assertEqual(target_id_for(root, head), self.bugfix.target_id_for(root, head))

    def test_a_flat_duplicate_of_a_grouped_field_is_not_a_valid_manifest(self) -> None:
        # CW2-a② 的扁平/分组二义性：顶层再抄一份 base_sha，跨 SKILL 比对就会各取一侧。
        manifest = signed_manifest()
        manifest["base_sha"] = snapshot_field(manifest, "committed_range", "base_sha")
        manifest["digest_scheme"] = "sha256:" + DIGEST
        sign_manifest(manifest)
        errors = ReportValidator(snapshot_manifest=manifest).validate(report_for_manifest(manifest))
        self.assertTrue(
            any("$.snapshot.manifest.base_sha" in error and "committed_range" in error for error in errors),
            errors,
        )
        # 编码声明也不能自述一套别人解不动的写法：带前缀就不属于任何已登记方案。
        self.assertTrue(
            any("$.snapshot.manifest.digest_scheme" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
