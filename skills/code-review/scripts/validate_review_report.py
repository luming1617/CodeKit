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

# 与快照脚本共享同一份常量，避免维度名/来源字面量在两处各自演化（审核报告 D4：
# 同一批枚举此前有 6 处书写且无测试看守）。以脚本目录为导入根，两个脚本同级分发。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from freeze_review_snapshot import (  # noqa: E402
    CHANGE_LIST_INLINE_LIMIT as _CHANGE_LIST_INLINE_LIMIT,
    CHANGE_LIST_PREVIEW as _CHANGE_LIST_PREVIEW,
    COMMITTED_RANGE_FIELDS as _COMMITTED_RANGE_FIELDS,
    COMPACTED_CHANGED_FIELDS as _COMPACTED_CHANGED_FIELDS,
    DIFF_SECTION_MARKERS as _DIFF_LAYERS,
    DIGEST_SCHEME as _DIGEST_SCHEME,
    DOCUMENT_FIELDS as _DOCUMENT_FIELDS,
    MANIFEST_SCHEMA_VERSION as _MANIFEST_SCHEMA_VERSION,
    PRODUCED_BY as FREEZE_PRODUCED_BY,
    RELEASE_CHECK_ITEMS as _SHARED_RELEASE_CHECKS,
    REVIEW_SCALE_FIELDS as _REVIEW_SCALE_FIELDS,
    RETRY_POLICIES as _RETRY_POLICIES,
    SCALE_TIERS as _SCALE_TIERS,
    SCALE_TIER_LIMITS as _SCALE_TIER_LIMITS,
    SNAPSHOT_REPORT_FIELDS as _SNAPSHOT_REPORT_FIELDS,
    SnapshotError,
    TARGET_ID_UNDECLARED as _TARGET_ID_UNDECLARED,
    HANDOFF_LIMITATIONS as _HANDOFF_LIMITATIONS,
    WORKTREE_STATE_FIELDS as _WORKTREE_STATE_FIELDS,
    _reconfigure_streams,
    build_snapshot as replay_snapshot,
    canonical_json as canonical_json,
    snapshot_field as snapshot_field,
    target_id_for as _target_id_for,
    write_text_atomic as _write_text_atomic,
    COVERAGE_DIMENSIONS as _SHARED_DIMENSIONS,  # noqa: E402
    MAX_REFREEZE as _MAX_REFREEZE,
)  # noqa: E402
from record_review_dispatch import (  # noqa: E402
    LedgerError as DispatchLedgerError,
    INPUT_FIELDS as LEDGER_INPUT_FIELDS,
    MAX_DISPATCHES as LEDGER_MAX_DISPATCHES,
    RETURNED_STATUSES as LEDGER_STATUSES,
    ROLES as LEDGER_ROLES,
    TIMESTAMP_RE as LEDGER_TIMESTAMP_RE,
    read_ledger as read_dispatch_ledger,
)
# cp936 控制台下 CJK 正文/路径会以 GBK 字节写出（宿主按 UTF-8 解码即乱码，严重时 print 直接抛
# UnicodeEncodeError）。强制 UTF-8 输出流这层与 freeze/dispatch 脚本共用同一实现，不再各自演化。
VERDICTS = {
    "APPROVE",
    "APPROVE_WITH_FOLLOW_UPS",
    "REQUEST_CHANGES",
    "BLOCKED",
}
DIMENSIONS = set(_SHARED_DIMENSIONS)
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
RELEASE_CHECKS = set(_SHARED_RELEASE_CHECKS)
RELEASE_CHECK_STATUSES = {"PASS", "CONDITIONAL", "FAIL", "NOT_VERIFIED", "NOT_APPLICABLE"}
# CW1-e：检查项分级。此前任何一项 NOT_VERIFIED 都把整道 Gate 拖进 NOT_VERIFIED，
# 进而强制 BLOCKED —— 实测「如实标注未核实」比「改标 NOT_APPLICABLE」多一次驳回，
# 等于给漏报发奖金。分级后如实标注非关键项只落 APPROVE_WITH_FOLLOW_UPS。
RELEASE_CHECK_CRITICALITY = {"critical", "non_critical"}
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
# 报告侧 snapshot 必须携带的键，直接取 freeze 脚本的绑定字段清单：加一个签名
# 字段时两处同时生效，不再靠人工同步（历史上 documents 就漏过一次）。
SNAPSHOT_FIELDS = set(_SNAPSHOT_REPORT_FIELDS)
# CW2-a③：--handoff 段的结构与限制枚举。字面量只有一份来源（freeze 脚本写，这里核对）。
HANDOFF_FIELDS = {
    "path",
    "sources",
    "digest_scheme",
    "target_id",
    "target_id_matched",
    "compared_fields",
    "uncomparable_digests",
    "limitations",
}
# CW1-f：派发台账的机械契约。role/状态/时间戳三个集合直接从写入脚本导入，
# 不在这里重复字面量（同一批枚举此前有 6 处书写且 0 个测试看守，见审核报告 D4）。
LEDGER_FIELDS = {
    "step",
    "role",
    "instance_id",
    "input_diff_digest",
    "returned_status",
    "started_at",
    "ended_at",
}
# 覆盖矩阵里 owner 允许的两个非 subagent 取值：本地验证器由主 Agent 直跑，
# "none" 只可能配 NOT_VERIFIED / NOT_APPLICABLE（上面按 status 分支处理）。
LEDGER_EXEMPT_OWNERS = {"local-validator"}
# CW3-a：manifest 一旦声明 diff 产物就必须成形（可选键，见 validate_diff_artifact）。
DIFF_ARTIFACT_FIELDS = {"path", "bytes", "digest", "sections", "shard_index"}
DIFF_ARTIFACT_SECTIONS = {*_DIFF_LAYERS, "untracked"}


