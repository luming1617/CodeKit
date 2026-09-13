#!/usr/bin/env python3
"""Validate initialize worker contracts, final files, commit gate, and lock ownership."""

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
from pathlib import Path

from _common import (
    COMPLETE_SKIP_CODES,
    ERROR_CODE_COLLAB_UNAVAILABLE,
    ERROR_CODE_TOOL_MISSING,
    EXPECTED_TASKS,
    GRAPHIFY_END,
    GRAPHIFY_START,
    IGNORE_END,
    IGNORE_START,
    INDEX_END,
    INDEX_START,
    MANAGED_GITIGNORE_ENTRIES,
    PROJECT_END,
    PROJECT_START,
    RESERVED_MARKERS,
    STANDARDS_END,
    STANDARDS_START,
    TASK_ALLOWED_PATHS,
    TASK_REQUIRED_ARTIFACTS,
    WORKER_ERROR_CODES,
    InitializeError,
    ValidationProgramMissing,
    atomic_json,
    git_status,
    gitnexus_registry_state,
    load_json,
    resolve_validation_command,
    resolved_repository_path,
    safe_relative,
    snapshot_trees,
)

VALID_STATUSES = {"success", "skipped", "failed", "partial"}
REQUIRED_FIELDS = {
    "schema_version", "run_id", "task", "status", "required", "runner", "command",
    "exit_code", "changed_files", "allowed_paths", "artifacts", "warnings", "started_at",
    "finished_at", "duration_ms", "error",
}


ValidationError = InitializeError


def cleanup_state(manifest: dict) -> dict[str, object]:
    """Remove successful-run state without touching unrelated ``.codekit`` files."""
    state_root_raw = manifest.get("state_root")
    run_dir_raw = manifest.get("state_run_dir")
    root_raw = manifest.get("root")
    if not all(isinstance(value, str) and value for value in (state_root_raw, run_dir_raw, root_raw)):
        return {"status": "skipped", "reason": "state paths are absent"}
    state_root = Path(state_root_raw).resolve()
    run_dir = Path(run_dir_raw).resolve()
    root = Path(root_raw).resolve()

    if manifest.get("state_is_temporary"):
        temp_parent = Path(tempfile.gettempdir()).resolve()
        if state_root.parent != temp_parent or not state_root.name.startswith("codekit-initialize-"):
            raise ValidationError("refusing to clean an unexpected temporary initialize state")
        shutil.rmtree(state_root, ignore_errors=False)
        return {"status": "removed", "path": str(state_root)}

    expected_root = (root / ".codekit").resolve()
    expected_run_parent = (expected_root / "initialize").resolve()
    if state_root != expected_root or run_dir.parent != expected_run_parent:
        raise ValidationError("refusing to clean state outside repository .codekit/initialize")
    if run_dir.exists():
        shutil.rmtree(run_dir)
    if expected_run_parent.is_dir() and not any(expected_run_parent.iterdir()):
        expected_run_parent.rmdir()
    if expected_root.is_dir() and not any(expected_root.iterdir()):
        expected_root.rmdir()
    return {"status": "removed", "path": str(run_dir)}


def path_allowed(raw: str, allowed: list[str]) -> bool:
    path = safe_relative(raw)
    for prefix_raw in allowed:
        prefix = safe_relative(prefix_raw.rstrip("/"))
        if path == prefix or prefix in path.parents:
            return True
    return False


def authoritative_paths(task: str, manifest: dict) -> list[str]:
    paths = list(TASK_ALLOWED_PATHS[task])
    if task == "gitnexus" and manifest.get("tools", {}).get("gitnexus", {}).get(
        "supports", {}
    ).get("skills"):
        # GitNexus emits sibling directories such as gitnexus-cli and
        # gitnexus-exploring, not only a nested gitnexus/ directory. Keep the
        # authority narrow by admitting only existing GitNexus-named dirs.
        paths.extend([".claude/skills/gitnexus/", ".agents/skills/gitnexus/"])
        root = Path(manifest.get("root", ".")).resolve()
        for relative_root in (".claude/skills", ".agents/skills"):
            skill_root = root / relative_root
            if not skill_root.is_dir():
                continue
            for child in sorted(skill_root.iterdir()):
                if child.is_dir() and child.name.startswith("gitnexus"):
                    paths.append(child.relative_to(root).as_posix() + "/")
    return paths


def file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


snapshot_worker_scopes = snapshot_trees


def worker_write_scopes(manifest: dict) -> list[str]:
    """Union of every path prefix a started Worker is authorized to write into."""
    scopes: list[str] = []
    for task in sorted(EXPECTED_TASKS):
        scopes.extend(authoritative_paths(task, manifest))
    return scopes


def covered_by(raw: str, prefixes: list[str]) -> bool:
    """Prefix containment that reports malformed claims as “not covered”."""
    try:
        return path_allowed(raw, prefixes)
    except ValidationError:
        return False


