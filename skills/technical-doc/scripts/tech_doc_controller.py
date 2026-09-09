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
AGENT_STATUSES = ("COMPLETED", "BLOCKED")
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
    "需求到设计",
    "关键技术决策记录",
    "风险登记与工作量评估",
    "审查闭环记录",
)
UPDATE_ONLY_ANCHORS = ("历史版本索引",)


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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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


def init_run(root: str | Path, feature_directory: str | Path, run_id: str) -> Path:
    """Freeze the inputs for one TDD run and open the evidence directory."""
    repo = _require_repo_root(root)
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
        "degradations": [],
        "history": [],
        "gates": [],
    }
    _atomic_json(target / "state.json", state)
    return target / "state.json"


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
    entry = {
        "source_id": source_id,
        "kind": kind,
        "locator": locator,
        "read_status": status,
        "revision": revision.strip() or "unversioned",
        "summary": summary,
        "confidence": confidence,
        "recorded_at": _now(),
    }
    state["sources"].append(entry)
    _save(path, state)
    return entry


def finalize_inputs(state_path: str | Path, status: str, reason: str) -> dict[str, Any]:
    """Set ``input_status`` and refuse to let a missing PRD be graded as usable."""
    path, state = load_state(state_path)
    if status not in INPUT_STATUSES:
        raise ControllerError(f"input_status must be one of {', '.join(INPUT_STATUSES)}")
    if not reason.strip():
        raise ControllerError("--reason is required so the gap is handed to later stages")
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
    _save(path, state)
    return {"input_status": status, "gaps": unread, "reason": reason}


def start_stage(state_path: str | Path, target_stage: str) -> dict[str, Any]:
    """Advance the workflow, enforcing every prerequisite the prose used to claim."""
    path, state = load_state(state_path)
    target = target_stage.upper()
    current = state["stage"]
    if target not in STAGE_TRANSITIONS:
        raise ControllerError(f"unknown stage: {target_stage}")
    if target not in STAGE_TRANSITIONS[current]:
        allowed = ", ".join(sorted(STAGE_TRANSITIONS[current])) or "(terminal)"
        raise ControllerError(f"illegal transition {current} -> {target}; allowed: {allowed}")

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
        state["review_round"] = round_ + 1
    elif target == "DELIVERY":
        _require_approved_review(state)
    elif target == "CLOSED":
        _require_gate_pass(state)

    state["stage"] = target
    state.setdefault("stage_history", []).append(
        {"stage": target, "review_round": state["review_round"], "at": _now()}
    )
    _save(path, state)
    return {"stage": target, "review_round": state["review_round"], "mode": state["mode"]}


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
    if last["status"] not in PASSING_REVIEW_STATUSES:
        raise ControllerError(
            f"review round {last['round']} returned {last['status']}; only "
            f"{', '.join(PASSING_REVIEW_STATUSES)} may reach delivery"
        )


def _require_gate_pass(state: dict[str, Any]) -> None:
    if not state["gates"] or state["gates"][-1]["status"] not in {"PASS", "PARTIAL_DEGRADED"}:
        raise ControllerError("the delivery gate has not passed; run 'gate' before closing")


def record_agent(
    state_path: str | Path, role: str, name: str, session_id: str, input_digest: str, status: str
) -> dict[str, Any]:
    """Log one delegated agent call, keyed by round so the cap is auditable."""
    path, state = load_state(state_path)
    if role not in ROLES.values():
        raise ControllerError(f"unsupported role: {role} (expected {', '.join(sorted(ROLES.values()))})")
    if status not in AGENT_STATUSES:
        raise ControllerError(f"agent status must be one of {', '.join(AGENT_STATUSES)}")
    for label, value in (("name", name), ("session-id", session_id), ("input-digest", input_digest)):
        if not value.strip():
            raise ControllerError(f"{label} is required")
    round_ = state["review_round"] if role == ROLES["REVIEW"] else 0
    duplicate = next(
        (item for item in state["agents"] if item["session_id"] == session_id), None
    )
    if duplicate is not None:
        raise ControllerError(f"agent session already recorded: {session_id}")
    if role == ROLES["REVIEW"]:
        if round_ < 1:
            raise ControllerError("no review round is open; enter the REVIEW stage first")
        if _completed_agents(state, role, round_):
            raise ControllerError(
                f"a {role} instance already ran in round {round_}; each round needs a fresh reviewer"
            )
    entry = {
        "role": role,
        "name": name,
        "session_id": session_id,
        "input_digest": input_digest,
        "status": status,
        "round": round_,
        "recorded_at": _now(),
    }
    state["agents"].append(entry)
    _save(path, state)
    return entry


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


