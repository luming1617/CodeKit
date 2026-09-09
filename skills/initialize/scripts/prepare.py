#!/usr/bin/env python3
"""Freeze an initialize baseline, acquire the repository lock, and prepare managed files."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from _common import (
    GRAPHIFY_END,
    GRAPHIFY_START,
    IGNORE_END,
    IGNORE_START,
    INDEX_END,
    INDEX_START,
    InitializeError,
    atomic_write,
    git_status,
    replace_managed_block,
    snapshot_trees,
    utc_now,
    utc_run_id_timestamp,
)

LANGUAGES = ("java", "python", "go", "cpp")
# Areas a Worker may populate; ``worker_tree_roots`` is watched at top-level
# entry granularity, never per file (see snapshot_worker_scopes).
WORKER_TREE_ROOTS = [".gitnexus", "graphify-out", ".claude/skills", ".agents/skills"]


PrepareError = InitializeError


def acquire_state_lock(root: Path, run_id: str) -> tuple[Path, Path, Path, bool, list[str], int]:
    """Create project-local state and acquire its exclusive initialize lock.

    The normal state location is outside ``.git``. Some hosted runners mount
    the repository read-only, so EROFS/EACCES/EPERM is an explicit, visible
    downgrade to a private temporary state directory. FileExistsError remains
    a real concurrent-run conflict.
    """
    preferred_root = root / ".codekit"
    preferred_parent = preferred_root / "initialize"
    try:
        require_repository_target(root, preferred_root)
        preferred_parent.mkdir(parents=True, exist_ok=True)
        lock_path = preferred_root / "initialize.lock"
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise PrepareError(f"another initialize run holds {lock_path}") from exc

        run_dir = preferred_parent / run_id
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise PrepareError(f"initialize run directory already exists: {run_dir}") from exc
        return preferred_root, lock_path, run_dir, False, [], descriptor
    except OSError as exc:
        if exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
            raise
        try:
            if 'descriptor' in locals():
                os.close(descriptor)
            if 'lock_path' in locals() and lock_path.exists():
                lock_path.unlink()
        except OSError:
            pass
        fallback_root = Path(tempfile.mkdtemp(prefix=f"codekit-initialize-{run_id}-"))
        fallback_parent = fallback_root / "initialize"
        fallback_parent.mkdir()
        fallback_lock = fallback_root / "initialize.lock"
        fallback_descriptor = os.open(
            fallback_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        fallback_run_dir = fallback_parent / run_id
        fallback_run_dir.mkdir()
        warning = (
            f"project-local initialize state {preferred_root} is not writable "
            f"({exc.errno}); downgraded to temporary state {fallback_root}. "
            "The temporary state will be removed after a successful finish."
        )
        print(f"initialize warning: {warning}", file=sys.stderr)
        return fallback_root, fallback_lock, fallback_run_dir, True, [warning], fallback_descriptor


def run(command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if check and completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise PrepareError(f"{' '.join(command)}: {message}")
    return completed


def repository_root(path: Path) -> Path:
    output = run(["git", "rev-parse", "--show-toplevel"], path).stdout.strip()
    return Path(output).resolve()


def parse_languages(raw: str | None, root: Path) -> list[str]:
    if raw is not None:
        parts = [item.strip().lower() for item in raw.split(",")]
        if not parts or any(not item for item in parts):
            raise PrepareError("--language contains an empty item")
        if len(parts) != len(set(parts)):
            raise PrepareError("--language contains duplicate values")
        invalid = [item for item in parts if item not in LANGUAGES]
        if invalid:
            raise PrepareError(f"unsupported language(s): {', '.join(invalid)}")
        return parts

    found: set[str] = set()
    if any((root / name).exists() for name in ("pom.xml", "build.gradle", "build.gradle.kts")):
        found.add("java")
    if any((root / name).exists() for name in ("pyproject.toml", "requirements.txt")):
        found.add("python")
    elif next((p for p in root.rglob("*.py") if ".git" not in p.parts and "graphify-out" not in p.parts), None):
        found.add("python")
    if (root / "go.mod").exists():
        found.add("go")
    makefile = root / "Makefile"
    if (root / "CMakeLists.txt").exists() or (
        makefile.exists()
        and next((p for suffix in ("*.cpp", "*.cc") for p in root.rglob(suffix)), None)
    ):
        found.add("cpp")
    return [language for language in LANGUAGES if language in found] or ["java"]


def require_repository_target(root: Path, path: Path) -> None:
    """Reject targets whose existing path components escape through symlinks."""
    root = root.resolve()
    try:
        path.parent.resolve().relative_to(root)
    except ValueError as exc:
        raise PrepareError(f"target escapes repository root: {path}") from exc
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise PrepareError(f"repository target contains a symlink: {cursor}")


def require_repository_tree(root: Path, tree: Path) -> None:
    """Reject symlinks anywhere in an existing repository-owned tree."""
    require_repository_target(root, tree)
    if not tree.exists():
        return
    for path in tree.rglob("*"):
        require_repository_target(root, path)


status_entries = git_status


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def probe(command: list[str], root: Path) -> dict[str, object]:
    version = run([*command, "--version"], root, check=False)
    help_result = run([*command, "analyze", "--help"], root, check=False)
    help_text = help_result.stdout + help_result.stderr
    return {
        "command": command,
        "version": (version.stdout or version.stderr).strip().splitlines()[:1],
        "available": version.returncode == 0 or help_result.returncode == 0,
        "supports": {
            "skip_agents_md": "--skip-agents-md" in help_text,
            "skip_skills": "--skip-skills" in help_text,
            "index_only": "--index-only" in help_text,
            "skills": "--skills" in help_text,
            "embeddings": "--embeddings" in help_text,
            "force": "--force" in help_text,
        },
    }


def _shebang_python(graphify_path: Path) -> list[str] | None:
    try:
        first_line = graphify_path.read_text(encoding="utf-8").splitlines()[0]
        if not first_line.startswith("#!"):
            return None
        parts = shlex.split(first_line[2:].strip())
    except (OSError, IndexError, ValueError):
        return None
    if not parts:
        return None

    if Path(parts[0]).name == "env":
        parts = parts[1:]
        if parts and parts[0] == "-S":
            parts = parts[1:]
        if not parts:
            return None
        interpreter = shutil.which(parts[0])
        if interpreter is None:
            return None
        return [interpreter, *parts[1:]]

    interpreter = parts[0] if os.path.isabs(parts[0]) else shutil.which(parts[0])
    if interpreter is None:
        return None
    return [interpreter, *parts[1:]]


def probe_graphify_ast_process_pool(graphify_path: Path, root: Path) -> bool:
    """Probe AST multiprocessing with the Python selected by Graphify's shebang."""
    interpreter = _shebang_python(graphify_path)
    if interpreter is None:
        return False

    probe_source = (
        "from concurrent.futures import ProcessPoolExecutor\n"
        "\n"
        "def _probe_worker(value):\n"
        "    return value * 2\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    with ProcessPoolExecutor(max_workers=2) as executor:\n"
        "        futures = [executor.submit(_probe_worker, value) for value in (1, 2)]\n"
        "        results = [future.result(timeout=1) for future in futures]\n"
        "    if results != [2, 4]:\n"
        "        raise SystemExit(1)\n"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="codekit-graphify-probe-") as directory:
            script = Path(directory) / "ast_process_pool_probe.py"
            script.write_text(probe_source, encoding="utf-8")
            completed = subprocess.run(
                [*interpreter, str(script)],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
                timeout=3,
            )
        return completed.returncode == 0
    except Exception:
        return False