def changed_since_baseline(manifest: dict, root: Path) -> set[str]:
    """Paths changed since Phase 0 that nobody can explain by prefix authority.

    Git-visible changes are always reported per path, minus the paths Phase 0
    itself made visible by rewriting its own managed ignore file: those were
    never written by anyone, the controller merely stopped ignoring them.
    Ignored analyzer areas are compared at top-level entry granularity and
    skipped when the entry lies inside an authorized write scope, so cache
    churn no longer masquerades as an out-of-bounds write while a brand-new
    unexpected directory still trips.
    """
    baseline_status = set(manifest.get("baseline_status", {}))
    visibility_delta = manifest.get("controller_visibility_delta", [])
    if not isinstance(visibility_delta, list):
        visibility_delta = []
    current = set(git_status(root))
    changed = current - baseline_status - {
        path for path in visibility_delta if isinstance(path, str)
    }
    for path, state in manifest.get("baseline_files", {}).items():
        if state.get("dirty") and file_sha(root / path) != state.get("sha256"):
            changed.add(path)
    scopes = worker_write_scopes(manifest)
    before = manifest.get("worker_scope_baseline", {})
    after = snapshot_worker_scopes(root, manifest.get("worker_tree_roots", []))
    for path in set(before) | set(after):
        if before.get(path) == after.get(path) or covered_by(path, scopes):
            continue
        changed.add(path)
    return changed


def uncovered_paths(paths: set[str], declared: set[str]) -> set[str]:
    """Paths that no worker declaration accounts for.

    Declarations come from model output and may name a directory
    (``.agents/skills/gitnexus-cli``) where ``git status`` reports the files
    beneath it, so a declared path also covers its descendants — the same
    containment rule the scope checks use.  A plain string difference would
    report every such file as an unreported write.
    """
    prefixes = [
        raw for raw in declared if isinstance(raw, str) and raw.strip()
    ]
    return {path for path in paths if not covered_by(path, prefixes)}


def within_or_error(
    raw: str, prefixes: list[str], task: str, message: str, errors: list[str]
) -> bool:
    """Collect a path claim violation instead of letting the gate raise.

    Worker results are model output, so a malformed path (absolute, ``..``,
    empty) is a contract violation to report: raising here would abort the whole
    run and drop the structured error list the orchestrator needs.
    """
    try:
        inside = path_allowed(raw, prefixes)
    except ValidationError as exc:
        errors.append(f"{task}: {exc}")
        return False
    if not inside:
        errors.append(f"{task}: {message} {raw}")
    return inside


