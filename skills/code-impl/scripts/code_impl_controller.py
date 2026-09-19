#!/usr/bin/env python3
"""Deterministic state, snapshot, scope, and contract gates for code-impl.

The workflow is still orchestrated by the host agent, but all evidence that
can be checked without model judgment is persisted under ``.codekit`` and
validated here.  The directory is intentionally ignored by the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROLES = {"impl-planner", "impl-implementer", "impl-debugger", "code-review"}
ROOT_CAUSES = {
    "CODE_OR_TEST",
    "TASK_PLAN",
    "SPEC_OR_ARCH",
    "ENVIRONMENT",
    "PERMISSION",
    "PREEXISTING_OR_FLAKY",
    "UNKNOWN",
}

# P0-1: Task state machine — the controller enforces valid transitions.
# Terminal states: PASS, BLOCKED, SUPERSEDED (no outgoing edges).
# FAILED → RUNNING enables bounded retry (retry budget tracked in Task Ledger).
TASK_STATE_TRANSITIONS: dict[str, set[str]] = {
    "PENDING":    {"READY", "SUPERSEDED"},
    "READY":      {"RUNNING", "BLOCKED", "SUPERSEDED"},
    "RUNNING":    {"VALIDATING", "FAILED", "BLOCKED"},
    "VALIDATING": {"PASS", "FAILED", "BLOCKED"},
    "FAILED":     {"RUNNING", "SUPERSEDED"},
    "PASS":       set(),
    "BLOCKED":    set(),
    "SUPERSEDED": set(),
}

# HIGH-1: SKILL.md budgets become mechanical enforcement instead of prose.
# MAX_FINGERPRINT_ATTEMPTS counts record-failure occurrences of one error
# fingerprint: the initial failure plus at most 3 retries (step 10 "同一错误指纹
# 最多重试 3 次"). The 5th occurrence of the same fingerprint forces
# disposition=human_intervention / resolution=ATTEMPT_CAP.
MAX_FINGERPRINT_ATTEMPTS = 4
# MAX_REVIEW_FREEZES caps freeze-review calls: initial review + 2 repair
# rounds (step 15 "审查修复最多 2 轮"); the 4th freeze raises and must route
# to step 17 (人工介入).
MAX_REVIEW_FREEZES = 3


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


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    """Run a git subcommand and return stdout.

    Any non-zero exit is surfaced as a ControllerError containing the failing
    command, exit code and a truncated stderr excerpt, so callers (and the
    top-level BLOCKED handler) produce an actionable message instead of an
    opaque CalledProcessError repr.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # encoding 只能在文本模式下传：带上它会强制开启文本模式，二进制调用
            # （``binary=True``）会因此拿到 str 而不是 bytes，把 diff 字节流改坏。
            **({"text": True, "encoding": "utf-8", "errors": "replace"} if not binary else {"text": False}),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if exc.stderr is not None else b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        excerpt = stderr.strip().splitlines()
        tail = " | ".join(excerpt[-3:]) if excerpt else "(no stderr)"
        raise ControllerError(
            f"git {' '.join(args)} failed in {root} (exit {exc.returncode}): {tail}"
        ) from exc
    return result.stdout


def _require_repo_root(path: str | Path) -> Path:
    """Resolve and validate a repository root directory.

    Named to avoid shadowing with the common local variable ``root`` used at
    every call site, and to signal that the function raises on invalid input
    rather than returning None.
    """
    value = Path(path).expanduser().resolve()
    if not value.is_dir():
        raise ControllerError(f"repository root is not a directory: {value}")
    return value


def _inside(root: Path, path: str | Path, *, allow_root: bool = False) -> tuple[Path, str]:
    raw = Path(path).expanduser()
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ControllerError(f"path escapes repository root: {candidate}") from exc
    if not allow_root and not relative.parts:
        raise ControllerError("path must be below repository root")
    return candidate, relative.as_posix() if relative.parts else "."