def record_fix(
    state_path: str | Path,
    finding_id: str,
    status: str,
    location: str,
    change: str,
    verification: str,
) -> dict[str, Any]:
    """Map one Finding to a disposition; the Refiner may not silently drop findings."""
    path, state = load_state(state_path)
    if status not in FIX_STATUSES:
        raise ControllerError(f"fix status must be one of {', '.join(FIX_STATUSES)}")
    finding = next((item for item in state["findings"] if item["id"] == finding_id), None)
    if finding is None:
        raise ControllerError(f"unknown finding id: {finding_id} (record the finding first)")
    for label, value in (("location", location), ("change", change), ("verification", verification)):
        if not value.strip():
            raise ControllerError(f"{label} is required")
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
        "verification": verification,
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
    state_path: str | Path, status: str, report: str | Path, tdd_file: str | Path, session_id: str
) -> dict[str, Any]:
    """Bind a review verdict to the exact TDD bytes that were reviewed."""
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
    repo = _require_repo_root(state["repository"]["root"])
    report_path, report_rel = _inside(repo, report)
    text = _read_document(report_path, "review report")
    _assert_substantive(text, "review report", report_path)
    tdd = Path(tdd_file).expanduser().resolve()
    tdd_text = _read_document(tdd, "TDD")
    entry = {
        "round": round_,
        "status": status,
        "session_id": session_id,
        "report": report_rel,
        "report_digest": _text_digest(text),
        "tdd_digest": _text_digest(tdd_text),
        "recorded_at": _now(),
    }
    state["reviews"].append(entry)
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
        try:
            current_digest = _text_digest(_read_document(tdd, "TDD"))
        except ControllerError as exc:
            current_digest = ""
            add("tdd-readable", False, str(exc))
        add(
            "review-report-matches-delivered-tdd",
            bool(current_digest) and current_digest == last["tdd_digest"],
            f"round {last['round']} reviewed a different snapshot"
            if current_digest and current_digest != last["tdd_digest"]
            else last["tdd_digest"][:18],
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
        document_checks = _scan_document(tdd.read_text(encoding="utf-8"), state)
        checks.extend(document_checks)
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
        "passed": [item["name"] for item in checks if item["ok"]],
        "failures": [item["name"] for item in failures],
        "checks": checks,
        "evaluated_at": _now(),
    }
    state["gates"].append(report)
    _save(path, state)
    return report


def show(state_path: str | Path) -> dict[str, Any]:
    _, state = load_state(state_path)
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
        "reviews": [f"r{item['round']}:{item['status']}" for item in state["reviews"]],
        "history": [item["path"] for item in state["history"]],
        "last_gate": state["gates"][-1]["status"] if state["gates"] else None,
    }


def _fixer_commands(sub: argparse._SubParsersAction) -> None:
    init = sub.add_parser("init", help="freeze inputs and open the evidence directory")
    init.add_argument("--repo-root", required=True)
    init.add_argument("--feature-directory", required=True)
    init.add_argument("--run-id", required=True)

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

    stage = sub.add_parser("start-stage", help="advance the stage state machine")
    stage.add_argument("--state", required=True)
    stage.add_argument("--to", required=True, choices=sorted(STAGE_TRANSITIONS))

    agent = sub.add_parser("record-agent", help="log one delegated subagent call")
    agent.add_argument("--state", required=True)
    agent.add_argument("--role", required=True, choices=sorted(ROLES.values()))
    agent.add_argument("--name", required=True)
    agent.add_argument("--session-id", required=True)
    agent.add_argument("--input-digest", required=True)
    agent.add_argument("--status", required=True, choices=AGENT_STATUSES)

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
    fix.add_argument("--verification", required=True)

    review = sub.add_parser("record-review", help="bind a verdict to the reviewed TDD snapshot")
    review.add_argument("--state", required=True)
    review.add_argument("--status", required=True, choices=REVIEW_STATUSES)
    review.add_argument("--report", required=True)
    review.add_argument("--tdd-file", required=True)
    review.add_argument("--session-id", required=True)

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

    shown = sub.add_parser("show", help="print a compact run summary")
    shown.add_argument("--state", required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    _fixer_commands(sub)
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            print(init_run(args.repo_root, args.feature_directory, args.run_id))
        elif args.command == "record-source":
            record_source(
                args.state, args.source_id, args.kind, args.locator,
                args.status, args.revision, args.summary, args.confidence,
            )
        elif args.command == "finalize-inputs":
            print(json.dumps(finalize_inputs(args.state, args.status, args.reason), ensure_ascii=False))
        elif args.command == "start-stage":
            print(json.dumps(start_stage(args.state, args.to), ensure_ascii=False))
        elif args.command == "record-agent":
            record_agent(
                args.state, args.role, args.name, args.session_id, args.input_digest, args.status
            )
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
            record_fix(args.state, args.finding_id, args.status, args.location, args.change, args.verification)
        elif args.command == "record-review":
            print(json.dumps(
                record_review(args.state, args.status, args.report, args.tdd_file, args.session_id),
                ensure_ascii=False,
            ))
        elif args.command == "degrade":
            print(json.dumps(degrade(args.state, args.role, args.reason), ensure_ascii=False))
        elif args.command == "archive-history":
            print(json.dumps(archive_history(args.state, args.version), ensure_ascii=False))
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