def validate_artifact(
    root: Path, artifact: dict, *, required_validation: str | None = None
) -> list[str]:
    errors: list[str] = []
    raw = artifact.get("path")
    if not isinstance(raw, str):
        return ["artifact path is required"]
    path = resolved_repository_path(root, raw)
    if not path.is_file():
        return [f"artifact missing: {raw}"]
    validation = required_validation or artifact.get("validation", "exists")
    if validation == "root-match":
        try:
            recorded_root = Path(path.read_text(encoding="utf-8").strip()).resolve()
        except OSError as exc:
            return [f"artifact root cannot be read: {raw}: {exc}"]
        if recorded_root != root:
            errors.append(f"artifact root mismatch: {raw}")
    if validation.startswith("json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [f"artifact is not valid JSON: {raw}: {exc}"]
        if validation == "json-nonempty-nodes":
            nodes = data.get("nodes") if isinstance(data, dict) else None
            if not isinstance(nodes, list) or not nodes:
                errors.append(f"artifact has no nodes: {raw}")
        if isinstance(data, dict) and "root" in data:
            try:
                artifact_root = Path(data["root"]).resolve()
            except TypeError:
                errors.append(f"artifact root is invalid: {raw}")
            else:
                if artifact_root != root:
                    errors.append(f"artifact root mismatch: {raw}")
    return errors


def validate_worker(result: dict, manifest: dict, root: Path) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - result.keys())
    if missing:
        errors.append(f"{result.get('task', '<unknown>')}: missing fields: {', '.join(missing)}")
        return errors
    task = result.get("task")
    if task not in EXPECTED_TASKS:
        errors.append(f"unknown worker task: {task}")
        return errors
    if result["schema_version"] != 1:
        errors.append(f"{result['task']}: unsupported schema_version")
    if result["run_id"] != manifest["run_id"]:
        errors.append(f"{result['task']}: run_id mismatch")
    if result["status"] not in VALID_STATUSES:
        errors.append(f"{result['task']}: invalid status")
    if result["status"] == "success" and result["exit_code"] != 0:
        errors.append(f"{result['task']}: success requires exit_code 0")
    if result["status"] == "skipped" and result["exit_code"] is not None:
        errors.append(f"{result['task']}: skipped requires null exit_code")
    # A partial result can be produced after a successful process exit (for
    # example, usable Graphify output with some failed chunks). Only a
    # declared failure is required to have a non-zero exit code.
    if result["status"] == "failed" and result["exit_code"] == 0:
        errors.append(f"{result['task']}: failed cannot use exit_code 0")
    if result.get("required") is not True:
        errors.append(f"{result['task']}: initialize workers must be required")
    for field in ("command", "changed_files", "allowed_paths", "artifacts", "warnings"):
        if not isinstance(result.get(field), list):
            errors.append(f"{result['task']}: {field} must be a list")
    if not isinstance(result.get("error"), dict):
        errors.append(f"{result['task']}: error must be an object")
    if errors:
        return errors
    error_code = result.get("error", {}).get("code") if isinstance(result.get("error"), dict) else None
    if not isinstance(error_code, (str, type(None))) or error_code not in WORKER_ERROR_CODES:
        errors.append(f"{result['task']}: unknown error code: {error_code!r}")
    elif result["status"] == "success" and error_code is not None:
        errors.append(f"{result['task']}: success requires null error code")
    elif result["status"] == "skipped" and error_code is None:
        errors.append(f"{result['task']}: skipped requires an error code")
    elif result["status"] == "partial" and error_code is None:
        errors.append(f"{result['task']}: partial requires an error code")
    tool = manifest.get("tools", {}).get(result["task"], {})
    if result["status"] == "skipped" and error_code == ERROR_CODE_TOOL_MISSING and tool.get("available"):
        errors.append(f"{result['task']}: installed tool cannot be reported missing")
    allowed = result.get("allowed_paths", [])
    if not all(isinstance(path, str) for path in allowed):
        errors.append(f"{result['task']}: allowed_paths must be a string list")
        allowed = []
    if not all(isinstance(path, str) for path in result.get("changed_files", [])):
        errors.append(f"{result['task']}: changed_files must be a string list")
        return errors
    if not all(isinstance(artifact, dict) for artifact in result.get("artifacts", [])):
        errors.append(f"{result['task']}: artifacts must contain objects")
        return errors
    authoritative = authoritative_paths(result["task"], manifest)
    for claimed in allowed:
        within_or_error(
            claimed.rstrip("/"), authoritative, result["task"],
            "unauthorized allowed_path claim:", errors,
        )
    for changed in result.get("changed_files", []):
        within_or_error(changed, allowed, result["task"], "changed path outside allowed_paths:", errors)
    required_artifacts = TASK_REQUIRED_ARTIFACTS[result["task"]]
    for artifact in result.get("artifacts", []):
        artifact_path = artifact.get("path") if isinstance(artifact, dict) else None
        in_scope = False
        if isinstance(artifact_path, str):
            in_scope = within_or_error(
                artifact_path, authoritative, result["task"],
                "artifact outside authoritative paths:", errors,
            ) and within_or_error(artifact_path, allowed, result["task"], "artifact outside allowed_paths:", errors)
            if in_scope:
                # Only inspect files inside the authorized scope; an out-of-scope
                # or unsafe path is already reported and must not be read.
                try:
                    errors.extend(
                        f"{result['task']}: {message}"
                        for message in validate_artifact(
                            root,
                            artifact,
                            required_validation=required_artifacts.get(artifact_path),
                        )
                    )
                except ValidationError as exc:
                    errors.append(f"{result['task']}: {exc}")
    complete = result["status"] == "success" or (
        result["status"] == "skipped" and error_code in COMPLETE_SKIP_CODES
    )
    if complete:
        artifacts = result.get("artifacts", [])
        artifact_paths = {
            artifact.get("path") for artifact in artifacts if isinstance(artifact, dict)
        }
        for required in required_artifacts:
            if required not in artifact_paths:
                errors.append(f"{result['task']}: success requires artifact: {required}")
        if result["task"] == "context-scanner" and result.get("changed_files"):
            errors.append("context-scanner: read-only worker cannot report changed files")
    if result["task"] == "gitnexus" and complete:
        # 索引写在仓库内、仓库解析却在全局 registry：只校验 .gitnexus/meta.json 会让
        # 「索引有效但 list/query/MCP 全部报 No indexed repositories」悄悄通过。这里由
        # 门禁自己在宿主环境重测注册，不采信 result 的 self-report。
        try:
            registry = gitnexus_registry_state(root)
        except OSError as exc:
            errors.append(f"gitnexus: cannot read the GitNexus registry: {exc}")
        else:
            if not registry["registered"]:
                errors.append(
                    "gitnexus: index is not registered in "
                    f"{registry['path']}; list/query/context/impact/trace/cypher/"
                    "check/detect-changes and the MCP graph queries cannot resolve "
                    "this repository"
                )
    return errors