def _path_digest(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "non-file"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _status_entries(root: Path) -> dict[str, str]:
    raw = _git(root, "status", "--porcelain=v1", "-z", binary=True)
    assert isinstance(raw, bytes)
    tokens = raw.split(b"\0")
    entries: dict[str, str] = {}
    index = 0
    while index < len(tokens) and tokens[index]:
        token = tokens[index].decode("utf-8", errors="surrogateescape")
        if len(token) < 4:
            index += 1
            continue
        status, path = token[:2], token[3:]
        if path.rstrip("/") != ".codekit" and not path.startswith(".codekit/"):
            entries[path] = status
        if "R" in status or "C" in status:
            index += 1
            if index < len(tokens) and tokens[index]:
                renamed = tokens[index].decode("utf-8", errors="surrogateescape")
                if renamed.rstrip("/") != ".codekit" and not renamed.startswith(".codekit/"):
                    entries[renamed] = status
        index += 1
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    assert isinstance(untracked, bytes)
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if path.rstrip("/") == ".codekit" or path.startswith(".codekit/"):
            continue
        if path.endswith("/"):
            entries.pop(path, None)
        else:
            for directory in list(entries):
                if directory.endswith("/") and path.startswith(directory):
                    entries.pop(directory, None)
            entries[path] = "??"
    return entries


def dirty_snapshot(root: Path) -> dict[str, Any]:
    """Capture a stable, content-addressed view of tracked and untracked changes."""
    entries = _status_entries(root)
    files = []
    for relative, status in sorted(entries.items()):
        path = root / relative
        files.append({"path": relative, "status": status, "digest": _path_digest(path)})
    result = {
        "head_sha": str(_git(root, "rev-parse", "HEAD")).strip(),
        "files": files,
    }
    result["digest"] = _json_digest(result)
    return result


def _document_snapshot(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControllerError(f"{label} is missing or not a regular file: {path}")
    text = path.read_text(encoding="utf-8")
    meaningful = [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in {"---", "```"}]
    if len("".join(meaningful)) < 20:
        raise ControllerError(f"{label} has no substantive content: {path}")
    placeholder = re.compile(r"^(?:TODO|TBD|待填写|待补充|<[^>]+>)$", re.IGNORECASE)
    if any(placeholder.fullmatch(line) for line in meaningful):
        raise ControllerError(f"{label} contains a template or placeholder line: {path}")
    return {
        "label": label,
        "path": str(path),
        "content_digest": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size": path.stat().st_size,
    }


def _run_dir(root: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ControllerError("run-id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    return root / ".codekit" / "code-impl" / run_id


def load_state(path: str | Path) -> tuple[Path, dict[str, Any]]:
    state_path = Path(path).expanduser().resolve()
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"cannot read state file: {state_path}: {exc}") from exc
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ControllerError(f"unsupported state schema: {state.get('schema_version')}")
    return state_path, state


def init_run(root: Path, feature_directory: str | Path, run_id: str) -> Path:
    root = _require_repo_root(root)
    feature, feature_rel = _inside(root, feature_directory)
    if not feature.is_dir():
        raise ControllerError(f"feature-directory is not a directory: {feature}")
    docs = {
        "product_requirements": _document_snapshot(feature / "product-doc.md", "PRD"),
        "technical_design": _document_snapshot(feature / "technical-doc.md", "TDD"),
        "release_plan": _document_snapshot(feature / "release-doc.md", "RPD"),
    }
    target = _run_dir(root, run_id)
    if target.exists():
        raise ControllerError(f"run already exists; resume it instead of overwriting: {target}")
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": _now(),
        "phase": "INPUT_FROZEN",
        "repository": {
            "root": str(root),
            "base_sha": str(_git(root, "rev-parse", "HEAD")).strip(),
            "branch": str(_git(root, "branch", "--show-current")).strip(),
        },
        "feature_directory": {"absolute": str(feature), "relative": feature_rel},
        "materials": docs,
        "preexisting_snapshot": dirty_snapshot(root),
        "agents": [],
        "tasks": [],
        "authorized_files": [],
        "current_plan_version": 1,
        "failures": [],
        "scope_checks": [],
        "review_snapshots": [],
    }
    target.mkdir(parents=True)
    _atomic_json(target / "state.json", state)
    return target / "state.json"


def _append_agent(
    state: dict[str, Any],
    role: str,
    name: str,
    session_id: str,
    input_digest: str,
    status: str,
) -> dict[str, Any]:
    """Register or update one agent entry inside ``state`` (no writes).

    Shared by record_agent and the dispatch-task composite so both paths keep
    the same metadata-immutability rule: a reused session id may only change
    status, never role/name/input_digest.
    """
    if role not in ROLES:
        raise ControllerError(f"unsupported role: {role}")
    if not all(value.strip() for value in (name, session_id, input_digest, status)):
        raise ControllerError("agent name, session_id, input_digest, and status are required")
    existing = next((item for item in state["agents"] if item["session_id"] == session_id), None)
    if existing is not None:
        metadata = {"role": role, "name": name, "input_digest": input_digest}
        if any(existing[key] != value for key, value in metadata.items()):
            raise ControllerError(f"agent session metadata changed: {session_id}")
        existing["status"] = status
        existing["updated_at"] = _now()
        return existing
    entry = {
        "role": role,
        "name": name,
        "session_id": session_id,
        "input_digest": input_digest,
        "status": status,
        "recorded_at": _now(),
    }
    state["agents"].append(entry)
    return entry


def record_agent(state_path: str | Path, role: str, name: str, session_id: str, input_digest: str, status: str) -> None:
    path, state = load_state(state_path)
    _append_agent(state, role, name, session_id, input_digest, status)
    _atomic_json(path, state)


def _require_task(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = next((t for t in state["tasks"] if t["task_id"] == task_id), None)
    if task is None:
        raise ControllerError(
            f"unknown task: {task_id}; register it with record-task before using task commands"
        )
    return task


def record_task(
    state_path: str | Path,
    task_id: str,
    allowed_files: Iterable[str],
    plan_version: int | None = None,
) -> None:
    """Register a task and its write scope.

    P1-3: plan_version ties the task to a specific planning iteration so that
    replan() can supersede old-version tasks and recompute authorized_files.
    """
    path, state = load_state(state_path)
    if not task_id.strip():
        raise ControllerError("task-id is required")
    if any(item["task_id"] == task_id for item in state["tasks"]):
        raise ControllerError(f"duplicate task: {task_id}")
    root = _require_repo_root(state["repository"]["root"])
    normalized = sorted({_inside(root, item)[1] for item in allowed_files})
    if not normalized:
        raise ControllerError("a task must declare at least one allowed file")
    pv = plan_version if plan_version is not None else state.get("current_plan_version", 1)
    state["tasks"].append({
        "task_id": task_id,
        "allowed_files": normalized,
        "status": "PENDING",
        "plan_version": pv,
    })
    _atomic_json(path, state)


def update_task(state_path: str | Path, task_id: str, status: str) -> dict[str, Any]:
    """Transition a task to a new status, enforcing TASK_STATE_TRANSITIONS (P0-1).

    Valid transitions:
        PENDING    → READY | SUPERSEDED
        READY      → RUNNING | BLOCKED | SUPERSEDED
        RUNNING    → VALIDATING | FAILED | BLOCKED
        VALIDATING → PASS | FAILED | BLOCKED
        FAILED     → RUNNING (retry) | SUPERSEDED
        PASS / BLOCKED / SUPERSEDED  are terminal.
    """
    path, state = load_state(state_path)
    task = _require_task(state, task_id)
    current = task.get("status", "PENDING")
    allowed = TASK_STATE_TRANSITIONS.get(current, set())
    if status not in allowed:
        raise ControllerError(
            f"invalid transition for {task_id}: {current} → {status}; "
            f"allowed: {sorted(allowed) if allowed else '(terminal state, no transitions)'}"
        )
    task["status"] = status
    task["updated_at"] = _now()
    _atomic_json(path, state)
    return task


def _compute_scope_result(
    root: Path,
    preexisting: dict[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    """Pure scope-check computation (P1-2).

    Extracted so that freeze_review can call it without the write-side-effects
    of check_scope.  Also fixes:
      - P0-2: authorised preexisting-file changes appear in authorized_overlaps
        and do NOT contribute to the BLOCKED verdict.
      - P2-1: deleted files are reported in deleted_files, not new_files.
    """
    baseline = {item["path"]: item for item in preexisting["files"]}
    current = dirty_snapshot(root)
    current_files = {item["path"]: item for item in current["files"]}

    def _changed(rel: str) -> bool:
        return current_files.get(rel, {}).get("digest") != baseline[rel]["digest"]

    # Preexisting files whose content changed and are NOT in the task scope.
    preexisting_modified = sorted(
        rel for rel in baseline if rel not in allowed and _changed(rel)
    )
    # Preexisting files whose content changed but ARE in the task scope
    # (intentional overlap, already approved by human intervention or
    # explicitly included by the Orchestrator in record-task).
    authorized_overlaps = sorted(
        rel for rel in baseline if rel in allowed and _changed(rel)
    )

    # Files that appeared after init (new or deleted by this run)
    outside_baseline = set(current_files) - set(baseline)
    new_files = sorted(
        rel for rel in outside_baseline if "D" not in current_files[rel]["status"]
    )
    deleted_files = sorted(
        rel for rel in outside_baseline if "D" in current_files[rel]["status"]
    )
    unauthorized_files = sorted(outside_baseline - allowed)

    return {
        "checked_at": _now(),
        "current_digest": current["digest"],
        "allowed_files": sorted(allowed),
        "new_files": new_files,
        "deleted_files": deleted_files,
        "unauthorized_files": unauthorized_files,
        "preexisting_modified": preexisting_modified,
        "authorized_overlaps": authorized_overlaps,
        "result": (
            "PASS"
            if not unauthorized_files and not preexisting_modified
            else "BLOCKED"
        ),
    }


def _task_allowed_set(state: dict[str, Any], task: dict[str, Any]) -> set[str]:
    """Registered write scope of one task plus files already owned by PASS tasks.

    HIGH-2: in a serial worktree the dirty diff is cumulative, so a task-bound
    scope check allows this task's registered files and anything an earlier
    task already delivered (status PASS, any plan version). Files belonging to
    tasks that have not reached PASS stay violations.
    """
    allowed: set[str] = set(task.get("allowed_files", []))
    allowed |= {
        f
        for t in state["tasks"]
        if t.get("status") == "PASS"
        for f in t.get("allowed_files", [])
    }
    return allowed


def check_scope(state_path: str | Path, task_id: str) -> dict[str, Any]:
    """Task-bound scope check persisted to state (HIGH-2).

    The allowed set is derived from the Task Ledger, never from the caller:
    check-scope no longer accepts ad-hoc file grants and no longer widens
    ``authorized_files`` permanently. To add files to a task's write scope,
    call expand-scope with an explicit reason first.
    """
    path, state = load_state(state_path)
    root = _require_repo_root(state["repository"]["root"])
    task = _require_task(state, task_id)
    result = _compute_scope_result(
        root, state["preexisting_snapshot"], _task_allowed_set(state, task)
    )
    result["task_id"] = task_id
    state["scope_checks"].append(result)
    _atomic_json(path, state)
    return result


# States from which a task's registered write scope may still be widened.
# PASS / BLOCKED / SUPERSEDED are terminal and cannot expand.
EXPANDABLE_TASK_STATES = {"PENDING", "READY", "RUNNING", "FAILED"}


def expand_scope(
    state_path: str | Path,
    task_id: str,
    extra_files: Iterable[str],
    reason: str,
) -> dict[str, Any]:
    """Widen one task's registered write scope with an audited reason (HIGH-2).

    Used when the Implementer returns NEEDS_SCOPE: the Orchestrator must call
    expand-scope before re-dispatching; out-of-scope writes stay BLOCKED and
    every expansion is recorded on the task for the final report.
    """
    path, state = load_state(state_path)
    task = _require_task(state, task_id)
    if task.get("status") not in EXPANDABLE_TASK_STATES:
        raise ControllerError(
            f"cannot expand scope for {task_id}: status {task.get('status')} is terminal; "
            f"expansion is only allowed in {sorted(EXPANDABLE_TASK_STATES)}"
        )
    if not reason.strip():
        raise ControllerError("expand-scope requires a non-empty --reason")
    root = _require_repo_root(state["repository"]["root"])
    added: list[str] = []
    for item in extra_files:
        _, relative = _inside(root, item)
        if relative not in task["allowed_files"]:
            task["allowed_files"].append(relative)
            added.append(relative)
    task["allowed_files"] = sorted(set(task["allowed_files"]))
    task.setdefault("scope_expansions", []).append({
        "added_files": sorted(added),
        "reason": reason.strip(),
        "at": _now(),
    })
    task["updated_at"] = _now()
    _atomic_json(path, state)
    return {
        "task_id": task_id,
        "added_files": sorted(added),
        "allowed_files": task["allowed_files"],
    }


def dispatch_task(
    state_path: str | Path,
    task_id: str,
    session_id: str,
    input_digest: str,
    *,
    role: str = "impl-implementer",
) -> dict[str, Any]:
    """Composite dispatch gate for steps 05/06: one call, one state write.

    Moves PENDING → READY → RUNNING (or FAILED → RUNNING for a retry) and
    registers the Implementer agent atomically, replacing the separate
    update-task ×2 + record-agent round-trips. Only registered tasks can be
    dispatched; the retry count is kept on the task ledger entry.
    """
    path, state = load_state(state_path)
    task = _require_task(state, task_id)
    current = task.get("status", "PENDING")
    if current not in {"PENDING", "FAILED"}:
        raise ControllerError(
            f"cannot dispatch {task_id}: status must be PENDING or FAILED (retry), got {current}"
        )
    now = _now()
    for step in (["READY", "RUNNING"] if current == "PENDING" else ["RUNNING"]):
        task["status"] = step
        task["updated_at"] = now
    task["attempts"] = int(task.get("attempts", 0)) + 1
    agent = _append_agent(state, role, role, session_id, input_digest, "RUNNING")
    _atomic_json(path, state)
    return {
        "task_id": task_id,
        "status": task["status"],
        "attempts": task["attempts"],
        "agent": {
            "session_id": agent["session_id"],
            "role": agent["role"],
            "status": agent["status"],
        },
    }


def settle_task(
    state_path: str | Path,
    task_id: str,
    status: str,
    error_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Composite verdict gate for steps 07/08: scope check + VALIDATING → PASS|FAILED.

    RUNNING is first advanced to VALIDATING (idempotent when already there),
    then the task-bound scope check runs and is persisted with the task id
    (and the error fingerprint when given). ``status=PASS`` raises when the
    scope check is BLOCKED and leaves the task in VALIDATING — an out-of-scope
    write can never be settled as PASS; the Orchestrator must fix scope with
    expand-scope, or settle FAILED and record-failure.
    """
    if status not in {"PASS", "FAILED"}:
        raise ControllerError("settle-task status must be PASS or FAILED")
    path, state = load_state(state_path)
    task = _require_task(state, task_id)
    current = task.get("status")
    if current == "RUNNING":
        task["status"] = "VALIDATING"
        task["updated_at"] = _now()
    elif current != "VALIDATING":
        raise ControllerError(
            f"cannot settle {task_id}: status must be RUNNING or VALIDATING, got {current}"
        )
    root = _require_repo_root(state["repository"]["root"])
    scope = _compute_scope_result(
        root, state["preexisting_snapshot"], _task_allowed_set(state, task)
    )
    scope["task_id"] = task_id
    if error_fingerprint:
        scope["error_fingerprint"] = error_fingerprint
    state["scope_checks"].append(scope)
    # Persist the scope evidence even if the PASS verdict is rejected below.
    _atomic_json(path, state)
    if status == "PASS" and scope["result"] != "PASS":
        raise ControllerError(
            f"cannot settle {task_id} as PASS: scope check BLOCKED "
            f"(unauthorized_files={scope['unauthorized_files']}, "
            f"preexisting_modified={scope['preexisting_modified']}); "
            "use expand-scope with a reason or settle FAILED and record-failure"
        )
    task["status"] = status
    task["updated_at"] = _now()
    _atomic_json(path, state)
    return {
        "task_id": task_id,
        "status": status,
        "scope_result": scope["result"],
        "unauthorized_files": scope["unauthorized_files"],
        "preexisting_modified": scope["preexisting_modified"],
    }


def freeze_review(state_path: str | Path) -> dict[str, Any]:
    """Freeze the review snapshot after all tasks pass.

    P1-2: uses _compute_scope_result (pure) — no check_scope side-effect.
    P1-3: derives the allowed set from current-plan tasks + older PASS tasks,
    then falls back to authorized_files for single-task runs without record_task.
    """
    path, state = load_state(state_path)
    root = _require_repo_root(state["repository"]["root"])
    # HIGH-1: freeze budget — initial review + 2 repair rounds (step 15).
    existing_freezes = len(state.get("review_snapshots", []))
    if existing_freezes >= MAX_REVIEW_FREEZES:
        raise ControllerError(
            f"review freeze limit reached ({existing_freezes}/{MAX_REVIEW_FREEZES}): "
            "initial review plus 2 repair rounds is the budget; a further "
            "REQUEST_CHANGES cycle requires human intervention (step 17)"
        )
    baseline_paths = {item["path"] for item in state["preexisting_snapshot"]["files"]}

    # Build allowed set: current-version tasks + older PASS tasks.
    current_pv = state.get("current_plan_version", 1)
    allowed: set[str] = set()
    for task in state.get("tasks", []):
        task_pv = task.get("plan_version", current_pv)
        if task_pv == current_pv or task.get("status") == "PASS":
            allowed.update(task.get("allowed_files", []))
    # Backward-compat: runs initialised before HIGH-2 may still carry
    # authorized_files accumulated by the old check_scope; newer code keeps
    # that list replan-managed only, so reading it here is harmless.
    allowed.update(state.get("authorized_files", []))

    # Pure computation — no state writes from the scope-check itself.
    scope = _compute_scope_result(root, state["preexisting_snapshot"], allowed)
    if scope["result"] != "PASS":
        raise ControllerError(
            f"cannot freeze review: unauthorized_files={scope['unauthorized_files']}, "
            f"preexisting_modified={scope['preexisting_modified']}"
        )
    current = dirty_snapshot(root)
    changed_files = [
        item["path"] for item in current["files"] if item["path"] not in baseline_paths
    ]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "run_id": state["run_id"],
        "base_sha": state["repository"]["base_sha"],
        "head_sha": current["head_sha"],
        "diff_digest": current["digest"],
        "preexisting_diff_snapshot": state["preexisting_snapshot"],
        "changed_files": changed_files,
        "frozen_at": _now(),
    }
    artifact = path.parent / "review-snapshot.json"
    _atomic_json(artifact, snapshot)
    # Keep only a compact reference in state.json; the full snapshot (with the
    # embedded preexisting diff) already lives in the artifact file, and
    # duplicating it per freeze grew state.json roughly 40 KB per round.
    state["review_snapshots"].append({
        "path": str(artifact),
        "base_sha": snapshot["base_sha"],
        "head_sha": snapshot["head_sha"],
        "diff_digest": snapshot["diff_digest"],
        "frozen_at": snapshot["frozen_at"],
    })
    state["phase"] = "REVIEW_FROZEN"
    _atomic_json(path, state)
    return snapshot


def record_failure(
    state_path: str | Path,
    failure_scope: str,
    impacted_task_ids: list[str],
    root_cause: str,
    error_fingerprint: str,
) -> dict[str, Any]:
    path, state = load_state(state_path)
    if failure_scope not in {"task", "overall"}:
        raise ControllerError("failure-scope must be task or overall")
    if root_cause not in ROOT_CAUSES:
        raise ControllerError(f"unsupported root cause: {root_cause}")
    impacted = sorted(set(item for item in impacted_task_ids if item))
    if failure_scope == "task" and len(impacted) != 1:
        raise ControllerError("task failure must map to exactly one task")
    known_tasks = {item["task_id"] for item in state["tasks"]}
    unknown_tasks = sorted(set(impacted) - known_tasks)
    if unknown_tasks:
        raise ControllerError(f"failure references unregistered task(s): {', '.join(unknown_tasks)}")
    if failure_scope == "overall" and not impacted:
        disposition = "human_intervention"
        resolution = "BLOCKED_MISSING_TASK_MAPPING"
    elif len(impacted) > 1:
        disposition = "replan"
        resolution = "REPLAN_MULTI_TASK_IMPACT"
    elif root_cause == "CODE_OR_TEST":
        disposition = "retry_implementation"
        resolution = "RETRY_MAPPED_TASK"
    else:
        disposition = "human_intervention" if root_cause in {"PERMISSION", "SPEC_OR_ARCH"} else "collect_evidence"
        resolution = disposition.upper()
    # HIGH-1: mechanical retry budget. Every retry cycle must be registered
    # here, so counting failures with this fingerprint counts attempts. The
    # first occurrence is the initial failure; retries are capped at
    # MAX_FINGERPRINT_ATTEMPTS - 1 (step 10). The 5th occurrence stops
    # authorising retries and forces human intervention.
    if not error_fingerprint.strip():
        raise ControllerError("error-fingerprint must be non-empty")
    prior = sum(
        1 for item in state["failures"] if item.get("error_fingerprint") == error_fingerprint
    )
    occurrences = prior + 1
    if disposition == "retry_implementation" and occurrences > MAX_FINGERPRINT_ATTEMPTS:
        disposition = "human_intervention"
        resolution = "ATTEMPT_CAP"
    event = {
        "failure_scope": failure_scope,
        "impacted_task_ids": impacted,
        "root_cause": root_cause,
        "error_fingerprint": error_fingerprint,
        "fingerprint_occurrences": occurrences,
        "disposition": disposition,
        "resolution": resolution,
        "recorded_at": _now(),
    }
    state["failures"].append(event)
    _atomic_json(path, state)
    return event


def replan(state_path: str | Path, new_plan_version: int) -> dict[str, Any]:
    """Advance the plan version and supersede incomplete old-version tasks (P1-3).

    After replan, freeze_review will only authorise files from:
      - tasks belonging to the new plan version (once recorded), and
      - tasks from any version that already reached PASS.

    Supersede-eligible states: PENDING, READY, FAILED.
    RUNNING / VALIDATING tasks are left in place (their Implementer may still
    be writing; the Orchestrator should handle them before calling replan).
    """
    path, state = load_state(state_path)
    old_pv = state.get("current_plan_version", 1)
    if new_plan_version <= old_pv:
        raise ControllerError(
            f"new plan version {new_plan_version} must exceed current {old_pv}"
        )
    supersede_eligible = {"PENDING", "READY", "FAILED"}
    superseded: list[str] = []
    for task in state["tasks"]:
        if task.get("plan_version") == old_pv and task["status"] in supersede_eligible:
            task["status"] = "SUPERSEDED"
            task["superseded_at"] = _now()
            superseded.append(task["task_id"])
    state["current_plan_version"] = new_plan_version
    # Recompute authorized_files: retain PASS task scopes; active tasks have none yet.
    state["authorized_files"] = sorted({
        f
        for t in state["tasks"]
        if t.get("status") == "PASS"
        for f in t.get("allowed_files", [])
    })
    _atomic_json(path, state)
    return {
        "old_version": old_pv,
        "new_version": new_plan_version,
        "superseded_tasks": superseded,
    }


def _required(value: Any, name: str, kind: type | tuple[type, ...]) -> None:
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise ControllerError(f"{name} is required and must be {kind}")


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    """Return True if the directed graph (node → depends_on) contains a cycle.

    Uses DFS coloring (WHITE / GRAY / BLACK) in O(V + E) time.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}

    def dfs(node: str) -> bool:
        color[node] = GRAY
        for dep in graph.get(node, []):
            dep_color = color.get(dep, WHITE)
            if dep_color == GRAY:
                return True
            if dep_color == WHITE and dfs(dep):
                return True
        color[node] = BLACK
        return False

    return any(color[n] == WHITE and dfs(n) for n in graph)


def validate_contract(payload: dict[str, Any], kind: str) -> None:
    if not isinstance(payload, dict):
        raise ControllerError("contract root must be an object")

    if kind == "input":
        _required(payload.get("feature_directory"), "feature_directory", str)
        if not Path(payload["feature_directory"]).is_absolute():
            raise ControllerError("feature_directory must be absolute")
        sources = payload.get("sources")
        _required(sources, "sources", dict)
        for key in ("product_requirements", "technical_design", "release_plan"):
            item = sources.get(key)
            _required(item, f"sources.{key}", dict)
            for field in ("locator", "content_digest"):
                _required(item.get(field), f"sources.{key}.{field}", str)
        repository = payload.get("repository")
        _required(repository, "repository", dict)
        for field in ("root", "base_sha", "branch"):
            _required(repository.get(field), f"repository.{field}", str)
        _required(payload.get("permissions"), "permissions", dict)
        return

    if kind == "task-contract":
        _required(payload.get("feature_directory"), "feature_directory", str)
        _required(payload.get("repo_root"), "repo_root", str)
        _required(payload.get("start_diff_snapshot"), "start_diff_snapshot", str)
        _required(payload.get("allowed_write_files"), "allowed_write_files", list)
        commands = payload.get("validator_commands")
        _required(commands, "validator_commands", list)
        for index, command in enumerate(commands):
            _required(command, f"validator_commands[{index}]", dict)
            for field in ("command", "cwd", "proves"):
                _required(command.get(field), f"validator_commands[{index}].{field}", str)
            if not isinstance(command.get("required"), bool):
                raise ControllerError(f"validator_commands[{index}].required must be boolean")
        return

    if kind == "task-dag":  # P1-1
        tasks = payload.get("tasks")
        _required(tasks, "tasks", list)
        if not tasks:
            raise ControllerError("task_dag must contain at least one task")
        ids: set[str] = set()
        for i, t in enumerate(tasks):
            _required(t, f"tasks[{i}]", dict)
            tid = t.get("id")
            _required(tid, f"tasks[{i}].id", str)
            if tid in ids:
                raise ControllerError(f"duplicate task id: {tid}")
            ids.add(tid)
            _required(t.get("objective"), f"tasks[{i}].objective", str)
            _required(t.get("allowed_write_files"), f"tasks[{i}].allowed_write_files", list)
            deps = t.get("depends_on", [])
            if not isinstance(deps, list):
                raise ControllerError(f"tasks[{i}].depends_on must be a list")
        for i, t in enumerate(tasks):
            for dep in t.get("depends_on", []):
                if dep not in ids:
                    raise ControllerError(
                        f"tasks[{i}] ({t['id']}) depends on unknown task: {dep}"
                    )
        graph = {t["id"]: list(t.get("depends_on", [])) for t in tasks}
        if _has_cycle(graph):
            raise ControllerError("task DAG contains a cycle — cannot schedule tasks")
        if payload.get("unresolved"):
            raise ControllerError(
                "cannot confirm plan while unresolved items exist: "
                + ", ".join(str(u) for u in payload["unresolved"])
            )
        trace = payload.get("requirement_trace")
        if not isinstance(trace, list) or not trace:
            raise ControllerError("requirement_trace must be a non-empty list")
        return

    if kind == "failure":
        _required(payload.get("failure_scope"), "failure_scope", str)
        _required(payload.get("impacted_task_ids"), "impacted_task_ids", list)
        _required(payload.get("error_fingerprint"), "error_fingerprint", str)
        if payload["failure_scope"] not in {"task", "overall"}:
            raise ControllerError("failure_scope must be task or overall")
        if payload["failure_scope"] == "task" and len(payload["impacted_task_ids"]) != 1:
            raise ControllerError("task failure must map to exactly one task")
        if (
            payload["failure_scope"] == "overall"
            and not payload["impacted_task_ids"]
            and payload.get("resolution") != "BLOCKED_MISSING_TASK_MAPPING"
        ):
            raise ControllerError(
                "overall failure must be explicitly mapped or marked for human intervention"
            )
        return

    if kind == "handoff":  # P1-1
        review_input = payload.get("review_input")
        _required(review_input, "review_input", dict)
        materials = review_input.get("materials")
        if not isinstance(materials, list):
            raise ControllerError("review_input.materials must be a list")
        doc_access: dict[str, str] = {}
        for m in materials:
            if isinstance(m, dict) and "kind" in m:
                doc_access[m["kind"]] = m.get("access", "")
        for doc in ("PRD", "TDD", "RPD"):
            if doc not in doc_access:
                raise ControllerError(f"handoff materials must include {doc}")
            if doc_access[doc] != "available":
                raise ControllerError(
                    f"code-impl Handoff requires {doc} access=available, "
                    f"got {doc_access[doc]!r}"
                )
        rm = payload.get("review_mode")
        _required(rm, "review_mode", str)
        if rm != "FULL":
            raise ControllerError(
                f"code-impl must always declare review_mode: FULL, got {rm!r}"
            )
        snapshot = payload.get("snapshot")
        _required(snapshot, "snapshot", dict)
        for field in ("base_sha", "head_sha", "diff_digest"):
            _required(snapshot.get(field), f"snapshot.{field}", str)
        return

    raise ControllerError(f"unsupported contract kind: {kind}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--repo-root", required=True)
    init.add_argument("--feature-directory", required=True)
    init.add_argument("--run-id", required=True)

    agent = sub.add_parser("record-agent")
    agent.add_argument("--state", required=True)
    agent.add_argument("--role", required=True)
    agent.add_argument("--name", required=True)
    agent.add_argument("--session-id", required=True)
    agent.add_argument("--input-digest", required=True)
    agent.add_argument("--status", required=True)

    task = sub.add_parser("record-task")
    task.add_argument("--state", required=True)
    task.add_argument("--task-id", required=True)
    task.add_argument("--allowed-file", action="append", default=[])
    task.add_argument("--plan-version", type=int, default=None,
                      help="Planning iteration this task belongs to (default: current_plan_version)")

    # P0-1: task state machine transition
    ut = sub.add_parser("update-task",
                        help="Transition a task status following TASK_STATE_TRANSITIONS")
    ut.add_argument("--state", required=True)
    ut.add_argument("--task-id", required=True)
    ut.add_argument("--status", required=True,
                    choices=sorted(TASK_STATE_TRANSITIONS))

    scope = sub.add_parser("check-scope",
                           help="Task-bound scope check; the allowed set comes from the "
                                "Task Ledger (task files + PASS-task files), not the caller")
    scope.add_argument("--state", required=True)
    scope.add_argument("--task-id", required=True)

    # HIGH-2: audited write-scope widening (Implementer returned NEEDS_SCOPE)
    ex = sub.add_parser("expand-scope",
                        help="Add files to a non-terminal task's registered scope with a reason")
    ex.add_argument("--state", required=True)
    ex.add_argument("--task-id", required=True)
    ex.add_argument("--add-file", action="append", default=[], required=True)
    ex.add_argument("--reason", required=True)

    # Composite dispatch gate: PENDING/FAILED → RUNNING + record-agent, one write
    dt = sub.add_parser("dispatch-task",
                        help="PENDING->READY->RUNNING (or FAILED->RUNNING retry) and register "
                             "the Implementer agent in one atomic step")
    dt.add_argument("--state", required=True)
    dt.add_argument("--task-id", required=True)
    dt.add_argument("--session-id", required=True)
    dt.add_argument("--input-digest", required=True)

    # Composite verdict gate: scope check + VALIDATING → PASS|FAILED
    st = sub.add_parser("settle-task",
                        help="Run the task-bound scope check, then move the task to PASS or FAILED")
    st.add_argument("--state", required=True)
    st.add_argument("--task-id", required=True)
    st.add_argument("--status", required=True, choices=("PASS", "FAILED"))
    st.add_argument("--error-fingerprint", default=None)

    review = sub.add_parser("freeze-review")
    review.add_argument("--state", required=True)

    failure = sub.add_parser("record-failure")
    failure.add_argument("--state", required=True)
    failure.add_argument("--failure-scope", required=True, choices=("task", "overall"))
    failure.add_argument("--impacted-task-id", action="append", default=[])
    failure.add_argument("--root-cause", required=True, choices=sorted(ROOT_CAUSES))
    failure.add_argument("--error-fingerprint", required=True)

    # P1-3: plan version advance
    rp = sub.add_parser("replan",
                        help="Advance plan version and supersede incomplete old-version tasks")
    rp.add_argument("--state", required=True)
    rp.add_argument("--new-plan-version", required=True, type=int)

    contract = sub.add_parser("validate-contract")
    contract.add_argument("--kind", required=True,
                          choices=("input", "task-contract", "task-dag", "failure", "handoff"))
    contract.add_argument("path")

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(init_run(args.repo_root, args.feature_directory, args.run_id))
        elif args.command == "record-agent":
            record_agent(args.state, args.role, args.name, args.session_id, args.input_digest, args.status)
        elif args.command == "record-task":
            record_task(args.state, args.task_id, args.allowed_file, args.plan_version)
        elif args.command == "update-task":
            print(json.dumps(update_task(args.state, args.task_id, args.status),
                             ensure_ascii=False, indent=2))
        elif args.command == "check-scope":
            print(json.dumps(check_scope(args.state, args.task_id),
                             ensure_ascii=False, indent=2))
        elif args.command == "expand-scope":
            print(json.dumps(expand_scope(args.state, args.task_id, args.add_file, args.reason),
                             ensure_ascii=False, indent=2))
        elif args.command == "dispatch-task":
            print(json.dumps(dispatch_task(args.state, args.task_id, args.session_id, args.input_digest),
                             ensure_ascii=False, indent=2))
        elif args.command == "settle-task":
            print(json.dumps(settle_task(args.state, args.task_id, args.status, args.error_fingerprint),
                             ensure_ascii=False, indent=2))
        elif args.command == "freeze-review":
            print(json.dumps(freeze_review(args.state), ensure_ascii=False, indent=2))
        elif args.command == "record-failure":
            print(json.dumps(
                record_failure(args.state, args.failure_scope, args.impacted_task_id,
                               args.root_cause, args.error_fingerprint),
                ensure_ascii=False, indent=2,
            ))
        elif args.command == "replan":
            print(json.dumps(replan(args.state, args.new_plan_version),
                             ensure_ascii=False, indent=2))
        elif args.command == "validate-contract":
            payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
            validate_contract(payload, args.kind)
            print("VALID")
        return 0
    except (
        ControllerError, OSError, subprocess.CalledProcessError, json.JSONDecodeError
    ) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
