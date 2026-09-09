"""Shared primitives for the initialize controller scripts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class InitializeError(RuntimeError):
    """Base error shared by all initialize phases."""


IGNORE_START = "# codekit:initialize:ignore:start"
IGNORE_END = "# codekit:initialize:ignore:end"
GRAPHIFY_START = "# codekit:initialize:graphify-ignore:start"
GRAPHIFY_END = "# codekit:initialize:graphify-ignore:end"
INDEX_START = "<!-- codekit:initialize:index-files:start -->"
INDEX_END = "<!-- codekit:initialize:index-files:end -->"
PROJECT_START = "<!-- codekit:initialize:project-context:start -->"
PROJECT_END = "<!-- codekit:initialize:project-context:end -->"
STANDARDS_START = "<!-- codekit:initialize:standards:start -->"
STANDARDS_END = "<!-- codekit:initialize:standards:end -->"
RESERVED_MARKERS = (
    "<!-- codekit:initialize:project-context:",
    "<!-- codekit:initialize:standards:",
)

EXPECTED_TASKS = {"gitnexus", "graphify", "context-scanner"}
ERROR_CODE_TOOL_MISSING = "tool-missing"
ERROR_CODE_INDEX_CURRENT = "index-current"
ERROR_CODE_NO_CHANGE = "no-change"
ERROR_CODE_COLLAB_UNAVAILABLE = "collab-unavailable"
ERROR_CODE_CHUNK_FAILURES = "chunk-failures"
ERROR_CODE_EXECUTION_FAILED = "execution-failed"
ERROR_CODE_ARTIFACT_INVALID = "artifact-invalid"
ERROR_CODE_OUT_OF_SCOPE = "out-of-scope"
ERROR_CODE_INTERNAL = "internal-error"
COMPLETE_SKIP_CODES = frozenset({ERROR_CODE_INDEX_CURRENT, ERROR_CODE_NO_CHANGE})
WORKER_ERROR_CODES = frozenset({
    None,
    ERROR_CODE_TOOL_MISSING,
    ERROR_CODE_INDEX_CURRENT,
    ERROR_CODE_NO_CHANGE,
    ERROR_CODE_COLLAB_UNAVAILABLE,
    ERROR_CODE_CHUNK_FAILURES,
    ERROR_CODE_EXECUTION_FAILED,
    ERROR_CODE_ARTIFACT_INVALID,
    ERROR_CODE_OUT_OF_SCOPE,
    ERROR_CODE_INTERNAL,
})
TASK_ALLOWED_PATHS = {
    "gitnexus": [".gitnexus/"],
    "graphify": ["graphify-out/"],
    "context-scanner": [],
}
TASK_REQUIRED_ARTIFACTS = {
    "gitnexus": {".gitnexus/meta.json": "json-valid"},
    "graphify": {
        "graphify-out/graph.json": "json-nonempty-nodes",
        "graphify-out/manifest.json": "json-valid",
        "graphify-out/.graphify_root": "root-match",
    },
    "context-scanner": {},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_run_id_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InitializeError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InitializeError(f"expected JSON object: {path}")
    return value


def atomic_write(
    path: Path, content: str, *, mode: int | None = None, force: bool = False
) -> bool:
    """Write content through a unique sibling file and atomically replace path."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if not force and old == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644

    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def atomic_json(path: Path, value: dict) -> None:
    atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
        force=True,
    )


def replace_managed_block(
    text: str, start: str, end: str, body: str | list[str]
) -> str:
    """Replace or append a managed block, rejecting malformed marker order."""
    start_count, end_count = text.count(start), text.count(end)
    if start_count != end_count or start_count > 1:
        raise InitializeError(f"invalid managed block markers: {start} / {end}")
    if start_count == 1 and text.index(start) > text.index(end):
        raise InitializeError(f"invalid managed block markers: {start} / {end}")

    body_text = "\n".join(body) if isinstance(body, list) else body.rstrip()
    block = "\n".join((start, body_text, end)) if body_text else "\n".join((start, end))
    if start_count == 1:
        before, remainder = text.split(start, 1)
        _, after = remainder.split(end, 1)
        prefix = before.rstrip("\n")
        result = (prefix + "\n\n" if prefix else "") + block + after
    else:
        prefix = text.rstrip("\n")
        result = (prefix + "\n\n" if prefix else "") + block + "\n"
    return result.rstrip("\n") + "\n"


def git_status(root: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if completed.returncode:
        raise InitializeError(completed.stderr.strip() or "git status failed")

    records = completed.stdout.split("\0")
    entries: dict[str, str] = {}
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status, path = record[:2], record[3:]
        entries[path] = status
        if status[0] in "RC" and index < len(records):
            old_path = records[index]
            index += 1
            if old_path:
                entries[old_path] = status
    return entries


def snapshot_trees(
    root: Path, relative_roots: list[str]
) -> dict[str, dict[str, object]]:
    """Snapshot top-level entries under authorized worker output trees."""
    snapshot: dict[str, dict[str, object]] = {}
    for relative_root in relative_roots:
        tree = root / relative_root
        if not tree.is_dir():
            continue
        for entry in sorted(tree.iterdir()):
            if entry.is_symlink():
                state: dict[str, object] = {"kind": "symlink", "target": os.readlink(entry)}
            elif entry.is_file():
                stat = entry.stat()
                state = {"kind": "file", "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            else:
                stat = entry.stat()
                state = {
                    "kind": "directory",
                    "entries": len(list(entry.iterdir())),
                    "mtime_ns": stat.st_mtime_ns,
                }
            snapshot[entry.relative_to(root).as_posix()] = state
    return snapshot