def aggregate_status(results: list[dict]) -> str:
    if any(
        result.get("status") == "skipped"
        and isinstance(result.get("error"), dict)
        and result["error"].get("code") == ERROR_CODE_COLLAB_UNAVAILABLE
        for result in results
    ):
        return "blocked"
    if any(result.get("status") == "failed" for result in results):
        return "failed"
    for result in results:
        if result.get("status") == "partial":
            return "partial"
        if result.get("status") == "skipped":
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            if error.get("code") not in COMPLETE_SKIP_CODES:
                return "partial"
    return "success"


def workers_command(args: argparse.Namespace, *, emit: bool = True) -> int:
    manifest = load_json(args.manifest)
    root = Path(manifest["root"]).resolve()
    if manifest.get("phase") != "prepared":
        raise ValidationError("worker gate requires manifest phase 'prepared'")
    results = [load_json(path) for path in args.results]
    errors: list[str] = []
    tasks: set[str] = set()
    declared: set[str] = set(manifest.get("prepared_changed_files", []))
    receipts: dict[str, dict[str, object]] = {}
    for result in results:
        task = result.get("task", "<unknown>")
        if task in tasks:
            errors.append(f"duplicate worker task: {task}")
        tasks.add(task)
        changed_files = result.get("changed_files", [])
        if isinstance(changed_files, list):
            declared.update(path for path in changed_files if isinstance(path, str))
        errors.extend(validate_worker(result, manifest, root))
    for path, result in zip(args.results, results):
        task = result.get("task")
        if task in EXPECTED_TASKS:
            receipts[task] = {
                "sha256": file_sha(path),
                "status": result.get("status"),
                "changed_files": result.get("changed_files", []),
                "artifacts": result.get("artifacts", []),
                "version": result.get("version"),
                "mode": result.get("mode", "default"),
                "registry": result.get("registry"),
                "error_code": (
                    result.get("error", {}).get("code")
                    if isinstance(result.get("error"), dict) else None
                ),
            }

    missing_tasks = EXPECTED_TASKS - tasks
    if missing_tasks:
        errors.append("missing worker results: " + ", ".join(sorted(missing_tasks)))

    unreported = uncovered_paths(changed_since_baseline(manifest, root), declared)
    if unreported:
        errors.append("unreported paths changed since baseline: " + ", ".join(sorted(unreported)))
    if errors:
        status = "failed"
    else:
        status = aggregate_status(results)
    if status == "blocked":
        errors.append("collaboration unavailable; initialize gate is blocked")
    elif status == "partial" and not manifest.get("options", {}).get("allow_partial"):
        errors.append("partial result requires --allow-partial")
    manifest["worker_gate"] = {"status": status, "errors": errors, "tasks": sorted(tasks)}
    manifest["worker_results"] = receipts
    if not errors and status in {"success", "partial"}:
        manifest["authorized_changed_paths"] = sorted(declared)
        manifest["phase"] = "gated"
    else:
        manifest["phase"] = "aborted"
        manifest["abort_reason"] = "worker gate failed"
        try:
            manifest["lock"] = release_lock(manifest)
        except ValidationError as exc:
            manifest["lock"] = f"error: {exc}"
    atomic_json(args.manifest, manifest)
    if emit:
        print(json.dumps(manifest["worker_gate"], ensure_ascii=False, indent=2))
    return 1 if errors or status == "failed" else 0


def validate_markers(path: Path) -> list[str]:
    if not path.is_file():
        return [f"required file missing: {path.name}"]
    text = path.read_text(encoding="utf-8")
    errors = []
    for start, end in (
        (STANDARDS_START, STANDARDS_END),
        (PROJECT_START, PROJECT_END),
    ):
        if path.name == "CLAUDE.md" and "project-context" in start:
            continue
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
            errors.append(f"invalid {path.name} managed block: {start}")
    return errors


def validate_exact_block(path: Path, start: str, end: str) -> list[str]:
    if not path.is_file():
        return [f"required file missing: {path.name}"]
    text = path.read_text(encoding="utf-8")
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        return [f"invalid managed block in {path.name}: {start}"]
    return []


