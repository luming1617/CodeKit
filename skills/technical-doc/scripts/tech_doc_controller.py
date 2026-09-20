#!/usr/bin/env python3
"""Deterministic stage, finding, round, and delivery gates for technical-doc.

The prose in ``SKILL.md`` still carries the judgment work, but every rule that can be
decided without a model is persisted under ``.codekit/technical-doc/<run-id>/`` and
enforced here: stage ordering, the ``review_round`` budget, Finding identity and
closure, artifact handoff by path plus digest, and the final delivery gate.  A stage
that has no recorded evidence is reported ``BLOCKED`` instead of being trusted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_REVIEW_ROUNDS = 5

ROLES = {
    "CONTEXT": "tech-context",
    "ARCHITECT": "tech-architect",
    "CRITIC": "tech-critic",
    "REFINER": "tech-refiner",
    "REVIEW": "tech-reviewer",
}
# ``DISPATCHED`` opens a timing window that the terminal status closes, so a run can be
# analysed afterwards without parsing host transcripts.  ``ABORTED`` exists because a
# dispatch that produced nothing still consumed time and must not stay invisible.
AGENT_STATUSES = ("DISPATCHED", "COMPLETED", "BLOCKED", "ABORTED")
PHASE_OF_ROLE = {
    ROLES["CONTEXT"]: "prep",
    ROLES["ARCHITECT"]: "write",
    ROLES["REFINER"]: "write",
    ROLES["CRITIC"]: "audit",
    ROLES["REVIEW"]: "audit",
}
ARTIFACT_KINDS = (
    "context-pack",
    "draft",
    "critic-findings",
    "refiner-map",
    "review-report",
)
SOURCE_KINDS = (
    "PRD",
    "PRD_CHANGELOG",
    "REPOSITORY_RULES",
    "SYSTEM_EVIDENCE",
    "SKILL_RESOURCE",
    "HUMAN_DECISION",
    "EXISTING_TDD",
)
READ_STATUSES = ("READ", "NOT_FOUND", "NOT_ACCESSIBLE")
# These kinds name a concrete artifact in the repository or in the Skill directory, so the
# declared read status is checkable against the filesystem.  ``HUMAN_DECISION`` and
# ``PRD_CHANGELOG`` may legitimately be pointed at by a non-path locator.
FILE_SOURCE_KINDS = (
    "PRD",
    "REPOSITORY_RULES",
    "SYSTEM_EVIDENCE",
    "SKILL_RESOURCE",
    "EXISTING_TDD",
)
# Git Bash rewrites absolute Windows paths in arguments (MSYS path conversion), which turns
# ``$SKILL_DIR/references/…`` into ``C:/Program Files/Git/references/…``.
MSYS_MANGLED = re.compile(r"Program Files[/\\]Git[/\\]", re.IGNORECASE)
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")
INPUT_STATUSES = ("PASS", "PARTIAL", "BLOCKED")
SEVERITIES = ("Blocker", "High", "Medium", "Low")
MUST_FIX_SEVERITIES = ("Blocker", "High")
FIX_STATUSES = ("FIXED", "PENDING_CONFIRMATION", "REJECTED")
REVIEW_STATUSES = ("APPROVE", "APPROVE_WITH_FOLLOW_UPS", "REQUEST_CHANGES", "BLOCKED")
PASSING_REVIEW_STATUSES = ("APPROVE", "APPROVE_WITH_FOLLOW_UPS")

STAGE_TRANSITIONS: dict[str, set[str]] = {
    "INPUT": {"CONTEXT"},
    "CONTEXT": {"ARCHITECT"},
    "ARCHITECT": {"CRITIC"},
    "CRITIC": {"REFINER"},
    "REFINER": {"REVIEW"},
    # A rejected review reopens the Refiner; an approved one moves to delivery.
    "REVIEW": {"REFINER", "DELIVERY"},
    "DELIVERY": {"CLOSED"},
    "CLOSED": set(),
}
FINDING_ID = {
    "CRITIC": re.compile(r"^CRITIC-(\d{3})$"),
    "REVIEWER": re.compile(r"^REVIEW-(\d{1,2})-(\d{3})$"),
}

PRD_FILE = "product-doc.md"
TDD_FILE = "technical-doc.md"
# The template ships these placeholders; a delivered TDD must not still contain them.
PLACEHOLDER = re.compile(
    r"(?:\bTBD\b|\bTODO\b|待定|待补充|后续补充|\bsimilar to\b|\brefer to\b|【填写】|【链接】|YYYY-MM-DD)",
    re.IGNORECASE,
)
CONFIRMATION = re.compile(r"\[需人工确认[:：].*?\]")
FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)\s*$")
ALLOWED_FENCE_LANGUAGES = {
    "", "mermaid", "sql", "json", "yaml", "yml", "java", "go", "python", "properties",
    "xml", "hocon", "text", "textfile", "plain", "bash", "sh", "sql-script", "dockerfile",
}
FORBIDDEN_FENCE_LANGUAGES = {"markdown", "md"}
# Guards against regressing to the removed "write the context pack into the repo
# agent configuration" behaviour, which let a stale run's design resurface as fact.
LEGACY_CONTEXT_MARKER = "technical-doc-context:"
REQUIRED_ANCHORS = (
    "变更记录",
    "影响范围",
    "关键技术决策",
    "里程碑规划",
    "审查闭环记录",
)
UPDATE_ONLY_ANCHORS = ("变更历史",)

# --- generated ledger blocks -----------------------------------------------------------
# The delivery tables in chapter 16 must match ``state.json`` verbatim, so the controller
# writes them instead of the model.  A hand-copied mirror cost ~47,000 characters of prose
# and produced 14 of the 53 findings in the reference run, all of them about the mirror.
LEDGER_SECTIONS = ("findings", "run-metadata", "history-index")
LEDGER_MARKERS = re.compile(
    r"^<!--\s*codekit:technical-doc:ledger:([a-z0-9-]+):(start|end)\s*-->\s*$"
)
LEDGER_START = "<!-- codekit:technical-doc:ledger:{section}:start -->"
LEDGER_END = "<!-- codekit:technical-doc:ledger:{section}:end -->"
LEDGER_ANCHOR = {
    "findings": "审查闭环记录",
    "run-metadata": "审查闭环记录",
    "history-index": "变更历史",
}
# Render order inside one anchor section.
LEDGER_ORDER = ("findings", "run-metadata", "history-index")
LEDGER_CELL_LIMIT = 300
# ``--prd-changes`` is the change ledger the history index mirrors: modified/added/deleted
# requirement lines, counted once by the orchestrator instead of prose-written per round.
PRD_CHANGES = re.compile(r"\d+/\d+/\d+")

# --- review rounds: severity-gated stop and incremental final review ---------------------
# Replayed against the reference run (26 Critic findings, then 12 → 7 → 5 → 3 per review round,
# with Blocker/High only in round 1): the two-quiet-round rule converts rounds 4 and 5 into
# scoped passes, and the scope those rounds would have been given measures ~52% of a full read
# (15 sections, ~114k tokens of a ~219k-token document -- a full pass no longer fits the window
# that run was working in).  Rounds 1-3 stay full because a full pass was still finding blockers
# up to round 1; the saving is real but it is two rounds, not the three an early estimate claimed.
REVIEW_STOP_POLICIES = ("severity", "fixed")
REVIEW_SCOPES = ("full", "incremental")
# Rounds of quiet review that license the smaller scope.  One quiet round is a coincidence;
# two is the trend the measure is about.
SEVERITY_STOP_STREAK = 2
SECTION_SNAPSHOT = "review-sections-r{round}.json"

# --- delivery scale and greenfield ------------------------------------------------------
# The template already labels chapters 条件必填 and "M/L 必填", but nothing decided which
# label applied, so a greenfield prototype-only run wrote the full 可观测设计 and L-only
# 部署拓扑 at full length and every later round read them back.  The probe decides the label
# from the frozen PRD and the repository, not from a self-report.
SCALE_ENFORCEMENTS = ("enforce", "record-only")
SCALES = ("S", "M", "L")
SCALE_SIGNALS: dict[str, re.Pattern[str]] = {
    "data": re.compile(r"新建表|建表|数据表|字段|索引|DDL|迁移|落库|数据库|schema", re.IGNORECASE),
    "api": re.compile(r"接口|API|HTTP|REST|端点|RPC|gRPC", re.IGNORECASE),
    "message": re.compile(r"消息|MQ|Kafka|RocketMQ|RabbitMQ|topic|生产者|消费者|事件驱动", re.IGNORECASE),
    "scheduled": re.compile(r"定时|轮询|cron|调度|延迟任务|补偿任务", re.IGNORECASE),
    "cross_service": re.compile(r"跨服务|上下游|第三方|外部系统|回调|服务间", re.IGNORECASE),
    "config": re.compile(r"配置项|开关|灰度|阈值|参数配置|环境变量", re.IGNORECASE),
}
# Scale follows the template's own 裁剪规则 rows: no structural change class at all is S,
# one to three is M ("接口/数据库/缓存/MQ 任一变化"), four or more is L.
SCALE_L_MIN_SIGNALS = 4
# Chapters that only exist because a signal says they do.
SIGNAL_CHAPTERS = {
    "data": "数据库设计",
    "api": "接口与契约设计",
    "message": "消息设计",
    "scheduled": "定时任务设计",
    "config": "环境配置设计",
}
# The template's S row already demands 基础监控与告警 (merged into 可观测设计); 部署拓扑 only joins at L.
ALWAYS_CHAPTERS = ("可观测设计",)
DEPLOYMENT_CHAPTER = "部署拓扑"
# The template's escape hatch ("填'不涉及'及原因", the 条件必填 labels it carries) is legal wherever the
# template itself leaves the chapter conditional: a signal-derived chapter may say 不涉及 with an
# argument at M/L.  The 最低要求 rows of the 裁剪规则 table name no such escape for the observability
# chapter at M/L or for the deployment topology at L, so those open with 不涉及 or a stub are reported --
# this is the "变快不等于变薄" guard the wave's acceptance criteria demand.
NOT_APPLICABLE = "不涉及"
NOT_APPLICABLE_WINDOW = 300
MIN_SUBSTANTIVE_CHARS = 400
BACKEND_CODE_SUFFIXES = {
    ".py", ".java", ".go", ".kt", ".kts", ".rb", ".php", ".cs", ".scala", ".c", ".cc", ".cpp",
    ".hpp", ".rs",
}
# A bounded listing: the probe must never become the slow thing it is measuring.
CODE_SCAN_FILE_LIMIT = 20_000


class ControllerError(ValueError):
    """A user-correctable workflow or contract error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_repo_root(path: str | Path) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_dir():
        raise ControllerError(f"repository root is not a directory: {value}")
    return value


def _inside(root: Path, path: str | Path) -> tuple[Path, str]:
    raw = Path(path).expanduser()
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ControllerError(f"path escapes repository root: {candidate}") from exc
    if not relative.parts:
        raise ControllerError("path must be below repository root")
    return candidate, relative.as_posix()


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        tail = " | ".join((exc.stderr or "").strip().splitlines()[-3:]) or "(no stderr)"
        raise ControllerError(f"git {' '.join(args)} failed (exit {exc.returncode}): {tail}") from exc
    return result.stdout