class ReportValidator:
    def __init__(
        self,
        snapshot_manifest: dict[str, Any] | None = None,
        dispatch_ledger: list[dict[str, Any]] | None = None,
    ) -> None:
        self.errors: list[str] = []
        self.snapshot_manifest = snapshot_manifest
        self.dispatch_ledger = dispatch_ledger

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
        self.validate_signed_disclosures(limitations)
        self.validate_dispatches(snapshot_digest, coverage)
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
        self.require_fields(scope, {"target_type", "target_locator", "target_id", "included", "excluded"}, "$.scope")
        self.require_string(scope.get("target_type"), "$.scope.target_type")
        self.require_string(scope.get("target_locator"), "$.scope.target_locator")
        self.require_string(scope.get("target_id"), "$.scope.target_id")
        self.require_string_list(scope.get("included"), "$.scope.included", nonempty=True)
        self.require_string_list(scope.get("excluded"), "$.scope.excluded")
        for group in ("committed_range", "worktree_state"):
            if group in scope:
                self.error(
                    f"$.scope.{group}",
                    "the report payload stays flat; grouped fields are copied by name from the manifest",
                )
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
        changed = snapshot.get("changed_files")
        if not isinstance(changed, dict):
            # CW3-c：超过阈值的变更清单在报告侧改为摘要形状（见 validate_changed_files），
            # 未compact 时仍是字符串数组，与 manifest 逐字段相等。
            self.require_string_list(changed, "$.snapshot.changed_files")
        self.require_string_list(snapshot.get("preexisting_changes"), "$.snapshot.preexisting_changes")
        # CW2-a③：跨 SKILL 连接键进报告负载，下游据此核对"这份结论对应的就是我交出去的那个
        # 目标"。分组本身（committed_range / worktree_state）只活在 manifest 里——报告侧一律
        # 扁平，历史消费方（code-impl 的 handoff 校验）按 snapshot.base_sha 取值。
        self.require_string(snapshot.get("target_id"), "$.snapshot.target_id")
        for group in ("committed_range", "worktree_state"):
            if group in snapshot:
                self.error(
                    f"$.snapshot.{group}",
                    "the report payload stays flat; grouped fields are copied by name from the manifest",
                )
        digest = snapshot.get("diff_digest")
        self.validate_snapshot_manifest(snapshot, scope)
        return digest if isinstance(digest, str) else ""

    def validate_snapshot_manifest(self, snapshot: dict[str, Any], scope: dict[str, Any] | None) -> None:
        manifest = self.snapshot_manifest
        if manifest is None:
            return
        if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
            self.error("$.snapshot.manifest", f"schema_version must equal {_MANIFEST_SCHEMA_VERSION!r}")
        # CW1-c：来源指纹。缺这三项的 manifest 只可能是手工拼的——digest 本身算得出
        # 来，但 produced_by 的字面量与 git_probe_digest 的形状要求模型精确复现脚本
        # 内部实现才写得对。成效是提高伪造成本，不声称不可伪造（无密钥体系做不到）。
        if manifest.get("produced_by") != FREEZE_PRODUCED_BY:
            self.error("$.snapshot.manifest.produced_by", f"must equal {FREEZE_PRODUCED_BY!r}")
        if not isinstance(manifest.get("generator_version"), str) or not manifest.get("generator_version"):
            self.error("$.snapshot.manifest.generator_version", "must be a non-empty string")
        probe = manifest.get("git_probe_digest")
        if not isinstance(probe, str) or not DIGEST_RE.fullmatch(probe):
            self.error("$.snapshot.manifest.git_probe_digest", "must be a lowercase SHA-256 digest")
        # CW4-a：重冻结预算。放大路径是"改标签→重校验→再重冻结"，步骤 22 的 3 轮上限
        # 管不到它；freeze_index 由冻结脚本按同一目标的磁盘谱系算出，这里核对它没被写大。
        # CW4-e：`retry_policy: prose`（回滚开关）只把这一道拒绝降级成"必须逐字披露"；
        # 派发预算 MAX_DISPATCHES 是 policy-independent 绝对上限，两种模式下都仍是硬错误。
        policy = manifest.get("retry_policy")
        if policy not in _RETRY_POLICIES:
            self.error("$.snapshot.manifest.retry_policy", f"must be one of {sorted(_RETRY_POLICIES)}")
        index = manifest.get("freeze_index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            self.error("$.snapshot.manifest.freeze_index", "must be an integer >= 1")
        elif index > _MAX_REFREEZE and policy != "prose":
            self.error(
                "$.snapshot.manifest.freeze_index",
                f"snapshot #{index} is above the freeze budget MAX_REFREEZE={_MAX_REFREEZE}; "
                "the run must end as BLOCKED with the uncovered items listed, not as a verdict",
            )
        self.validate_target_groups(manifest)
        self.validate_diff_artifact(manifest.get("diff_artifact"))
        self.validate_documents(manifest.get("documents"))
        self.validate_review_scale(manifest.get("review_scale"))
        self.validate_handoff(manifest.get("handoff"))
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
        for field, path in _SNAPSHOT_REPORT_FIELDS.items():
            # `changed_files` 有逐字照抄与摘要两种形状，由 validate_changed_files 单独核对。
            if field == "changed_files":
                continue
            if snapshot.get(field) != snapshot_field(manifest, *path):
                self.error(f"$.snapshot.{field}", "must match the frozen snapshot manifest")
        self.validate_changed_files(snapshot.get("changed_files"), manifest)
        if scope is not None:
            for field in ("target_type", "target_locator", "target_id"):
                if scope.get(field) != manifest.get(field):
                    self.error(f"$.scope.{field}", "must match the frozen snapshot manifest")

    def validate_target_groups(self, manifest: dict[str, Any]) -> None:
        """CW2-a①②：编码自述、跨 SKILL 连接键与两段目标标识都必须成形状。

        两段拆分后同一值不得再留在顶层（`flat duplicate` 分支）：手抄一份扁平 base_sha
        与分组里的值不一致时，跨 SKILL 比对会各取一侧，正好复现审核报告 A5 的形状。
        """
        scheme = manifest.get("digest_scheme")
        if scheme != _DIGEST_SCHEME:
            self.error(
                "$.snapshot.manifest.digest_scheme",
                f"must equal {_DIGEST_SCHEME!r}: every digest in this artifact is a bare lowercase "
                "sha256 hexdigest; a producer that encodes differently must say so instead of being "
                "compared by `==`",
            )
        recorded_id = manifest.get("target_id")
        expected_id = _target_id_for(
            manifest.get("repository_root", ""),
            snapshot_field(manifest, "committed_range", "head_sha"),
        )
        if not isinstance(recorded_id, str) or not recorded_id:
            self.error("$.snapshot.manifest.target_id", "must be a non-empty string")
        elif recorded_id != expected_id:
            self.error(
                "$.snapshot.manifest.target_id",
                f"must equal the cross-skill join key {expected_id!r} "
                "(normalized repository_root + committed_range.head_sha)",
            )
        for name, fields in (
            ("committed_range", _COMMITTED_RANGE_FIELDS),
            ("worktree_state", _WORKTREE_STATE_FIELDS),
        ):
            path = f"$.snapshot.manifest.{name}"
            group = manifest.get(name)
            if not isinstance(group, dict):
                self.error(path, "must be an object")
                continue
            self.require_fields(group, set(fields), path)
            for field in fields:
                value = group.get(field)
                if field.endswith("_sha"):
                    self.require_string(value, f"{path}.{field}")
                    if isinstance(value, str) and not SHA_RE.fullmatch(value):
                        self.error(f"{path}.{field}", "must be a full Git SHA")
                else:
                    digest = self.require_string(value, f"{path}.{field}")
                    if digest and not DIGEST_RE.fullmatch(digest):
                        self.error(f"{path}.{field}", "must be a lowercase SHA-256 digest")
        grouped = {field: owner for owner, fields in (
            ("committed_range", _COMMITTED_RANGE_FIELDS),
            ("worktree_state", _WORKTREE_STATE_FIELDS),
        ) for field in fields}
        for field in sorted(set(grouped) & set(_SNAPSHOT_REPORT_FIELDS)):
            if field in manifest:
                self.error(
                    f"$.snapshot.manifest.{field}",
                    f"moved into {grouped[field]}; a flat duplicate of a grouped field is not a "
                    "valid manifest",
                )

    def validate_handoff(self, block: Any) -> None:
        """CW2-a③：`--handoff` 的比对结果一旦签进 manifest 就必须成形，且不得被改写。"""
        if block is None:
            return
        path = "$.snapshot.manifest.handoff"
        if not isinstance(block, dict):
            self.error(path, "must be an object")
            return
        self.require_fields(block, HANDOFF_FIELDS, path)
        self.require_string(block.get("path"), f"{path}.path")
        self.require_string(block.get("target_id"), f"{path}.target_id")
        for field in ("sources", "compared_fields", "uncomparable_digests", "limitations"):
            self.require_string_list(block.get(field), f"{path}.{field}")
        if block.get("digest_scheme") != _DIGEST_SCHEME:
            self.error(f"{path}.digest_scheme", f"must equal {_DIGEST_SCHEME!r}")
        matched = block.get("target_id_matched")
        if matched is not True and matched != _TARGET_ID_UNDECLARED:
            self.error(
                f"{path}.target_id_matched",
                f"must be true, or the literal {_TARGET_ID_UNDECLARED!r} when the upstream predates target_id",
            )
        for entry in block.get("limitations", []) or []:
            if isinstance(entry, str) and entry.split(":", 1)[0].strip() not in _HANDOFF_LIMITATIONS:
                self.error(
                    f"{path}.limitations",
                    f"unknown handoff limitation {entry!r}; the script only emits {list(_HANDOFF_LIMITATIONS)}",
                )

    def validate_signed_disclosures(self, limitations: list[str]) -> None:
        """脚本签进 manifest 的每一项放行/限制，报告都必须逐字带一条 —— 凭据在签名里，措辞在模型侧。

        两类凭据共用这一条核对：
        ① CW4-e：``retry_policy: prose`` 且 freeze_index 超预算 → 需要 ``MAX_REFREEZE=<序号>``，
           否则回滚开关就成了静默绕过；
        ② CW2-a③：``handoff.limitations`` 里的每个标记（``SCOPE_SUPERSET`` /
           ``HANDOFF_SCHEME_MISMATCH``）→ 需要该标记逐字出现在 limitations 里。上游没看过
           已提交范围不是缺陷，但"报告读者以为审全了"是缺陷。
        校验器不判断这句话写得好不好，只判断这些放行是否留下了可核对的凭据。
        """
        manifest = self.snapshot_manifest
        if not isinstance(manifest, dict):
            return
        required: dict[str, str] = {}
        if manifest.get("retry_policy") == "prose":
            index = manifest.get("freeze_index")
            if not isinstance(index, bool) and isinstance(index, int) and index > _MAX_REFREEZE:
                required[f"MAX_REFREEZE={index}"] = "retry_policy=prose 且重冻结已超预算"
        block = manifest.get("handoff")
        if isinstance(block, dict):
            for entry in block.get("limitations", []) or []:
                if isinstance(entry, str):
                    marker = entry.split(":", 1)[0].strip()
                    if marker:
                        required[marker] = "handoff.limitations"
        for marker, reason in sorted(required.items()):
            if not any(marker in item for item in limitations):
                self.error(
                    "$.limitations",
                    f"the frozen manifest records the marker {marker!r} ({reason}); copy it into "
                    "`limitations` verbatim instead of letting a bounded or narrowed run read as a clean one",
                )

    def validate_diff_artifact(self, artifact: Any) -> None:
        # CW3-a：产物是派发读物而不是判据，manifest 里没有这个键也合法（未用
        # --emit-diff 的历史 run 与 Handoff 复用），所以整段可选。但一旦声明，
        # 路径/字节数/摘要就必须成形状——"我读了这份 diff"不能挂在一个含糊的文件上。
        if artifact is None:
            return
        path = "$.snapshot.manifest.diff_artifact"
        if not isinstance(artifact, dict):
            self.error(path, "must be an object")
            return
        self.require_fields(artifact, DIFF_ARTIFACT_FIELDS, path)
        self.require_string(artifact.get("path"), f"{path}.path")
        digest = self.require_string(artifact.get("digest"), f"{path}.digest")
        if digest and not DIGEST_RE.fullmatch(digest):
            self.error(f"{path}.digest", "must be a lowercase SHA-256 digest")
        size = artifact.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            self.error(f"{path}.bytes", "must be a non-negative integer")
        sections = artifact.get("sections")
        if not isinstance(sections, list) or set(sections) != DIFF_ARTIFACT_SECTIONS:
            self.error(f"{path}.sections", f"must list {sorted(DIFF_ARTIFACT_SECTIONS)}")
        shard_index = artifact.get("shard_index")
        if not (shard_index is None or (isinstance(shard_index, str) and shard_index.strip())):
            self.error(f"{path}.shard_index", "must be null or a non-empty path")

    def validate_dispatch_inputs(self, inputs: Any, path: str) -> None:
        # CW2-c：角色按路径读中间契约，所以台账要能说出交出去了哪几份文件。
        # 缺 inputs 键是 CW2-c 之前的历史台账，合法；一旦声明就必须成形状。
        if inputs is None:
            return
        field = f"{path}.inputs"
        if not isinstance(inputs, list):
            self.error(field, "must be a list of {path, digest} objects")
            return
        seen: set[str] = set()
        for index, item in enumerate(inputs):
            item_path = f"{field}[{index}]"
            if not isinstance(item, dict):
                self.error(item_path, "must be an object")
                continue
            self.require_fields(item, set(LEDGER_INPUT_FIELDS), item_path)
            locator = self.require_string(item.get("path"), f"{item_path}.path")
            if locator:
                if not Path(locator).is_absolute():
                    self.error(f"{item_path}.path", "must be an absolute path")
                if locator in seen:
                    self.error(f"{item_path}.path", f"duplicate input: {locator}")
                seen.add(locator)
            digest = self.require_string(item.get("digest"), f"{item_path}.digest")
            if digest and not DIGEST_RE.fullmatch(digest):
                self.error(f"{item_path}.digest", "must be a lowercase SHA-256 digest")

    def validate_review_scale(self, scale: Any) -> None:
        # CW4-b：档位决定派发力度，所以它必须是脚本算出来的那一份。允许模型自报档位
        # 就等于把"这轮很小，只派两个人"写成了自述；tier_limits 一并核对，改阈值要两侧同改。
        path = "$.snapshot.manifest.review_scale"
        if not isinstance(scale, dict):
            self.error(path, "must be an object produced by the freeze script")
            return
        self.require_fields(scale, set(_REVIEW_SCALE_FIELDS), path)
        if scale.get("tier") not in _SCALE_TIERS:
            self.error(f"{path}.tier", f"must be one of {list(_SCALE_TIERS)}")
        for field in ("changed_files", "changed_lines"):
            value = scale.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                self.error(f"{path}.{field}", "must be a non-negative integer")
        expected_limits = [list(item) for item in _SCALE_TIER_LIMITS]
        if scale.get("tier_limits") != expected_limits:
            self.error(f"{path}.tier_limits", f"must equal {expected_limits}")

    def validate_documents(self, documents: Any) -> None:
        # CW2-b：文档版本从"Orchestrator 自称读过"变成签名 manifest 的一部分。
        # 空清单是合法状态（DOCUMENTS: NOT_AVAILABLE），缺键不是——缺键意味着这份
        # manifest 不是当前版本的快照脚本产出的。
        path = "$.snapshot.manifest.documents"
        if not isinstance(documents, list):
            self.error(path, "must be a list of frozen documents (empty when no document is available)")
            return
        seen: set[str] = set()
        for index, entry in enumerate(documents):
            item = f"{path}[{index}]"
            if not isinstance(entry, dict):
                self.error(item, "must be an object")
                continue
            self.require_fields(entry, set(_DOCUMENT_FIELDS), item)
            locator = self.require_string(entry.get("locator"), f"{item}.locator")
            if locator:
                if locator in seen:
                    self.error(f"{item}.locator", f"duplicate document entry: {locator}")
                seen.add(locator)
            revision = self.require_string(entry.get("revision"), f"{item}.revision")
            if revision and not revision.strip():
                self.error(f"{item}.revision", "must not be blank")
            digest = self.require_string(entry.get("content_digest"), f"{item}.content_digest")
            if digest and not DIGEST_RE.fullmatch(digest):
                self.error(f"{item}.content_digest", "must be a lowercase SHA-256 digest")

    def validate_changed_files(self, value: Any, manifest: dict[str, Any]) -> None:
        # CW3-c：报告侧要么逐字段照抄全量清单，要么改用摘要形状（大 MR 不再把几百行
        # 路径抄进每个角色上下文）。摘要形状必须能核回 manifest：数量、前缀、全量摘要
        # 与清单文件落点四项都对得上，截断的只是读物，不是判据。
        path = "$.snapshot.changed_files"
        full = manifest.get("changed_files")
        expected = hashlib.sha256(canonical_json(full)).hexdigest() if isinstance(full, list) else ""
        own = manifest.get("changed_files_digest")
        if isinstance(full, list):
            if not isinstance(own, str) or not DIGEST_RE.fullmatch(own):
                self.error("$.snapshot.manifest.changed_files_digest", "must be a lowercase SHA-256 digest")
            elif own != expected:
                self.error("$.snapshot.manifest.changed_files_digest", "does not match manifest.changed_files")
        if isinstance(value, dict):
            for field in _COMPACTED_CHANGED_FIELDS:
                if field not in value:
                    self.error(f"{path}.{field}", "is required")
            self.require_string(value.get("full_list_path"), f"{path}.full_list_path")
            digest = self.require_string(value.get("digest"), f"{path}.digest")
            if digest and not DIGEST_RE.fullmatch(digest):
                self.error(f"{path}.digest", "must be a lowercase SHA-256 digest")
            count = value.get("count")
            if isinstance(count, bool) or not isinstance(count, int):
                self.error(f"{path}.count", "must be an integer")
            first = value.get("first")
            if isinstance(first, list):
                self.require_string_list(first, f"{path}.first")
                if len(first) > _CHANGE_LIST_PREVIEW:
                    self.error(f"{path}.first", f"may carry at most {_CHANGE_LIST_PREVIEW} preview entries")
            elif first is not None:
                self.error(f"{path}.first", "must be an array of strings")
            if not isinstance(full, list):
                return
            if value.get("full_list_path") != manifest.get("changed_files_path"):
                self.error(f"{path}.full_list_path", "must match the manifest's changed_files_path")
            if isinstance(count, int) and not isinstance(count, bool) and count != len(full):
                self.error(f"{path}.count", f"must equal the manifest change list length ({len(full)})")
            if len(full) <= _CHANGE_LIST_INLINE_LIMIT:
                self.error(
                    path,
                    f"must be copied verbatim while the change list has at most {_CHANGE_LIST_INLINE_LIMIT} entries",
                )
            if isinstance(first, list) and first != full[: len(first)]:
                self.error(f"{path}.first", "must be the leading entries of the manifest change list")
            if digest and DIGEST_RE.fullmatch(digest) and digest != expected:
                self.error(f"{path}.digest", "does not match the manifest change list")
            return
        self.require_string_list(value, path)

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
        critical_not_verified: list[str] = []
        non_critical_not_verified: list[str] = []
        for index, raw in enumerate(checks):
            path = f"$.release_gate.checks[{index}]"
            check = self.require_object(raw, path)
            self.require_fields(check, {"item", "status", "evidence", "condition_or_gap", "criticality"}, path)
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
            criticality = check.get("criticality")
            if criticality not in RELEASE_CHECK_CRITICALITY:
                self.error(f"{path}.criticality", f"must be one of {sorted(RELEASE_CHECK_CRITICALITY)}")
            if check_status == "NOT_VERIFIED":
                (critical_not_verified if criticality == "critical" else non_critical_not_verified).append(str(item))
            evidence = self.require_string_list(check.get("evidence"), f"{path}.evidence")
            if not evidence:
                self.error(f"{path}.evidence", "each release check requires evidence")
            gap = check.get("condition_or_gap")
            if check_status in {"CONDITIONAL", "FAIL", "NOT_VERIFIED", "NOT_APPLICABLE"}:
                # NOT_APPLICABLE 同样必须写明"为什么不适用"：它是 A4 里唯一能同时摘掉
                # Gate 缺口和 verdict 约束的标注，此前允许 condition_or_gap 为 null。
                self.require_string(gap, f"{path}.condition_or_gap")
            elif gap is not None and (not isinstance(gap, str) or not gap.strip()):
                self.error(f"{path}.condition_or_gap", "must be null or a non-empty string")
        # CW1-e：顶层 NOT_APPLICABLE 不再豁免检查项清单——过去只要把整道 Gate 标成
        # NOT_APPLICABLE，十项检查就可以一条都不写，这是 A4 第三条场景里最省事的漏报路径。
        if seen != RELEASE_CHECKS:
            self.error("$.release_gate.checks", f"must contain all release checks; missing {sorted(RELEASE_CHECKS - seen)}")
        if status == "NOT_APPLICABLE" and check_statuses and any(item != "NOT_APPLICABLE" for item in check_statuses):
            self.error("$.release_gate.status", "NOT_APPLICABLE requires every release check to be NOT_APPLICABLE")
        blockers = self.require_string_list(gate.get("blocking_reasons"), "$.release_gate.blocking_reasons")
        conditions = self.require_string_list(gate.get("conditions"), "$.release_gate.conditions")
        # Gate 状态按 FAIL > critical NOT_VERIFIED > 非 critical NOT_VERIFIED > CONDITIONAL > 全 PASS
        # 的优先级裁决：高优先级检查项存在时，低优先级分支不得再对 status 提出互斥要求，
        # 否则 release-gate.md 允许的混合严重度报告（如 FAIL + CONDITIONAL → NOT_READY）
        # 会落进无解死锁，逼迫伪造检查项状态才能通过校验。
        fail_present = "FAIL" in check_statuses
        critical_gap_present = bool(critical_not_verified)
        not_verified_present = "NOT_VERIFIED" in check_statuses
        conditional_present = "CONDITIONAL" in check_statuses
        if status == "READY" and any(item not in {"PASS", "NOT_APPLICABLE"} for item in check_statuses):
            self.error("$.release_gate.status", "READY permits only PASS or NOT_APPLICABLE checks")
        if status != "NOT_APPLICABLE" and fail_present and status != "NOT_READY":
            self.error("$.release_gate.status", "any FAIL check requires NOT_READY")
        elif status != "NOT_APPLICABLE" and not fail_present and critical_gap_present and status != "NOT_VERIFIED":
            self.error(
                "$.release_gate.status",
                f"critical NOT_VERIFIED checks {sorted(critical_not_verified)} require NOT_VERIFIED "
                "unless a FAIL requires NOT_READY",
            )
        elif (
            status != "NOT_APPLICABLE"
            and not fail_present
            and not critical_gap_present
            and non_critical_not_verified
            and status not in {"READY_WITH_CONDITIONS", "NOT_VERIFIED"}
        ):
            # 如实标注的非关键缺口不强制 BLOCKED：改标 NOT_APPLICABLE 不再比诚实标注更省事。
            self.error(
                "$.release_gate.status",
                f"non-critical NOT_VERIFIED checks {sorted(non_critical_not_verified)} require "
                "READY_WITH_CONDITIONS (or NOT_VERIFIED to escalate the whole gate)",
            )
        elif (
            status != "NOT_APPLICABLE"
            and not fail_present
            and not not_verified_present
            and conditional_present
            and status != "READY_WITH_CONDITIONS"
        ):
            self.error("$.release_gate.status", "any CONDITIONAL check requires READY_WITH_CONDITIONS unless a FAIL or NOT_VERIFIED check takes precedence")
        elif (
            status != "NOT_APPLICABLE"
            and not fail_present
            and not not_verified_present
            and not conditional_present
            and check_statuses
            and all(item in {"PASS", "NOT_APPLICABLE"} for item in check_statuses)
            and status != "READY"
        ):
            self.error("$.release_gate.status", "all PASS or NOT_APPLICABLE checks require READY")
        if status == "READY_WITH_CONDITIONS" and not (conditional_present or non_critical_not_verified):
            self.error(
                "$.release_gate",
                "READY_WITH_CONDITIONS requires a CONDITIONAL check or a non-critical NOT_VERIFIED check",
            )
        if status == "READY_WITH_CONDITIONS" and not conditions:
            self.error("$.release_gate.conditions", "READY_WITH_CONDITIONS requires at least one condition")
        if status == "NOT_READY" and ("FAIL" not in check_statuses or not blockers):
            self.error("$.release_gate", "NOT_READY requires a FAIL check and blocking_reasons")
        if status == "NOT_VERIFIED" and ("NOT_VERIFIED" not in check_statuses or not blockers):
            self.error("$.release_gate", "NOT_VERIFIED requires a NOT_VERIFIED check and blocking_reasons")
        return status if isinstance(status, str) else None

    def validate_dispatches(self, snapshot_digest: str, coverage: list[dict[str, Any]]) -> None:
        # CW1-f：派发台账把「覆盖矩阵说这一维由某 Specialist 审过」从一句自述变成一个
        # 可核对的外键。此前 SKILL 要求步骤 18 必须是「未参与步骤 10 的全新 Verifier」，
        # 但没有任何落盘记录能核对——报告里写谁的名字都行。
        if self.dispatch_ledger is None:
            return
        if not self.dispatch_ledger:
            self.error(
                "$dispatch_ledger",
                "ledger is empty; append every subagent dispatch with record_review_dispatch.py",
            )
            return
        if len(self.dispatch_ledger) > LEDGER_MAX_DISPATCHES:
            # CW4-a：预算同时在写入侧（record_review_dispatch.py 拒绝第 15 条）和门禁侧
            # 核对——绕开登记脚本手搓台账也不能超出预算。
            self.error(
                "$dispatch_ledger",
                f"ledger holds {len(self.dispatch_ledger)} dispatches, above the "
                f"MAX_DISPATCHES={LEDGER_MAX_DISPATCHES} budget; stop dispatching and report the "
                "uncovered dimensions, candidates and checks as BLOCKED",
            )
        seen_instances: set[str] = set()
        instance_ids: set[str] = set()
        step_times: dict[int, list[tuple[str, str]]] = {}
        for index, record in enumerate(self.dispatch_ledger):
            path = f"$dispatch_ledger[{index}]"
            if not isinstance(record, dict):
                self.error(path, "must be an object")
                continue
            self.require_fields(record, LEDGER_FIELDS, path)
            role = record.get("role")
            if role not in LEDGER_ROLES:
                self.error(f"{path}.role", f"must be one of {sorted(LEDGER_ROLES)}")
            instance_id = self.require_string(record.get("instance_id"), f"{path}.instance_id")
            instance_ids.add(instance_id)
            key = f"{role}:{instance_id}"
            if key in seen_instances:
                self.error(f"{path}.instance_id", f"{key} is already dispatched earlier in the ledger")
            seen_instances.add(key)
            if record.get("returned_status") not in LEDGER_STATUSES:
                self.error(f"{path}.returned_status", f"must be one of {sorted(LEDGER_STATUSES)}")
            digest = record.get("input_diff_digest")
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                self.error(f"{path}.input_diff_digest", "must be a lowercase SHA-256 digest")
            elif snapshot_digest and digest != snapshot_digest:
                self.error(
                    f"{path}.input_diff_digest",
                    "dispatch was not run against the frozen snapshot diff_digest",
                )
            started, ended = record.get("started_at"), record.get("ended_at")
            stamps = [value if isinstance(value, str) and LEDGER_TIMESTAMP_RE.fullmatch(value) else "" for value in (started, ended)]
            for value, field in zip(stamps, ("started_at", "ended_at")):
                if not value:
                    self.error(f"{path}.{field}", "must be UTC, e.g. 2026-01-02T03:04:05Z")
            if all(stamps) and stamps[1] < stamps[0]:
                self.error(f"{path}.ended_at", "must not precede started_at")
            step = record.get("step")
            if not isinstance(step, int) or not 1 <= step <= 22:
                self.error(f"{path}.step", "must be an integer SKILL step number between 01 and 22")
            elif all(stamps):
                step_times.setdefault(step, []).append((stamps[0], stamps[1]))
            # CW2-c：角色按路径读中间契约，所以台账要能说出交出去了哪几份文件。
            # 缺 inputs 键是 CW2-c 之前的历史台账，合法；一旦声明就必须成形状。
            self.validate_dispatch_inputs(record.get("inputs"), path)

        for index, entry in enumerate(coverage):
            if entry.get("status") not in {"REVIEWED", "VALIDATED"}:
                continue
            owner = entry.get("owner")
            if owner in LEDGER_EXEMPT_OWNERS:
                continue
            if owner not in instance_ids:
                self.error(
                    f"$.coverage[{index}].owner",
                    f"{owner!r} never appears in the dispatch ledger, so this "
                    f"{entry.get('status')} coverage cannot be attributed to a dispatched reviewer",
                )

        def earliest(step: int, role: str) -> str:
            started_at = [
                record["started_at"]
                for record in self.dispatch_ledger or []
                if isinstance(record, dict)
                and record.get("role") == role
                and record.get("step") == step
                and isinstance(record.get("started_at"), str)
                and LEDGER_TIMESTAMP_RE.fullmatch(record["started_at"])
            ]
            return min(started_at) if started_at else ""

        specialists = earliest(8, "review-specialist")
        rebuttals = earliest(10, "review-evidence")
        rechecks = earliest(18, "review-evidence")
        if specialists and rebuttals and rebuttals < specialists:
            self.error("$dispatch_ledger", "a step 10 rebuttal dispatch starts before any step 08 specialist dispatch")
        if rebuttals and rechecks and rechecks < rebuttals:
            self.error("$dispatch_ledger", "the step 18 re-review dispatch starts before the step 10 rebuttal dispatch")
        rebuttal_ids = {
            str(record.get("instance_id"))
            for record in self.dispatch_ledger or []
            if isinstance(record, dict) and record.get("role") == "review-evidence" and record.get("step") == 10
        }
        recheck_ids = {
            str(record.get("instance_id"))
            for record in self.dispatch_ledger or []
            if isinstance(record, dict) and record.get("role") == "review-evidence" and record.get("step") == 18
        }
        shared = sorted(rebuttal_ids & recheck_ids)
        if shared:
            self.error(
                "$dispatch_ledger",
                f"step 18 must use a fresh Evidence Verifier; {shared} already rebutted at step 10",
            )

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


# CW1-d：manifest 只自证内部一致（manifest_digest 是对自己签的），不证明它对应
# 此刻的工作树。可选地把 freeze 脚本重放一遍，比对结构指纹，把"审查完又改了代码还
# 沿用旧结论"变成 rc=1 的机器判据。默认关闭：它要读工作树、且 git 子进程有成本。
# CW2-a②：manifest 已分段，这里比对的是"标签 + manifest 路径"，错误信息仍按标签播报，
# 读者不需要知道 base_sha 住在哪个分组里。target_id 是派生字段：它由 repository_root 与
# head_sha 算出，重放时重算一遍即可核对跨 SKILL 的连接键没被手写（A5 的另一半病根）。
REPLAY_COMPARED_FIELDS = (
    ("target_id", ("target_id",)),
    ("committed_range.base_sha", ("committed_range", "base_sha")),
    ("committed_range.head_sha", ("committed_range", "head_sha")),
    ("committed_range.range_digest", ("committed_range", "range_digest")),
    ("worktree_state.diff_digest", ("worktree_state", "diff_digest")),
    ("worktree_state.staged_digest", ("worktree_state", "staged_digest")),
    ("worktree_state.unstaged_digest", ("worktree_state", "unstaged_digest")),
    ("worktree_state.untracked_digest", ("worktree_state", "untracked_digest")),
    ("changed_files", ("changed_files",)),
    ("changed_files_digest", ("changed_files_digest",)),
    ("preexisting_changes", ("preexisting_changes",)),
    ("documents", ("documents",)),
)


def replayed_field(manifest: dict[str, Any], label: str, path: tuple[str, ...]) -> object:
    """派生字段的取值口径唯一化：磁盘上的重放方与报告侧的比对共用这一条表达式。"""
    if label == "target_id":
        return _target_id_for(
            manifest.get("repository_root", ""),
            snapshot_field(manifest, "committed_range", "head_sha"),
        )
    return snapshot_field(manifest, *path)


def handoff_required_limitations(block: object) -> list[str]:
    """Handoff 比对段里必须逐字进报告 `limitations` 的那些标记。"""
    if not isinstance(block, dict):
        return []
    return sorted(
        {
            marker
            for entry in block.get("limitations", []) or []
            if isinstance(entry, str)
            for marker in (entry.split(":", 1)[0].strip(),)
            if marker
        }
    )


def diff_artifact_staleness_errors(artifact: dict[str, Any]) -> list[str]:
    """核对 manifest 声明的 diff 读物仍是磁盘上那一份（派发只传路径后的闭环）。"""
    path = artifact.get("path")
    digest = artifact.get("digest")
    if not isinstance(path, str) or not path or not isinstance(digest, str) or not digest:
        return ["$--check-worktree STALE_SNAPSHOT: diff_artifact must record both path and digest"]
    try:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        return [f"$--check-worktree STALE_SNAPSHOT: diff artifact {path} is unreadable: {exc}"]
    if actual != digest:
        return [
            f"$--check-worktree STALE_SNAPSHOT: diff_artifact digest changed "
            f"(manifest {digest}, on disk {actual})"
        ]
    return []


def worktree_staleness_errors(manifest: dict[str, Any]) -> list[str]:
    root = manifest.get("repository_root")
    if not isinstance(root, str) or not root:
        return ["$--check-worktree STALE_SNAPSHOT: manifest has no repository_root to replay against"]
    try:
        recorded = manifest.get("documents")
        specs = (
            [(str(entry.get("locator", "")), entry.get("revision")) for entry in recorded if isinstance(entry, dict)]
            if isinstance(recorded, list)
            else []
        )
        replayed = replay_snapshot(
            argparse.Namespace(
                repo=root,
                base_sha=snapshot_field(manifest, "committed_range", "base_sha"),
                head_sha=snapshot_field(manifest, "committed_range", "head_sha"),
                target_type=manifest.get("target_type"),
                target_locator=manifest.get("target_locator"),
                preexisting_change=list(manifest.get("preexisting_changes", [])),
                document_specs=specs,
                # CW4-e：重放要逐字段复现磁盘上那份 manifest，策略也得带过去；
                # 缺该属性时 build_snapshot 会按默认 gated 落笔，prose 运行就会对不上。
                retry_policy=manifest.get("retry_policy", "gated"),
            )
        )
    except (SnapshotError, OSError, ValueError) as exc:
        return [f"$--check-worktree STALE_SNAPSHOT: unable to replay snapshot: {exc}"]
    stale = [
        f"$--check-worktree STALE_SNAPSHOT: {label} no longer matches the working tree "
        f"(manifest {replayed_field(manifest, label, path)!r}, replayed {replayed_field(replayed, label, path)!r})"
        for label, path in REPLAY_COMPARED_FIELDS
        if replayed_field(manifest, label, path) != replayed_field(replayed, label, path)
    ]
    artifact = manifest.get("diff_artifact")
    if isinstance(artifact, dict):
        stale += diff_artifact_staleness_errors(artifact)
    return stale


def input_artifact_staleness_errors(ledger: Any) -> list[str]:
    """--check-worktree 的另一侧：台账声明交出去的中间契约必须还在，且摘要没变。

    角色读的是磁盘上的契约文件；文件在派发后被改写或删除，"按 Context Contract
    审查"这句话就失去指称对象，步骤 18 的复核也无从谈起。
    """
    errors: list[str] = []
    for index, record in enumerate(ledger or []):
        if not isinstance(record, dict):
            continue
        inputs = record.get("inputs")
        if not isinstance(inputs, list):
            continue
        for item in inputs:
            if not isinstance(item, dict):
                continue
            locator, digest = item.get("path"), item.get("digest")
            if not isinstance(locator, str) or not isinstance(digest, str):
                continue
            try:
                current = hashlib.sha256(Path(locator).read_bytes()).hexdigest()
            except OSError as exc:
                errors.append(
                    f"$--check-worktree STALE_SNAPSHOT: dispatch[{index}] input {locator} "
                    f"is unreadable ({exc})"
                )
                continue
            if current != digest:
                errors.append(
                    f"$--check-worktree STALE_SNAPSHOT: dispatch[{index}] input {locator} "
                    f"digest changed after it was handed to the role "
                    f"(ledger {digest}, on disk {current})"
                )
    return errors


def split_error(error: str) -> tuple[str, str]:
    """把 `path: message` 形态的错误拆成机器可读两段。

    路径段是 JSON path / `$dispatch_ledger[i]` / `$--check-worktree STALE_SNAPSHOT`，
    本身不含 `": "`，所以按首个分隔符切一次即可。
    """
    path, separator, message = error.partition(": ")
    return (path, message) if separator else (error, "")


def errors_payload(errors: list[str], only: list[str]) -> dict[str, object]:
    """CW4-c：步骤 22 需要的机器可读出口（E3：以前只有 37 行给人读的错误）。"""
    matched = [error for error in errors if any(error.startswith(prefix) for prefix in only)] if only else errors
    return {
        "valid": not errors,
        # 带 --only 的运行将"某一段没有错"当成结论是错的，所以显式声明这不是门禁结果。
        "gate_result": not only,
        "filtered_by": list(only) if only else None,
        "matched_errors": len(matched),
        "total_errors": len(errors),
        "errors": [
            {"path": path, "message": message} for path, message in (split_error(item) for item in errors)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="JSON report path, or '-' to read stdin")
    parser.add_argument(
        "--snapshot-manifest",
        required=True,
        help="deterministic snapshot manifest produced by freeze_review_snapshot.py",
    )
    parser.add_argument(
        "--check-worktree",
        action="store_true",
        help="replay freeze_review_snapshot.build_snapshot against the manifest's repository_root "
        "and fail with STALE_SNAPSHOT when the working tree drifted from the frozen snapshot; also "
        "re-hashes manifest.diff_artifact and every ledger record's declared inputs when present "
        "(off by default)",
    )
    parser.add_argument(
        "--dispatch-ledger",
        default=None,
        help="path to dispatch-ledger.jsonl; when omitted, <snapshot-manifest dir>/dispatch-ledger.jsonl "
        "is picked up automatically if it exists (CW1-f coverage-owner and step-ordering checks)",
    )
    parser.add_argument(
        "--errors-json",
        type=Path,
        default=None,
        metavar="PATH",
        help="write the full error list as {valid, gate_result, errors:[{path, message}]} JSON to PATH "
        "so step 22 can repair named fields instead of re-reading prose output",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="JSON-PATH",
        help="print only errors whose path starts with this JSON path (e.g. '$.findings[2]'); repeatable. "
        "Filters the output, never the checks: a filtered run is not a gate result",
    )
    args = parser.parse_args()
    if not all(prefix.startswith("$") for prefix in args.only):
        parser.error("--only takes a JSON path starting with '$' (e.g. '$.coverage[3]' or '$.findings')")
    _reconfigure_streams()
    try:
        payload = load_payload(args.report)
        snapshot_manifest = load_object(args.snapshot_manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"INVALID: unable to read JSON: {exc}", file=sys.stderr)
        return 2

    ledger_path: Path | None = None
    if args.dispatch_ledger is not None:
        ledger_path = Path(args.dispatch_ledger)
    else:
        # 自动发现：台账与 manifest 同目录，忘带参数也不会静默跳过 CW1-f 核对；
        # 显式指定的路径不存在时报 rc=2，避免"文件名打错 = 门禁没跑"。
        candidate = Path(args.snapshot_manifest).resolve().parent / "dispatch-ledger.jsonl"
        ledger_path = candidate if candidate.exists() else None
    dispatch_ledger: list[dict[str, Any]] | None = None
    if ledger_path is not None:
        try:
            dispatch_ledger = read_dispatch_ledger(ledger_path)
        except (DispatchLedgerError, OSError) as exc:
            print(f"INVALID: unable to read dispatch ledger: {exc}", file=sys.stderr)
            return 2

    errors = ReportValidator(
        snapshot_manifest=snapshot_manifest, dispatch_ledger=dispatch_ledger
    ).validate(payload)
    if args.check_worktree:
        errors = (
            worktree_staleness_errors(snapshot_manifest)
            + input_artifact_staleness_errors(dispatch_ledger)
            + errors
        )
    if args.errors_json is not None:
        try:
            # 全量结果先落盘，再看 --only 决定给人读的输出：过滤态丢掉的错误也还在文件里。
            _write_text_atomic(
                args.errors_json,
                json.dumps(errors_payload(errors, args.only), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except OSError as exc:
            print(f"INVALID: unable to write --errors-json {args.errors_json}: {exc}", file=sys.stderr)
            return 2
    if args.only:
        matched = [error for error in errors if any(error.startswith(prefix) for prefix in args.only)]
        print(
            f"FILTERED: {len(matched)} of {len(errors)} error(s) match --only {' '.join(args.only)}. "
            "This is a repair-loop view, not a gate result — the verdict still needs one full "
            "validation without --only.",
            file=sys.stderr,
        )
        for error in matched:
            print(f"- {error}", file=sys.stderr)
        return 1 if matched else 0
    if errors:
        print(f"INVALID: {len(errors)} error(s)", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    checked = ["manifest", "worktree freshness"] if args.check_worktree else ["manifest"]
    if dispatch_ledger is not None:
        checked.append(f"dispatch ledger ({len(dispatch_ledger)} record(s))")
    print(f"VALID: code-review report structure and verdict invariants passed [{', '.join(checked)}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