def validate_graphify_state(root: Path, manifest: dict) -> list[str]:
    receipt = manifest.get("worker_results", {}).get("graphify", {})
    complete = receipt.get("status") == "success" or (
        receipt.get("status") == "skipped"
        and receipt.get("error_code") in COMPLETE_SKIP_CODES
    )
    if not complete:
        return []
    errors: list[str] = []
    for artifact in receipt.get("artifacts", []):
        if isinstance(artifact, dict):
            artifact_path = artifact.get("path")
            errors.extend(validate_artifact(
                root,
                artifact,
                required_validation=TASK_REQUIRED_ARTIFACTS["graphify"].get(artifact_path),
            ))
    sidecar = root / "graphify-out/.codekit-state.json"
    if not sidecar.is_file():
        return errors + ["Graphify state sidecar is missing"]
    try:
        state = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return errors + [f"Graphify state sidecar is invalid: {exc}"]
    if state.get("schema_version") != 1:
        errors.append("Graphify state sidecar schema mismatch")
    if Path(state.get("root", "")).resolve() != root or state.get("last_status") != "success":
        errors.append("Graphify state sidecar root/status mismatch")
    ignore = root / ".graphifyignore"
    start, end = GRAPHIFY_START, GRAPHIFY_END
    if ignore.is_file():
        text = ignore.read_text(encoding="utf-8")
        if text.count(start) == 1 and text.count(end) == 1:
            managed = start + text.split(start, 1)[1].split(end, 1)[0] + end
            expected = "sha256:" + hashlib.sha256(managed.encode("utf-8")).hexdigest()
            if state.get("ignore_digest") != expected:
                errors.append("Graphify state sidecar ignore digest mismatch")
    if state.get("mode") != receipt.get("mode", "default"):
        errors.append("Graphify state sidecar mode mismatch")
    if receipt.get("version") and state.get("graphify_version") != receipt.get("version"):
        errors.append("Graphify state sidecar version mismatch")
    return errors


def validate_managed_gitignore(path: Path) -> list[str]:
    """Validate required managed entries while preserving user rules."""
    errors = validate_exact_block(path, IGNORE_START, IGNORE_END)
    if errors:
        return errors
    text = path.read_text(encoding="utf-8")
    body = text.split(IGNORE_START, 1)[1].split(IGNORE_END, 1)[0]
    present = {line.strip() for line in body.splitlines() if line.strip()}
    required = set(MANAGED_GITIGNORE_ENTRIES)
    for entry in sorted(required - present):
        errors.append(f"managed .gitignore is missing {entry}")
    return errors


def validate_standard_references(root: Path, manifest: dict) -> list[str]:
    errors: list[str] = []
    required = manifest.get("required_standard_paths")
    if not isinstance(required, list) or not required or not all(
        isinstance(path, str) for path in required
    ):
        errors.append("manifest does not contain required standard paths")
        required = []
    for relative in required:
        try:
            required_path = resolved_repository_path(root, relative)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        if not required_path.is_file():
            errors.append(f"required standard file is missing: {relative}")
    candidates = [root / ".standard/01-global/00-instructions.md"]
    stack = root / ".standard/02-stack"
    if stack.is_dir():
        candidates.extend(
            directory / "00-instructions.md"
            for directory in sorted(stack.iterdir()) if directory.is_dir()
        )
    candidates.append(root / ".standard/03-project/00-instructions.md")
    indexes = [path for path in candidates if path.is_file()]
    expected = {path.relative_to(root).as_posix() for path in indexes if path.is_file()}
    for index in indexes:
        if any(path.name != "00-instructions.md" for path in index.parent.rglob("*.md")):
            errors.extend(validate_exact_block(
                index,
                INDEX_START,
                INDEX_END,
            ))
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8") if (root / "CLAUDE.md").is_file() else ""
    imported = {line[1:].strip() for line in claude.splitlines() if line.startswith("@.standard/")}
    if imported != expected:
        errors.append("CLAUDE.md standard imports do not match the generated indexes")
    agents = (root / "AGENTS.md").read_text(encoding="utf-8") if (root / "AGENTS.md").is_file() else ""
    mentioned = set(re.findall(r"`(\.standard/[^`]+/00-instructions\.md)`", agents))
    if mentioned != expected:
        errors.append("AGENTS.md standard index references do not match the generated indexes")
    for relative in imported | mentioned:
        try:
            path = resolved_repository_path(root, relative)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file():
            errors.append(f"referenced standard index is missing: {relative}")
    return errors


def release_lock(manifest: dict) -> str:
    lock_path = Path(manifest["lock_path"])
    if not lock_path.exists():
        return "already-absent"
    lock = load_json(lock_path)
    if lock.get("run_id") != manifest.get("run_id"):
        raise ValidationError(f"refusing to release lock owned by {lock.get('run_id')}")
    lock_path.unlink()
    return "released"


def ignored_by_git(root: Path, relative: str) -> bool:
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", relative],
        cwd=root, capture_output=True, check=False,
    )
    return completed.returncode == 0