def _read_document(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ControllerError(f"{label} is missing or not a regular file: {path}")
    return path.read_text(encoding="utf-8")


def _assert_substantive(text: str, label: str, path: Path) -> None:
    """Reject empty files, template skeletons, and placeholder-only inputs."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    body = [line for line in lines if line not in {"---"} and not line.startswith("```")]
    if len("".join(body)) < 40:
        raise ControllerError(f"{label} has no substantive content: {path}")
    placeholder = [line for line in body if PLACEHOLDER.search(line)]
    if len(placeholder) / max(len(body), 1) > 0.3:
        raise ControllerError(
            f"{label} is still dominated by template placeholders "
            f"({len(placeholder)}/{len(body)} lines): {path}"
        )


def _run_dir(root: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ControllerError("run-id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return root / ".codekit" / "technical-doc" / run_id


def load_state(path: str | Path) -> tuple[Path, dict[str, Any]]:
    state_path = Path(path).expanduser().resolve()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot read state file: {state_path}: {exc}") from exc
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ControllerError(f"unsupported state schema: {state.get('schema_version')}")
    return state_path, state


def _save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _atomic_json(path, state)


def _require_stage(state: dict[str, Any], stage: str, action: str) -> None:
    if state["stage"] != stage:
        raise ControllerError(f"{action} requires stage {stage}, current stage is {state['stage']}")


def _artifacts(state: dict[str, Any], kind: str, round_: int | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in state["artifacts"]
        if item["kind"] == kind and (round_ is None or item["round"] == round_)
    ]


def _completed_agents(state: dict[str, Any], role: str, round_: int | None = None) -> list[dict[str, Any]]:
    return [
        item
        for item in state["agents"]
        if item["role"] == role
        and item["status"] == "COMPLETED"
        and (round_ is None or item["round"] == round_)
    ]


def init_run(
    root: str | Path,
    feature_directory: str | Path,
    run_id: str,
    context_window: int | None = None,
    review_stop_policy: str = REVIEW_STOP_POLICIES[0],
    scale_enforcement: str = SCALE_ENFORCEMENTS[0],
) -> Path:
    """Freeze the inputs for one TDD run and open the evidence directory."""
    repo = _require_repo_root(root)
    if context_window is not None and context_window < 1_000:
        raise ControllerError(
            f"--context-window must be the host's real window in tokens, got {context_window}"
        )
    if review_stop_policy not in REVIEW_STOP_POLICIES:
        raise ControllerError(
            f"--review-stop-policy must be one of {', '.join(REVIEW_STOP_POLICIES)}"
        )
    if scale_enforcement not in SCALE_ENFORCEMENTS:
        raise ControllerError(f"--scale-enforcement must be one of {', '.join(SCALE_ENFORCEMENTS)}")
    feature, feature_rel = _inside(repo, feature_directory)
    if not feature.is_dir():
        raise ControllerError(f"feature-directory is not a directory: {feature}")

    prd = feature / PRD_FILE
    prd_text = _read_document(prd, "PRD")
    _assert_substantive(prd_text, "PRD", prd)

    existing = feature / TDD_FILE
    mode = "NEW"
    materials: dict[str, dict[str, Any]] = {
        "product_requirements": {
            "locator": str(prd),
            "content_digest": _text_digest(prd_text),
            "size": prd.stat().st_size,
        }
    }
    if existing.is_file():
        mode = "UPDATE"
        tdd_text = _read_document(existing, "TDD")
        materials["previous_technical_design"] = {
            "locator": str(existing),
            "content_digest": _text_digest(tdd_text),
            "size": existing.stat().st_size,
        }

    target = _run_dir(repo, run_id)
    if target.exists():
        raise ControllerError(f"run already exists; resume it instead of overwriting: {target}")
    (target / "artifacts").mkdir(parents=True)
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": _now(),
        "updated_at": _now(),
        "stage": "INPUT",
        "mode": mode,
        "degraded": False,
        "repository": {
            "root": str(repo),
            "base_sha": _git(repo, "rev-parse", "HEAD").strip(),
            "branch": _git(repo, "branch", "--show-current").strip() or "(detached)",
        },
        "feature_directory": {"absolute": str(feature), "relative": feature_rel},
        "output_document": {"absolute": str(existing), "relative": f"{feature_rel}/{TDD_FILE}"},
        "materials": materials,
        "input_status": None,
        "sources": [],
        "agents": [],
        "artifacts": [],
        "findings": [],
        "fixes": [],
        "reviews": [],
        "review_round": 0,
        "max_review_rounds": MAX_REVIEW_ROUNDS,
        "context_window": context_window or DEFAULT_CONTEXT_WINDOW,
        "review_stop_policy": review_stop_policy,
        "scale_enforcement": scale_enforcement,
        "scale": None,
        "pending_review": None,
        "degradations": [],
        "history": [],
        "gates": [],
        "prd_changes": None,
        "ledger": None,
    }
    _atomic_json(target / "state.json", state)
    return target / "state.json"


def _verify_locator(root: Path, source_id: str, kind: str, locator: str, status: str) -> dict[str, Any]:
    """Make the declared read status a measured fact instead of a self-report.

    A locator recorded as ``READ`` that does not exist is the signature of a shell-rewritten
    path (Git Bash turns ``$SKILL_DIR/references`` into ``C:/Program Files/Git/...``), and a
    ``NOT_FOUND`` that does exist is a source nobody actually looked for.  Both used to enter
    the run undetected and then propagate into every dispatch as a claimed input.
    """
    if kind not in FILE_SOURCE_KINDS:
        return {}
    raw = locator.strip()
    candidate = Path(raw).expanduser()
    candidate = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=False)
    exists = candidate.exists()
    if status == "READ":
        if not exists:
            hint = (
                "; this looks like a Git Bash (MSYS) rewritten path, pass the native absolute "
                "path resolved without the shell"
                if MSYS_MANGLED.search(candidate.as_posix())
                else ""
            )
            raise ControllerError(f"source '{source_id}' claims READ but nothing exists at {candidate}{hint}")
        if candidate.is_dir():
            return {"resolved": str(candidate), "resolved_kind": "directory"}
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            raise ControllerError(
                f"source '{source_id}' claims READ but {candidate} cannot be opened: {exc}"
            ) from exc
        return {
            "resolved": str(candidate),
            "resolved_kind": "file",
            "size": len(payload),
            "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    if status == "NOT_FOUND" and exists:
        raise ControllerError(
            f"source '{source_id}' claims NOT_FOUND but {candidate} exists; read it and record READ"
            ", or NOT_ACCESSIBLE if it cannot be opened"
        )
    if status == "NOT_ACCESSIBLE" and not exists:
        raise ControllerError(
            f"source '{source_id}' claims NOT_ACCESSIBLE but nothing exists at {candidate}; that is NOT_FOUND"
        )
    # What is left: NOT_FOUND with nothing there (truly absent), or NOT_ACCESSIBLE with
    # something there (present but unopenable) -- labelling the second one "absent" would
    # state the opposite of what the caller just verified.
    kind = "absent" if not exists else "inaccessible"
    return {"resolved": str(candidate), "resolved_kind": kind}


def record_source(
    state_path: str | Path,
    source_id: str,
    kind: str,
    locator: str,
    status: str,
    revision: str,
    summary: str,
    confidence: str,
) -> dict[str, Any]:
    """Register one frozen input item so ``NOT_FOUND`` is a fact, not a guess."""
    path, state = load_state(state_path)
    if kind not in SOURCE_KINDS:
        raise ControllerError(f"unsupported source kind: {kind} (expected {', '.join(SOURCE_KINDS)})")
    if status not in READ_STATUSES:
        raise ControllerError(f"read status must be one of {', '.join(READ_STATUSES)}")
    if confidence not in CONFIDENCES:
        raise ControllerError(f"confidence must be one of {', '.join(CONFIDENCES)}")
    for label, value in (("source-id", source_id), ("locator", locator), ("summary", summary)):
        if not value.strip():
            raise ControllerError(f"{label} is required")
    if any(item["source_id"] == source_id for item in state["sources"]):
        raise ControllerError(f"duplicate source-id: {source_id}")
    evidence = _verify_locator(
        _require_repo_root(state["repository"]["root"]), source_id, kind, locator, status
    )
    entry = {
        "source_id": source_id,
        "kind": kind,
        "locator": locator,
        "read_status": status,
        "revision": revision.strip() or "unversioned",
        "summary": summary,
        "confidence": confidence,
        **evidence,
        "recorded_at": _now(),
    }
    state["sources"].append(entry)
    _save(path, state)
    return entry


def _scale_signals(prd_text: str) -> dict[str, list[str]]:
    """Which change classes the frozen PRD asks for, with the matched text as evidence."""
    found: dict[str, list[str]] = {}
    for name, pattern in SCALE_SIGNALS.items():
        hits: list[str] = []
        for match in pattern.finditer(prd_text):
            if match.group(0) not in hits:
                hits.append(match.group(0))
        if hits:
            found[name] = hits[:3]
    return found


def _backend_code_files(repo: Path) -> tuple[int, str]:
    """Tracked or untracked backend source files, from a bounded listing."""
    try:
        listed = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard")
    except ControllerError as exc:
        return -1, f"git ls-files unavailable: {exc}"
    files = [line for line in listed.splitlines() if line.strip()][:CODE_SCAN_FILE_LIMIT]
    count = sum(1 for line in files if Path(line).suffix.lower() in BACKEND_CODE_SUFFIXES)
    note = "complete" if len(files) < CODE_SCAN_FILE_LIMIT else f"truncated at {CODE_SCAN_FILE_LIMIT} entries"
    return count, note


def _probe_scale(state: dict[str, Any]) -> dict[str, Any]:
    """Decide S/M/L and greenfield from measured inputs, once, at the input gate.

    Everything downstream keys off this record: which chapters ``gate`` demands, whether the
    Context Loader hunts for existing implementations at all, and what the run-metadata ledger
    tells the reader.  It is a measurement, not a declaration -- the only inputs are the frozen
    PRD bytes, the sources already verified against the filesystem, and ``git ls-files``.
    """
    prd = Path(state["materials"]["product_requirements"]["locator"])
    prd_text = _read_document(prd, "PRD")
    frozen = state["materials"]["product_requirements"].get("content_digest") or ""
    live_digest = _text_digest(prd_text)
    if frozen and live_digest != frozen:
        raise ControllerError(
            f"the PRD changed after init froze it (init digest {frozen[:18]}…, "
            f"now {live_digest[:18]}…); every later stage would design against bytes the "
            "run never froze — start a new run against the current PRD"
        )
    signals = _scale_signals(prd_text)
    count = len(signals)
    if count == 0:
        scale = "S"
    elif count < SCALE_L_MIN_SIGNALS:
        scale = "M"
    else:
        scale = "L"
    code_files, scan = _backend_code_files(_require_repo_root(state["repository"]["root"]))
    evidence_read = [
        item["source_id"]
        for item in state["sources"]
        if item["kind"] in {"SYSTEM_EVIDENCE", "EXISTING_TDD"} and item["read_status"] == "READ"
    ]
    greenfield = code_files == 0 and not evidence_read
    return {
        "scale": scale,
        "signals": signals,
        "signal_count": count,
        "greenfield": greenfield,
        "backend_code_files": code_files,
        "code_scan": scan,
        "system_evidence_read": evidence_read,
        "existing_tdd": state["mode"] == "UPDATE",
        "prd_chars": len(prd_text),
        "enforcement": state.get("scale_enforcement", SCALE_ENFORCEMENTS[0]),
        "detected_at": _now(),
    }


def finalize_inputs(
    state_path: str | Path,
    status: str,
    reason: str,
    prd_changes: str | None = None,
) -> dict[str, Any]:
    """Set ``input_status`` and refuse to let a missing PRD be graded as usable."""
    path, state = load_state(state_path)
    if status not in INPUT_STATUSES:
        raise ControllerError(f"input_status must be one of {', '.join(INPUT_STATUSES)}")
    if not reason.strip():
        raise ControllerError("--reason is required so the gap is handed to later stages")
    changes = prd_changes.strip() if prd_changes else ""
    if changes and not PRD_CHANGES.fullmatch(changes):
        raise ControllerError(
            f"--prd-changes must be '修改数/新增数/删除数' like '3/1/0' (got {changes!r})"
        )
    if changes and state["mode"] != "UPDATE":
        raise ControllerError("--prd-changes only applies to an UPDATE run")
    if state["mode"] == "UPDATE" and status != "BLOCKED" and not changes:
        raise ControllerError(
            "an UPDATE run must pass --prd-changes; the history index states which requirement "
            "lines were modified, added, or deleted"
        )
    prd_sources = [item for item in state["sources"] if item["kind"] == "PRD"]
    if not prd_sources:
        raise ControllerError("a PRD source must be recorded before inputs are finalized")
    readable = [item for item in prd_sources if item["read_status"] == "READ"]
    if not readable and status != "BLOCKED":
        raise ControllerError("the PRD was not read; input_status must be BLOCKED")
    unread = [item["source_id"] for item in state["sources"] if item["read_status"] != "READ"]
    if unread and status == "PASS":
        raise ControllerError(f"input_status PASS is impossible while sources are unread: {unread}")
    state["input_status"] = status
    state["input_status_reason"] = reason
    state["input_gaps"] = unread
    state["prd_changes"] = changes or None
    probe = _probe_scale(state)
    state["scale"] = probe
    _save(path, state)
    return {
        "input_status": status,
        "gaps": unread,
        "reason": reason,
        "prd_changes": changes or None,
        "scale": {
            "scale": probe["scale"],
            "greenfield": probe["greenfield"],
            "signals": sorted(probe["signals"]),
            "enforcement": probe["enforcement"],
        },
    }


def _reviewer_findings(state: dict[str, Any], round_: int) -> list[dict[str, Any]]:
    return [
        item for item in state["findings"]
        if item["source"] == "REVIEWER" and item["round"] == round_
    ]


def _open_must_fix(state: dict[str, Any]) -> list[str]:
    return [
        item["id"] for item in state["findings"]
        if item["must_fix"] and not _closed(state, item["id"])
    ]


def _severity_stop(state: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a further *full* review can still be expected to pay for itself.

    The condition is the measured one: two finished rounds that surfaced no Blocker/High and
    no must_fix left open.  Anything past that point has been shown to produce Medium/Low and
    bookkeeping findings at the same per-finding cost as a blocker round, so the round is
    scoped to what moved instead.  This never closes a round early: an open ``must_fix`` still
    forces more review, and ``gate`` still refuses delivery on a non-passing verdict.
    """
    policy = state.get("review_stop_policy") or REVIEW_STOP_POLICIES[0]
    verdicts = state["reviews"]
    window = verdicts[-SEVERITY_STOP_STREAK:]
    quiet = len(window) == SEVERITY_STOP_STREAK and all(
        not [item for item in _reviewer_findings(state, verdict["round"])
             if item["severity"] in MUST_FIX_SEVERITIES]
        for verdict in window
    )
    open_fix = _open_must_fix(state)
    return {
        "policy": policy,
        "forced": policy == "severity" and quiet and not open_fix,
        "quiet": quiet,
        "quiet_rounds": [verdict["round"] for verdict in window] if quiet else [],
        "open_must_fix": open_fix,
    }


def _snapshot_path(state_path: Path, round_: int) -> Path:
    return state_path.parent / SECTION_SNAPSHOT.format(round=round_)


def _read_snapshot(state_path: Path, verdict: dict[str, Any]) -> dict[str, str] | None:
    stored = verdict.get("section_snapshot")
    if not stored:
        return None
    target = (state_path.parent / stored).resolve(strict=False)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    digests = payload.get("digests")
    return dict(digests) if isinstance(digests, dict) else None


def _write_snapshot(state_path: Path, round_: int, index: dict[str, Any], digest: str) -> str:
    payload = {
        "round": round_,
        "tdd_digest": digest,
        "generated_at": _now(),
        "digests": {item["key"]: item["digest"] for item in index["sections"]},
    }
    target = _snapshot_path(state_path, round_)
    _atomic_json(target, payload)
    return target.name


def _derive_incremental_scope(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Which sections a scoped review must re-read: what moved, plus what the findings cite.

    The finding locations matter as much as the edits.  A ``REJECTED`` or
    ``PENDING_CONFIRMATION`` disposition changes no bytes, yet the next reviewer has to look at
    that section again to judge whether the reasoning holds -- scoping purely to the diff would
    let a contested decision slip through unreviewed.
    """
    previous = state["reviews"][-1]
    baseline = _read_snapshot(path, previous)
    if baseline is None:
        raise ControllerError(
            f"round {previous['round']} recorded no section snapshot, so an incremental scope "
            "cannot be derived; review this round in full"
        )
    index = _fresh_index(path, state, _resolve_tdd(state, None))
    by_key = {item["key"]: item for item in index["sections"]}
    numbers = index["numbers"]
    titles = [(item["title"], item["key"]) for item in index["sections"]]
    moved = [item["key"] for item in index["sections"] if baseline.get(item["key"]) != item["digest"]]
    cited: list[str] = []
    for finding in _reviewer_findings(state, previous["round"]):
        key, _ = _resolve_location(numbers, titles, finding["location"])
        if key and key not in cited:
            cited.append(key)
    views: list[dict[str, Any]] = []
    for key in moved:
        views.append(_section_view(by_key[key], f"changed since round {previous['round']}"))
    for key in cited:
        if key in by_key and key not in moved:
            views.append(_section_view(by_key[key], f"raised in round {previous['round']}"))
    tokens = sum(item["tokens_estimated"] for item in views)
    window = int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW)
    budget = int(window * BUDGET_SHARE)
    return {
        "baseline_round": previous["round"],
        "sections": [item["key"] for item in views],
        "ranges": [{"key": item["key"], "start": item["start"], "end": item["end"]} for item in views],
        "moved": moved,
        "cited": [item["key"] for item in views if item["key"] not in moved],
        "tokens_estimated": tokens,
        "budget_tokens": budget,
        "over_budget": tokens > budget,
    }


def _incremental_coverage(path: Path, state: dict[str, Any], tdd: Path) -> tuple[bool, Any]:
    """Prove the scoped rounds add up to full coverage since the last pass that saw everything.

    A section deleted outright has no range left to read, so it is credited through the parent
    that still contains its heading's neighbours: deleting ``7.2.1`` changes ``7.2``'s digest and
    ``7.2`` must then appear in some later scope.
    """
    reviews = state["reviews"]
    if not any(item.get("scope") == "incremental" for item in reviews):
        return True, "no incremental rounds"
    last_full = next((item for item in reversed(reviews) if item.get("scope") == "full"), None)
    if last_full is None:
        return False, "an incremental round was reviewed with no full baseline in this run"
    baseline = _read_snapshot(path, last_full)
    if baseline is None:
        return False, f"round {last_full['round']}' section snapshot is missing; cannot prove coverage"
    later = [item for item in reviews if item["round"] > last_full["round"]]
    covered = {key for item in later for key in item.get("sections") or []}
    index = _fresh_index(path, state, tdd)
    current = {item["key"]: item["digest"] for item in index["sections"]}
    changed = {key for key, digest in current.items() if baseline.get(key) != digest}
    removed = {key for key in baseline if key not in current}
    ancestors: set[str] = set()
    orphans: set[str] = set()
    for key in removed:
        head = key
        parent = ""
        while "." in head:
            head = head.rsplit(".", 1)[0]
            if head in current:
                parent = head
                break
        if parent:
            ancestors.add(parent)
        else:
            # A section with no surviving ancestor has nothing left whose digest can
            # witness its deletion -- a whole top-level chapter is the case that used to
            # pass silently.  It must itself have appeared in some later scope.
            orphans.add(key)
    missing = sorted((changed | ancestors | orphans) - covered)
    if missing:
        return False, (
            f"round {last_full['round']} was the last full pass; sections changed or were "
            f"deleted since it and never reviewed: {', '.join(missing[:12])}"
        )
    return True, (
        f"{len(changed)} changed section(s) since round {last_full['round']} covered by "
        f"{len(later)} incremental round(s)"
    )


def _scale_requirements(state: dict[str, Any]) -> dict[str, str]:
    """Chapter -> why this run must have it, straight from the template's 裁剪规则 rows."""
    probe = state.get("scale") or {}
    scale = probe.get("scale") or "M"
    wanted = {chapter: "S 级最低要求" for chapter in ALWAYS_CHAPTERS}
    if scale == "L":
        wanted[DEPLOYMENT_CHAPTER] = f"{scale} 级最低要求"
    for signal in probe.get("signals") or {}:
        chapter = SIGNAL_CHAPTERS.get(signal)
        if chapter:
            wanted[chapter] = f"PRD 命中 {signal} 变更"
    return wanted


def _scale_chapter_check(
    path: Path, state: dict[str, Any], tdd: Path, doc_text: str
) -> tuple[bool, Any]:
    """Give the template's scale labels a decision procedure instead of a suggestion.

    Presence is the cheap half.  The half that matters for this wave is the floor: a run may
    not get faster by getting thinner, so at M/L a required chapter that opens with 不涉及 or
    holds less than a paragraph of design is reported, exactly as the S row of the template
    allows it to at S.
    """
    if (state.get("scale_enforcement") or SCALE_ENFORCEMENTS[0]) != "enforce":
        return True, f"scale={((state.get('scale') or {}).get('scale')) or '未判定'}; 探针仅记录"
    if not state.get("scale"):
        return True, "scale 探针未运行（finalize-inputs 之前的门禁）"
    try:
        index = _fresh_index(path, state, tdd)
    except ControllerError as exc:
        # Without an index there is no range to measure, so the check reports what it cannot
        # prove instead of passing quietly over an unparsable document.
        return False, f"文档索引不可用，无法按规模核对章节：{exc}"
    lines = doc_text.splitlines()
    scale = (state["scale"] or {}).get("scale") or "M"
    wanted = _scale_requirements(state)
    conditional = set(SIGNAL_CHAPTERS.values())
    missing: list[str] = []
    thin: list[str] = []
    for chapter, why in sorted(wanted.items()):
        section = next((item for item in index["sections"] if item["title"].startswith(chapter)), None)
        if section is None:
            missing.append(f"{chapter}（{why}）")
            continue
        if scale == "S":
            continue
        body = "\n".join(lines[section["start"]: section["end"]]).strip()
        if chapter in conditional:
            # 条件必填 chapters keep the template's escape at every scale: a reasoned 不涉及 is a
            # decision, not padding.  What the escape never buys is silence, so one that stops
            # short of an argument is still reported.
            if NOT_APPLICABLE not in body and len(body) < MIN_SUBSTANTIVE_CHARS:
                thin.append(f"{chapter} 正文仅 {len(body)} 字符，低于 {scale} 级 {MIN_SUBSTANTIVE_CHARS} 字符下限")
            elif NOT_APPLICABLE in body and len(body) < MIN_SUBSTANTIVE_CHARS:
                thin.append(f"{chapter} 写了“{NOT_APPLICABLE}”却没给原因，低于 {MIN_SUBSTANTIVE_CHARS} 字符")
            continue
        if NOT_APPLICABLE in body[:NOT_APPLICABLE_WINDOW]:
            thin.append(f"{chapter} 以“{NOT_APPLICABLE}”收缩，但 {scale} 级要求实质设计")
        elif len(body) < MIN_SUBSTANTIVE_CHARS:
            thin.append(f"{chapter} 正文仅 {len(body)} 字符，低于 {scale} 级 {MIN_SUBSTANTIVE_CHARS} 字符下限")
    if missing or thin:
        return False, "; ".join(missing + thin)
    return True, f"scale={scale}，{len(wanted)} 个规模相关章节齐备且有实质内容"


def start_stage(state_path: str | Path, target_stage: str, review_scope: str | None = None) -> dict[str, Any]:
    """Advance the workflow, enforcing every prerequisite the prose used to claim."""
    path, state = load_state(state_path)
    target = target_stage.upper()
    current = state["stage"]
    if target not in STAGE_TRANSITIONS:
        raise ControllerError(f"unknown stage: {target_stage}")
    if target not in STAGE_TRANSITIONS[current]:
        allowed = ", ".join(sorted(STAGE_TRANSITIONS[current])) or "(terminal)"
        raise ControllerError(f"illegal transition {current} -> {target}; allowed: {allowed}")
    asked = (review_scope or "").strip().lower()
    if asked and asked not in REVIEW_SCOPES:
        raise ControllerError(f"--review-scope must be one of {', '.join(REVIEW_SCOPES)}")
    if asked and target != "REVIEW":
        raise ControllerError(f"--review-scope only applies to a REVIEW stage, not {target}")

    round_ = state["review_round"]
    if target == "CONTEXT":
        if state["input_status"] not in {"PASS", "PARTIAL"}:
            raise ControllerError(
                f"input_status is {state['input_status']!r}; Context Loader may not start"
            )
    elif target == "ARCHITECT":
        _require_evidence(state, "CONTEXT", "context-pack")
    elif target == "CRITIC":
        _require_evidence(state, "ARCHITECT", "draft")
    elif target == "REFINER":
        if current == "CRITIC":
            _require_evidence(state, "CRITIC", "critic-findings")
        else:
            _require_open_reviewer_findings(state)
    elif target == "REVIEW":
        _require_evidence(state, "REFINER", "refiner-map", artifact_round=round_)
        if round_ + 1 > state["max_review_rounds"]:
            raise ControllerError(
                f"review_round would exceed the cap of {state['max_review_rounds']}; "
                "deliver a blocking report instead of opening another round"
            )
        stop = _severity_stop(state)
        if asked == "incremental" and not stop["forced"]:
            raise ControllerError(
                f"round {round_ + 1} may not be scoped yet: the last {SEVERITY_STOP_STREAK} review "
                "rounds have not both come back without Blocker/High, so a full pass is still what "
                "pays (init --review-stop-policy fixed restores the unconditional cap)"
            )
        if asked == "full" and stop["forced"]:
            raise ControllerError(
                f"rounds {'、'.join(str(item) for item in stop['quiet_rounds'])} raised no Blocker/High "
                "and no must_fix is open, so a full pass now re-reads a document the earlier rounds "
                "show to be mostly unchanged; omit --review-scope and let the controller pick the "
                "smallest scope it can prove (it still falls back to a full round when no "
                "incremental scope can be derived), or deliver on the passing verdict "
                "(init --review-stop-policy fixed restores the unconditional cap)"
            )
        plan: dict[str, Any] | None = None
        if stop["forced"] and state["reviews"]:
            try:
                derived = _derive_incremental_scope(path, state)
            except ControllerError:
                # Deriving less than the honest scope is the one failure mode worth paying a full
                # round for, so a missing snapshot degrades upward instead of downward.
                derived = None
            if derived and derived["sections"]:
                plan = derived
        scope = "incremental" if plan else "full"
        state["review_round"] = round_ + 1
        state["pending_review"] = {
            "round": state["review_round"],
            "scope": scope,
            "sections": (plan or {}).get("sections", []),
            "ranges": (plan or {}).get("ranges", []),
            "baseline_round": (plan or {}).get("baseline_round"),
            "tokens_estimated": (plan or {}).get("tokens_estimated"),
            "budget_tokens": (plan or {}).get("budget_tokens"),
            "over_budget": (plan or {}).get("over_budget", False),
            "severity_stop": stop,
            "at": _now(),
        }
    elif target == "DELIVERY":
        _require_approved_review(state)
        _require_all_findings_tracked(state)
    elif target == "CLOSED":
        _require_gate_pass(state)

    state["stage"] = target
    state.setdefault("stage_history", []).append(
        {"stage": target, "review_round": state["review_round"], "at": _now()}
    )
    _save(path, state)
    result: dict[str, Any] = {"stage": target, "review_round": state["review_round"], "mode": state["mode"]}
    if target == "REVIEW":
        pending = state["pending_review"]
        result["review_scope"] = pending["scope"]
        if pending["scope"] == "incremental":
            result["sections"] = pending["sections"]
            result["tokens_estimated"] = pending["tokens_estimated"]
            result["over_budget"] = pending["over_budget"]
            result["hint"] = "locate each section for its line range, then dispatch one reviewer over that set"
    return result


def _require_evidence(
    state: dict[str, Any], stage: str, kind: str, *, artifact_round: int | None = None, agent_round: int = 0
) -> None:
    """A delegated stage needs both a COMPLETED agent and a persisted artifact.

    Only ``tech-reviewer`` instances are round-scoped; every other role is recorded at
    round 0, so the agent and artifact rounds must be resolved separately.
    """
    role = ROLES[stage]
    if not _completed_agents(state, role, agent_round):
        raise ControllerError(
            f"no COMPLETED {role} agent recorded for stage {stage}; the orchestrator may not "
            "self-certify a delegated stage"
        )
    if not _artifacts(state, kind, artifact_round):
        raise ControllerError(f"stage {stage} produced no persisted '{kind}' artifact")


def _require_open_reviewer_findings(state: dict[str, Any]) -> None:
    round_ = state["review_round"]
    pending = [item["id"] for item in state["findings"] if item["round"] == round_ and not _closed(state, item["id"])]
    if not pending:
        raise ControllerError(
            f"round {round_} review did not raise findings; deliver the current TDD "
            "instead of opening another Refiner round"
        )


def _require_approved_review(state: dict[str, Any]) -> None:
    if not state["reviews"]:
        raise ControllerError("no independent review has been recorded")
    last = state["reviews"][-1]
    if last["round"] != state["review_round"]:
        raise ControllerError(
            f"the newest verdict belongs to round {last['round']} while round "
            f"{state['review_round']} is open; a stale round may not carry the delivery, "
            "so revise in a Refiner round and review with a fresh reviewer"
        )
    if last["status"] not in PASSING_REVIEW_STATUSES:
        raise ControllerError(
            f"review round {last['round']} returned {last['status']}; only "
            f"{', '.join(PASSING_REVIEW_STATUSES)} may reach delivery"
        )


def _require_all_findings_tracked(state: dict[str, Any]) -> None:
    """Fail while the run is still in a stage that can turn back, not after DELIVERY.

    The same condition is the gate's ``every-finding-has-a-disposition``; discovering it
    only at the gate left no legal transition out of DELIVERY, so one late non-blocking
    finding could strand the whole run.
    """
    untracked = [
        item["id"]
        for item in state["findings"]
        if not any(fix["finding_id"] == item["id"] for fix in state["fixes"])
    ]
    if untracked:
        raise ControllerError(
            f"findings without a disposition: {', '.join(untracked[:12])}; delivery may not "
            "open on an unfinished ledger — from this stage, record-fix accepts "
            "PENDING_CONFIRMATION/REJECTED for this round's non-blocking findings, and "
            "must_fix or older findings need a Refiner round first"
        )


def _require_gate_pass(state: dict[str, Any]) -> None:
    if not state["gates"] or state["gates"][-1]["status"] not in {"PASS", "PARTIAL_DEGRADED"}:
        raise ControllerError("the delivery gate has not passed; run 'gate' before closing")


def _parse_at(value: Any) -> datetime | None:
    """``_now()`` output back to a stamp, or ``None``: comparing them as strings is wrong when
    a timestamp happens to carry zero microseconds and drops the fraction."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(started_at: str, finished_at: str) -> float | None:
    start = _parse_at(started_at)
    end = _parse_at(finished_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _reviewer_slot_taken(state: dict[str, Any], role: str, round_: int) -> bool:
    """One reviewer instance per round, counted from the dispatch, not only the verdict."""
    return any(
        item["role"] == role
        and item["round"] == round_
        and item["status"] in {"COMPLETED", "DISPATCHED"}
        for item in state["agents"]
    )


def record_agent(
    state_path: str | Path,
    role: str,
    name: str,
    session_id: str,
    input_digest: str,
    status: str,
    scope: str = "",
) -> dict[str, Any]:
    """Log one delegated subagent call, keyed by round so the cap is auditable.

    ``--status DISPATCHED`` opens a window the terminal status closes with the same session
    id, which is how per-role wall time is measured without parsing host transcripts.
    ``ABORTED`` keeps a dispatch that consumed time but produced nothing visible in the run.
    ``--scope`` declares the sections actually injected into the prompt; the read budget is
    then measured from the index instead of accepted as a claim.
    """
    path, state = load_state(state_path)
    if role not in ROLES.values():
        raise ControllerError(f"unsupported role: {role} (expected {', '.join(sorted(ROLES.values()))})")
    if status not in AGENT_STATUSES:
        raise ControllerError(f"agent status must be one of {', '.join(AGENT_STATUSES)}")
    for label, value in (("name", name), ("session-id", session_id)):
        if not value.strip():
            raise ControllerError(f"{label} is required")
    open_entry = next(
        (item for item in state["agents"] if item["session_id"] == session_id and item["status"] == "DISPATCHED"),
        None,
    )
    settled = next(
        (item for item in state["agents"] if item["session_id"] == session_id and item["status"] != "DISPATCHED"),
        None,
    )
    if scope.strip() and (open_entry is not None or settled is not None):
        raise ControllerError(
            f"--scope belongs on the call that opens session {session_id}; widening it after the "
            "role was handed its prompt would rewrite what the dispatch actually read"
        )
    budget = _scope_budget(path, state, scope)
    if status == "DISPATCHED":
        if settled is not None:
            raise ControllerError(f"agent session already finished: {session_id} ({settled['status']})")
        if open_entry is not None:
            raise ControllerError(f"agent session already dispatched: {session_id}")
        if not input_digest.strip():
            raise ControllerError("input-digest is required when a dispatch is opened")
        round_ = state["review_round"] if role == ROLES["REVIEW"] else 0
        if role == ROLES["REVIEW"]:
            if round_ < 1:
                raise ControllerError("no review round is open; enter the REVIEW stage first")
            if _reviewer_slot_taken(state, role, round_):
                raise ControllerError(
                    f"a {role} instance already ran in round {round_}; each round needs a fresh reviewer"
                )
        entry = {
            "role": role,
            "name": name,
            "session_id": session_id,
            "input_digest": input_digest,
            "status": "DISPATCHED",
            "round": round_,
            "budget": budget,
            "dispatched_at": _now(),
            "recorded_at": _now(),
        }
        state["agents"].append(entry)
        _save(path, state)
        return entry

    if settled is not None:
        raise ControllerError(f"agent session already recorded: {session_id}")
    round_ = state["review_round"] if role == ROLES["REVIEW"] else 0
    if role == ROLES["REVIEW"]:
        if round_ < 1:
            raise ControllerError("no review round is open; enter the REVIEW stage first")
        if _reviewer_slot_taken(state, role, round_):
            raise ControllerError(
                f"a {role} instance already ran in round {round_}; each round needs a fresh reviewer"
            )
    if open_entry is None:
        entry = {
            "role": role,
            "name": name,
            "session_id": session_id,
            "input_digest": input_digest,
            "status": status,
            "round": round_,
            "wall_seconds": None,
            "budget": budget,
            "recorded_at": _now(),
        }
        state["agents"].append(entry)
        _save(path, state)
        return entry
    if open_entry["role"] != role or open_entry["name"] != name:
        raise ControllerError(
            f"session {session_id} was dispatched as {open_entry['role']}/{open_entry['name']}, "
            f"not {role}/{name}"
        )
    open_entry["status"] = status
    open_entry["input_digest"] = input_digest or open_entry["input_digest"]
    open_entry["finished_at"] = _now()
    open_entry["wall_seconds"] = _elapsed_seconds(open_entry["dispatched_at"], open_entry["finished_at"])
    _save(path, state)
    return open_entry


def write_artifact(
    state_path: str | Path, kind: str, source_file: str | Path, role: str | None, round_override: int | None
) -> dict[str, Any]:
    """Copy a stage output into the run directory so handoff is by path plus digest."""
    path, state = load_state(state_path)
    if kind not in ARTIFACT_KINDS:
        raise ControllerError(f"artifact kind must be one of {', '.join(ARTIFACT_KINDS)}")
    origin = Path(source_file).expanduser().resolve()
    text = _read_document(origin, f"artifact '{kind}'")
    _assert_substantive(text, f"artifact '{kind}'", origin)
    round_ = state["review_round"] if round_override is None else round_override
    if kind == "review-report" and round_ < 1:
        raise ControllerError("review-report artifacts belong to a review round (round >= 1)")
    stage = {
        "context-pack": "CONTEXT",
        "draft": "ARCHITECT",
        "critic-findings": "CRITIC",
        "refiner-map": "REFINER",
        "review-report": "REVIEW",
    }[kind]
    if role is not None and role != ROLES[stage]:
        raise ControllerError(f"artifact '{kind}' must come from {ROLES[stage]}, got {role}")
    if not _completed_agents(state, ROLES[stage], round_ if stage == "REVIEW" else 0):
        raise ControllerError(
            f"refusing to store '{kind}': no COMPLETED {ROLES[stage]} agent recorded"
        )
    relative = f"{kind}-r{round_}.md" if stage == "REVIEW" else f"{kind}.md"
    destination = path.parent / "artifacts" / relative
    if destination.exists() and destination.read_text(encoding="utf-8") == text:
        raise ControllerError(f"artifact already recorded with identical content: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent, prefix=f".{destination.name}.", delete=False
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    entry = {
        "kind": kind,
        "stage": stage,
        "round": round_,
        "path": str(destination),
        "origin": str(origin),
        "content_digest": _text_digest(text),
        "size": destination.stat().st_size,
        "recorded_at": _now(),
    }
    state["artifacts"].append(entry)
    _save(path, state)
    return entry


def _validate_finding_id(source: str, round_: int, finding_id: str) -> None:
    if source == "CRITIC":
        if not FINDING_ID["CRITIC"].fullmatch(finding_id):
            raise ControllerError("Critic findings must be named CRITIC-NNN, e.g. CRITIC-001")
        if round_ != 0:
            raise ControllerError("Critic findings belong to the one-shot round 0")
        return
    match = FINDING_ID["REVIEWER"].fullmatch(finding_id)
    if not match:
        raise ControllerError("Reviewer findings must be named REVIEW-<round>-NNN, e.g. REVIEW-2-001")
    if int(match.group(1)) != round_:
        raise ControllerError(f"finding id says round {match.group(1)} but the open round is {round_}")


def record_finding(
    state_path: str | Path,
    source: str,
    finding_id: str,
    severity: str,
    location: str,
    evidence: str,
    must_fix: bool | None,
    round_override: int | None,
) -> dict[str, Any]:
    """Add a structured Finding; Critic runs once, Reviewer findings are round-scoped."""
    path, state = load_state(state_path)
    source = source.upper()
    if source not in FINDING_ID:
        raise ControllerError("finding source must be CRITIC or REVIEWER")
    if severity not in SEVERITIES:
        raise ControllerError(f"severity must be one of {', '.join(SEVERITIES)}")
    for label, value in (("location", location), ("evidence", evidence)):
        if not value.strip():
            raise ControllerError(f"{label} is required")
    round_ = 0 if source == "CRITIC" else state["review_round"]
    if round_override is not None:
        round_ = round_override
    _validate_finding_id(source, round_, finding_id)
    if any(item["id"] == finding_id for item in state["findings"]):
        raise ControllerError(f"duplicate finding id: {finding_id}")
    entry = {
        "id": finding_id,
        "source": source,
        "round": round_,
        "severity": severity,
        "must_fix": severity in MUST_FIX_SEVERITIES if must_fix is None else must_fix,
        "location": location,
        "evidence": evidence,
        "status": "OPEN",
        "recorded_at": _now(),
    }
    state["findings"].append(entry)
    _save(path, state)
    return entry


def _closed(state: dict[str, Any], finding_id: str) -> bool:
    fix = next((item for item in state["fixes"] if item["finding_id"] == finding_id), None)
    return bool(fix and fix["status"] in {"FIXED", "REJECTED"})


def _passing_verdict_open(state: dict[str, Any]) -> bool:
    """The newest verdict is this round's and is one of the two approving statuses."""
    if not state["reviews"]:
        return False
    last = state["reviews"][-1]
    return (
        last["round"] == state["review_round"]
        and last["status"] in PASSING_REVIEW_STATUSES
    )


def _locations_agree(state_path: Path, finding_location: str, location: str) -> bool:
    """Whether a disposition answers the section the finding was raised against."""
    numbers = _index_numbers(state_path)
    key_finding, _ = _resolve_location(numbers, [], finding_location)
    key_fix, _ = _resolve_location(numbers, [], location)
    if key_finding and key_fix:
        return key_finding == key_fix
    return finding_location.strip() == location.strip()


def record_fix(
    state_path: str | Path,
    finding_id: str,
    status: str,
    location: str,
    change: str,
) -> dict[str, Any]:
    """Map one Finding to a disposition; the Refiner may not silently drop findings.

    A disposition is normally the Refiner stage's output: logging one while a review
    verdict is already on the table is how the orchestrator ends up grading its own
    edits.  The one exception is a passing verdict's non-blocking follow-up — a
    ``PENDING_CONFIRMATION`` or ``REJECTED`` changes no bytes and closes no reviewer's
    edit, and demanding a whole Refiner+Reviewer round just to register one would
    strand the run whenever the round budget is already spent.  ``FIXED`` claims a text
    change and keeps requiring the Refiner stage either way.
    """
    path, state = load_state(state_path)
    if status not in FIX_STATUSES:
        raise ControllerError(f"fix status must be one of {', '.join(FIX_STATUSES)}")
    finding = next((item for item in state["findings"] if item["id"] == finding_id), None)
    if finding is None:
        raise ControllerError(f"unknown finding id: {finding_id} (record the finding first)")
    for label, value in (("location", location), ("change", change)):
        if not value.strip():
            raise ControllerError(f"{label} is required")
    stage = state["stage"]
    if stage == "REFINER":
        _refiner_scope_covers(path, state, finding, location)
    elif stage in {"REVIEW", "DELIVERY"} and _passing_verdict_open(state):
        if status == "FIXED":
            raise ControllerError(
                f"{finding_id}: FIXED claims a text change, which after a verdict needs a "
                "Refiner round and a fresh review — dispose the follow-up as "
                "PENDING_CONFIRMATION or REJECTED instead"
            )
        if finding["source"] != "REVIEWER" or finding["round"] != state["review_round"]:
            raise ControllerError(
                f"only findings this round's review just raised may be disposed without a "
                f"Refiner round, and {finding_id} comes from {finding['source'].lower()} "
                f"round {finding['round']}"
            )
        if not _locations_agree(path, finding["location"], location):
            raise ControllerError(
                f"the disposition for {finding_id} points at '{location}' but the finding was "
                f"raised against '{finding['location']}'; a disposition must answer that finding"
            )
    else:
        raise ControllerError(
            "record-fix requires stage REFINER"
            " (or, after a passing verdict, REVIEW/DELIVERY for this round's non-blocking "
            f"follow-ups), current stage is {stage}"
        )
    if status == "REJECTED" and finding["must_fix"]:
        raise ControllerError(f"{finding_id} is {finding['severity']} and cannot be rejected")
    if status == "PENDING_CONFIRMATION" and finding["must_fix"]:
        raise ControllerError(
            f"{finding_id} is {finding['severity']}; a pending human decision keeps the round open"
        )
    entry = {
        "finding_id": finding_id,
        "status": status,
        "location": location,
        "change": change,
        "recorded_at": _now(),
    }
    existing = next((item for item in state["fixes"] if item["finding_id"] == finding_id), None)
    if existing is None:
        state["fixes"].append(entry)
    else:
        state["fixes"][state["fixes"].index(existing)] = entry
    finding["status"] = status
    _save(path, state)
    return entry


def record_review(
    state_path: str | Path,
    status: str,
    report: str | Path,
    tdd_file: str | Path,
    session_id: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """Bind a review verdict to the reviewed TDD content.

    The digest is taken over the ledger-stripped body: a later ``render-ledger`` refresh of
    chapter 16 must not invalidate an independent review, while every hand-written chapter
    still stays bound to the verdict.

    The round's scope is fixed when the round was opened, not here: recording an incremental
    verdict as ``full`` would let a reviewer that only read three sections certify the whole
    document.  The per-section snapshot written alongside the verdict is what the next round
    diffs against, so a scoped pass cannot quietly become the last word on a section nobody
    re-read.
    """
    path, state = load_state(state_path)
    _require_stage(state, "REVIEW", "record-review")
    if status not in REVIEW_STATUSES:
        raise ControllerError(f"review status must be one of {', '.join(REVIEW_STATUSES)}")
    round_ = state["review_round"]
    if round_ < 1:
        raise ControllerError("no review round is open")
    if any(item["round"] == round_ for item in state["reviews"]):
        raise ControllerError(f"round {round_} already has a review verdict")
    if not _completed_agents(state, ROLES["REVIEW"], round_):
        raise ControllerError(f"round {round_} has no COMPLETED {ROLES['REVIEW']} agent")
    pending = state.get("pending_review") or {}
    declared = (scope or pending.get("scope") or "full").strip().lower()
    if declared not in REVIEW_SCOPES:
        raise ControllerError(f"review scope must be one of {', '.join(REVIEW_SCOPES)}")
    if pending.get("round") == round_ and pending.get("scope") != declared:
        raise ControllerError(
            f"round {round_} was opened as a {pending['scope']} review; the verdict may not "
            "re-label what the reviewer was actually shown"
        )
    sections = list(pending.get("sections") or []) if declared == "incremental" else []
    if declared == "incremental" and not sections:
        raise ControllerError(
            "an incremental verdict needs a derived section scope; open the round with "
            "--review-scope incremental so the controller can compute it"
        )
    repo = _require_repo_root(state["repository"]["root"])
    report_path, report_rel = _inside(repo, report)
    text = _read_document(report_path, "review report")
    _assert_substantive(text, "review report", report_path)
    tdd = Path(tdd_file).expanduser().resolve()
    entry = {
        "round": round_,
        "status": status,
        "scope": declared,
        "sections": sections,
        "session_id": session_id,
        "report": report_rel,
        "report_digest": _text_digest(text),
        "tdd_digest": _document_digest(tdd),
        "recorded_at": _now(),
    }
    try:
        index = _fresh_index(path, state, tdd)
    except ControllerError as exc:
        # The snapshot exists so a later round can be scoped by diffing against it.  A document
        # the index cannot parse -- an unclosed fence -- is already a gate failure, so the honest
        # trade is to keep the verdict recordable and mark it unscopeable downstream, rather than
        # losing the review that was actually performed.
        entry["section_snapshot"] = None
        entry["snapshot_error"] = str(exc)
    else:
        entry["section_snapshot"] = _write_snapshot(path, round_, index, entry["tdd_digest"])
        entry["snapshot_sections"] = len(index["sections"])
    state["reviews"].append(entry)
    state["pending_review"] = None
    _save(path, state)
    return entry


def degrade(state_path: str | Path, role: str, reason: str) -> dict[str, Any]:
    """Record that the host could not dispatch a role, so delivery is qualified."""
    path, state = load_state(state_path)
    if role not in ROLES.values() and role != "orchestrator":
        raise ControllerError(f"unknown role for degradation: {role}")
    if not reason.strip():
        raise ControllerError("--reason is required")
    state["degraded"] = True
    state["degradations"].append({"role": role, "reason": reason, "at": _now()})
    _save(path, state)
    return {"degraded": True, "role": role}


def archive_history(state_path: str | Path, version: str) -> dict[str, Any]:
    """Store the superseded TDD as its own file instead of nesting it in a code fence."""
    path, state = load_state(state_path)
    tdd = Path(state["output_document"]["absolute"])
    if not tdd.is_file():
        raise ControllerError(f"no current TDD to archive: {tdd}")
    if not version.strip():
        raise ControllerError("--version is required (the version being superseded)")
    text = tdd.read_text(encoding="utf-8")
    for line in text.splitlines():
        match = FENCE.match(line.strip())
        if match and match.group(1).lower() in FORBIDDEN_FENCE_LANGUAGES:
            raise ControllerError(
                "the outgoing TDD embeds a ```markdown fence; archive history as files, "
                f"not nested fences (offending line: {line.strip()!r})"
            )
    index = len(state["history"]) + 1
    destination = path.parent / "history" / f"{index:02d}-{version.strip()}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tdd, destination)
    entry = {
        "index": index,
        "version": version.strip(),
        "path": str(destination),
        "content_digest": _text_digest(text),
        "size": destination.stat().st_size,
        "archived_at": _now(),
    }
    state["history"].append(entry)
    _save(path, state)
    return entry


def _cell(value: Any, limit: int = LEDGER_CELL_LIMIT) -> str:
    text = " ".join(str(value).split()).replace("|", r"\|")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "—"


def _ledger_blocks(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each generated section to the line indices of its marker pair."""
    blocks: dict[str, tuple[int, int]] = {}
    opened: dict[str, int] = {}
    inside_fence = False
    for index, line in enumerate(lines):
        if FENCE.match(line.strip()):
            inside_fence = not inside_fence
            continue
        match = LEDGER_MARKERS.match(line.strip())
        if not match:
            continue
        if inside_fence:
            raise ControllerError(
                f"ledger marker inside a code fence at line {index + 1}; move it out of the fence"
            )
        section, edge = match.group(1), match.group(2)
        if edge == "start":
            if section in opened or section in blocks:
                raise ControllerError(f"ledger section '{section}' is opened twice")
            opened[section] = index
        else:
            if section not in opened:
                raise ControllerError(f"ledger section '{section}' has an end marker with no start")
            blocks[section] = (opened.pop(section), index)
    if opened:
        raise ControllerError(f"unclosed ledger marker(s): {', '.join(sorted(opened))}")
    unknown = sorted(set(blocks) - set(LEDGER_SECTIONS))
    if unknown:
        raise ControllerError(f"unknown ledger section(s): {', '.join(unknown)}")
    return blocks


def strip_ledger_bodies(text: str) -> str:
    """Drop generated blocks and collapse the blank padding around them.

    Digests are taken over this normalised form, so refreshing the ledger cannot invalidate an
    independent review while every model-authored byte still stays bound to the verdict: only
    the block markers and their body disappear, and a run of blank lines reads as one, which is
    what makes ``render-ledger`` free to add or remove the blank line that separates a block
    from the surrounding prose.
    """
    lines = text.splitlines()
    blocks = _ledger_blocks(lines)
    dropped: set[int] = set()
    for start, end in blocks.values():
        dropped.update(range(start, end + 1))
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in dropped:
            continue
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return "\n".join(out)


def _document_digest(path: Path) -> str:
    return _text_digest(strip_ledger_bodies(_read_document(path, "TDD")))


def _anchor_index(lines: list[str], needle: str) -> int | None:
    inside_fence = False
    for index, line in enumerate(lines):
        if FENCE.match(line.strip()):
            inside_fence = not inside_fence
            continue
        if not inside_fence and line.startswith("#") and needle in line:
            return index
    if inside_fence:
        raise ControllerError("the TDD has an unclosed code fence; ledger anchors are unreliable")
    return None


def _findings_ledger_body(state: dict[str, Any]) -> list[str]:
    fixes = {item["finding_id"]: item for item in state["fixes"]}
    rows = [
        "| Finding ID | 来源 | 轮次 | 严重度 | 位置 | 处置 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for finding in sorted(state["findings"], key=lambda item: (item["round"], item["id"])):
        fix = fixes.get(finding["id"])
        rows.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(finding["id"]),
                _cell("Critic" if finding["source"] == "CRITIC" else "Reviewer"),
                _cell(finding["round"]),
                _cell(finding["severity"]),
                _cell(finding["location"]),
                _cell(fix["change"] if fix else "—"),
                _cell(fix["status"] if fix else finding["status"]),
            )
        )
    if len(rows) == 2:
        rows.append("| — | 无 | — | — | — | — | 本轮未登记 Finding |")
    return [
        "**Finding 处置映射**（由 `tech_doc_controller.py render-ledger` 从 `state.json` 生成，"
        "请勿手改；完整证据见运行目录 `state.json`）",
        "",
        *rows,
    ]


def _run_metadata_body(state: dict[str, Any], doc_text: str, run_dir: str) -> list[str]:
    severities = {severity: 0 for severity in SEVERITIES}
    for finding in state["findings"]:
        severities[finding["severity"]] = severities.get(finding["severity"], 0) + 1
    open_must_fix = [
        item["id"]
        for item in state["findings"]
        if item["must_fix"] and not _closed(state, item["id"])
    ]
    if state["reviews"]:
        verdict = f"r{state['reviews'][-1]['round']} {state['reviews'][-1]['status']}"
    else:
        verdict = "尚无独立终审结论"
    if state["gates"]:
        gate_summary = f"{state['gates'][-1]['status']} @ {state['gates'][-1]['evaluated_at'][:19]}"
    else:
        gate_summary = "尚未运行"
    confirmations: list[str] = []
    for line in doc_text.splitlines():
        for match in CONFIRMATION.finditer(line):
            if match.group(0) not in confirmations:
                confirmations.append(match.group(0))
    degraded = (
        "是：" + "、".join(_cell(item["role"]) for item in state["degradations"])
        if state["degraded"]
        else "否"
    )
    # A repository-relative run directory stays readable on every host; an absolute Windows
    # path would otherwise dominate the table and re-wrap differently per viewer.
    try:
        label = Path(run_dir).relative_to(Path(state["repository"]["root"])).as_posix()
    except ValueError:
        label = run_dir
    probe = state.get("scale") or {}
    if probe:
        signals = "、".join(sorted(probe.get("signals") or {})) or "无结构变更信号"
        scale_cell = "{} 级（{}；{}）".format(
            probe["scale"], signals, "绿地仓库" if probe["greenfield"] else "既有实现"
        )
    else:
        scale_cell = "尚未判定"
    scopes = "、".join(
        f"r{item['round']} {item.get('scope') or 'full'}" for item in state["reviews"]
    ) or "尚无终审"
    rows = [
        "| 项目 | 值 |",
        "|---|---|",
        f"| run-id | {_cell(state['run_id'])} |",
        f"| 运行目录 | `{label}` |",
        f"| `review_round` | {state['review_round']} / {state['max_review_rounds']} |",
        f"| 独立终审结论 | {_cell(verdict)} |",
        f"| 终审范围 | {_cell(scopes)} |",
        f"| 规模判定 | {_cell(scale_cell)} |",
        f"| 交付门禁 | {_cell(gate_summary)} |",
        "| Finding 合计 | {}（Blocker {} / High {} / Medium {} / Low {}；must_fix 未闭环 {}） |".format(
            len(state["findings"]),
            severities["Blocker"],
            severities["High"],
            severities["Medium"],
            severities["Low"],
            len(open_must_fix),
        ),
        f"| 降级模式 | {_cell(degraded)} |",
    ]
    body = ["**运行元数据**（生成，勿手改）", "", *rows, ""]
    if confirmations:
        body += [f"仍需人工确认（{len(confirmations)} 项）："] + [
            f"- `{_cell(item)}`" for item in confirmations[:40]
        ]
    else:
        body.append("仍需人工确认：无。")
    return body


def _prior_history_rows(text: str) -> list[str]:
    """History rows already rendered into the document, verbatim and in order.

    ``render-ledger`` replaces the whole block, so without merging the previous rows back in,
    the second UPDATE run would erase what the first run wrote — the index promises
    只追加、不覆盖、不重排 across runs, not just within one.  Rows are trusted as parsed: the
    archived files beside them are the evidence, and this is the only channel they survived in.
    """
    try:
        blocks = _ledger_blocks(text.splitlines())
    except ControllerError:
        return []
    span = blocks.get("history-index")
    if span is None:
        return []
    rows: list[str] = []
    for line in text.splitlines()[span[0] + 1 : span[1]]:
        stripped = line.strip()
        if not stripped.startswith("|") or "序号" in stripped or "无历史版本" in stripped:
            continue
        if set(stripped) <= set("|-: "):
            continue
        if len(stripped.split("|")) < 7:
            continue
        rows.append(stripped)
    return rows


def _history_index_body(
    state: dict[str, Any], prior_rows: list[str] | tuple[str, ...] = ()
) -> list[str]:
    rows = [
        "| 序号 | 版本 | 归档日期 | 变更台账（修改/新增/删除） | 归档文件 |",
        "|---|---|---|---|---|",
    ]
    seen: set[tuple[str, str]] = set()
    for line in prior_rows:
        cells = line.split("|")
        key = (cells[2].strip(), cells[-2].strip())
        if key in seen:
            continue
        seen.add(key)
        # 序号 restates the rendered position; everything else survives verbatim.
        cells[1] = f" {len(seen)} "
        rows.append("|".join(cells))
    changes = state.get("prd_changes") or "—"
    for item in state["history"]:
        # Three trailing parts keep the run-id visible: two runs archiving the same version
        # would otherwise print an identical, unresolvable file cell.
        archived = "`{}`".format("/".join(Path(item["path"]).as_posix().split("/")[-3:]))
        key = (item["version"], archived)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            "| {} | {} | {} | {} | {} |".format(
                len(seen),
                _cell(item["version"]),
                _cell(str(item.get("archived_at", ""))[:10]),
                _cell(changes),
                archived,
            )
        )
    if not seen:
        rows.append("| — | 无历史版本 | — | — | 本 run 未归档旧版 TDD |")
    return ["**历史版本索引**（生成，只追加不手改）", "", *rows]


def _expected_ledger(state: dict[str, Any], doc_text: str, run_dir: str) -> dict[str, list[str]]:
    return {
        "findings": _findings_ledger_body(state),
        "run-metadata": _run_metadata_body(state, strip_ledger_bodies(doc_text), run_dir),
        "history-index": _history_index_body(state, _prior_history_rows(doc_text)),
    }


def _reject_handwritten_mirror(lines: list[str], blocks: dict[str, tuple[int, int]]) -> None:
    """A hand-copied ledger beside the generated one reintroduces the drift this removes."""
    covered = {index for start, end in blocks.values() for index in range(start, end + 1)}
    stray = [
        f"line {index + 1}: {line.strip()[:60]}"
        for index, line in enumerate(lines)
        if index not in covered
        and line.lstrip().startswith("|")
        and re.search(r"(?:CRITIC-\d|REVIEW-\d{1,2}-\d)", line)
    ]
    if stray:
        raise ControllerError(
            "the TDD still carries a hand-written Finding table; delete it and let "
            f"'render-ledger' own chapter 16 (found: {'; '.join(stray[:5])})"
        )


def render_ledger(state_path: str | Path, tdd_file: str | Path) -> dict[str, Any]:
    """Write the delivery ledger from ``state.json`` so the mirror cannot drift."""
    path, state = load_state(state_path)
    if state["stage"] == "CLOSED":
        raise ControllerError("the run is closed; the delivered document is frozen")
    run_dir = str(path.parent)
    tdd = Path(tdd_file).expanduser().resolve()
    text = _read_document(tdd, "TDD")
    expected = _expected_ledger(state, text, run_dir)
    lines = text.splitlines()
    rendered: list[str] = []
    for section in LEDGER_ORDER:
        blocks = _ledger_blocks(lines)
        if section in blocks:
            start, end = blocks[section]
            lines = lines[: start + 1] + expected[section] + lines[end:]
            rendered.append(section)
            continue
        anchor = _anchor_index(lines, LEDGER_ANCHOR[section])
        if anchor is None:
            if section == "history-index" and state["mode"] != "UPDATE":
                continue
            raise ControllerError(
                f"anchor '{LEDGER_ANCHOR[section]}' is missing; chapter 16 must be restored from "
                "the template before the ledger can be rendered"
            )
        siblings = [
            end + 1
            for named, (_, end) in blocks.items()
            if LEDGER_ANCHOR[named] == LEDGER_ANCHOR[section]
        ]
        insert_at = max([anchor + 1] + siblings)
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        block = [LEDGER_START.format(section=section), *expected[section],
                 LEDGER_END.format(section=section)]
        before_blank = insert_at > 0 and not lines[insert_at - 1].strip()
        after_blank = insert_at == len(lines) or not lines[insert_at].strip()
        lines = (
            lines[:insert_at]
            + ([] if before_blank else [""])
            + block
            + ([] if after_blank else [""])
            + lines[insert_at:]
        )
        rendered.append(section)
    blocks = _ledger_blocks(lines)
    mirror: list[str] = []
    try:
        _reject_handwritten_mirror(lines, blocks)
    except ControllerError as exc:
        # Rendered anyway: the generated chapter is now in place, and the leftover hand-written
        # rows are gate failures the orchestrator can delete in one pass instead of one per round.
        mirror.append(str(exc))
    next_text = "\n".join(lines).rstrip("\n") + "\n"
    changed = next_text != text
    if changed:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=tdd.parent,
            prefix=f".{tdd.name}.", delete=False,
        ) as handle:
            handle.write(next_text)
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, tdd)
    finding_rows = len(state["findings"])
    state["ledger"] = {
        "rendered_at": _now(),
        "sections": [section for section in LEDGER_ORDER if section in blocks],
        "finding_rows": finding_rows,
        "tdd_digest": _text_digest(strip_ledger_bodies(next_text)),
        "tdd_size": len(next_text.encode("utf-8")),
    }
    _save(path, state)
    # The ledger rows shift every line below chapter 16, so the index is rebuilt here rather
    # than left to go stale between a render and the next Refiner dispatch.  A document the
    # index cannot parse (an unclosed fence) must not fail a render that already succeeded:
    # the fence is a gate finding, and failing here would hide it behind a ledger error.
    try:
        index = _write_index(path, state, tdd, "render-ledger")
        summary: dict[str, Any] = _index_summary(index)
    except ControllerError as exc:
        summary = {"status": "not_built", "reason": str(exc)}
    return {
        "changed": changed,
        "rendered": rendered,
        "finding_rows": finding_rows,
        "tdd_digest": state["ledger"]["tdd_digest"],
        "handwritten_mirror": mirror,
        "path": str(tdd),
        "doc_index": summary,
    }


def _required_sections(state: dict[str, Any]) -> list[str]:
    if state["mode"] == "UPDATE":
        return list(LEDGER_ORDER)
    return [section for section in LEDGER_ORDER if section != "history-index"]


def _ledger_problems(
    state: dict[str, Any], text: str, run_dir: str
) -> tuple[list[str], list[str]]:
    """Split ledger failures so a missing block reads differently from an edited one."""
    lines = text.splitlines()
    try:
        blocks = _ledger_blocks(lines)
    except ControllerError as exc:
        return [], [f"markers unreadable: {exc}"]
    expected = _expected_ledger(state, text, run_dir)
    missing: list[str] = []
    stale: list[str] = []
    for section in _required_sections(state):
        if section not in blocks:
            missing.append(f"'{section}' block is missing; run render-ledger")
            continue
        start, end = blocks[section]
        if lines[start + 1 : end] != expected[section]:
            stale.append(f"'{section}' block was edited by hand or is stale; run render-ledger")
    try:
        _reject_handwritten_mirror(lines, blocks)
    except ControllerError as exc:
        stale.append(str(exc))
    return missing, stale


# --- document index ---------------------------------------------------------------------
# In the reference run, 107.9 of the Refiner's 199.8 minutes (54%) went to Grep and Read
# calls whose only job was answering "which paragraph do I change, and what does it say
# today", at 4.8 probes per edit point.  The cause is arithmetic: the delivered TDD is
# ~223K tokens against a 200K window, so a role that wants section 7.2 has to page through
# the file.  The index turns that into one deterministic call whose output is a line range.
DOC_INDEX_FILE = "doc-index.json"
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
HEADING_NUMBER = re.compile(r"^(\d{1,2})(?:\.(\d{1,2}))?[.、]?\s+(.*)$")
LOCATION_TOKEN = re.compile(r"\d{1,2}(?:\.\d{1,2})?")
CROSS_REF = re.compile(r"(?:§|见|参见|详见)\s*(\d{1,2}\.\d{1,2})|(\d{1,2}\.\d{1,2})\s*(?:节|章节)")
CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?([A-Za-z][A-Za-z0-9_.]{1,63})", re.IGNORECASE
)
ALTER_TABLE = re.compile(r"ALTER\s+TABLE\s+[\"'`]?([A-Za-z][A-Za-z0-9_.]{1,63})", re.IGNORECASE)
# The path charset is ASCII-only on purpose: a prose annotation right after an endpoint
# (``GET /api/x（见 7.2 节`` ) otherwise gets swallowed into the symbol name, and query strings
# are noise for the question the index answers, which is "which interfaces does this section
# declare".
API_ENDPOINT = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9{}/._~+-]*)")
DEFAULT_CONTEXT_WINDOW = 200_000
# One dispatch may not claim more than this share of the window: the role still has to hold
# its own instructions, the finding list and the prose it is writing.
BUDGET_SHARE = 0.5
# CJK and ASCII cost very different amounts: the delivered reference TDD is 428,429 characters,
# only 27.6% of them CJK, and it costs ~220K tokens.  A single flat divisor is wrong in both
# directions on that mix, so the estimate keeps the two terms apart.
CJK_CHARS = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
TOKENS_PER_CJK = 1.2
CHARS_PER_OTHER_TOKEN = 4
LOCATE_FALLBACK = "区间不足时自行扩大 Read 范围，并在回报里声明多读了哪些章节"
# Section 7.2 of the reference TDD declares 48 table names.  Echoing all of them would cost the
# lookup call more than the grep it replaces, so the list is capped and the full version stays
# in ``doc-index.json`` for whoever actually needs to enumerate it.
LOCATE_LIST_LIMIT = 12


def _estimate_tokens(text: str) -> int:
    cjk = len(CJK_CHARS.findall(text))
    return max(1, int(cjk * TOKENS_PER_CJK + (len(text) - cjk) / CHARS_PER_OTHER_TOKEN))


def _heading_entries(lines: list[str]) -> list[dict[str, Any]]:
    """Every heading outside a code fence, with its number when the title carries one."""
    inside_fence = False
    entries: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if FENCE.match(stripped):
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        match = HEADING.match(stripped)
        if match is None:
            continue
        title = match.group(2)
        number: str | None = None
        numbered = HEADING_NUMBER.match(title)
        if numbered:
            chapter, part = numbered.group(1), numbered.group(2)
            number = f"{chapter}.{part}" if part else chapter
            title = numbered.group(3).strip()
        entries.append(
            {"level": len(match.group(1)), "number": number, "title": title, "start": index + 1}
        )
    if inside_fence:
        raise ControllerError("the TDD has an unclosed code fence; the document index is unreliable")
    return entries


def _section_marks(lines: list[str], start: int, end: int) -> tuple[list[str], list[str]]:
    """Symbols declared inside one range, plus the sections it cites by number."""
    symbols: list[str] = []
    refs: list[str] = []
    inside_fence = False
    for number in range(start - 1, min(end, len(lines))):
        line = lines[number]
        if FENCE.match(line.strip()):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            for match in CROSS_REF.finditer(line):
                token = match.group(1) or match.group(2)
                if token not in refs:
                    refs.append(token)
        for pattern in (CREATE_TABLE, ALTER_TABLE):
            for match in pattern.finditer(line):
                if match.group(1) not in symbols:
                    symbols.append(match.group(1))
        for match in API_ENDPOINT.finditer(line):
            token = f"{match.group(1)} {match.group(2)}"
            if token not in symbols:
                symbols.append(token)
    return symbols, refs


def _resolve_location(
    numbers: dict[str, str], titles: list[tuple[str, str]], location: str
) -> tuple[str | None, str]:
    """Map a finding's free-form location onto an indexed section.

    ``7.2``, ``§7.2``, ``s7.2#table.column`` and a bare chapter all resolve; a location that
    names no heading comes back ``None`` instead of being guessed, so ``locate`` can say so.
    """
    candidates: list[tuple[str, str]] = []
    for token in LOCATION_TOKEN.findall(location or ""):
        candidates.append((token, "exact"))
        if "." in token:
            candidates.append((token.split(".")[0], "chapter"))
    for candidate, kind in candidates:
        if candidate in numbers:
            return numbers[candidate], kind
    text = (location or "").strip()
    if text:
        for title, key in titles:
            if title and (text in title or title in text):
                return key, "title"
    return None, "none"


def _ledger_line_mask(lines: list[str]) -> set[int]:
    """Line numbers whose content is generated, so a refresh cannot read as a design change.

    ``render-ledger`` rewrites chapter 16 after every round.  Digesting those lines would put
    chapter 16 into the changed set of every incremental review forever, which is the noise
    this measure exists to remove; ``strip_ledger_bodies`` already treats them as invisible to
    the review binding, so the per-section digest does too.
    """
    try:
        blocks = _ledger_blocks(lines)
    except ControllerError:
        return set()
    mask: set[int] = set()
    for start, end in blocks.values():
        mask.update(range(start, end + 1))
    return mask


def build_doc_index(text: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    lines = text.splitlines()
    mask = _ledger_line_mask(lines)
    entries = _heading_entries(lines)
    sections: list[dict[str, Any]] = []
    numbers: dict[str, str] = {}
    titles: list[tuple[str, str]] = []
    for position, entry in enumerate(entries):
        end = len(lines)
        for other in entries[position + 1 :]:
            if other["level"] <= entry["level"]:
                end = other["start"] - 1
                break
        parent = None
        for previous in reversed(entries[:position]):
            if previous["level"] < entry["level"]:
                parent = previous["number"]
                break
        key = entry["number"] or f"title:{entry['title']}"
        if entry["number"] and entry["number"] not in numbers:
            numbers[entry["number"]] = key
        titles.append((entry["title"], key))
        body = "\n".join(lines[entry["start"] - 1 : end])
        symbols, refs = _section_marks(lines, entry["start"], end)
        sections.append(
            {
                "key": key,
                "number": entry["number"],
                "title": entry["title"],
                "level": entry["level"],
                "parent": parent,
                "start": entry["start"],
                "end": end,
                "chars": len(body),
                # Taken over the same generated-content-blind view the review binding uses, so
                # "this section changed" always means a model authored something different here.
                # Blank lines are dropped for the same reason: the generated blocks arrive with
                # their own padding, and a section must not read as redesigned because one blank
                # line appeared beside the ledger.
                "digest": _text_digest(
                    "\n".join(
                        line
                        for offset, line in enumerate(lines[entry["start"] - 1 : end])
                        if line.strip() and (entry["start"] - 1 + offset) not in mask
                    )
                ),
                "tokens_estimated": _estimate_tokens(body),
                "symbols": symbols,
                "refs": [item for item in refs if item != entry["number"]],
                "findings": [],
            }
        )
    unlocated: list[str] = []
    if state is not None:
        for finding in state["findings"]:
            key, _ = _resolve_location(numbers, titles, finding["location"])
            if key is None:
                unlocated.append(finding["id"])
            else:
                for item in sections:
                    if item["key"] == key:
                        item["findings"].append(finding["id"])
                        break
    return {
        "generated_at": _now(),
        "tdd_digest": _text_digest(text),
        "size": len(text.encode("utf-8")),
        "section_count": len(sections),
        "numbers": numbers,
        "sections": sections,
        "unlocated_findings": unlocated,
    }


def _index_path(state_path: Path) -> Path:
    return state_path.parent / DOC_INDEX_FILE


def _write_index(path: Path, state: dict[str, Any], tdd: Path, reason: str) -> dict[str, Any]:
    """Rebuild ``doc-index.json``.  Only the index file is written: callers hold their own
    copy of ``state`` and a second writer would clobber it."""
    text = _read_document(tdd, "TDD")
    index = build_doc_index(text, state)
    index["reason"] = reason
    _atomic_json(_index_path(path), index)
    return index


def _resolve_tdd(state: dict[str, Any], tdd_file: str | Path | None) -> Path:
    """An explicit path wins; otherwise use the one ``init`` froze into the state."""
    if str(tdd_file or "").strip():
        return Path(tdd_file).expanduser().resolve()
    return Path(state["output_document"]["absolute"])


def _fresh_index(state_path: Path, state: dict[str, Any], tdd: Path) -> dict[str, Any]:
    """Reuse the on-disk index only while it still describes the current bytes."""
    cached = _index_path(state_path)
    text_digest = _text_digest(_read_document(tdd, "TDD"))
    if cached.is_file():
        try:
            index = json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
        if index.get("tdd_digest") == text_digest:
            index["cached"] = True
            return index
    return _write_index(state_path, state, tdd, "locate")


def _index_summary(index: dict[str, Any]) -> dict[str, Any]:
    unlocated = index["unlocated_findings"]
    return {
        "sections": index["section_count"],
        "tdd_digest": index["tdd_digest"][:12],
        # The full lists stay in ``doc-index.json``; a summary that grows with the run is how
        # a status check turns into the token cost this wave exists to remove.
        "unlocated_findings": unlocated[:8],
        "unlocated_total": len(unlocated),
        "generated_at": index["generated_at"],
    }


def doc_index(state_path: str | Path, tdd_file: str | Path | None = None) -> dict[str, Any]:
    """Rebuild the index on demand; the full section list stays in the file, not stdout."""
    path, state = load_state(state_path)
    index = _write_index(path, state, _resolve_tdd(state, tdd_file), "index")
    return {
        "status": "OK",
        "path": str(_index_path(path)),
        **_index_summary(index),
    }


def _section_view(item: dict[str, Any], why: str) -> dict[str, Any]:
    return {
        "key": item["key"],
        "title": item["title"],
        "start": item["start"],
        "end": item["end"],
        "chars": item["chars"],
        "tokens_estimated": item["tokens_estimated"],
        "why": why,
    }


def locate(
    state_path: str | Path,
    tdd_file: str | Path | None,
    finding_id: str | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    """Answer "which lines do I have to read" for one finding or one section number.

    The result holds the target range, the sections it cites (a design fact stated in 7.2 is
    consumed by 4.2, and reading only one of the two is how a fix creates the next finding),
    and the token estimate of the whole plan against the run's context budget.
    """
    path, state = load_state(state_path)
    tdd = _resolve_tdd(state, tdd_file)
    index = _fresh_index(path, state, tdd)
    by_key = {item["key"]: item for item in index["sections"]}
    numbers = index["numbers"]
    window = int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW)
    budget = int(window * BUDGET_SHARE)

    location: str | None = None
    if finding_id:
        finding = next((item for item in state["findings"] if item["id"] == finding_id), None)
        if finding is None:
            raise ControllerError(f"unknown finding id: {finding_id} (record the finding first)")
        location = finding["location"]
        key, match = _resolve_location(numbers, [(item["title"], item["key"]) for item in index["sections"]], location)
    elif section:
        wanted = section.strip()
        key = wanted if wanted in by_key else numbers.get(wanted)
        match = "section" if key else "none"
    else:
        raise ControllerError("one of --finding or --section is required")

    if key is None or key not in by_key:
        return {
            "status": "NOT_LOCATED",
            "finding": finding_id,
            "location": location or section,
            "sections": [],
            "reason": "no heading in the TDD resolves this location",
            "fallback": LOCATE_FALLBACK,
        }

    target = by_key[key]
    views = [_section_view(target, "finding location" if finding_id else "requested section")]
    for ref in target["refs"]:
        ref_key = numbers.get(ref)
        if ref_key and ref_key in by_key and ref_key != key:
            views.append(_section_view(by_key[ref_key], f"cited by {key}"))
    chars = sum(item["chars"] for item in views)
    # Summing the per-section estimates rather than re-estimating the concatenated length:
    # each figure was taken over that section's own CJK/ASCII mix, which the joint average
    # would lose.
    tokens = sum(item["tokens_estimated"] for item in views)
    return {
        "status": "OK",
        "finding": finding_id,
        "location": location or section,
        "resolved_key": key,
        "match": match,
        "cached": index.get("cached", False),
        "sections": views,
        "symbols": target["symbols"][:LOCATE_LIST_LIMIT],
        "symbols_total": len(target["symbols"]),
        "findings_here": [item for item in target["findings"] if item != finding_id][:LOCATE_LIST_LIMIT],
        "total_chars": chars,
        "tokens_estimated": tokens,
        "budget_tokens": budget,
        "over_budget": tokens > budget,
        "fallback": LOCATE_FALLBACK,
    }


def _scope_budget(state_path: Path, state: dict[str, Any], raw: str) -> dict[str, Any]:
    """Derive what a per-section dispatch is actually being handed, from the index.

    The byte and token figures come from the document rather than from a self-report, so an
    over-budget claim cannot be talked down, and a section key that does not exist is an
    error instead of a silently ignored typo.
    """
    keys = [item.strip() for item in re.split(r"[,\s]+", raw) if item.strip()]
    if not keys:
        return {}
    tdd = _resolve_tdd(state, None)
    if not tdd.is_file():
        raise ControllerError(
            f"--scope needs the TDD to exist; run 'index' after the draft is written "
            f"and drop --scope for stages that precede it ({tdd} is absent)"
        )
    index = _fresh_index(state_path, state, tdd)
    by_key = {item["key"]: item for item in index["sections"]}
    numbers = index["numbers"]
    chosen: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for token in keys:
        item = by_key.get(token) or by_key.get(numbers.get(token, ""))
        if item is None:
            missing.append(token)
            continue
        chosen[item["key"]] = item
    if missing:
        raise ControllerError(
            f"unknown section key(s) in --scope: {', '.join(missing)}; run 'index' to see the keys"
        )
    views = [_section_view(item, "declared scope") for item in chosen.values()]
    chars = sum(item["chars"] for item in views)
    tokens = sum(item["tokens_estimated"] for item in views)
    window = int(state.get("context_window") or DEFAULT_CONTEXT_WINDOW)
    budget = int(window * BUDGET_SHARE)
    return {
        "sections": [item["key"] for item in views],
        "ranges": [{"key": item["key"], "start": item["start"], "end": item["end"]} for item in views],
        "read_chars": chars,
        "tokens_estimated": tokens,
        "context_window": window,
        "budget_tokens": budget,
        "over_budget": tokens > budget,
    }


def _index_numbers(state_path: Path) -> dict[str, str]:
    """The section numbers the on-disk index knows about, or empty when there is no index."""
    cached = _index_path(state_path)
    if not cached.is_file():
        return {}
    try:
        return dict(json.loads(cached.read_text(encoding="utf-8")).get("numbers", {}))
    except (OSError, ValueError):
        return {}


def _refiner_scope_covers(
    state_path: Path, state: dict[str, Any], finding: dict[str, Any], location: str
) -> None:
    """A per-section Refiner dispatch may not dispose of a finding it was never handed.

    Both ends are checked: the section the disposition claims to have touched *and* the
    section the finding was raised against.  A fix may land in a different chapter than
    the finding names, but the round still has to have been handed the finding's own
    location -- naming an in-scope section while the finding sits outside would otherwise
    stay a bypass.  A location that resolves to no heading stays disposable: the guard
    must not invent a scope for it.

    The window is the current REFINER stage, which is what makes a split round legal: several
    scoped dispatches contribute the union of their sections, while a scope declared in an
    earlier round does not license this round's edit.  One dispatch that declares no ``--scope``
    does not stand the guard down either -- it would be an escape hatch, and the ledger already
    says which sections were injected.  A run where no dispatch declares a scope at all keeps the
    reference behaviour of every earlier run.
    """
    entered = [
        _parse_at(item.get("at"))
        for item in state.get("stage_history", [])
        if item.get("stage") == "REFINER"
    ]
    since = next((stamp for stamp in reversed(entered) if stamp is not None), None)
    scopes: list[str] = []
    for item in state["agents"]:
        if item["role"] != ROLES["REFINER"]:
            continue
        if since is not None and (_parse_at(item.get("recorded_at")) or since) < since:
            continue
        for key in (item.get("budget") or {}).get("sections") or []:
            if key not in scopes:
                scopes.append(key)
    if not scopes:
        return
    numbers = _fresh_index(state_path, state, _resolve_tdd(state, None))["numbers"]
    for label, value in (("disposition", location), ("finding", finding["location"])):
        key, _ = _resolve_location(numbers, [], value)
        if key is not None and key not in scopes:
            raise ControllerError(
                f"'{value}' ({label} location) resolves to section {key}, outside every --scope "
                f"this round's Refiner dispatches were handed ({', '.join(scopes)}); widen it or "
                "read that section first"
            )


def _scan_document(text: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic delivery checks over the TDD body."""
    checks: list[dict[str, Any]] = []

    placeholders: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if CONFIRMATION.search(line):
            continue
        if PLACEHOLDER.search(line):
            placeholders.append(f"line {number}: {line.strip()[:80]}")
    checks.append({
        "name": "no-template-placeholders",
        "ok": not placeholders,
        "detail": placeholders[:12] or "clean",
    })

    fences = [line.strip() for line in text.splitlines() if FENCE.match(line.strip())]
    unbalanced = len(fences) % 2 != 0
    unknown = sorted({
        match.group(1)
        for number, line in enumerate(text.splitlines(), start=1)
        for match in [FENCE.match(line.strip())]
        if match and match.group(1)
        and match.group(1).lower() not in ALLOWED_FENCE_LANGUAGES
        and match.group(1).lower() not in FORBIDDEN_FENCE_LANGUAGES
    })
    embedded = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        for match in [FENCE.match(line.strip())]
        if match and match.group(1).lower() in FORBIDDEN_FENCE_LANGUAGES
    ]
    checks.append({
        "name": "diagram-fences",
        "ok": not unbalanced and not unknown and not embedded,
        "detail": (
            ["unbalanced code fences"] if unbalanced else []
        ) + ([f"unknown fence languages: {', '.join(unknown)}"] if unknown else [])
        + ([f"nested document fences: {embedded[:3]}"] if embedded else [])
        or "balanced; mermaid/text/sql/json only",
    })

    anchors = list(REQUIRED_ANCHORS)
    if state["mode"] == "UPDATE":
        anchors += list(UPDATE_ONLY_ANCHORS)
    missing = [anchor for anchor in anchors if anchor not in text]
    checks.append({
        "name": "template-anchors",
        "ok": not missing,
        "detail": missing or "all required chapters present",
    })

    legacy = [
        f"line {number}"
        for number, line in enumerate(text.splitlines(), start=1)
        if LEGACY_CONTEXT_MARKER in line
    ]
    checks.append({
        "name": "no-repo-agent-config-writeback",
        "ok": not legacy,
        "detail": legacy or "context pack stayed inside the feature directory",
    })

    return checks


def gate(state_path: str | Path, tdd_file: str | Path) -> dict[str, Any]:
    """Decide delivery without model judgment; a green gate is what may be claimed."""
    path, state = load_state(state_path)
    tdd = Path(tdd_file).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("input-status-usable", state["input_status"] in {"PASS", "PARTIAL"}, state["input_status"])
    add("context-pack-persisted", bool(_artifacts(state, "context-pack")), "artifact required")
    add("architect-draft-persisted", bool(_artifacts(state, "draft")), "artifact required")
    add("critic-findings-persisted", bool(_artifacts(state, "critic-findings")), "artifact required")
    add("independent-review-ran", bool(state["reviews"]), "no review verdict recorded")

    if state["reviews"]:
        last = state["reviews"][-1]
        add("final-review-approved", last["status"] in PASSING_REVIEW_STATUSES, last["status"])
        add(
            "final-review-covers-open-round",
            last["round"] == state["review_round"],
            f"newest verdict is for round {last['round']} but round {state['review_round']} "
            "is open; the orchestrator may not deliver on an earlier round's independence",
        )
        try:
            current_digest = _document_digest(tdd)
        except ControllerError as exc:
            current_digest = ""
            add("tdd-readable", False, str(exc))
        add(
            "review-report-matches-delivered-tdd",
            bool(current_digest) and current_digest == last["tdd_digest"],
            (
                f"round {last['round']} reviewed {last['tdd_digest'][:12]} but the delivered "
                f"TDD is {current_digest[:12]}; edits after a verdict need a Refiner round and "
                "a fresh reviewer — the orchestrator editing the TDD is not a disposition"
                if current_digest and current_digest != last["tdd_digest"]
                else last["tdd_digest"][:18]
            ),
        )
    else:
        add("tdd-readable", tdd.is_file(), str(tdd))

    add(
        "review-round-within-cap",
        1 <= state["review_round"] <= state["max_review_rounds"],
        state["review_round"],
    )

    unclosed = [
        item["id"]
        for item in state["findings"]
        if item["must_fix"] and not _closed(state, item["id"])
    ]
    add("blocking-findings-closed", not unclosed, unclosed or "all Blocker/High closed")
    untracked = [
        item["id"]
        for item in state["findings"]
        if not any(fix["finding_id"] == item["id"] for fix in state["fixes"])
    ]
    add("every-finding-has-a-disposition", not untracked, untracked or "complete")

    if state["mode"] == "UPDATE":
        add("previous-tdd-archived", bool(state["history"]), "UPDATE run needs a history snapshot")

    document_checks: list[dict[str, Any]] = []
    if tdd.is_file():
        doc_text = tdd.read_text(encoding="utf-8")
        document_checks = _scan_document(doc_text, state)
        checks.extend(document_checks)
        missing, stale = _ledger_problems(state, doc_text, str(path.parent))
        add("ledger-blocks-present", not missing, missing or "generated blocks present")
        add("ledger-matches-state", not stale, stale or "chapter 16 mirrors state.json")
        ok, detail = _scale_chapter_check(path, state, tdd, doc_text)
        add("scale-chapters", ok, detail)
        if state["reviews"]:
            ok, detail = _incremental_coverage(path, state, tdd)
            add("incremental-review-covers-changes", ok, detail)
    else:
        add("tdd-exists", False, str(tdd))

    failures = [item for item in checks if not item["ok"]]
    if state["degraded"]:
        status = "PARTIAL_DEGRADED"
    else:
        status = "PASS" if not failures else "BLOCKED"
    report = {
        "status": status,
        "review_round": state["review_round"],
        "mode": state["mode"],
        "degraded": state["degraded"],
        "degradations": state["degradations"],
        "gaps": state.get("input_gaps", []),
        # Both are read-only here on purpose: they tell the caller why the next round is scoped
        # and which chapters the scale probe will demand, without either becoming a gate failure.
        "review_stop": _severity_stop(state),
        "scale": {
            key: value
            for key, value in (state.get("scale") or {}).items()
            if key in {"scale", "signals", "greenfield", "enforcement"}
        } or None,
        "passed": [item["name"] for item in checks if item["ok"]],
        "failures": [item["name"] for item in failures],
        "checks": checks,
        "evaluated_at": _now(),
    }
    state["gates"].append(report)
    _save(path, state)
    return report


def _cached_index(state_path: Path) -> dict[str, Any] | None:
    """The index file as it stands on disk, without reading or rebuilding the TDD.

    ``show`` runs on every status check, so it must not turn into a second reason to page
    through the document; a missing or unreadable index is simply reported as missing.
    """
    cached = _index_path(state_path)
    if not cached.is_file():
        return None
    try:
        index = json.loads(cached.read_text(encoding="utf-8"))
        return {**_index_summary(index), "path": str(cached)}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unreadable", "path": str(cached)}


def show(state_path: str | Path) -> dict[str, Any]:
    path, state = load_state(state_path)
    minutes: dict[str, float] = {}
    for item in state["agents"]:
        wall = item.get("wall_seconds")
        if wall is None:
            continue
        phase = PHASE_OF_ROLE.get(item["role"], "other")
        minutes[phase] = minutes.get(phase, 0.0) + wall / 60.0
    timed = sum(1 for item in state["agents"] if item.get("wall_seconds") is not None)
    probe = state.get("scale")
    pending = state.get("pending_review")
    return {
        "stage": state["stage"],
        "mode": state["mode"],
        "review_round": f"{state['review_round']}/{state['max_review_rounds']}",
        "input_status": state["input_status"],
        "degraded": state["degraded"],
        "sources": len(state["sources"]),
        "agents": [f"{item['role']}#{item['round']}:{item['status']}" for item in state["agents"]],
        "artifacts": [f"{item['kind']}@{item['round']}" for item in state["artifacts"]],
        "findings": {
            "open": sum(1 for item in state["findings"] if item["status"] == "OPEN"),
            "total": len(state["findings"]),
        },
        "reviews": [
            f"r{item['round']}:{item['status']}({item.get('scope') or 'full'})"
            for item in state["reviews"]
        ],
        # The scope the controller derived for an open round, so a caller reading status sees the
        # same section set the reviewer was handed rather than recomputing it.
        "pending_review": (
            {key: pending[key] for key in ("round", "scope", "baseline_round", "over_budget")}
            | {"sections": len(pending.get("sections") or [])}
            if pending
            else None
        ),
        "review_stop": _severity_stop(state),
        "scale": (
            {key: probe[key] for key in ("scale", "signal_count", "greenfield", "enforcement")}
            if probe
            else None
        ),
        "history": [item["path"] for item in state["history"]],
        "ledger": (
            f"{state['ledger']['sections']} @ {state['ledger']['rendered_at'][:19]}"
            if state.get("ledger")
            else None
        ),
        "last_gate": state["gates"][-1]["status"] if state["gates"] else None,
        # ``prep`` is context loading, ``write`` is Architect plus every Refiner round,
        # ``audit`` is Critic plus every independent review: the three buckets the run's
        # wall time actually splits into.
        "minutes_by_phase": {key: round(value, 1) for key, value in sorted(minutes.items())},
        "dispatches": {
            "total": len(state["agents"]),
            "timed": timed,
            "aborted": sum(1 for item in state["agents"] if item["status"] == "ABORTED"),
            "open": sum(1 for item in state["agents"] if item["status"] == "DISPATCHED"),
            # A dispatch that was handed a section scope carries the read budget the index
            # derived for it; an over-budget one is the signal to split the round instead of
            # letting the role page through the document itself.
            "scoped": sum(1 for item in state["agents"] if (item.get("budget") or {}).get("sections")),
            "over_budget": [
                f"{item['role']}#{item['round']}:{item['budget']['tokens_estimated']}"
                for item in state["agents"]
                if (item.get("budget") or {}).get("over_budget")
            ],
        },
        "context_window": state.get("context_window") or DEFAULT_CONTEXT_WINDOW,
        "doc_index": _cached_index(path),
    }


def _fixer_commands(sub: argparse._SubParsersAction) -> None:
    init = sub.add_parser("init", help="freeze inputs and open the evidence directory")
    init.add_argument("--repo-root", required=True)
    init.add_argument("--feature-directory", required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="the host model's real context window in tokens (default 200000); scales every --scope budget",
    )
    init.add_argument(
        "--review-stop-policy",
        choices=REVIEW_STOP_POLICIES,
        default=REVIEW_STOP_POLICIES[0],
        help="'severity' (default) scopes a review round once two rounds surface no Blocker/High; "
        "'fixed' restores the unconditional 1-5 round cap",
    )
    init.add_argument(
        "--scale-enforcement",
        choices=SCALE_ENFORCEMENTS,
        default=SCALE_ENFORCEMENTS[0],
        help="'enforce' (default) makes the scale probe decide which chapters gate demands; "
        "'record-only' probes and reports without gate effect",
    )

    source = sub.add_parser("record-source", help="register one frozen input item")
    source.add_argument("--state", required=True)
    source.add_argument("--source-id", required=True)
    source.add_argument("--kind", required=True, choices=SOURCE_KINDS)
    source.add_argument("--locator", required=True)
    source.add_argument("--status", required=True, choices=READ_STATUSES)
    source.add_argument("--revision", default="")
    source.add_argument("--summary", required=True)
    source.add_argument("--confidence", required=True, choices=CONFIDENCES)

    finalize = sub.add_parser("finalize-inputs", help="set input_status")
    finalize.add_argument("--state", required=True)
    finalize.add_argument("--status", required=True, choices=INPUT_STATUSES)
    finalize.add_argument("--reason", required=True)
    finalize.add_argument(
        "--prd-changes",
        default=None,
        help="UPDATE runs only: 'modified/added/deleted' PRD line counts, e.g. 3/1/0",
    )

    stage = sub.add_parser("start-stage", help="advance the stage state machine")
    stage.add_argument("--state", required=True)
    stage.add_argument("--to", required=True, choices=sorted(STAGE_TRANSITIONS))
    stage.add_argument(
        "--review-scope",
        default=None,
        choices=REVIEW_SCOPES,
        help="REVIEW only: 'incremental' asks for the derived what-moved scope, which the "
        "controller grants only after the severity stop; omit it and the controller decides",
    )

    agent = sub.add_parser("record-agent", help="log one delegated subagent call")
    agent.add_argument("--state", required=True)
    agent.add_argument("--role", required=True, choices=sorted(ROLES.values()))
    agent.add_argument("--name", required=True)
    agent.add_argument("--session-id", required=True)
    # Required on the DISPATCHED call that opens the window; a closing call may omit it and
    # inherits the digest recorded at dispatch time.
    agent.add_argument("--input-digest", default="")
    agent.add_argument("--status", required=True, choices=AGENT_STATUSES)
    agent.add_argument(
        "--scope",
        default="",
        help="section keys injected into this dispatch (e.g. '7.2,4.2'); the read budget is derived from doc-index.json",
    )

    artifact = sub.add_parser("write-artifact", help="persist a stage output under the run directory")
    artifact.add_argument("--state", required=True)
    artifact.add_argument("--kind", required=True, choices=ARTIFACT_KINDS)
    artifact.add_argument("--file", required=True)
    artifact.add_argument("--role", default=None)
    artifact.add_argument("--round", type=int, default=None, dest="round_override")

    finding = sub.add_parser("record-finding", help="add a Critic or Reviewer finding")
    finding.add_argument("--state", required=True)
    finding.add_argument("--source", required=True, choices=("CRITIC", "REVIEWER"))
    finding.add_argument("--id", required=True, dest="finding_id")
    finding.add_argument("--severity", required=True, choices=SEVERITIES)
    finding.add_argument("--location", required=True)
    finding.add_argument("--evidence", required=True)
    finding.add_argument("--must-fix", choices=("true", "false"), default=None)
    finding.add_argument("--round", type=int, default=None, dest="round_override")

    fix = sub.add_parser("record-fix", help="map one finding to a disposition")
    fix.add_argument("--state", required=True)
    fix.add_argument("--finding-id", required=True)
    fix.add_argument("--status", required=True, choices=FIX_STATUSES)
    fix.add_argument("--location", required=True)
    fix.add_argument("--change", required=True)

    review = sub.add_parser("record-review", help="bind a verdict to the reviewed TDD snapshot")
    review.add_argument("--state", required=True)
    review.add_argument("--status", required=True, choices=REVIEW_STATUSES)
    review.add_argument("--report", required=True)
    review.add_argument("--tdd-file", required=True)
    review.add_argument("--session-id", required=True)
    review.add_argument(
        "--scope",
        default=None,
        choices=REVIEW_SCOPES,
        help="only to confirm the scope start-stage opened; a verdict may not re-label it",
    )

    degraded = sub.add_parser("degrade", help="record that a role could not be dispatched")
    degraded.add_argument("--state", required=True)
    degraded.add_argument("--role", required=True)
    degraded.add_argument("--reason", required=True)

    history = sub.add_parser("archive-history", help="store the superseded TDD as a file")
    history.add_argument("--state", required=True)
    history.add_argument("--version", required=True)

    gate_cmd = sub.add_parser("gate", help="run the deterministic delivery gate")
    gate_cmd.add_argument("--state", required=True)
    gate_cmd.add_argument("--tdd-file", required=True)

    ledger = sub.add_parser(
        "render-ledger", help="write the chapter 16 blocks from state.json"
    )
    ledger.add_argument("--state", required=True)
    ledger.add_argument("--tdd-file", required=True)

    index_cmd = sub.add_parser(
        "index", help="rebuild doc-index.json (section ranges, symbols, finding locations)"
    )
    index_cmd.add_argument("--state", required=True)
    index_cmd.add_argument("--tdd-file", default=None, help="defaults to the path init froze")

    locate_cmd = sub.add_parser(
        "locate", help="resolve a finding location or section number into line ranges"
    )
    locate_cmd.add_argument("--state", required=True)
    locate_cmd.add_argument("--tdd-file", default=None, help="defaults to the path init froze")
    locate_cmd.add_argument("--finding", default=None, dest="finding_id")
    locate_cmd.add_argument("--section", default=None)

    shown = sub.add_parser("show", help="print a compact run summary")
    shown.add_argument("--state", required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    _fixer_commands(sub)
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            # 带 ``STATE=`` 前缀是为了让主 Agent 能直接读出状态文件路径：Skill 命令块
            # 不能用 ``$(...)``/``$PWD`` 接 stdout（cmd.exe 与 PowerShell 都不支持），
            # 所以输出必须自解释、可按行读取。
            state_path = init_run(
                args.repo_root, args.feature_directory, args.run_id, args.context_window,
                args.review_stop_policy, args.scale_enforcement,
            )
            print(f"STATE={state_path}")
        elif args.command == "record-source":
            print(json.dumps(record_source(
                args.state, args.source_id, args.kind, args.locator,
                args.status, args.revision, args.summary, args.confidence,
            ), ensure_ascii=False))
        elif args.command == "finalize-inputs":
            print(json.dumps(
                finalize_inputs(args.state, args.status, args.reason, args.prd_changes),
                ensure_ascii=False,
            ))
        elif args.command == "start-stage":
            print(json.dumps(
                start_stage(args.state, args.to, args.review_scope), ensure_ascii=False
            ))
        elif args.command == "record-agent":
            entry = record_agent(
                args.state, args.role, args.name, args.session_id,
                args.input_digest, args.status, args.scope,
            )
            # Only the derived fields come back: role, name and session are ones the caller
            # just typed, and echoing them would charge tokens for information nobody lacks.
            out = {
                key: entry[key]
                for key in ("session_id", "role", "round", "status")
            }
            if entry.get("budget"):
                out["budget"] = entry["budget"]
            print(json.dumps(out, ensure_ascii=False))
        elif args.command == "write-artifact":
            print(json.dumps(
                write_artifact(args.state, args.kind, args.file, args.role, args.round_override),
                ensure_ascii=False,
            ))
        elif args.command == "record-finding":
            must_fix = None if args.must_fix is None else args.must_fix == "true"
            print(json.dumps(
                record_finding(
                    args.state, args.source, args.finding_id, args.severity,
                    args.location, args.evidence, must_fix, args.round_override,
                ),
                ensure_ascii=False,
            ))
        elif args.command == "record-fix":
            record_fix(args.state, args.finding_id, args.status, args.location, args.change)
        elif args.command == "record-review":
            print(json.dumps(
                record_review(
                    args.state, args.status, args.report, args.tdd_file,
                    args.session_id, args.scope,
                ),
                ensure_ascii=False,
            ))
        elif args.command == "degrade":
            print(json.dumps(degrade(args.state, args.role, args.reason), ensure_ascii=False))
        elif args.command == "archive-history":
            print(json.dumps(archive_history(args.state, args.version), ensure_ascii=False))
        elif args.command == "render-ledger":
            print(json.dumps(render_ledger(args.state, args.tdd_file), ensure_ascii=False))
        elif args.command == "index":
            print(json.dumps(doc_index(args.state, args.tdd_file), ensure_ascii=False))
        elif args.command == "locate":
            # ``NOT_LOCATED`` exits 0 on purpose: it is an answer that tells the caller to read
            # wider, not a gate failure, and a non-zero code would put it on the same path as
            # "this run may not proceed".
            print(json.dumps(locate(args.state, args.tdd_file, args.finding_id, args.section), ensure_ascii=False))
        elif args.command == "gate":
            report = gate(args.state, args.tdd_file)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["status"] == "BLOCKED":
                return 2
        elif args.command == "show":
            print(json.dumps(show(args.state), ensure_ascii=False, indent=2))
        return 0
    except (ControllerError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
