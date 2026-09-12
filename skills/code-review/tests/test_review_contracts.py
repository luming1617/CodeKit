from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from freeze_review_snapshot import (  # noqa: E402
    SnapshotError,
    build_snapshot,
    parse_name_status,
    run_git,
    untracked_manifest,
)
from validate_review_report import ReportValidator  # noqa: E402


DIGEST = "d" * 64
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
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
        "scope": {"target_type": "local_diff", "target_locator": "test", "included": ["src"], "excluded": []},
        "snapshot": {
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "diff_digest": DIGEST,
            "changed_files": ["M:src/app.py"],
            "staged_digest": "1" * 64,
            "unstaged_digest": "2" * 64,
            "untracked_digest": "3" * 64,
            "preexisting_changes": [],
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
            {"dimension": dimension, "required": True, "status": "REVIEWED", "owner": "reviewer", "evidence": ["checked"], "reason": "checked"}
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
        "release_gate": {"status": "NOT_APPLICABLE", "checks": [], "blocking_reasons": [], "conditions": []},
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


RELEASE_CHECK_ITEMS = [
    "deployment_order",
    "dependencies",
    "configuration",
    "feature_flag",
    "rollout",
    "stop_conditions",
    "monitoring",
    "observation_window",
    "data_repair",
    "rollback",
]


def release_gate(status: str, overrides: dict[str, str] | None = None) -> dict:
    overrides = overrides or {}
    checks = []
    for item in RELEASE_CHECK_ITEMS:
        item_status = overrides.get(item, "PASS")
        gap = None if item_status == "PASS" else "follow-up required"
        checks.append({"item": item, "status": item_status, "evidence": ["checked"], "condition_or_gap": gap})
    return {
        "status": status,
        "checks": checks,
        "blocking_reasons": ["release blocker"] if status in {"NOT_READY", "NOT_VERIFIED"} else [],
        "conditions": ["follow-up required"] if status == "READY_WITH_CONDITIONS" else [],
    }


def signed_manifest(**fields: object) -> dict:
    manifest = {
        "schema_version": "1.0",
        "repository_root": "/repo",
        "target_type": "local_diff",
        "target_locator": "test",
        "base_sha": BASE_SHA,
        "head_sha": HEAD_SHA,
        "changed_files": ["M:src/app.py"],
        "staged_digest": "1" * 64,
        "unstaged_digest": "2" * 64,
        "untracked_digest": "3" * 64,
        "preexisting_changes": [],
        "diff_digest": DIGEST,
    }
    manifest.update(fields)
    manifest["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest


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
        items = [
            "deployment_order",
            "dependencies",
            "configuration",
            "feature_flag",
            "rollout",
            "stop_conditions",
            "monitoring",
            "observation_window",
            "data_repair",
            "rollback",
        ]
        report["release_gate"] = {
            "status": "READY_WITH_CONDITIONS",
            "checks": [
                {
                    "item": item,
                    "status": "FAIL" if item == "rollback" else "CONDITIONAL" if item == "monitoring" else "PASS",
                    "evidence": ["checked"],
                    "condition_or_gap": "cannot rollback" if item == "rollback" else "add alert" if item == "monitoring" else None,
                }
                for item in items
            ],
            "blocking_reasons": [],
            "conditions": ["add alert"],
        }
        errors = ReportValidator().validate(report)
        self.assertTrue(any("any FAIL check requires NOT_READY" in error for error in errors))

    def test_snapshot_manifest_is_bound_to_report(self) -> None:
        report = valid_report()
        manifest = {
            "schema_version": "1.0",
            "repository_root": "/repo",
            "target_type": "local_diff",
            "target_locator": "test",
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "changed_files": ["M:src/app.py"],
            "staged_digest": "1" * 64,
            "unstaged_digest": "2" * 64,
            "untracked_digest": "3" * 64,
            "preexisting_changes": [],
            "diff_digest": DIGEST,
        }
        manifest["manifest_digest"] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        report["snapshot"]["manifest_digest"] = manifest["manifest_digest"]
        self.assertEqual(ReportValidator(snapshot_manifest=manifest).validate(report), [])

        report["snapshot"]["head_sha"] = "c" * 40
        self.assertTrue(ReportValidator(snapshot_manifest=manifest).validate(report))

    def test_cli_validates_against_manifest_file(self) -> None:
        report = valid_report()
        manifest = {
            "schema_version": "1.0",
            "repository_root": "/repo",
            "target_type": "local_diff",
            "target_locator": "test",
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "changed_files": ["M:src/app.py"],
            "staged_digest": "1" * 64,
            "unstaged_digest": "2" * 64,
            "untracked_digest": "3" * 64,
            "preexisting_changes": [],
            "diff_digest": DIGEST,
        }
        manifest["manifest_digest"] = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        report["snapshot"].update(manifest)
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


class SnapshotScriptTests(unittest.TestCase):
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
            self.assertEqual(len(first["diff_digest"]), 64)
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
            paths, digest = untracked_manifest(repo)
            self.assertEqual(paths, ["link.py", "regular.py"])
            self.assertEqual(len(digest), 64)

    def test_run_git_raises_snapshot_error_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SnapshotError):
                run_git(Path(directory), "rev-parse", "--verify", "deadbeef^{commit}")


if __name__ == "__main__":
    unittest.main()