def git_value(root: Path, command: list[str]) -> str | None:
    completed = subprocess.run(
        ["git", *command], cwd=root, text=True, capture_output=True, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def run_repository_validations(
    root: Path, manifest: dict, timeout: int
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    """Execute the required validations and record the real exit codes.

    Commit evidence must not be self-attested by the orchestrating agent, so the
    controller runs every ``required_for_initialize`` command itself.  Each
    command is echoed first so the executed action stays visible in the
    transcript.  A command whose allow-listed program is not installed on this
    host is reported as ``skipped``: a missing toolchain must not fail a run that
    already wrote its context files.
    """
    results: list[dict[str, object]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for item in manifest.get("validation_commands", []):
        command = item.get("command") if isinstance(item, dict) else None
        if not isinstance(command, str):
            errors.append("recorded repository validation command is not a string")
            continue
        try:
            argv = resolve_validation_command(root, command)
        except ValidationProgramMissing as exc:
            warnings.append(f"{exc}; recorded as skipped")
            results.append({
                "command": command,
                "status": "skipped",
                "exit_code": None,
                "reason": ERROR_CODE_TOOL_MISSING,
            })
            print(f"initialize warning: {exc}; recorded as skipped", file=sys.stderr)
            continue
        except (ValidationError, ValueError) as exc:
            errors.append(f"repository validation is not executable: {exc}")
            results.append({"command": command, "status": "blocked", "exit_code": None})
            continue
        print(f"initialize: running repository validation: {command}", file=sys.stderr)
        try:
            completed = subprocess.run(
                argv, cwd=root, text=True, capture_output=True, check=False, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"repository validation could not complete: {command}: {exc}")
            results.append({"command": command, "status": "error", "exit_code": None})
            continue
        results.append({
            "command": command,
            "exit_code": completed.returncode,
            "status": "success" if completed.returncode == 0 else "failed",
            "output_tail": (completed.stdout + completed.stderr)[-2000:],
        })
        if completed.returncode:
            errors.append(f"repository validation failed: {command}")
    return results, errors, warnings


GRAPHIFY_HOOK_COMMAND = "graphify hook install"
GRAPHIFY_HOOK_ROLLBACK = "graphify hook uninstall"

# 宿主只在启动时加载 skills、agents 与上下文文件，运行中新写的文件不会热生效；
# 提示必须同时进入 final_report 和 stderr，避免只剩下一个模型可以忽略的 JSON 字段。
RESTART_NOTICE = (
    "需要重启 Agent 应用以生效：本次生成或更新的 AGENTS.md、CLAUDE.md、.standard/ "
    "与 GitNexus / Graphify skills 只有在重启 Claude Code、Codex、OpenCode 或 Qoder "
    "之后才会被加载。"
)


def graphify_git_hook_plan(manifest: dict) -> tuple[str, str]:
    """决定本次 run 是否为本仓库安装 graphify 的 git 钩子。

    返回 ``(action, detail)``：``("install", <graphify runner>)`` 或
    ``("skip", <reason>)``。这一步默认开启 —— 只在 initialize 期间刷新的图谱会在
    下一次提交后立刻腐烂，post-commit/post-checkout 钩子才是它长期有效的条件。
    只有显式 ``--no-git-hooks``、graphify 缺失或过旧、以及 graphify 阶段本身没有
    产出可用图谱时才跳过。
    """
    if not manifest.get("options", {}).get("graph_git_hooks", True):
        return "skip", "disabled-by-flag"
    result = manifest.get("worker_results", {}).get("graphify")
    status = result.get("status") if isinstance(result, dict) else None
    if status not in {"success", "partial"}:
        return "skip", f"graphify-{status or 'missing'}"
    tools = manifest.get("tools", {}).get("graphify")
    tools = tools if isinstance(tools, dict) else {}
    if not tools.get("available"):
        return "skip", "tool-missing"
    if not tools.get("supports", {}).get("git_hooks"):
        return "skip", "unsupported"
    runner = tools.get("runner")
    if not isinstance(runner, str) or not runner:
        return "skip", "tool-missing"
    return "install", runner


def install_graphify_git_hooks(
    root: Path, manifest: dict, timeout: int
) -> tuple[dict[str, object], list[str], bool]:
    """Execute ``graphify hook install`` inside this repository.

    Returns ``(record, warnings, degraded)``.  A failure is never swallowed: the
    graph would silently stop refreshing, so the run drops to ``partial`` and the
    commit gate blocks until the user re-runs the command or opts out.
    """
    action, detail = graphify_git_hook_plan(manifest)
    if action != "install":
        record: dict[str, object] = {
            "status": "skipped",
            "reason": detail,
            "command": GRAPHIFY_HOOK_COMMAND,
        }
        if detail == "unsupported":
            return record, [
                f"当前 graphify 不支持 `{GRAPHIFY_HOOK_COMMAND}`；升级后在仓库根目录执行，"
                "图谱才会在提交时自动刷新"
            ], False
        return record, [], False
    record = {"status": "failed", "command": GRAPHIFY_HOOK_COMMAND, "rollback": GRAPHIFY_HOOK_ROLLBACK}
    print(f"initialize: running {GRAPHIFY_HOOK_COMMAND}", file=sys.stderr)
    completed = None
    try:
        completed = subprocess.run(
            [detail, "hook", "install"],
            cwd=root, text=True, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record["error"] = str(exc)
    if completed is not None:
        record["exit_code"] = completed.returncode
        record["output_tail"] = (completed.stdout + completed.stderr)[-2000:]
        if completed.returncode == 0:
            record["status"] = "installed"
            return record, [], False
    tail = str(record.get("output_tail") or record.get("error") or "").strip()
    return record, [
        f"{GRAPHIFY_HOOK_COMMAND} 失败（{tail[-300:]}）：本仓库的图谱不会随提交刷新。"
        f"修复后可重试 `{GRAPHIFY_HOOK_COMMAND}`，或回滚 `{GRAPHIFY_HOOK_ROLLBACK}`；"
        "不需要该钩子时带 --no-git-hooks 重跑"
    ], True


def commit_exact_paths(root: Path, paths: list[str]) -> str:
    if not paths:
        raise ValidationError("no eligible initialize changes to commit")
    add = subprocess.run(
        ["git", "add", "--", *paths], cwd=root, text=True, capture_output=True, check=False,
    )
    if add.returncode:
        raise ValidationError(add.stderr.strip() or "git add failed")
    commit = subprocess.run(
        [
            "git", "commit", "--only", "-m",
            "chore: initialize repository for AI development", "--", *paths,
        ],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if commit.returncode:
        raise ValidationError(commit.stderr.strip() or commit.stdout.strip() or "git commit failed")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False,
    )
    if head.returncode:
        raise ValidationError(head.stderr.strip() or "cannot resolve commit SHA")
    return head.stdout.strip()


def final_command(args: argparse.Namespace, *, emit: bool = True) -> int:
    manifest = load_json(args.manifest)
    root = Path(manifest["root"]).resolve()
    errors: list[str] = []
    report: dict[str, object] = {
        "status": "failed",
        "errors": errors,
        "changed_files": [],
        "dirty_managed_at_baseline": [],
        "commit_requested": bool(manifest.get("options", {}).get("commit")),
        "commit_allowed": False,
    }
    try:
        if manifest.get("phase") != "reconciled":
            errors.append("final validation requires manifest phase 'reconciled'")
        gate_status = manifest.get("worker_gate", {}).get("status")
        if gate_status not in {"success", "partial"}:
            errors.append("worker gate is not complete")
        if manifest.get("context_status") != "success":
            errors.append("context generation did not succeed")
        if git_value(root, ["rev-parse", "--verify", "HEAD"]) != manifest.get("head"):
            errors.append("repository HEAD changed after initialize baseline")
        if git_value(root, ["branch", "--show-current"]) != manifest.get("branch"):
            errors.append("repository branch changed after initialize baseline")
        validation_results = [load_json(path) for path in args.validation_result]
        expected_validations = manifest.get("validation_commands", [])
        attested = bool(args.validation_result)
        validations: list[dict[str, object]] = []
        validation_warnings: list[str] = []
        if attested and not args.allow_attested_validations:
            errors.append(
                "--validation-result needs --allow-attested-validations: the controller "
                "executes repository validations itself so success cannot be self-attested"
            )
        if attested:
            if len(validation_results) != len(expected_validations):
                errors.append(
                    f"expected {len(expected_validations)} repository validation result(s), "
                    f"received {len(validation_results)}"
                )
            expected_commands = {
                item.get("command") for item in expected_validations if isinstance(item, dict)
            }
            received_commands: set[str] = set()
            for result in validation_results:
                command = result.get("command")
                if not isinstance(command, str) or command not in expected_commands:
                    errors.append(f"unexpected repository validation result: {command!r}")
                elif command in received_commands:
                    errors.append(f"duplicate repository validation result: {command}")
                else:
                    received_commands.add(command)
                if result.get("status") != "success" or result.get("exit_code") != 0:
                    errors.append(f"repository validation failed: {command or '<unknown>'}")
            validations = validation_results
        elif expected_validations:
            validations, execution_errors, validation_warnings = run_repository_validations(
                root, manifest, args.validation_timeout
            )
            errors.extend(execution_errors)
        errors.extend(validate_markers(root / "AGENTS.md"))
        errors.extend(validate_markers(root / "CLAUDE.md"))
        errors.extend(validate_managed_gitignore(root / ".gitignore"))
        errors.extend(validate_exact_block(root / ".graphifyignore", "# codekit:initialize:graphify-ignore:start", "# codekit:initialize:graphify-ignore:end"))
        errors.extend(validate_standard_references(root, manifest))
        errors.extend(validate_graphify_state(root, manifest))
        current_changes = changed_since_baseline(manifest, root)
        authorized = set(manifest.get("authorized_changed_paths", []))
        outside = uncovered_paths(current_changes, authorized)
        if outside:
            errors.append("changes outside managed paths: " + ", ".join(sorted(outside)))
        commit_paths = sorted(path for path in current_changes if not ignored_by_git(root, path))
        dirty_at_start = set(manifest.get("baseline_status", {}))
        dirty_at_start.update(
            path for path, state in manifest.get("baseline_files", {}).items()
            if state.get("dirty")
        )
        dirty_managed = sorted(dirty_at_start & set(commit_paths))
        commit_requested = bool(manifest.get("options", {}).get("commit"))
        # 图谱钩子是仓库级写入：只有在其余校验全部干净时才动手，已经失败的 run
        # 不再往 .git 里追加副作用。
        if errors:
            planned, reason = graphify_git_hook_plan(manifest)
            hook_record: dict[str, object] = {
                "status": "skipped",
                "reason": "run-already-failed" if planned == "install" else reason,
                "command": GRAPHIFY_HOOK_COMMAND,
            }
            hook_warnings: list[str] = []
            hook_degraded = False
        else:
            hook_record, hook_warnings, hook_degraded = install_graphify_git_hooks(
                root, manifest, args.validation_timeout
            )
        validation_warnings.extend(hook_warnings)
        for warning in hook_warnings:
            print(f"initialize warning: {warning}", file=sys.stderr)
        # A required validation that could not run keeps the run out of success:
        # the missing toolchain may hide a real regression.
        skipped_validations = [
            item for item in validations if item.get("status") == "skipped"
        ]
        degraded = bool(skipped_validations) or hook_degraded
        overall = "failed" if errors else ("partial" if degraded else gate_status)
        commit_allowed = (
            commit_requested and overall == "success" and not dirty_managed
            and bool(commit_paths) and not attested
        )
        commit_sha = None
        commit_status = "not-requested"
        if commit_requested and attested:
            commit_status = "blocked-unverified-validations"
        elif commit_requested and skipped_validations:
            commit_status = "blocked-skipped-validations"
        elif commit_requested and hook_degraded:
            commit_status = "blocked-graphify-git-hooks"
        elif commit_requested and not commit_paths:
            commit_status = "no-changes"
        elif commit_requested and not commit_allowed:
            commit_status = "blocked"
        elif commit_allowed:
            try:
                commit_sha = commit_exact_paths(root, commit_paths)
                commit_status = "committed"
            except ValidationError as exc:
                errors.append(f"commit failed: {exc}")
                overall = "failed"
                commit_allowed = False
                commit_status = "failed"
        report = {
            "status": overall,
            "errors": errors,
            "changed_files": sorted(current_changes),
            "validations": validations,
            "validation_warnings": validation_warnings,
            "validation_source": "attested" if attested else "executed",
            "graphify_git_hooks": hook_record,
            "dirty_managed_at_baseline": dirty_managed,
            "commit_requested": commit_requested,
            "commit_allowed": commit_allowed,
            "commit_paths": commit_paths,
            "commit_status": commit_status,
            "commit_sha": commit_sha,
        }
    finally:
        try:
            lock_status = release_lock(manifest)
        except ValidationError as exc:
            lock_status = f"error: {exc}"
            errors.append(str(exc))
    report["lock"] = lock_status
    if errors:
        report["status"] = "failed"
        report["commit_allowed"] = False
    report["restart_required"] = report["status"] != "failed"
    report["restart_notice"] = RESTART_NOTICE if report["restart_required"] else None
    if report["restart_required"]:
        print(f"initialize: {RESTART_NOTICE}", file=sys.stderr)
    manifest["phase"] = "finished" if report["status"] != "failed" else "aborted"
    manifest["final_report"] = report
    atomic_json(args.manifest, manifest)
    if emit:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "failed" else 0


def abort_command(args: argparse.Namespace, *, emit: bool = True) -> int:
    manifest = load_json(args.manifest)
    if manifest.get("phase") in {"finished", "aborted"}:
        raise ValidationError(f"cannot abort terminal phase: {manifest.get('phase')}")
    lock_status = release_lock(manifest)
    manifest["phase"] = "aborted"
    manifest["abort_reason"] = args.reason
    manifest["lock"] = lock_status
    atomic_json(args.manifest, manifest)
    if emit:
        print(json.dumps({"status": "aborted", "reason": args.reason, "lock": lock_status}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    workers = subparsers.add_parser("workers")
    workers.add_argument("--manifest", required=True, type=Path)
    workers.add_argument("--results", required=True, nargs="+", type=Path)
    workers.set_defaults(handler=workers_command)
    final = subparsers.add_parser("final")
    final.add_argument("--manifest", required=True, type=Path)
    final.add_argument("--validation-result", action="append", default=[], type=Path)
    final.add_argument("--allow-attested-validations", action="store_true")
    final.add_argument("--validation-timeout", type=int, default=900)
    final.set_defaults(handler=final_command)
    abort = subparsers.add_parser("abort")
    abort.add_argument("--manifest", required=True, type=Path)
    abort.add_argument("--reason", required=True)
    abort.set_defaults(handler=abort_command)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    print("initialize validation: internal module; use controller.py", file=sys.stderr)
    raise SystemExit(2)
