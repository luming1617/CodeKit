#!/usr/bin/env python3
"""Validate the canonical JSON payload for the code-review skill.

This validator checks structure, verdict invariants, and a frozen snapshot
manifest. It does not decide whether code-review evidence or conclusions are correct.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


VERDICTS = {
    "APPROVE",
    "APPROVE_WITH_FOLLOW_UPS",
    "REQUEST_CHANGES",
    "BLOCKED",
}
DIMENSIONS = {
    "correctness",
    "security_access",
    "data_transaction",
    "distributed_reliability",
    "compatibility_contract",
    "performance_capacity",
    "test_adequacy",
    "observability",
    "release_rollback",
}
COVERAGE_STATUSES = {"REVIEWED", "VALIDATED", "NOT_APPLICABLE", "NOT_VERIFIED"}
TRACE_STATUSES = {
    "COVERED",
    "PARTIAL",
    "MISSING",
    "CONFLICT",
    "NOT_VERIFIED",
    "NOT_APPLICABLE",
}
VALIDATION_STATUSES = {
    "PASS",
    "FAIL_INTRODUCED",
    "FAIL_BASELINE",
    "ENVIRONMENT_ERROR",
    "PERMISSION_DENIED",
    "FLAKY",
    "NOT_RUN",
}
RELEASE_STATUSES = {
    "READY",
    "READY_WITH_CONDITIONS",
    "NOT_READY",
    "NOT_VERIFIED",
    "NOT_APPLICABLE",
}
RELEASE_CHECKS = {
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
}
RELEASE_CHECK_STATUSES = {"PASS", "CONDITIONAL", "FAIL", "NOT_VERIFIED", "NOT_APPLICABLE"}
FINDING_FIELDS = {
    "id",
    "candidate_id",
    "title",
    "dimension",
    "severity",
    "blocking",
    "blocking_reason",
    "confidence",
    "evidence_level",
    "locations",
    "causal_chain",
    "trigger_scenario",
    "impact",
    "affected_scope",
    "evidence",
    "remediation",
    "regression_test",
}
QUESTION_FIELDS = {
    "id",
    "question",
    "reason",
    "required_evidence",
    "owner_type",
    "impact_if_unanswered",
    "blocking",
}
TOP_LEVEL_FIELDS = {
    "schema_version",
    "review_context",
    "verdict",
    "verdict_basis",
    "blockers",
    "scope",
    "snapshot",
    "summary",
    "findings",
    "open_questions",
    "traceability",
    "coverage",
    "validation",
    "release_gate",
    "rejected_candidates",
    "limitations",
    "residual_risks",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_FIELDS = {
    "base_sha",
    "head_sha",
    "diff_digest",
    "changed_files",
    "staged_digest",
    "unstaged_digest",
    "untracked_digest",
    "preexisting_changes",
    "manifest_digest",
}


class ReportValidator:
    def __init__(self, snapshot_manifest: dict[str, Any] | None = None) -> None:
        self.errors: list[str] = []
        self.snapshot_manifest = snapshot_manifest

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def require_object(self, value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.error(path, "must be an object")
            return {}
        return value

    def require_list(self, value: Any, path: str) -> list[Any]:
        if not isinstance(value, list):
            self.error(path, "must be an array")
            return []
        return value

    def require_string(self, value: Any, path: str) -> str:
        if not isinstance(value, str) or not value.strip():
            self.error(path, "must be a non-empty string")
            return ""
        return value

    def require_string_list(self, value: Any, path: str, *, nonempty: bool = False) -> list[str]:
        items = self.require_list(value, path)
        if nonempty and not items:
            self.error(path, "must contain at least one item")
        for index, item in enumerate(items):
            self.require_string(item, f"{path}[{index}]")
        return [item for item in items if isinstance(item, str) and item.strip()]

    def require_fields(self, value: dict[str, Any], fields: set[str], path: str) -> None:
        for field in sorted(fields - value.keys()):
            self.error(f"{path}.{field}", "is required")

    def validate(self, payload: Any) -> list[str]:
        report = self.require_object(payload, "$")
        self.require_fields(report, TOP_LEVEL_FIELDS, "$")
        if report.get("schema_version") != "1.0":
            self.error("$.schema_version", "must equal '1.0'")

        review_context = self.validate_review_context(report.get("review_context"))
        verdict = report.get("verdict")
        if verdict not in VERDICTS:
            self.error("$.verdict", f"must be one of {sorted(VERDICTS)}")

        self.require_string_list(report.get("verdict_basis"), "$.verdict_basis", nonempty=True)
        blockers = self.require_string_list(report.get("blockers"), "$.blockers")
        self.require_string(report.get("summary"), "$.summary")
        scope = self.validate_scope(report.get("scope"))
        snapshot_digest = self.validate_snapshot(report.get("snapshot"), scope)
        findings = self.validate_findings(report.get("findings"))
        questions = self.validate_questions(report.get("open_questions"))
        traceability = self.validate_traceability(report.get("traceability"))
        coverage = self.validate_coverage(report.get("coverage"))
        validation = self.validate_validation(report.get("validation"), snapshot_digest)
        release_status = self.validate_release_gate(report.get("release_gate"))
        self.validate_rejected(report.get("rejected_candidates"))
        limitations = self.require_string_list(report.get("limitations"), "$.limitations")
        residual_risks = self.require_string_list(report.get("residual_risks"), "$.residual_risks")
        self.validate_verdict(
            verdict,
            blockers,
            findings,
            questions,
            coverage,
            validation,
            release_status,
            review_context,
            traceability,
            limitations,
            residual_risks,
        )
        return self.errors

    def validate_review_context(self, value: Any) -> dict[str, Any]:
        context = self.require_object(value, "$.review_context")
        self.require_fields(context, {"review_mode", "documents_status"}, "$.review_context")
        review_mode = context.get("review_mode")
        if review_mode is not None and review_mode not in {"FULL", "LITE", "WAIVED"}:
            self.error("$.review_context.review_mode", "must be FULL, LITE, WAIVED, or null")
        documents_status = context.get("documents_status")
        if documents_status not in {"available", "NOT_AVAILABLE"}:
            self.error(
                "$.review_context.documents_status",
                "must be available or NOT_AVAILABLE",
            )
        if review_mode == "LITE" and documents_status != "NOT_AVAILABLE":
            self.error(
                "$.review_context",
                "LITE requires documents_status NOT_AVAILABLE",
            )
        return context

    def validate_scope(self, value: Any) -> dict[str, Any]:
        scope = self.require_object(value, "$.scope")
        self.require_fields(scope, {"target_type", "target_locator", "included", "excluded"}, "$.scope")
        self.require_string(scope.get("target_type"), "$.scope.target_type")
        self.require_string(scope.get("target_locator"), "$.scope.target_locator")
        self.require_string_list(scope.get("included"), "$.scope.included", nonempty=True)
        self.require_string_list(scope.get("excluded"), "$.scope.excluded")
        return scope

    def validate_snapshot(self, value: Any, scope: dict[str, Any] | None = None) -> str:
        snapshot = self.require_object(value, "$.snapshot")
        self.require_fields(snapshot, SNAPSHOT_FIELDS, "$.snapshot")
        for field in ("base_sha", "head_sha"):
            self.require_string(snapshot.get(field), f"$.snapshot.{field}")
            value = snapshot.get(field)
            if isinstance(value, str) and not SHA_RE.fullmatch(value):
                self.error(f"$.snapshot.{field}", "must be a full Git SHA")
        for field in (
            "diff_digest",
            "staged_digest",
            "unstaged_digest",
            "untracked_digest",
            "manifest_digest",
        ):
            digest = self.require_string(snapshot.get(field), f"$.snapshot.{field}")
            if digest and not DIGEST_RE.fullmatch(digest):
                self.error(f"$.snapshot.{field}", "must be a lowercase SHA-256 digest")
        self.require_string_list(snapshot.get("changed_files"), "$.snapshot.changed_files")
        self.require_string_list(snapshot.get("preexisting_changes"), "$.snapshot.preexisting_changes")
        digest = snapshot.get("diff_digest")
        self.validate_snapshot_manifest(snapshot, scope)
        return digest if isinstance(digest, str) else ""

    def validate_snapshot_manifest(self, snapshot: dict[str, Any], scope: dict[str, Any] | None) -> None:
        manifest = self.snapshot_manifest
        if manifest is None:
            return
        if manifest.get("schema_version") != "1.0":
            self.error("$.snapshot.manifest", "schema_version must equal '1.0'")
        manifest_digest = manifest.get("manifest_digest")
        if not isinstance(manifest_digest, str) or not DIGEST_RE.fullmatch(manifest_digest):
            self.error("$.snapshot.manifest.manifest_digest", "must be a lowercase SHA-256 digest")
        else:
            unsigned = dict(manifest)
            unsigned.pop("manifest_digest", None)
            expected = hashlib.sha256(
                json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if expected != manifest_digest:
                self.error("$.snapshot.manifest.manifest_digest", "does not match manifest contents")
        for field in (
            "base_sha",
            "head_sha",
            "diff_digest",
            "changed_files",
            "staged_digest",
            "unstaged_digest",
            "untracked_digest",
            "preexisting_changes",
            "manifest_digest",
        ):
            if snapshot.get(field) != manifest.get(field):
                self.error(f"$.snapshot.{field}", "must match the frozen snapshot manifest")
        if scope is not None:
            for field in ("target_type", "target_locator"):
                if scope.get(field) != manifest.get(field):
                    self.error(f"$.scope.{field}", "must match the frozen snapshot manifest")

    def validate_findings(self, value: Any) -> list[dict[str, Any]]:
        findings = self.require_list(value, "$.findings")
        result: list[dict[str, Any]] = []
        ids: set[str] = set()
        for index, raw in enumerate(findings):
            path = f"$.findings[{index}]"
            finding = self.require_object(raw, path)
            self.require_fields(finding, FINDING_FIELDS, path)
            finding_id = self.require_string(finding.get("id"), f"{path}.id")
            if finding_id and not re.fullmatch(r"F-\d+", finding_id):
                self.error(f"{path}.id", "must match F-<digits>")
            if finding_id in ids:
                self.error(f"{path}.id", "must be unique")
            ids.add(finding_id)
            candidate_id = self.require_string(finding.get("candidate_id"), f"{path}.candidate_id")
            if candidate_id and not re.fullmatch(r"CF-\d+", candidate_id):
                self.error(f"{path}.candidate_id", "must match CF-<digits>")
            self.require_string(finding.get("title"), f"{path}.title")
            if finding.get("dimension") not in DIMENSIONS:
                self.error(f"{path}.dimension", f"must be one of {sorted(DIMENSIONS)}")
            if finding.get("severity") not in {"BLOCKER", "HIGH", "MEDIUM", "LOW"}:
                self.error(f"{path}.severity", "must be BLOCKER, HIGH, MEDIUM, or LOW")
            if not isinstance(finding.get("blocking"), bool):
                self.error(f"{path}.blocking", "must be a boolean")
            self.require_string(finding.get("blocking_reason"), f"{path}.blocking_reason")
            confidence = finding.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
                self.error(f"{path}.confidence", "must be an integer from 0 to 100")
            if finding.get("evidence_level") not in {"E2", "E3"}:
                self.error(f"{path}.evidence_level", "formal findings require E2 or E3")
            for field in ("locations", "causal_chain", "affected_scope", "evidence"):
                self.require_string_list(finding.get(field), f"{path}.{field}", nonempty=True)
            for field in ("trigger_scenario", "impact", "remediation", "regression_test"):
                self.require_string(finding.get(field), f"{path}.{field}")
            result.append(finding)
        return result

    def validate_questions(self, value: Any) -> list[dict[str, Any]]:
        questions = self.require_list(value, "$.open_questions")
        result: list[dict[str, Any]] = []
        ids: set[str] = set()
        for index, raw in enumerate(questions):
            path = f"$.open_questions[{index}]"
            question = self.require_object(raw, path)
            self.require_fields(question, QUESTION_FIELDS, path)
            question_id = self.require_string(question.get("id"), f"{path}.id")
            if question_id and not re.fullmatch(r"Q-\d+", question_id):
                self.error(f"{path}.id", "must match Q-<digits>")
            if question_id in ids:
                self.error(f"{path}.id", "must be unique")
            ids.add(question_id)
            for field in ("question", "reason", "required_evidence", "owner_type", "impact_if_unanswered"):
                self.require_string(question.get(field), f"{path}.{field}")
            if not isinstance(question.get("blocking"), bool):
                self.error(f"{path}.blocking", "must be a boolean")
            result.append(question)
        return result

    def validate_traceability(self, value: Any) -> list[dict[str, Any]]:
        entries = self.require_list(value, "$.traceability")
        result: list[dict[str, Any]] = []
        ids: set[str] = set()
        fields = {"id", "kind", "source", "statement", "links", "status", "evidence", "gap"}
        for index, raw in enumerate(entries):
            path = f"$.traceability[{index}]"
            entry = self.require_object(raw, path)
            self.require_fields(entry, fields, path)
            entry_id = self.require_string(entry.get("id"), f"{path}.id")
            if entry_id and not re.fullmatch(r"(?:REQ|DES|REL)-\d+", entry_id):
                self.error(f"{path}.id", "must match REQ-, DES-, or REL- followed by digits")
            if entry_id in ids:
                self.error(f"{path}.id", "must be unique")
            ids.add(entry_id)
            kind = entry.get("kind")
            if kind not in {"requirement", "design", "release"}:
                self.error(f"{path}.kind", "must be requirement, design, or release")
            expected_prefix = {"requirement": "REQ-", "design": "DES-", "release": "REL-"}.get(kind)
            if entry_id and expected_prefix and not entry_id.startswith(expected_prefix):
                self.error(f"{path}.id", f"kind {kind} requires prefix {expected_prefix}")
            self.require_string(entry.get("source"), f"{path}.source")
            self.require_string(entry.get("statement"), f"{path}.statement")
            links = self.require_object(entry.get("links"), f"{path}.links")
            link_fields = {"requirements", "designs", "code", "tests", "releases"}
            self.require_fields(links, link_fields, f"{path}.links")
            linked_items: list[str] = []
            for field in link_fields:
                linked_items.extend(self.require_string_list(links.get(field), f"{path}.links.{field}"))
            status = entry.get("status")
            if status not in TRACE_STATUSES:
                self.error(f"{path}.status", f"must be one of {sorted(TRACE_STATUSES)}")
            evidence = self.require_string_list(entry.get("evidence"), f"{path}.evidence")
            gap = entry.get("gap")
            if gap is not None and (not isinstance(gap, str) or not gap.strip()):
                self.error(f"{path}.gap", "must be null or a non-empty string")
            if status == "COVERED" and (not evidence or not linked_items):
                self.error(path, "COVERED requires evidence and at least one traceability link")
            if status in {"PARTIAL", "MISSING", "CONFLICT", "NOT_VERIFIED"}:
                self.require_string(gap, f"{path}.gap")
            if status == "NOT_APPLICABLE" and not evidence:
                self.error(f"{path}.evidence", "NOT_APPLICABLE requires evidence")
            result.append(entry)
        return result

    def validate_coverage(self, value: Any) -> list[dict[str, Any]]:
        entries = self.require_list(value, "$.coverage")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        fields = {"dimension", "required", "status", "owner", "evidence", "reason"}
        for index, raw in enumerate(entries):
            path = f"$.coverage[{index}]"
            entry = self.require_object(raw, path)
            self.require_fields(entry, fields, path)
            dimension = entry.get("dimension")
            if dimension not in DIMENSIONS:
                self.error(f"{path}.dimension", f"must be one of {sorted(DIMENSIONS)}")
            elif dimension in seen:
                self.error(f"{path}.dimension", "must be unique")
            else:
                seen.add(dimension)
            if not isinstance(entry.get("required"), bool):
                self.error(f"{path}.required", "must be a boolean")
            if entry.get("status") not in COVERAGE_STATUSES:
                self.error(f"{path}.status", f"must be one of {sorted(COVERAGE_STATUSES)}")
            self.require_string(entry.get("owner"), f"{path}.owner")
            evidence = self.require_string_list(entry.get("evidence"), f"{path}.evidence")
            self.require_string(entry.get("reason"), f"{path}.reason")
            if entry.get("status") in {"REVIEWED", "VALIDATED", "NOT_APPLICABLE"} and not evidence:
                self.error(f"{path}.evidence", f"{entry.get('status')} requires evidence")
            result.append(entry)
        missing = DIMENSIONS - seen
        extra = seen - DIMENSIONS
        if missing:
            self.error("$.coverage", f"missing dimensions: {sorted(missing)}")
        if extra:
            self.error("$.coverage", f"unexpected dimensions: {sorted(extra)}")
        return result

    def validate_validation(self, value: Any, snapshot_digest: str) -> list[dict[str, Any]]:
        entries = self.require_list(value, "$.validation")
        result: list[dict[str, Any]] = []
        fields = {
            "id",
            "target_claim",
            "command",
            "cwd",
            "diff_digest",
            "started_at",
            "finished_at",
            "exit_code",
            "status",
            "evidence",
            "not_run_reason",
        }
        for index, raw in enumerate(entries):
            path = f"$.validation[{index}]"
            entry = self.require_object(raw, path)
            self.require_fields(entry, fields, path)
            for field in ("id", "target_claim", "command", "cwd", "diff_digest", "started_at", "finished_at", "evidence"):
                self.require_string(entry.get(field), f"{path}.{field}")
            if snapshot_digest and entry.get("diff_digest") != snapshot_digest:
                self.error(f"{path}.diff_digest", "must match $.snapshot.diff_digest")
            status = entry.get("status")
            if status not in VALIDATION_STATUSES:
                self.error(f"{path}.status", f"must be one of {sorted(VALIDATION_STATUSES)}")
            exit_code = entry.get("exit_code")
            if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
                self.error(f"{path}.exit_code", "must be an integer or null")
            if status == "PASS" and exit_code != 0:
                self.error(f"{path}.exit_code", "PASS requires exit_code 0")
            if status in {"FAIL_INTRODUCED", "FAIL_BASELINE"} and (not isinstance(exit_code, int) or exit_code == 0):
                self.error(f"{path}.exit_code", f"{status} requires a non-zero integer")
            reason = entry.get("not_run_reason")
            if status == "NOT_RUN":
                self.require_string(reason, f"{path}.not_run_reason")
                if exit_code is not None:
                    self.error(f"{path}.exit_code", "NOT_RUN requires null exit_code")
            elif reason is not None and (not isinstance(reason, str) or not reason.strip()):
                self.error(f"{path}.not_run_reason", "must be null or a non-empty string")
            result.append(entry)
        return result

    def validate_release_gate(self, value: Any) -> str | None:
        gate = self.require_object(value, "$.release_gate")
        self.require_fields(gate, {"status", "checks", "blocking_reasons", "conditions"}, "$.release_gate")
        status = gate.get("status")
        if status not in RELEASE_STATUSES:
            self.error("$.release_gate.status", f"must be one of {sorted(RELEASE_STATUSES)}")
        checks = self.require_list(gate.get("checks"), "$.release_gate.checks")
        seen: set[str] = set()
        check_statuses: list[str] = []
        for index, raw in enumerate(checks):
            path = f"$.release_gate.checks[{index}]"
            check = self.require_object(raw, path)
            self.require_fields(check, {"item", "status", "evidence", "condition_or_gap"}, path)
            item = check.get("item")
            if item not in RELEASE_CHECKS:
                self.error(f"{path}.item", f"must be one of {sorted(RELEASE_CHECKS)}")
            elif item in seen:
                self.error(f"{path}.item", "must be unique")
            else:
                seen.add(item)
            check_status = check.get("status")
            if check_status not in RELEASE_CHECK_STATUSES:
                self.error(f"{path}.status", f"must be one of {sorted(RELEASE_CHECK_STATUSES)}")
            else:
                check_statuses.append(check_status)
            evidence = self.require_string_list(check.get("evidence"), f"{path}.evidence")
            if not evidence:
                self.error(f"{path}.evidence", "each release check requires evidence")
            gap = check.get("condition_or_gap")
            if check_status in {"CONDITIONAL", "FAIL", "NOT_VERIFIED"}:
                self.require_string(gap, f"{path}.condition_or_gap")
            elif gap is not None and (not isinstance(gap, str) or not gap.strip()):
                self.error(f"{path}.condition_or_gap", "must be null or a non-empty string")
        if status != "NOT_APPLICABLE" and seen != RELEASE_CHECKS:
            self.error("$.release_gate.checks", f"must contain all release checks; missing {sorted(RELEASE_CHECKS - seen)}")
        blockers = self.require_string_list(gate.get("blocking_reasons"), "$.release_gate.blocking_reasons")
        conditions = self.require_string_list(gate.get("conditions"), "$.release_gate.conditions")
        if status == "READY" and any(item not in {"PASS", "NOT_APPLICABLE"} for item in check_statuses):
            self.error("$.release_gate.status", "READY permits only PASS or NOT_APPLICABLE checks")
        if status != "NOT_APPLICABLE" and "FAIL" in check_statuses and status != "NOT_READY":
            self.error("$.release_gate.status", "any FAIL check requires NOT_READY")
        elif status != "NOT_APPLICABLE" and "NOT_VERIFIED" in check_statuses and status != "NOT_VERIFIED":
            self.error("$.release_gate.status", "any NOT_VERIFIED check requires NOT_VERIFIED unless a FAIL requires NOT_READY")
        elif status != "NOT_APPLICABLE" and "CONDITIONAL" in check_statuses and status != "READY_WITH_CONDITIONS":
            self.error("$.release_gate.status", "any CONDITIONAL check requires READY_WITH_CONDITIONS")
        elif status != "NOT_APPLICABLE" and check_statuses and all(
            item in {"PASS", "NOT_APPLICABLE"} for item in check_statuses
        ) and status != "READY":
            self.error("$.release_gate.status", "all PASS or NOT_APPLICABLE checks require READY")
        if status == "READY_WITH_CONDITIONS" and ("CONDITIONAL" not in check_statuses or not conditions):
            self.error("$.release_gate", "READY_WITH_CONDITIONS requires a CONDITIONAL check and conditions")
        if status == "NOT_READY" and ("FAIL" not in check_statuses or not blockers):
            self.error("$.release_gate", "NOT_READY requires a FAIL check and blocking_reasons")
        if status == "NOT_VERIFIED" and ("NOT_VERIFIED" not in check_statuses or not blockers):
            self.error("$.release_gate", "NOT_VERIFIED requires a NOT_VERIFIED check and blocking_reasons")
        return status if isinstance(status, str) else None

    def validate_rejected(self, value: Any) -> None:
        entries = self.require_list(value, "$.rejected_candidates")
        for index, raw in enumerate(entries):
            path = f"$.rejected_candidates[{index}]"
            entry = self.require_object(raw, path)
            self.require_fields(entry, {"candidate_id", "reason"}, path)
            candidate_id = self.require_string(entry.get("candidate_id"), f"{path}.candidate_id")
            if candidate_id and not re.fullmatch(r"CF-\d+", candidate_id):
                self.error(f"{path}.candidate_id", "must match CF-<digits>")
            self.require_string(entry.get("reason"), f"{path}.reason")

    def validate_verdict(
        self,
        verdict: Any,
        blockers: list[str],
        findings: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        coverage: list[dict[str, Any]],
        validation: list[dict[str, Any]],
        release_status: str | None,
        review_context: dict[str, Any],
        traceability: list[dict[str, Any]],
        limitations: list[str],
        residual_risks: list[str],
    ) -> None:
        blocking_findings = [item for item in findings if item.get("blocking") is True]
        blocking_questions = [item for item in questions if item.get("blocking") is True]
        missing_required_coverage = [
            item for item in coverage if item.get("required") is True and item.get("status") == "NOT_VERIFIED"
        ]
        introduced_failures = [item for item in validation if item.get("status") == "FAIL_INTRODUCED"]
        traceability_gaps = [
            item
            for item in traceability
            if item.get("status") in {"PARTIAL", "MISSING", "CONFLICT", "NOT_VERIFIED"}
        ]
        validation_gaps = [item for item in validation if item.get("status") != "PASS"]
        restricted_review = (
            review_context.get("review_mode") == "LITE"
            or review_context.get("documents_status") == "NOT_AVAILABLE"
        )
        waived_review = review_context.get("review_mode") == "WAIVED"
        change_required = bool(blocking_findings or introduced_failures or release_status == "NOT_READY")
        evidence_blocked = bool(
            blockers
            or blocking_questions
            or missing_required_coverage
            or release_status == "NOT_VERIFIED"
            or waived_review
        )
        follow_ups = bool(
            findings
            or questions
            or limitations
            or residual_risks
            or release_status == "READY_WITH_CONDITIONS"
            or any(item.get("status") == "NOT_VERIFIED" for item in coverage)
            or traceability_gaps
            or validation_gaps
            or restricted_review
        )

        if verdict != "BLOCKED" and not traceability:
            self.error("$.traceability", "must contain an explicit entry for every non-blocked review")
        if verdict != "BLOCKED" and not validation:
            self.error("$.validation", "must contain an execution or NOT_RUN record for every non-blocked review")
        if restricted_review and not (questions or limitations or residual_risks):
            self.error(
                "$.review_context",
                "restricted reviews must record the missing-document limitation or an open question",
            )
        if waived_review and not blockers:
            self.error("$.blockers", "WAIVED reviews must explain why the review is BLOCKED")
        if waived_review and verdict != "BLOCKED":
            self.error("$.verdict", "WAIVED reviews must return BLOCKED")
        if restricted_review and verdict == "APPROVE":
            self.error("$.verdict", "reviews without complete documents cannot be APPROVE")

        if change_required and verdict != "REQUEST_CHANGES":
            self.error("$.verdict", "must be REQUEST_CHANGES when blocking findings, introduced failures, or NOT_READY release gate exist")
        elif not change_required and evidence_blocked and verdict != "BLOCKED":
            self.error("$.verdict", "must be BLOCKED when decisive evidence is unavailable")
        elif not change_required and not evidence_blocked and follow_ups and verdict != "APPROVE_WITH_FOLLOW_UPS":
            self.error("$.verdict", "must be APPROVE_WITH_FOLLOW_UPS when non-blocking follow-ups remain")
        elif not change_required and not evidence_blocked and not follow_ups and verdict != "APPROVE":
            self.error("$.verdict", "must be APPROVE when no changes, blockers, or follow-ups remain")

        if verdict == "REQUEST_CHANGES" and not change_required:
            self.error("$.verdict", "REQUEST_CHANGES requires a blocking finding, introduced failure, or NOT_READY release gate")
        if verdict == "BLOCKED" and not evidence_blocked:
            self.error("$.verdict", "BLOCKED requires blockers, blocking questions, required coverage gaps, or NOT_VERIFIED release gate")


def load_payload(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_object(path: str) -> dict[str, Any]:
    value = load_payload(path)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="JSON report path, or '-' to read stdin")
    parser.add_argument(
        "--snapshot-manifest",
        required=True,
        help="deterministic snapshot manifest produced by freeze_review_snapshot.py",
    )
    args = parser.parse_args()
    try:
        payload = load_payload(args.report)
        snapshot_manifest = load_object(args.snapshot_manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"INVALID: unable to read JSON: {exc}", file=sys.stderr)
        return 2

    errors = ReportValidator(snapshot_manifest=snapshot_manifest).validate(payload)
    if errors:
        print(f"INVALID: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("VALID: code-review report structure and verdict invariants passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