def tool_capabilities(root: Path) -> dict[str, object]:
    local = root / ".gitnexus" / "run.cjs"
    if local.is_file() and shutil.which("node"):
        gitnexus = probe(["node", str(local)], root)
        gitnexus["runner"] = "local"
    elif shutil.which("gitnexus"):
        gitnexus = probe(["gitnexus"], root)
        gitnexus["runner"] = "global"
    else:
        gitnexus = {"available": False, "runner": None, "supports": {}}

    graphify_path = shutil.which("graphify")
    if graphify_path:
        version = run([graphify_path, "--version"], root, check=False)
        help_result = run([graphify_path, "--help"], root, check=False)
        extract_help = run([graphify_path, "extract", "--help"], root, check=False)
        help_text = help_result.stdout + help_result.stderr + extract_help.stdout + extract_help.stderr
        graphify = {
            "available": version.returncode == 0 or help_result.returncode == 0,
            "runner": graphify_path,
            "version": (version.stdout or version.stderr).strip().splitlines()[:1],
            "supports": {
                "update": "update" in help_text,
                "cluster_only": "cluster-only" in help_text,
                "no_viz": "--no-viz" in help_text,
                "force": "--force" in help_text,
                "code_only": "--code-only" in help_text,
                "backend_selection": "--backend" in help_text,
                "max_workers": "--max-workers" in help_text,
                "ast_process_pool": probe_graphify_ast_process_pool(Path(graphify_path), root),
            },
        }
    else:
        graphify = {"available": False, "runner": None, "version": [], "supports": {}}
    return {"gitnexus": gitnexus, "graphify": graphify}


snapshot_worker_scopes = snapshot_trees


