#!/usr/bin/env python3
"""Deterministic state, snapshot, scope, and contract gates for code-impl.

The workflow is still orchestrated by the host agent, but all evidence that
can be checked without model judgment is persisted under ``.codekit`` and
validated here.  The directory is intentionally ignored by the repository.
"""

from __future__ import annotations

import argparse
import codecs
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
# CW2-a: the artifact this controller hands to code-review used to share one filename
# (``review-snapshot.json``) with code-review's own signed manifest while carrying a
# different key set (audit finding A5). It now gets its own version counter, and the
# shape is pinned by ``handoff-snapshot`` contract validation plus the cross-skill
# drift test -- the two producers stay separate skills with no import path between them.
SNAPSHOT_SCHEMA_VERSION = 2
PRODUCED_BY = "codekit:code-impl/scripts/code_impl_controller.py"
# Byte-for-byte the literal code-review and bugfix declare; a mismatch is exactly the
# bugfix-prefixed-digest defect that made every cross-skill ``==`` come out False.
_DIGEST_SCHEME = "sha256/hex"
# porcelain's XY column collapsed to the single letter code-review's `changed_files`
# entries carry; ``??`` (untracked) maps to ``A`` there.
_STATUS_LETTERS = "AMRCDTUX?"
# ``_DIGEST_SCHEME`` is a promise about the *text*: 64 lowercase hex, no ``sha256:``
# prefix. Checking the shape is what stops a prefixed digest from passing as one.
HEX64_RE = re.compile(r"[0-9a-f]{64}")
# ``STATUS:path`` / ``R:old->new``. Repo-relative paths carry no colon (git stores
# POSIX-style paths, and Windows file names forbid ':'), so the first colon splits.
CHANGED_FILE_RE = re.compile(r"[A-Z]:[^\n]+")


def _status_letter(status: str) -> str:
    """One porcelain XY pair -> the status letter of a ``STATUS:path`` entry.

    Deliberately the *first* column, matching code-review's ``parse_name_status``
    byte for byte; ``??`` has no letter there but is an addition there too.
    """
    stripped = status.strip()
    if not stripped or stripped == "??":
        return "A"
    return stripped[0] if stripped[0] in _STATUS_LETTERS else "M"


def _handoff_target_id(root: Path, head_sha: object) -> str:
    """The cross-skill join key: normalized repository root + target head.

    Same expression as code-review's ``target_id_for``; both sides must be able to
    state it from their own git facts, which is why the run's ``base_sha`` and the
    feature directory stay out of it.
    """
    return f"{os.path.normcase(str(root.resolve()))}@{head_sha}"

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

# KW1-a: an agent entry records lifecycle, never a verdict. Without this
# whitelist ``record-agent --role code-review --status APPROVE`` was accepted and
# a spoken verdict was indistinguishable from a recorded one in ``.codekit``.
AGENT_STATUSES = {"RUNNING", "DONE", "ARCHIVED", "FAILED", "BLOCKED"}
# KW1-c: the only verdicts the review system emits, and the subset that clears
# the delivery gate. They live in ``record-review`` records, not agent entries.
REVIEW_VERDICTS = {"APPROVE", "APPROVE_WITH_FOLLOW_UPS", "REQUEST_CHANGES", "BLOCKED"}
PASSING_REVIEW_VERDICTS = {"APPROVE", "APPROVE_WITH_FOLLOW_UPS"}
VALIDATION_RESULTS = {"PASS", "FAIL", "NOT_RUN"}
VALIDATION_SCOPES = {"task", "overall"}
# KW1-f: the sources a plan requirement may trace back to. ``user_request``
# covers instructions given in the conversation when a document is silent.
TRACE_SOURCES = {"product_requirements", "technical_design", "release_plan", "user_request"}

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
# KW1-e: MAX_FINGERPRINT_ATTEMPTS can only bite when every retry is registered
# with the *same* fingerprint, so counting is done on a normalised form (see
# normalize_error_fingerprint). MAX_TASK_ATTEMPTS is the absolute per-task
# dispatch cap that stays valid even if the Orchestrator keeps rewriting the
# fingerprint text: no single task may be dispatched more than 6 times.
MAX_TASK_ATTEMPTS = 6
# Retry gating policy: "gated" requires a record-failure entry for the failed
# attempt before dispatch_task will reopen the task; "prose" restores the
# pre-KW1 behaviour and is the rollback switch.
RETRY_POLICIES = {"gated", "prose"}


class ControllerError(ValueError):
    """A user-correctable workflow or contract error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# KW1-e: volatile parts of an error fingerprint. Retrying the same assertion and
# registering ``test_login.py:412`` then ``test_login.py:517`` used to reset
# MAX_FINGERPRINT_ATTEMPTS, which made the retry budget prose-only in practice.
_FINGERPRINT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[0-9a-f]{6,}"), "<hex>"),          # sha / addresses / ids
    (re.compile(r"\d+"), "#"),                        # line numbers, counts, ports
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),     # quoted payloads
    (re.compile(r"(?:[A-Za-z]:)?(?:/|\\)[\w./\\@+-]+"), "<path>"),
)


def normalize_error_fingerprint(fingerprint: str) -> str:
    """Fold the volatile part of an error fingerprint so retries really count.

    Only ever used for *counting*; the raw text stays in the ledger and the
    report, because the raw text is what a human reads.
    """
    normalized = " ".join(str(fingerprint).split()).lower()
    for pattern, replacement in _FINGERPRINT_RULES:
        normalized = pattern.sub(replacement, normalized)
    return normalized


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


def _atomic_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Same write discipline as ``_atomic_json``, for the rendered ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
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


def _status_entries(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(path -> porcelain XY, new path -> old path)``.

    CW2-a②: the rename's old path sits in the second NUL field. It used to be dropped,
    which left the handoff artifact unable to write ``R:old->new`` — the notation
    code-review freezes with — so the two change lists could never be compared.
    """
    raw = _git(root, "status", "--porcelain=v1", "-z", binary=True)
    assert isinstance(raw, bytes)
    tokens = raw.split(b"\0")
    entries: dict[str, str] = {}
    renamed: dict[str, str] = {}
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
                renamed_from = tokens[index].decode("utf-8", errors="surrogateescape")
                if renamed_from.rstrip("/") != ".codekit" and not renamed_from.startswith(".codekit/"):
                    entries[renamed_from] = status
                    renamed[path] = renamed_from
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
    return entries, renamed


def dirty_snapshot(root: Path) -> dict[str, Any]:
    """Capture a stable, content-addressed view of tracked and untracked changes."""
    entries, renamed = _status_entries(root)
    files = []
    for relative, status in sorted(entries.items()):
        path = root / relative
        files.append({"path": relative, "status": status, "digest": _path_digest(path)})
    result = {
        "head_sha": str(_git(root, "rev-parse", "HEAD")).strip(),
        "files": files,
    }
    result["digest"] = _json_digest(result)
    # CW2-a②: rename pairs are carried beside the digest, never inside it — the
    # content address stays the (path, status, digest) triple, so baselines frozen
    # before this field existed keep comparing equal.
    result["renamed"] = {new: old for new, old in sorted(renamed.items())}
    return result


# KW2-e: candidate encodings for a document that is not valid UTF-8. Real
# product/technical documents in this ecosystem come out of Windows editors, and
# the previous strict ``read_text(encoding="utf-8")`` answered with a traceback,
# exit 1 and no ``BLOCKED`` reason the Orchestrator could act on.
_ENCODING_CANDIDATES = ("gb18030", "big5", "shift_jis", "cp1252")


def _detect_encoding(raw: bytes) -> str:
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for encoding in _ENCODING_CANDIDATES:
        try:
            raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return encoding
    return "unknown"


