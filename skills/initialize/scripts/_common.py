"""Shared primitives for the initialize controller scripts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class InitializeError(RuntimeError):
    """Base error shared by all initialize phases."""


def configure_utf8_streams() -> None:
    """把标准流切到 UTF-8，中文提示与 JSON 才能被上层按 UTF-8 读回。

    Windows 默认用 ANSI 代码页编码 ``stdout``/``stderr``：重定向到管道时
    ``UnicodeEncodeError``，被 UTF-8 解码的调用方（Agent 工具、CI）拿到的是乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


class ValidationProgramMissing(InitializeError):
    """An allow-listed validation program is not installed on this host.

    Distinguished from other failures so the caller can record the validation
    as skipped: a missing toolchain must not turn a finished run into a failure.
    """


IGNORE_START = "# codekit:initialize:ignore:start"
IGNORE_END = "# codekit:initialize:ignore:end"
GRAPHIFY_START = "# codekit:initialize:graphify-ignore:start"
GRAPHIFY_END = "# codekit:initialize:graphify-ignore:end"
# Canonical managed .gitignore entries.  GitNexus writes mirror skills into the
# user-owned ``.claude/skills`` and ``.agents/skills`` trees, so its output is
# matched by a narrow glob instead of ignoring those trees wholesale.  The list
# is frozen rather than derived from the current worker output: a block that
# changes shape between runs would flip the git visibility of its own entries
# and make the baseline comparison meaningless.
MANAGED_GITIGNORE_ENTRIES = (
    ".codekit/",
    ".gitnexus/",
    "graphify-out/",
    ".graphifyignore",
    ".claude/skills/gitnexus*/",
    ".agents/skills/gitnexus*/",
)

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
ERROR_CODE_REGISTRY_MISSING = "registry-missing"
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
    ERROR_CODE_REGISTRY_MISSING,
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
# Repository validation commands come from the Scanner result, so they are
# untrusted input: no shell, no shell syntax, and a bounded program list.
VALIDATION_FORBIDDEN = re.compile(r"[;&|<>$`\n\r*?(){}\"'\\]")
VALIDATION_PROGRAMS = {
    "bash", "cargo", "cc", "cmake", "deno", "dtl", "gcc", "go", "gradle", "just",
    "make", "mvn", "node", "nox", "npm", "pnpm", "pre-commit", "py", "pytest",
    "python", "python3", "sh", "tox", "yarn", "zig",
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


def safe_relative(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InitializeError(f"unsafe repository-relative path: {raw!r}")
    return path


def resolved_repository_path(root: Path, raw: str) -> Path:
    relative = safe_relative(raw)
    lexical = root.joinpath(*relative.parts)
    try:
        lexical.resolve().relative_to(root)
    except ValueError as exc:
        raise InitializeError(f"path escapes repository through symlink: {raw}") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise InitializeError(f"path contains a symlink: {raw}")
    return lexical


def resolve_validation_command(root: Path, command: str) -> list[str]:
    """Turn a recorded repository command into a directly executable argv.

    The Scanner invents these strings from repository content, so they are
    treated as untrusted: shell syntax is rejected, the program must be on the
    allow list (or a repository script path), and nothing is ever run through a
    shell.  The working directory is always the frozen repository root.
    """
    if not isinstance(command, str) or not command.strip():
        raise InitializeError("repository validation command is empty")
    if VALIDATION_FORBIDDEN.search(command):
        raise InitializeError(
            f"repository validation command uses forbidden shell syntax: {command!r}"
        )
    argv = shlex.split(command)
    if not argv:
        raise InitializeError("repository validation command is empty")
    program = argv[0]
    if program.startswith(("./", "scripts/")):
        candidate = resolved_repository_path(root, program)
        if not candidate.is_file():
            raise InitializeError(f"repository validation script is missing: {program}")
        return [str(candidate), *argv[1:]]
    if program not in VALIDATION_PROGRAMS:
        raise InitializeError(f"repository validation program is not allowed: {program}")
    executable = shutil.which(program)
    if executable is None:
        raise ValidationProgramMissing(
            f"repository validation program is not installed: {program}"
        )
    return [executable, *argv[1:]]


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


def gitnexus_home() -> Path:
    """GitNexus global directory; mirrors the CLI's ``getGlobalDir()``."""
    configured = os.environ.get("GITNEXUS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".gitnexus"


def likely_writable(path: Path) -> bool:
    """Check the nearest existing directory without creating external files."""
    cursor = path if path.is_dir() else path.parent
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    return os.access(cursor, os.W_OK)


def gitnexus_registry_state(root: Path) -> dict[str, object]:
    """Report whether ``root`` is registered in the real GitNexus registry.

    GitNexus keeps the index inside the repository (``.gitnexus/``) but resolves
    repositories for ``list``/``query``/``context``/``impact``/``trace``/
    ``cypher``/``check``/``detect-changes`` and for every MCP graph query from
    ``<GITNEXUS_HOME>/registry.json``.  An index written without that
    registration therefore satisfies ``status`` while answering nothing, so the
    registry state is part of the contract rather than an optional extra.
    """
    home = gitnexus_home()
    registry = home / "registry.json"
    try:
        entries = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        entries = None
    targets: set[str] = set()
    for candidate in (root, root / ".gitnexus"):
        try:
            targets.add(os.path.normcase(str(candidate.resolve())))
        except OSError:
            continue
    registered = False
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in ("path", "storagePath"):
                raw = entry.get(key)
                if not isinstance(raw, str) or not raw:
                    continue
                try:
                    value = os.path.normcase(str(Path(raw).expanduser().resolve()))
                except OSError:
                    continue
                if value in targets:
                    registered = True
                    break
            if registered:
                break
    return {
        "dir": str(home),
        "path": str(registry),
        "registered": registered,
        "writable": likely_writable(home),
    }