def prepare_gitnexus_skill_dirs(root: Path) -> list[str]:
    """Prepare the two skill destinations used by GitNexus ``--skills``."""
    prepared: list[str] = []
    for relative in (".claude/skills", ".agents/skills"):
        target = root / relative
        require_repository_target(root, target)
        if not target.exists():
            target.mkdir(parents=True)
            prepared.append(relative)
        elif not target.is_dir():
            raise PrepareError(f"GitNexus skill destination is not a directory: {target}")
    return prepared


def gitnexus_skill_mode(root: Path) -> tuple[str, list[str]]:
    """Select full or community-only GitNexus skill generation.

    Some hosts expose a read-only ``.agents`` directory.  That is not a
    repository failure: GitNexus can still generate its repository-specific
    community skills while skipping the standard mirror, provided the CLI
    supports ``--skip-skills``.
    """
    try:
        prepare_gitnexus_skill_dirs(root)
    except OSError as exc:
        if exc.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
            raise
        warning = (
            f"GitNexus skill mirror destinations are not writable ({exc}); "
            "the worker must use community-only skills mode when supported."
        )
        return "community-only", [warning]
    return "full", []


def standard_targets(skill_dir: Path, root: Path, languages: list[str]) -> list[tuple[Path, Path]]:
    mappings: list[tuple[Path, Path]] = []
    roots = [(skill_dir / "standard/01-global", root / ".standard/01-global")]
    roots += [
        (skill_dir / "standard/02-stack" / language, root / ".standard/02-stack" / language)
        for language in languages
    ]
    roots.append((skill_dir / "standard/03-project", root / ".standard/03-project"))
    for source_root, target_root in roots:
        if not source_root.is_dir():
            continue
        for source in sorted(source_root.rglob("*.md")):
            if source.name == "00-instructions.md":
                continue
            mappings.append((source, target_root / source.relative_to(source_root)))
    return mappings


def update_standard_indexes(root: Path) -> list[str]:
    directories = [root / ".standard/01-global", root / ".standard/03-project"]
    stack = root / ".standard/02-stack"
    require_repository_target(root, stack)
    if stack.is_dir():
        stack_directories: list[Path] = []
        for path in sorted(stack.iterdir()):
            require_repository_target(root, path)
            if path.is_dir():
                stack_directories.append(path)
        directories[1:1] = stack_directories
    changed: list[str] = []
    for directory in directories:
        require_repository_target(root, directory)
        if not directory.is_dir():
            continue
        details = []
        for path in sorted(directory.rglob("*.md")):
            require_repository_target(root, path)
            if path.name != "00-instructions.md":
                details.append(path)
        if not details:
            continue
        index = directory / "00-instructions.md"
        require_repository_target(root, index)
        existing = index.read_text(encoding="utf-8") if index.exists() else ""
        if not existing:
            existing = (
                "这是规范索引，不是完整规范。规范优先级为：Security > Project > Stack > Global。\n\n"
                "仅在任务进入对应场景时读取相关详细文件；不要预先加载全部规范。\n"
            )
        entries = [
            f"- `{path.relative_to(root).as_posix()}`：进入与 `{path.stem}` 相关的场景时读取。"
            for path in details
        ]
        content = replace_managed_block(existing, INDEX_START, INDEX_END, entries)
        if atomic_write(index, content):
            changed.append(index.relative_to(root).as_posix())
    return changed