def _read_document_text(
    path: Path, label: str, *, allow_lossy: bool = False
) -> tuple[str, str | None]:
    """Decode one frozen document, returning ``(text, decoding_note)``.

    A note is only present when something had to be forgiven: a stripped BOM or
    a lossy decode. ``gate``/``show`` surface it so a lossy freeze can never read
    as a clean one.
    """
    raw = path.read_bytes()
    note: str | None = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        detected = _detect_encoding(raw)
        if not allow_lossy:
            raise ControllerError(
                f"{label} is not valid UTF-8 (byte {exc.start}: {exc.reason}), detected "
                f"{detected}: {path}. Convert it before freezing, e.g. "
                f"`iconv -f {detected} -t utf-8 {path} -o {path}.utf8`, or re-run init with "
                "--allow-lossy-decoding to record a lossy freeze (the report will say so)."
            )
        text = raw.decode(detected if detected != "unknown" else "utf-8", errors="replace")
        note = f"lossy:{detected}"
    if text.startswith("\ufeff"):
        text = text[1:]
        note = (note + "+" if note else "") + "bom-stripped"
    return text, note


def _document_snapshot(
    path: Path, label: str, *, allow_lossy: bool = False
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ControllerError(f"{label} is missing or not a regular file: {path}")
    text, decoding = _read_document_text(path, label, allow_lossy=allow_lossy)
    meaningful = [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in {"---", "```"}]
    if len("".join(meaningful)) < 20:
        raise ControllerError(f"{label} has no substantive content: {path}")
    placeholder = re.compile(r"^(?:TODO|TBD|待填写|待补充|<[^>]+>)$", re.IGNORECASE)
    if any(placeholder.fullmatch(line) for line in meaningful):
        raise ControllerError(f"{label} contains a template or placeholder line: {path}")
    stat = path.stat()
    snapshot: dict[str, Any] = {
        "label": label,
        # KW2-d: the frozen material speaks the same key as the Input Contract
        # validator ("locator"); the two disagreed, so no run could validate its
        # own freeze.
        "locator": str(path),
        "revision": "unversioned",
        "content_digest": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "size": stat.st_size,
        "last_modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    if decoding:
        snapshot["decoding"] = decoding
    return snapshot


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
        raise ControllerError(
            f"unsupported state schema: {state.get('schema_version')} "
            f"(this controller writes {SCHEMA_VERSION}); start a new run with a fresh "
            f"--run-id instead of resuming {path}"
        )
    # KW1/KW2 records: a run initialised by an older controller of the same
    # schema version still reads, it simply has no evidence for the new checks.
    state.setdefault("validations", [])
    state.setdefault("reviews", [])
    state.setdefault("scope_checks_total", len(state.get("scope_checks", [])))
    return state_path, state


def init_run(
    root: Path,
    feature_directory: str | Path,
    run_id: str,
    *,
    allow_lossy_decoding: bool = False,
) -> Path:
    root = _require_repo_root(root)
    feature, feature_rel = _inside(root, feature_directory)
    if not feature.is_dir():
        raise ControllerError(f"feature-directory is not a directory: {feature}")
    docs = {
        "product_requirements": _document_snapshot(
            feature / "product-doc.md", "PRD", allow_lossy=allow_lossy_decoding
        ),
        "technical_design": _document_snapshot(
            feature / "technical-doc.md", "TDD", allow_lossy=allow_lossy_decoding
        ),
        "release_plan": _document_snapshot(
            feature / "release-doc.md", "RPD", allow_lossy=allow_lossy_decoding
        ),
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
        "scope_checks_total": 0,
        "validations": [],
        "reviews": [],
        "review_snapshots": [],
    }
    target.mkdir(parents=True)
    # KW2-d: the freeze is written in the same shape the Input Contract validator
    # reads, and validated on the spot, so step 03 can no longer discover that
    # the run's own inputs are unrepresentable.
    input_contract = {
        "feature_directory": str(feature),
        "sources": docs,
        "repository": dict(state["repository"]),
        "preexisting_changes": [
            item["path"] for item in state["preexisting_snapshot"]["files"]
        ],
        "permissions": {"status": "pending-step-03"},
    }
    _atomic_json(target / "input-contract.json", input_contract)
    validate_contract(input_contract, "input")
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
    if status not in AGENT_STATUSES:
        raise ControllerError(
            f"unsupported agent status: {status!r}; an agent entry records lifecycle "
            f"({sorted(AGENT_STATUSES)}) only. Review verdicts belong to record-review "
            "and validation evidence belongs to record-validation."
        )
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


def _record_scope_check(path: Path, state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Store the full scope record in its own file; keep only a row in state.json (KW2-c).

    A 20-task replay put 146 KB of the 183 KB state file in ``scope_checks`` —
    five file-list arrays repeated per check, all of which the Orchestrator was
    told not to read. The full record still lands on disk for the audit trail.

    A repeat on an unchanged working tree adds no evidence — the digest *is* the
    tree — so it increments ``repeat_count`` on the existing row instead of
    appending a byte-identical one. Polling ``check-scope`` in a loop therefore
    costs nothing, while every distinct tree keeps its own artifact.
    """
    task_id = str(result.get("task_id") or "overall")
    fingerprint = str(result.get("error_fingerprint") or "").strip()
    total = int(state.get("scope_checks_total", len(state["scope_checks"])))
    previous = state["scope_checks"][-1] if state["scope_checks"] else None
    if (
        previous is not None
        and not fingerprint
        and not previous.get("error_fingerprint")
        and previous["task_id"] == task_id
        and previous["result"] == result["result"]
        and previous["current_digest"] == result["current_digest"]
    ):
        previous["repeat_count"] = int(previous.get("repeat_count", 1)) + 1
        previous["checked_at"] = result["checked_at"]
        state["scope_checks_total"] = total + 1
        return previous
    artifact = path.parent / "scope-checks" / f"{total + 1:03d}-{task_id}.json"
    _atomic_json(artifact, result)
    compact: dict[str, Any] = {
        "path": str(artifact),
        "task_id": task_id,
        "checked_at": result["checked_at"],
        "result": result["result"],
        "current_digest": result["current_digest"],
        "counts": {
            key: len(result.get(key) or [])
            for key in (
                "allowed_files",
                "new_files",
                "deleted_files",
                "unauthorized_files",
                "preexisting_modified",
                "authorized_overlaps",
            )
        },
    }
    # Only violations are worth reading inline; the empty lists were 80% of the bulk.
    for key in ("unauthorized_files", "preexisting_modified"):
        if result.get(key):
            compact[key] = result[key]
    if fingerprint:
        compact["error_fingerprint"] = fingerprint
    state["scope_checks"].append(compact)
    state["scope_checks_total"] = total + 1
    return compact


def _task_changed_files(
    root: Path, preexisting: dict[str, Any], task: dict[str, Any]
) -> list[str]:
    """Files this task's own scope actually changed, hashed against the init baseline.

    ``_compute_scope_result`` answers "did anything escape the shared allow-list",
    which in a serial worktree cannot attribute a file to one task. The ledger
    needs that attribution, so this hashes only the task's registered files.
    """
    baseline = {item["path"]: item["digest"] for item in preexisting["files"]}
    changed: list[str] = []
    for relative in task.get("allowed_files", []):
        digest = _path_digest(root / relative)
        if baseline.get(relative) != digest:
            changed.append(relative)
    return sorted(changed)


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
    _record_scope_check(path, state, result)
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


def _latest_task_failure(state: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in reversed(state.get("failures", []))
            if task_id in item.get("impacted_task_ids", [])
        ),
        None,
    )


def dispatch_task(
    state_path: str | Path,
    task_id: str,
    session_id: str,
    input_digest: str,
    *,
    role: str = "impl-implementer",
    retry_policy: str = "gated",
) -> dict[str, Any]:
    """Composite dispatch gate for steps 05/06: one call, one state write.

    Moves PENDING → READY → RUNNING (or FAILED → RUNNING for a retry) and
    registers the Implementer agent atomically, replacing the separate
    update-task ×2 + record-agent round-trips. Only registered tasks can be
    dispatched; the retry count is kept on the task ledger entry.

    KW1-e makes step 10's "同一错误指纹最多重试 3 次，且由控制器机械执行" actually
    mechanical: under the default ``gated`` policy a retry requires a
    ``record-failure`` entry for *this* task that was logged after the task
    turned FAILED, refuses a retry whose disposition already escalated to a
    human, and caps total dispatches at MAX_TASK_ATTEMPTS. ``--retry-policy
    prose`` restores the previous behaviour as the rollback switch.
    """
    if retry_policy not in RETRY_POLICIES:
        raise ControllerError(f"retry policy must be one of {sorted(RETRY_POLICIES)}")
    path, state = load_state(state_path)
    task = _require_task(state, task_id)
    current = task.get("status", "PENDING")
    if current not in {"PENDING", "FAILED"}:
        raise ControllerError(
            f"cannot dispatch {task_id}: status must be PENDING or FAILED (retry), got {current}"
        )
    attempts = int(task.get("attempts", 0))
    if current == "FAILED" and attempts >= MAX_TASK_ATTEMPTS:
        # Policy-independent ceiling: --retry-policy prose loosens the
        # diagnosis-record requirement, never the absolute dispatch budget.
        raise ControllerError(
            f"cannot retry {task_id}: {attempts} dispatches already spent "
            f"(MAX_TASK_ATTEMPTS={MAX_TASK_ATTEMPTS}); escalate to step 17."
        )
    if current == "FAILED" and retry_policy == "gated":
        failed_at = str(task.get("updated_at", ""))
        failure = _latest_task_failure(state, task_id)
        if failure is None:
            raise ControllerError(
                f"cannot retry {task_id}: no record-failure entry maps to this task. "
                "A retry without a registered diagnosis is how the same failure is "
                "re-dispatched ten times while the budget reads as unused."
            )
        if str(failure.get("recorded_at", "")) < failed_at:
            raise ControllerError(
                f"cannot retry {task_id}: the newest failure for this task "
                f"({failure.get('recorded_at')}) predates the FAILED transition "
                f"({failed_at}); register this attempt with record-failure first."
            )
        if failure.get("disposition") == "human_intervention":
            raise ControllerError(
                f"cannot retry {task_id}: the registered failure resolved to "
                f"{failure.get('resolution')} and needs replan or human intervention "
                "(step 17), not another Implementer."
            )
    now = _now()
    for step in (["READY", "RUNNING"] if current == "PENDING" else ["RUNNING"]):
        task["status"] = step
        task["updated_at"] = now
    task["attempts"] = attempts + 1
    # KW2-a: the ledger needs the real start/end of every attempt, and the
    # dispatch is the only place that knows when one began.
    task.setdefault("attempts_log", []).append({
        "attempt": task["attempts"],
        "session_id": session_id,
        "started_at": now,
    })
    if current == "FAILED":
        # A prose bypass must leave a trace the final report can count.
        task["attempts_log"][-1]["retry_policy"] = retry_policy
    agent = _append_agent(state, role, role, session_id, input_digest, "RUNNING")
    _atomic_json(path, state)
    result: dict[str, Any] = {
        "task_id": task_id,
        "status": task["status"],
        "attempts": task["attempts"],
        "agent": {
            "session_id": agent["session_id"],
            "role": agent["role"],
            "status": agent["status"],
        },
    }
    if current == "FAILED":
        result["retry_policy"] = retry_policy
    return result


def settle_task(
    state_path: str | Path,
    task_id: str,
    status: str,
    error_fingerprint: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Composite verdict gate for steps 07/08: scope check + VALIDATING → PASS|FAILED.

    RUNNING is first advanced to VALIDATING (idempotent when already there),
    then the task-bound scope check runs and is persisted with the task id
    (and the error fingerprint when given). ``status=PASS`` raises when the
    scope check is BLOCKED and leaves the task in VALIDATING — an out-of-scope
    write can never be settled as PASS; the Orchestrator must fix scope with
    expand-scope, or settle FAILED and record-failure.

    KW1-e: a FAILED verdict now requires the error fingerprint, because that is
    the record the retry budget counts; without it step 10 could not tell one
    repeat from another. KW2-a: the verdict also writes the ledger row (when the
    attempt ended, on which tree, and which files this task actually changed).
    """
    if status not in {"PASS", "FAILED"}:
        raise ControllerError("settle-task status must be PASS or FAILED")
    if status == "FAILED" and not (error_fingerprint or "").strip():
        raise ControllerError(
            "settle-task --status FAILED requires a non-empty --error-fingerprint: "
            "it is the key the retry budget counts and the key the Debugger report is filed under"
        )
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
        scope["error_fingerprint"] = error_fingerprint.strip()
    _record_scope_check(path, state, scope)
    # Persist the scope evidence even if the PASS verdict is rejected below.
    _atomic_json(path, state)
    if status == "PASS" and scope["result"] != "PASS":
        raise ControllerError(
            f"cannot settle {task_id} as PASS: scope check BLOCKED "
            f"(unauthorized_files={scope['unauthorized_files']}, "
            f"preexisting_modified={scope['preexisting_modified']}); "
            "use expand-scope with a reason or settle FAILED and record-failure"
        )
    now = _now()
    task["status"] = status
    task["updated_at"] = now
    task["completed_at"] = now
    task["final_diff_digest"] = scope["current_digest"]
    task["final_head_sha"] = str(_git(root, "rev-parse", "HEAD")).strip()
    own_changed = _task_changed_files(root, state["preexisting_snapshot"], task)
    if own_changed:
        task["changed_files"] = own_changed
    if error_fingerprint:
        task.setdefault("error_fingerprints", []).append(error_fingerprint.strip())
    if note and note.strip():
        task["observations"] = note.strip()[:2000]
    if task.get("attempts_log"):
        task["attempts_log"][-1]["settled_at"] = now
        task["attempts_log"][-1]["result"] = status
    _atomic_json(path, state)
    return {
        "task_id": task_id,
        "status": status,
        "scope_result": scope["result"],
        "unauthorized_files": scope["unauthorized_files"],
        "preexisting_modified": scope["preexisting_modified"],
        "changed_files": own_changed,
        "diff_digest": scope["current_digest"][:12],
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
    renamed = current.get("renamed", {})
    # CW2-a②：`STATUS:path`，重命名写成 `R:old->new`，未跟踪写成 `A:path`——与
    # code-review 冻结的清单同一记法，两侧才可比。旧的裸路径写法会让集合比较
    # 恒不等（审核报告 A5 的 `44 项 vs []` 就是这么来的）。
    # porcelain 把重命名拆成新旧两条，code-review 的 `--name-status` 只有一条
    # `R:old->new`，所以这里把旧路径那条折进去，不再单独成项。
    rename_sources = set(renamed.values())
    changed_files = sorted(
        {
            f"{_status_letter(item['status'])}:{renamed[item['path']]}->{item['path']}"
            if item["path"] in renamed
            else f"{_status_letter(item['status'])}:{item['path']}"
            for item in current["files"]
            if item["path"] not in baseline_paths and item["path"] not in rename_sources
        }
    )
    # CW2-a①③：交给 code-review 的那一份必须是自述的 Handoff 输入，而不是恰好同名的
    # 内部快照。两个 home 此前都叫 review-snapshot.json，键集却不同（A5）。
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "run_id": state["run_id"],
        "repository_root": str(root),
        "produced_by": PRODUCED_BY,
        "digest_scheme": _DIGEST_SCHEME,
        # 本控制器不计算 committed 补丁摘要，所以显式声明"不含已提交范围"（null 而不是
        # 缺键）：下游据此记 SCOPE_SUPERSET，不得判 BLOCKED。
        "committed_range": None,
        "target_id": _handoff_target_id(root, current["head_sha"]),
        "worktree_state": {
            "head_sha": current["head_sha"],
            "diff_digest": current["digest"],
            "changed_files": changed_files,
        },
        "preexisting_diff_snapshot": state["preexisting_snapshot"],
        "frozen_at": _now(),
    }
    artifact = path.parent / "review-snapshot.json"
    _atomic_json(artifact, snapshot)
    snapshot["path"] = str(artifact)
    snapshot["freeze_index"] = existing_freezes + 1
    # Keep only a compact reference in state.json; the full snapshot (with the
    # embedded preexisting diff) already lives in the artifact file, and
    # duplicating it per freeze grew state.json roughly 40 KB per round.
    state["review_snapshots"].append({
        "path": str(artifact),
        "target_id": snapshot["target_id"],
        "head_sha": snapshot["worktree_state"]["head_sha"],
        "diff_digest": snapshot["worktree_state"]["diff_digest"],
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
    # KW1-e: counting is on the normalised form, so re-wording the same failure
    # ("line 412" → "line 517") no longer buys a fresh budget.
    if not error_fingerprint.strip():
        raise ControllerError("error-fingerprint must be non-empty")
    normalized = normalize_error_fingerprint(error_fingerprint)
    prior = sum(
        1
        for item in state["failures"]
        if normalize_error_fingerprint(item.get("error_fingerprint", "")) == normalized
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
        "normalized_fingerprint": normalized,
        "fingerprint_occurrences": occurrences,
        "disposition": disposition,
        "resolution": resolution,
        "recorded_at": _now(),
    }
    state["failures"].append(event)
    _atomic_json(path, state)
    return event


def _resolve_within_repo(root: Path, value: str, *, what: str) -> Path:
    raw = Path(value).expanduser()
    candidate = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
    if not candidate.is_dir():
        raise ControllerError(f"{what} is not a directory: {candidate}")
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ControllerError(f"{what} escapes repository root: {candidate}") from None
    return candidate


def record_validation(
    state_path: str | Path,
    scope: str,
    name: str,
    command: str,
    cwd: str,
    exit_code: int | None,
    result: str,
    task_id: str | None = None,
    evidence: str | None = None,
    not_run_reason: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Persist one validation record so a PASS claim has a re-checkable provenance (KW1-b).

    Before this existed, ``SKILL.md`` step 12 asked for full-suite tests, compile,
    lint, contract and migration checks, and the controller stored none of it:
    the exit codes lived only in the conversation, so ``freeze-review`` and
    delivery accepted "I ran the tests" exactly as happily as a real log.

    The rules encode step 04's "不把未执行、超时、跳过或日志缺失记为 PASS":
      * ``PASS``  requires ``exit_code == 0`` and non-empty evidence;
      * ``FAIL``  requires a non-zero ``exit_code``;
      * ``NOT_RUN`` requires a reason and must not carry an exit code.
    Every record is stamped with the working-tree digest it was collected
    against, which is what lets ``gate`` refuse evidence that predates the diff.
    """
    if scope not in VALIDATION_SCOPES:
        raise ControllerError(f"validation scope must be one of {sorted(VALIDATION_SCOPES)}")
    if result not in VALIDATION_RESULTS:
        raise ControllerError(f"validation result must be one of {sorted(VALIDATION_RESULTS)}")
    path, state = load_state(state_path)
    root = _require_repo_root(state["repository"]["root"])
    if scope == "task":
        if not task_id:
            raise ControllerError("scope=task requires --task-id")
        _require_task(state, task_id)
    elif task_id:
        raise ControllerError("scope=overall must not carry --task-id")
    if not name.strip():
        raise ControllerError("validation --name is required")
    if not command.strip():
        raise ControllerError("validation --command is required (the exact command line)")

    if result == "PASS":
        if exit_code != 0:
            raise ControllerError(
                f"cannot record {name} as PASS with exit_code={exit_code!r}; "
                "a non-zero or missing exit code is FAIL or NOT_RUN"
            )
        if not (evidence or "").strip():
            raise ControllerError(
                f"cannot record {name} as PASS without evidence; pass --evidence "
                "or --evidence-file with the log that proves it"
            )
    elif result == "FAIL":
        if exit_code == 0:
            raise ControllerError(f"cannot record {name} as FAIL with exit_code=0")
    else:  # NOT_RUN
        if exit_code is not None:
            raise ControllerError(
                f"NOT_RUN must not carry an exit code (got {exit_code}); the command did not finish, "
                "so record FAIL, or record NOT_RUN with --not-run-reason only"
            )
        if not (not_run_reason or "").strip():
            raise ControllerError(f"NOT_RUN requires a non-empty --not-run-reason for {name}")

    current = dirty_snapshot(root)
    record: dict[str, Any] = {
        "scope": scope,
        "name": name.strip(),
        "command": command,
        "cwd": str(_resolve_within_repo(root, cwd, what=f"{name} --cwd")),
        "exit_code": exit_code,
        "result": result,
        "recorded_at": _now(),
        "head_sha": current["head_sha"],
        "diff_digest": current["digest"],
    }
    if task_id:
        record["task_id"] = task_id
    if started_at:
        record["started_at"] = started_at
    if finished_at:
        record["finished_at"] = finished_at
    if not_run_reason:
        record["not_run_reason"] = not_run_reason.strip()
    if evidence:
        if len(evidence) > 4000:
            raise ControllerError(
                f"--evidence is {len(evidence)} characters; keep the state file small and "
                "point --evidence-file at the log instead"
            )
        record["evidence"] = evidence.strip()
    state["validations"].append(record)
    _atomic_json(path, state)
    return {
        "name": record["name"],
        "scope": scope,
        "task_id": task_id,
        "result": result,
        "exit_code": exit_code,
        "diff_digest": record["diff_digest"][:12],
        "validations_total": len(state["validations"]),
    }


def record_review(
    state_path: str | Path,
    verdict: str,
    report_path: str,
    validator_exit_code: int,
    summary: str | None = None,
) -> dict[str, Any]:
    """Bind a code-review verdict to the frozen snapshot it was produced on (KW1-c).

    ``SKILL.md:425`` already demanded "only accept a report that matches the
    Handoff exactly and whose structural validation exited 0", but nothing
    recorded the pairing, so a verdict from an older snapshot — or a spoken
    APPROVE — left the same trace in ``.codekit``. The digests are taken from
    the controller's own last freeze, never typed by the caller.
    """
    if verdict not in REVIEW_VERDICTS:
        raise ControllerError(f"review verdict must be one of {sorted(REVIEW_VERDICTS)}")
    path, state = load_state(state_path)
    freezes = state.get("review_snapshots", [])
    if not freezes:
        raise ControllerError(
            "cannot record a review verdict before freeze-review: the verdict must be bound "
            "to a frozen snapshot, otherwise it certifies an undefined diff"
        )
    last = freezes[-1]
    root = _require_repo_root(state["repository"]["root"])
    report = Path(report_path).expanduser()
    report = report if report.is_absolute() else root / report
    report = report.resolve(strict=False)
    if not report.is_file():
        raise ControllerError(f"review --report-path is not a readable file: {report}")
    record: dict[str, Any] = {
        "verdict": verdict,
        "report_path": str(report),
        "report_digest": _path_digest(report),
        "validator_exit_code": int(validator_exit_code),
        # CW2-a①③：结论绑到 target_id（规范化仓库根 + 冻结时的 head_sha），不再抄一个
        # 本控制器从未冻结过的 base_sha。缺 target_id 的台账是升级前的冻结记录，
        # 用同样的两个事实重算即可，结果与当时写下的值相同。
        "target_id": last.get("target_id") or _handoff_target_id(root, last["head_sha"]),
        "head_sha": last["head_sha"],
        "diff_digest": last["diff_digest"],
        "freeze_index": len(freezes),
        "recorded_at": _now(),
    }
    if summary:
        record["summary"] = summary.strip()[:2000]
    state["reviews"].append(record)
    _atomic_json(path, state)
    return {
        "verdict": verdict,
        "diff_digest": record["diff_digest"][:12],
        "freeze_index": record["freeze_index"],
        "validator_exit_code": record["validator_exit_code"],
        "reviews_total": len(state["reviews"]),
    }


def gate(state_path: str | Path, mode: str = "enforce") -> dict[str, Any]:
    """Decide delivery from recorded evidence only; the model cannot talk its way past this.

    The five completion conditions in ``SKILL.md`` used to be a checklist the
    Orchestrator graded itself. Every check below reads persisted records, and
    ``--mode warn-only`` is the rollback switch that keeps printing the same
    report without the non-zero exit.
    """
    if mode not in {"enforce", "warn-only"}:
        raise ControllerError(f"gate mode must be enforce or warn-only, got {mode}")
    path, state = load_state(state_path)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    tasks = [t for t in state["tasks"] if t.get("plan_version") == state.get("current_plan_version", 1)]
    delivered = [t for t in state["tasks"] if t.get("status") == "PASS"]
    add("tasks-registered", bool(state["tasks"]), f"{len(state['tasks'])} task(s) recorded")
    unfinished = sorted(
        f"{t['task_id']}={t['status']}"
        for t in tasks
        if t.get("status") not in {"PASS", "SUPERSEDED"}
    )
    add("all-current-plan-tasks-pass", not unfinished, unfinished or "every current-plan task is PASS")

    missing_validation = sorted(
        t["task_id"]
        for t in delivered
        if not any(
            v.get("scope") == "task"
            and v.get("task_id") == t["task_id"]
            and v.get("result") == "PASS"
            for v in state["validations"]
        )
    )
    add(
        "task-validations-recorded",
        not missing_validation,
        missing_validation or f"{len(state['validations'])} validation record(s)",
    )

    freezes = state.get("review_snapshots", [])
    add("review-snapshot-frozen", bool(freezes), "no freeze-review record")
    frozen_digest = freezes[-1]["diff_digest"] if freezes else ""
    if freezes:
        artifact = Path(freezes[-1].get("path", ""))
        add("review-snapshot-artifact-readable", artifact.is_file(), str(artifact))

    overall = [
        v
        for v in state["validations"]
        if v.get("scope") == "overall" and v.get("result") == "PASS"
    ]
    add("overall-validation-recorded", bool(overall), "step 12 evidence is missing")
    stale_overall = sorted(
        f"{v['name']}@{str(v.get('diff_digest', ''))[:12]}"
        for v in overall
        if frozen_digest and v.get("diff_digest") != frozen_digest
    )
    add(
        "overall-validation-covers-frozen-diff",
        bool(overall) and not stale_overall,
        stale_overall or f"frozen diff {frozen_digest[:12]}",
    )

    reviews = state.get("reviews", [])
    add("review-verdict-recorded", bool(reviews), "code-review ran but recorded nothing")
    if reviews:
        last = reviews[-1]
        add(
            "review-verdict-passing",
            last["verdict"] in PASSING_REVIEW_VERDICTS,
            last["verdict"],
        )
        add(
            "review-validator-exited-0",
            last.get("validator_exit_code") == 0,
            f"validator exit {last.get('validator_exit_code')!r}; "
            "a structural reject is not a review result",
        )
        add(
            "review-verdict-bound-to-frozen-diff",
            bool(frozen_digest) and last.get("diff_digest") == frozen_digest,
            (
                f"newest verdict certifies {str(last.get('diff_digest', ''))[:12]} but the "
                f"frozen snapshot is {frozen_digest[:12]}; edits after a verdict need a new "
                "freeze and a fresh review"
                if frozen_digest and last.get("diff_digest") != frozen_digest
                else str(last.get("diff_digest", ""))[:12]
            ),
        )
        add("review-report-file-present", Path(last.get("report_path", "")).is_file(), last.get("report_path", ""))

    # A final scope pass is the only way to catch writes that landed after the
    # last task settled (including writes made while the review was running).
    if freezes:
        allowed = _task_allowed_set(state, {}) | set(state.get("authorized_files", []))
        for task in state["tasks"]:
            allowed.update(task.get("allowed_files", []))
        scope = _compute_scope_result(
            _require_repo_root(state["repository"]["root"]), state["preexisting_snapshot"], allowed
        )
        add(
            "final-scope-clean",
            scope["result"] == "PASS",
            {
                "unauthorized_files": scope["unauthorized_files"],
                "preexisting_modified": scope["preexisting_modified"],
            },
        )

    failures = [item["name"] for item in checks if not item["ok"]]
    status = "PASS" if not failures else "BLOCKED"
    report = {
        "status": status if mode == "enforce" else f"{status} ({mode})",
        "mode": mode,
        "run_id": state["run_id"],
        "phase": state.get("phase"),
        "checks": checks,
        "failures": failures,
        "tasks": {
            "total": len(state["tasks"]),
            "pass": len(delivered),
            "current_plan_version": state.get("current_plan_version", 1),
        },
        "freezes": len(freezes),
        "reviews": len(reviews),
    }
    _atomic_json(path.parent / "gate-report.json", report)
    return report


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


# KW2-b: the Task Ledger is rendered from state.json, never hand-written. Step 11
# used to keep it "在内存中", so the report had to be re-typed by the model at the
# end of a run and had no provenance.
LEDGER_START = "<!-- codekit:code-impl:task-ledger:start -->"
LEDGER_END = "<!-- codekit:code-impl:task-ledger:end -->"
LEDGER_COLUMNS = (
    "任务", "状态", "计划版本", "尝试", "开始", "结束",
    "授权文件", "自有变更", "验证", "失败指纹", "扩权",
)


def _validations_for(state: dict[str, Any], task_id: str) -> list[dict[str, Any]]:
    return [v for v in state.get("validations", []) if v.get("task_id") == task_id]


def _ledger_row(state: dict[str, Any], task: dict[str, Any]) -> list[str]:
    task_id = task["task_id"]
    validations = _validations_for(state, task_id)
    passed = sum(1 for v in validations if v.get("result") == "PASS")
    failures = [
        f.get("error_fingerprint", "")
        for f in state.get("failures", [])
        if task_id in f.get("impacted_task_ids", [])
    ]
    started = (task.get("attempts_log") or [{}])[0].get("started_at", "")
    return [
        task_id,
        str(task.get("status", "PENDING")),
        str(task.get("plan_version", state.get("current_plan_version", 1))),
        str(task.get("attempts", 0)),
        started[:19],
        str(task.get("completed_at", ""))[:19],
        str(len(task.get("allowed_files", []))),
        str(len(task.get("changed_files", []))),
        f"{passed}/{len(validations)}" if validations else "-",
        ", ".join(sorted({f[:40] for f in failures})) or "-",
        str(len(task.get("scope_expansions", []))),
    ]


def render_ledger(state_path: str | Path, output: str | Path | None = None) -> dict[str, Any]:
    """Write ``task-ledger.md`` from state.json and return a compact summary."""
    path, state = load_state(state_path)
    target = Path(output).expanduser().resolve() if output else path.parent / "task-ledger.md"
    tasks = sorted(state["tasks"], key=lambda t: (t.get("plan_version", 1), t["task_id"]))
    rows = [_ledger_row(state, task) for task in tasks]
    body = [
        f"| {' | '.join(LEDGER_COLUMNS)} |",
        f"|{'---|' * len(LEDGER_COLUMNS)}",
        *[f"| {' | '.join(row)} |" for row in rows],
        "",
        f"- 验证记录：{len(state.get('validations', []))} 条（任务级 + 整体）",
        f"- 失败登记：{len(state.get('failures', []))} 条；范围检查：{state.get('scope_checks_total', 0)} 次"
        f"（{len(state.get('scope_checks', []))} 条不同工作树记录）",
        f"- 冻结快照：{len(state.get('review_snapshots', []))} 次；审查结论：{len(state.get('reviews', []))} 条",
        f"- 生成时间：{_now()}（由 render-ledger 从 state.json 渲染，勿手工编辑本块）",
    ]
    block = [LEDGER_START, "# Task Ledger", "", *body, LEDGER_END]
    if target.is_file():
        text, _ = _read_document_text(target, "task ledger")
        lines = text.splitlines()
        if LEDGER_START in lines and LEDGER_END in lines:
            start, end = lines.index(LEDGER_START), lines.index(LEDGER_END)
            lines = lines[: start + 1] + body + lines[end:]
        else:
            lines = [*lines, "", *block]
        content = "\n".join(lines).rstrip("\n") + "\n"
    else:
        content = "\n".join(block) + "\n"
    _atomic_text(target, content)
    return {
        "path": str(target),
        "rows": len(rows),
        "passed": sum(1 for task in tasks if task.get("status") == "PASS"),
        "total": len(tasks),
        "ledger_digest": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
    }


def show(state_path: str | Path, tasks: bool = False) -> dict[str, Any]:
    """Print the run's real state in a bounded payload instead of the raw file.

    ``SKILL.md`` tells the Orchestrator not to restate state.json, but resume
    still meant reading a 183 KB file back into context. This is the answer to
    "where am I": a few hundred bytes, plus the drift the model cannot see.
    """
    path, state = load_state(state_path)
    by_status: dict[str, int] = {}
    for task in state["tasks"]:
        by_status[str(task.get("status", "PENDING"))] = by_status.get(str(task.get("status")), 0) + 1
    materials: dict[str, Any] = {}
    drifted: list[str] = []
    for key, item in state["materials"].items():
        entry: dict[str, Any] = {
            "locator": item.get("locator", ""),
            "content_digest": str(item.get("content_digest", ""))[:18],
            "size": item.get("size"),
        }
        if item.get("decoding"):
            entry["decoding"] = item["decoding"]
        try:
            current, _ = _read_document_text(Path(item["locator"]), key)
        except (ControllerError, OSError):
            entry["on_disk"] = "missing"
            drifted.append(key)
            materials[key] = entry
            continue
        live = "sha256:" + hashlib.sha256(current.encode("utf-8")).hexdigest()
        if live != item.get("content_digest"):
            entry["on_disk"] = "changed-since-freeze"
            drifted.append(key)
        else:
            entry["on_disk"] = "matches-freeze"
        materials[key] = entry
    validation_counts: dict[str, int] = {}
    for record in state.get("validations", []):
        key = f"{record.get('scope')}/{record.get('result')}"
        validation_counts[key] = validation_counts.get(key, 0) + 1
    last_freeze = (state.get("review_snapshots") or [{}])[-1]
    last_review = (state.get("reviews") or [{}])[-1]
    newest_failure = (state.get("failures") or [{}])[-1]
    open_tasks = sorted(
        t["task_id"] for t in state["tasks"] if t.get("status") in {"RUNNING", "VALIDATING", "READY"}
    )
    report: dict[str, Any] = {
        "run_id": state["run_id"],
        "phase": state.get("phase"),
        "plan_version": state.get("current_plan_version", 1),
        "repository": {
            "root": state["repository"]["root"],
            "branch": state["repository"]["branch"],
            "base_sha": str(state["repository"]["base_sha"])[:12],
        },
        "tasks": {
            "total": len(state["tasks"]),
            "by_status": by_status,
            "open": open_tasks,
            "prose_retries": sum(
                1
                for t in state["tasks"]
                for entry in t.get("attempts_log", [])
                if entry.get("retry_policy") == "prose"
            ),
        },
        "agents": len(state["agents"]),
        "scope_checks": {
            "total": state.get("scope_checks_total", len(state.get("scope_checks", []))),
            "distinct": len(state.get("scope_checks", [])),
            "blocked": sum(1 for c in state.get("scope_checks", []) if c.get("result") != "PASS"),
        },
        "validations": validation_counts,
        "failures": {
            "total": len(state.get("failures", [])),
            "newest_disposition": newest_failure.get("disposition"),
            "newest_resolution": newest_failure.get("resolution"),
        },
        "review": {
            "freezes": len(state.get("review_snapshots", [])),
            "freeze_budget": MAX_REVIEW_FREEZES,
            "frozen_diff_digest": str(last_freeze.get("diff_digest", ""))[:12],
            "verdict": last_review.get("verdict"),
            "verdict_diff_digest": str(last_review.get("diff_digest", ""))[:12],
        },
        "materials": materials,
        "drifted_materials": drifted,
        "preexisting_files": len(state["preexisting_snapshot"]["files"]),
    }
    if tasks:
        report["task_rows"] = [_ledger_row(state, task) for task in sorted(
            state["tasks"], key=lambda t: (t.get("plan_version", 1), t["task_id"])
        )]
    return report


def record_tasks_from_dag(state_path: str | Path, dag_path: str | Path) -> dict[str, Any]:
    """Validate a Task DAG file and register every task in one state write (KW2-g).

    Step 04 used to need one ``record-task`` round-trip per task, each of which
    re-read and re-wrote the whole state file; the DAG is already on disk as the
    Planner's artifact, so the controller can take it directly.
    """
    payload = json.loads(Path(dag_path).read_text(encoding="utf-8"))
    contract = payload.get("task_dag", payload)
    validate_contract(contract, "task-dag")
    path, state = load_state(state_path)
    root = _require_repo_root(state["repository"]["root"])
    known = {item["task_id"] for item in state["tasks"]}
    declared = {str(spec["id"]) for spec in contract["tasks"]}
    pv = int(contract.get("plan_version") or state.get("current_plan_version", 1))
    added: list[str] = []
    for spec in contract["tasks"]:
        task_id = spec["id"]
        if task_id in known:
            continue
        normalized = sorted({_inside(root, item)[1] for item in spec["allowed_write_files"]})
        if not normalized:
            raise ControllerError(f"task {task_id} declares no allowed_write_files")
        entry: dict[str, Any] = {
            "task_id": task_id,
            "allowed_files": normalized,
            "status": "PENDING",
            "plan_version": pv,
        }
        if spec.get("objective"):
            entry["objective"] = str(spec["objective"])[:500]
        if spec.get("depends_on"):
            entry["depends_on"] = list(spec["depends_on"])
        state["tasks"].append(entry)
        added.append(task_id)
    _atomic_json(path, state)
    return {
        "registered": added,
        "already_present": sorted(known & declared),
        "plan_version": pv,
        "tasks_total": len(state["tasks"]),
    }


def _required(value: Any, name: str, kind: type | tuple[type, ...]) -> None:
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise ControllerError(f"{name} is required and must be {kind}")


# A locator that is not a filesystem path is a remote/VCS reference and is
# exempt from the existence probe below.
_NON_FILE_LOCATOR = re.compile(r"^(?:https?://|s3://|gs://|rev:|git@)", re.IGNORECASE)
# Git Bash (MSYS) silently rewrites ``$SKILL_DIR/references`` into
# ``C:/Program Files/Git/...``; a claims-to-exist locator with that prefix is
# always that bug, so name it instead of reporting a missing file.
MSYS_MANGLED = re.compile(r"Program Files[/\\]Git[/\\]", re.IGNORECASE)


def _verify_locator(locator: str, name: str, *, root: Path | None = None) -> None:
    """Check that a declared input actually exists, so 'available' is measured.

    ``access: available`` and a ``content_digest`` were both self-reported: the
    Handoff could name a document that was never on disk and still clear every
    check the controller had.
    """
    raw = locator.strip()
    if _NON_FILE_LOCATOR.match(raw) and not Path(raw).expanduser().is_absolute():
        return
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if root is None:
            raise ControllerError(f"{name} must be an absolute path or a rev/URL locator: {raw}")
        candidate = root / candidate
    candidate = candidate.resolve(strict=False)
    if not candidate.is_file():
        hint = (
            "; this looks like a Git Bash (MSYS) rewritten path — pass the native absolute "
            "path resolved without the shell"
            if MSYS_MANGLED.search(candidate.as_posix())
            else ""
        )
        raise ControllerError(f"{name} does not resolve to a file: {candidate}{hint}")


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
            # KW2-d: the digest and the "available" claim are only worth
            # anything if the file is really there.
            _verify_locator(item["locator"], f"sources.{key}.locator")
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
        # KW1-f: the trace is the only link between the frozen documents and the
        # plan, so a dangling reference has to fail mechanically instead of
        # reading as "covered" to a reviewer three steps later.
        human_ids = {
            h.get("id")
            for h in payload.get("human_acceptance", [])
            if isinstance(h, dict) and h.get("id")
        }
        for i, item in enumerate(trace):
            if not isinstance(item, dict):
                raise ControllerError(f"requirement_trace[{i}] must be an object")
            source = item.get("source")
            if source not in TRACE_SOURCES:
                raise ControllerError(
                    f"requirement_trace[{i}].source must be one of {sorted(TRACE_SOURCES)}, got {source!r}"
                )
            if not isinstance(item.get("requirement"), str) or not item["requirement"].strip():
                raise ControllerError(f"requirement_trace[{i}].requirement must be a non-empty string")
            covered = item.get("covered_by")
            if not isinstance(covered, list) or not covered:
                raise ControllerError(
                    f"requirement_trace[{i}].covered_by must name at least one task or "
                    "human-acceptance item; an uncovered requirement belongs in `unresolved`"
                )
            for ref in covered:
                if ref not in ids and ref not in human_ids:
                    raise ControllerError(
                        f"requirement_trace[{i}] covers unknown id {ref!r}: not a task "
                        f"({sorted(ids)}) nor a human_acceptance item ({sorted(human_ids)})"
                    )
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
        rm = payload.get("review_mode")
        _required(rm, "review_mode", str)
        if rm != "FULL":
            raise ControllerError(
                f"code-impl must always declare review_mode: FULL, got {rm!r}"
            )
        review_input = payload.get("review_input")
        _required(review_input, "review_input", dict)
        materials = review_input.get("materials")
        if not isinstance(materials, list):
            raise ControllerError("review_input.materials must be a list")
        doc_access: dict[str, dict[str, Any]] = {}
        for m in materials:
            if isinstance(m, dict) and "kind" in m:
                doc_access[m["kind"]] = m
        for doc in ("PRD", "TDD", "RPD"):
            entry = doc_access.get(doc)
            if entry is None:
                raise ControllerError(f"handoff materials must include {doc}")
            if entry.get("access") != "available":
                raise ControllerError(
                    f"code-impl Handoff requires {doc} access=available, "
                    f"got {entry.get('access')!r}"
                )
        root = payload.get("repo_root")
        root_path = Path(root).expanduser().resolve(strict=False) if isinstance(root, str) and root else None
        for doc in ("PRD", "TDD", "RPD"):
            # KW2-d: access=available used to be a word. Now the named document
            # has to exist, which is also what stops a review being dispatched
            # against a feature directory that was never produced.
            entry = doc_access[doc]
            _required(entry.get("locator"), f"handoff.materials.{doc}.locator", str)
            _verify_locator(
                entry["locator"], f"handoff.materials.{doc}.locator", root=root_path
            )
        snapshot = payload.get("snapshot")
        _required(snapshot, "snapshot", dict)
        # CW2-a①②③：Handoff 里的快照必须与 `freeze-review` 落盘的是同一种形状。
        # 原来这里只看三个非空字符串，所以模型手写一份、或把 code-review 的
        # 8 字段 manifest 贴进来都能过门禁——A5 的比对失败就是这么攒出来的。
        if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ControllerError(
                f"snapshot.schema_version must be {SNAPSHOT_SCHEMA_VERSION} "
                f"(this controller's freeze format), got {snapshot.get('schema_version')!r}"
            )
        if snapshot.get("digest_scheme") != _DIGEST_SCHEME:
            raise ControllerError(
                f"snapshot.digest_scheme must be {_DIGEST_SCHEME!r}, got "
                f"{snapshot.get('digest_scheme')!r}; the three codekit skills share one "
                "encoding or their digests can never be compared"
            )
        if snapshot.get("committed_range") is not None:
            raise ControllerError(
                "snapshot.committed_range must be null in a code-impl freeze: this "
                "controller never commits, so it has no committed range to declare. "
                "code-review records SCOPE_SUPERSET from that null, not a BLOCKED."
            )
        worktree = snapshot.get("worktree_state")
        _required(worktree, "snapshot.worktree_state", dict)
        for field in ("head_sha", "diff_digest"):
            _required(worktree.get(field), f"snapshot.worktree_state.{field}", str)
        if not HEX64_RE.fullmatch(worktree["diff_digest"]):
            raise ControllerError(
                "snapshot.worktree_state.diff_digest must be 64 lowercase hex characters "
                f"under digest_scheme {_DIGEST_SCHEME!r}, got {worktree['diff_digest']!r}"
            )
        changed = worktree.get("changed_files")
        _required(changed, "snapshot.worktree_state.changed_files", list)
        for item in changed:
            if not isinstance(item, str) or not CHANGED_FILE_RE.fullmatch(item):
                raise ControllerError(
                    f"snapshot.worktree_state.changed_files entries must be "
                    f"'STATUS:path' (rename: 'R:old->new'), got {item!r}"
                )
        _required(snapshot.get("target_id"), "snapshot.target_id", str)
        if root_path is None:
            raise ControllerError(
                "handoff.repo_root must be a non-empty absolute path: snapshot.target_id "
                "is derived from it, so without it there is no cross-skill join key"
            )
        expected_id = _handoff_target_id(root_path, worktree["head_sha"])
        if snapshot["target_id"] != expected_id:
            raise ControllerError(
                f"snapshot.target_id {snapshot['target_id']!r} does not match "
                f"{expected_id!r} derived from repo_root + snapshot head_sha; re-run "
                "freeze-review instead of composing the Handoff snapshot by hand"
            )
        return

    raise ControllerError(f"unsupported contract kind: {kind}")


def _scope_view(result: dict[str, Any]) -> dict[str, Any]:
    """The bounded answer for the Orchestrator; the full record is already on disk (KW2-h).

    A 20-task replay printed 6.1 KB per ``check-scope`` into the main context,
    five of them empty file lists, against the same document's own rule of not
    restating state.
    """
    view: dict[str, Any] = {
        "task_id": result.get("task_id"),
        "result": result["result"],
        "current_digest": str(result.get("current_digest", ""))[:12],
        "counts": {
            key: len(result.get(key) or [])
            for key in (
                "allowed_files",
                "new_files",
                "deleted_files",
                "unauthorized_files",
                "preexisting_modified",
                "authorized_overlaps",
            )
        },
    }
    # Violations and expected overlaps are what a decision actually reads.
    for key in ("unauthorized_files", "preexisting_modified", "authorized_overlaps"):
        if result.get(key):
            view[key] = result[key]
    return view


def _freeze_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compact freeze output: the embedded preexisting diff was 9 KB per round."""
    worktree = snapshot["worktree_state"]
    return {
        "path": snapshot.get("path"),
        "freeze_index": snapshot.get("freeze_index"),
        "schema_version": snapshot["schema_version"],
        "digest_scheme": snapshot["digest_scheme"],
        "target_id": snapshot["target_id"],
        "committed_range": snapshot["committed_range"],
        "head_sha": worktree["head_sha"],
        "diff_digest": worktree["diff_digest"],
        "changed_files": worktree["changed_files"],
        "changed_files_count": len(worktree["changed_files"]),
        "frozen_at": snapshot["frozen_at"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--repo-root", required=True)
    init.add_argument("--feature-directory", required=True)
    init.add_argument("--run-id", required=True)
    init.add_argument("--allow-lossy-decoding", action="store_true",
                      help="Freeze a non-UTF-8 document with U+FFFD replacements instead of "
                           "refusing; the lossy decoding is recorded and surfaced by show/gate")

    agent = sub.add_parser("record-agent")
    agent.add_argument("--state", required=True)
    agent.add_argument("--role", required=True)
    agent.add_argument("--name", required=True)
    agent.add_argument("--session-id", required=True)
    agent.add_argument("--input-digest", required=True)
    agent.add_argument("--status", required=True, choices=sorted(AGENT_STATUSES))

    task = sub.add_parser("record-task")
    task.add_argument("--state", required=True)
    task.add_argument("--task-id", required=True)
    task.add_argument("--allowed-file", action="append", default=[])
    task.add_argument("--plan-version", type=int, default=None,
                      help="Planning iteration this task belongs to (default: current_plan_version)")

    # KW2-g: register the whole DAG in one call instead of one round-trip per task
    rts = sub.add_parser("record-tasks",
                         help="Validate a Task DAG contract file and register every task in one write")
    rts.add_argument("--state", required=True)
    rts.add_argument("--from-dag", required=True)

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
    dt.add_argument("--retry-policy", default="gated", choices=sorted(RETRY_POLICIES),
                    help="gated (default): a FAILED->RUNNING retry requires this task's "
                         "record-failure entry; prose restores the pre-KW1 behaviour")

    # Composite verdict gate: scope check + VALIDATING → PASS|FAILED
    st = sub.add_parser("settle-task",
                        help="Run the task-bound scope check, then move the task to PASS or FAILED")
    st.add_argument("--state", required=True)
    st.add_argument("--task-id", required=True)
    st.add_argument("--status", required=True, choices=("PASS", "FAILED"))
    st.add_argument("--error-fingerprint", default=None,
                    help="Required for FAILED: the key the retry budget counts")
    st.add_argument("--note", default=None, help="One-line observation for the ledger row")

    review = sub.add_parser("freeze-review")
    review.add_argument("--state", required=True)

    failure = sub.add_parser("record-failure")
    failure.add_argument("--state", required=True)
    failure.add_argument("--failure-scope", required=True, choices=("task", "overall"))
    failure.add_argument("--impacted-task-id", action="append", default=[])
    failure.add_argument("--root-cause", required=True, choices=sorted(ROOT_CAUSES))
    failure.add_argument("--error-fingerprint", required=True)

    # KW1-b: validation evidence has to be on disk, not in the transcript
    val = sub.add_parser("record-validation",
                         help="Persist one validation record (command, cwd, exit code, evidence) "
                              "bound to the working-tree digest it was collected against")
    val.add_argument("--state", required=True)
    val.add_argument("--scope", required=True, choices=sorted(VALIDATION_SCOPES))
    val.add_argument("--task-id", default=None)
    val.add_argument("--name", required=True)
    # dest 必须显式改名：``command`` 已被子解析器占用（``args.command`` 是子命令名），
    # 共用同一个 dest 会让这条参数把子命令名覆盖掉，分支再也匹配不上。
    val.add_argument("--command", dest="command_line", required=True)
    val.add_argument("--cwd", required=True)
    val.add_argument("--exit-code", type=int, default=None)
    val.add_argument("--result", required=True, choices=sorted(VALIDATION_RESULTS))
    val.add_argument("--evidence", default=None)
    val.add_argument("--evidence-file", default=None,
                     help="Path to a log file; only its digest is recorded, never its content")
    val.add_argument("--not-run-reason", default=None)
    val.add_argument("--started-at", default=None)
    val.add_argument("--finished-at", default=None)

    # KW1-c: the review verdict, bound to the frozen snapshot by the controller
    rr = sub.add_parser("record-review",
                        help="Record the code-review verdict against the newest frozen snapshot")
    rr.add_argument("--state", required=True)
    rr.add_argument("--verdict", required=True, choices=sorted(REVIEW_VERDICTS))
    rr.add_argument("--report-path", required=True)
    rr.add_argument("--validator-exit-code", required=True, type=int)
    rr.add_argument("--summary", default=None)

    # KW1-a: the delivery gate, decided from recorded evidence
    gt = sub.add_parser("gate",
                        help="Decide delivery from persisted evidence; exits 2 while blocked")
    gt.add_argument("--state", required=True)
    gt.add_argument("--mode", default="enforce", choices=("enforce", "warn-only"),
                    help="warn-only prints the same report without the non-zero exit")

    sh = sub.add_parser("show",
                        help="Bounded view of where the run actually stands (resume without "
                             "re-reading state.json)")
    sh.add_argument("--state", required=True)
    sh.add_argument("--tasks", action="store_true", help="Append one ledger row per task")

    rl = sub.add_parser("render-ledger",
                        help="Render task-ledger.md from state.json (managed block)")
    rl.add_argument("--state", required=True)
    rl.add_argument("--output", default=None)
    rl.add_argument("--print", dest="print_ledger", action="store_true")

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
            print(init_run(args.repo_root, args.feature_directory, args.run_id,
                           allow_lossy_decoding=args.allow_lossy_decoding))
        elif args.command == "record-agent":
            record_agent(args.state, args.role, args.name, args.session_id, args.input_digest, args.status)
        elif args.command == "record-task":
            record_task(args.state, args.task_id, args.allowed_file, args.plan_version)
        elif args.command == "record-tasks":
            print(json.dumps(record_tasks_from_dag(args.state, args.from_dag),
                             ensure_ascii=False))
        elif args.command == "update-task":
            print(json.dumps(update_task(args.state, args.task_id, args.status),
                             ensure_ascii=False, indent=2))
        elif args.command == "check-scope":
            print(json.dumps(_scope_view(check_scope(args.state, args.task_id)),
                             ensure_ascii=False))
        elif args.command == "expand-scope":
            print(json.dumps(expand_scope(args.state, args.task_id, args.add_file, args.reason),
                             ensure_ascii=False, indent=2))
        elif args.command == "dispatch-task":
            print(json.dumps(dispatch_task(args.state, args.task_id, args.session_id,
                                           args.input_digest, retry_policy=args.retry_policy),
                             ensure_ascii=False))
        elif args.command == "settle-task":
            print(json.dumps(settle_task(args.state, args.task_id, args.status,
                                         args.error_fingerprint, args.note),
                             ensure_ascii=False))
        elif args.command == "freeze-review":
            print(json.dumps(_freeze_view(freeze_review(args.state)), ensure_ascii=False))
        elif args.command == "record-failure":
            print(json.dumps(
                record_failure(args.state, args.failure_scope, args.impacted_task_id,
                               args.root_cause, args.error_fingerprint),
                ensure_ascii=False, indent=2,
            ))
        elif args.command == "record-validation":
            evidence = args.evidence
            if args.evidence_file:
                log = Path(args.evidence_file).expanduser().resolve(strict=False)
                if not log.is_file():
                    raise ControllerError(f"--evidence-file is not a readable file: {log}")
                payload = log.read_bytes()
                evidence = (evidence or "") + (
                    f"file://{log} ({len(payload)} bytes, "
                    "sha256:" + hashlib.sha256(payload).hexdigest()[:16] + ")"
                )
            print(json.dumps(record_validation(
                args.state, args.scope, args.name, args.command_line, args.cwd, args.exit_code,
                args.result, task_id=args.task_id, evidence=evidence,
                not_run_reason=args.not_run_reason, started_at=args.started_at,
                finished_at=args.finished_at,
            ), ensure_ascii=False))
        elif args.command == "record-review":
            print(json.dumps(record_review(
                args.state, args.verdict, args.report_path, args.validator_exit_code,
                summary=args.summary,
            ), ensure_ascii=False))
        elif args.command == "gate":
            report = gate(args.state, args.mode)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if report["status"].startswith("BLOCKED") and args.mode == "enforce":
                return 2
        elif args.command == "show":
            print(json.dumps(show(args.state, args.tasks), ensure_ascii=False, indent=2))
        elif args.command == "render-ledger":
            result = render_ledger(args.state, args.output)
            print(json.dumps(result, ensure_ascii=False)
                  if not args.print_ledger else json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "replan":
            print(json.dumps(replan(args.state, args.new_plan_version),
                             ensure_ascii=False, indent=2))
        elif args.command == "validate-contract":
            payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
            validate_contract(payload, args.kind)
            print("VALID")
        return 0
    except (
        ControllerError, OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError
    ) as exc:
        # ValueError is on purpose: ControllerError and UnicodeDecodeError both
        # descend from it, so a mis-decoded document reports BLOCKED with the
        # detected encoding instead of a traceback and exit 1.
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