def main(argv: list[str] | None = None, *, emit: bool = True) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--language")
    parser.add_argument("--embeddings", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = repository_root(Path(args.root).resolve())
    skill_dir = Path(__file__).resolve().parents[1]
    languages = parse_languages(args.language, root)
    run_id = args.run_id or utc_run_id_timestamp() + "-" + uuid.uuid4().hex[:8]

    baseline_status = status_entries(root)
    mappings = standard_targets(skill_dir, root, languages)
    require_repository_tree(root, root / ".standard")
    required_standard_paths = {
        target.relative_to(root).as_posix() for _, target in mappings
    }
    required_standard_paths.update(
        (target.parent / "00-instructions.md").relative_to(root).as_posix()
        for _, target in mappings
    )
    managed = {".gitignore", ".graphifyignore", "AGENTS.md", "CLAUDE.md"}
    managed.update(target.relative_to(root).as_posix() for _, target in mappings)
    managed.update(
        path.relative_to(root).as_posix()
        for path in (root / ".standard").rglob("*.md")
        if path.is_file()
    )
    for directory in [root / ".standard/01-global", root / ".standard/03-project"]:
        if directory.is_dir() and any(path.name != "00-instructions.md" for path in directory.rglob("*.md")):
            managed.add((directory / "00-instructions.md").relative_to(root).as_posix())
    stack_root = root / ".standard/02-stack"
    if stack_root.is_dir():
        for directory in (path for path in stack_root.iterdir() if path.is_dir()):
            if any(path.name != "00-instructions.md" for path in directory.rglob("*.md")):
                managed.add((directory / "00-instructions.md").relative_to(root).as_posix())
    baseline_files = {
        path: {"exists": (root / path).is_file(), "sha256": sha256(root / path), "dirty": path in baseline_status}
        for path in sorted(managed)
    }
    head_result = run(["git", "rev-parse", "--verify", "HEAD"], root, check=False)
    tools = tool_capabilities(root)
    state_root, lock_path, run_dir, state_is_temporary, state_warnings, descriptor = acquire_state_lock(root, run_id)
    manifest_path = run_dir / "run-manifest.json"
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "root": str(root),
        "state_root": str(state_root),
        "state_run_dir": str(run_dir),
        "state_is_temporary": state_is_temporary,
        "lock_path": str(lock_path),
        "manifest_path": str(manifest_path),
        "started_at": utc_now(),
        "head": head_result.stdout.strip() if head_result.returncode == 0 else None,
        "branch": run(["git", "branch", "--show-current"], root).stdout.strip(),
        "languages": languages,
        "options": {
            "embeddings": args.embeddings,
            "force": args.force,
            "allow_partial": args.allow_partial,
            "commit": args.commit,
        },
        "baseline_status": baseline_status,
        "baseline_files": baseline_files,
        "worker_tree_roots": WORKER_TREE_ROOTS,
        "worker_scope_baseline": snapshot_worker_scopes(root, WORKER_TREE_ROOTS),
        "managed_paths": sorted(managed),
        "required_standard_paths": sorted(required_standard_paths),
        "tools": tools,
        "warnings": state_warnings,
        "prepared_changed_files": [],
        "authorized_changed_paths": [],
        "phase": "preparing",
    }

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock:
            json.dump({"schema_version": 1, "run_id": run_id, "root": str(root), "manifest": str(manifest_path), "created_at": utc_now()}, lock, ensure_ascii=False, indent=2)
            lock.write("\n")
        # Run state stays private, like the lock file; managed repository files
        # keep or receive the conventional 0644.
        atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o600)

        changed: list[str] = []
        gitnexus_mode = "not-applicable"
        if tools.get("gitnexus", {}).get("supports", {}).get("skills"):
            gitnexus_mode, skill_warnings = gitnexus_skill_mode(root)
            manifest["warnings"] = list(manifest.get("warnings", [])) + skill_warnings
            if gitnexus_mode == "community-only" and not tools.get("gitnexus", {}).get(
                "supports", {}
            ).get("skip_skills"):
                raise PrepareError(
                    "GitNexus skill mirror is not writable and --skip-skills is unsupported"
                )
        manifest["gitnexus_skill_mode"] = gitnexus_mode
        gitignore = root / ".gitignore"
        require_repository_target(root, gitignore)
        if atomic_write(gitignore, replace_managed_block(gitignore.read_text(encoding="utf-8") if gitignore.exists() else "", IGNORE_START, IGNORE_END, [".codekit/", ".gitnexus/", "graphify-out/", ".graphifyignore"])):
            changed.append(".gitignore")
        graphifyignore = root / ".graphifyignore"
        require_repository_target(root, graphifyignore)
        graphify_rules = [".codekit/", ".standard/", "AGENTS.md", "CLAUDE.md", ".gitnexus/", ".claude/", ".agents/", "graphify-out/"]
        if atomic_write(graphifyignore, replace_managed_block(graphifyignore.read_text(encoding="utf-8") if graphifyignore.exists() else "", GRAPHIFY_START, GRAPHIFY_END, graphify_rules)):
            changed.append(".graphifyignore")

        for source, target in mappings:
            require_repository_target(root, target)
            if target.name == "00-instructions.md" and target.exists():
                continue
            content = source.read_text(encoding="utf-8")
            if atomic_write(target, content.rstrip("\n") + "\n"):
                changed.append(target.relative_to(root).as_posix())

        changed.extend(update_standard_indexes(root))

        manifest["prepared_changed_files"] = sorted(changed)
        manifest["managed_paths"] = sorted(managed | set(changed))
        manifest["authorized_changed_paths"] = sorted(changed)
        manifest["phase"] = "prepared"
        atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    except Exception:
        try:
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            if lock_data.get("run_id") == run_id:
                lock_path.unlink()
        except (OSError, json.JSONDecodeError):
            pass
        if state_is_temporary:
            shutil.rmtree(state_root, ignore_errors=True)
        raise

    if emit:
        print(json.dumps({"status": "success", "run_id": run_id, "manifest": str(manifest_path), "languages": languages}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    print("initialize prepare: internal module; use controller.py start", file=sys.stderr)
    raise SystemExit(2)
