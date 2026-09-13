#!/usr/bin/env python3
"""Freeze an initialize baseline, acquire the repository lock, and prepare managed files."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
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
    MANAGED_GITIGNORE_ENTRIES,
    InitializeError,
    atomic_write,
    git_status,
    gitnexus_registry_state,
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


TERMINAL_LOCK_PHASES = {"finished", "aborted"}


def inspect_conflicting_lock(lock_path: Path) -> tuple[bool, str]:
    """判定冲突锁是否可回收，返回 ``(reclaimable, detail)``。

    每一条正常退出路径都先释放锁再写终态 phase，所以「锁还在、manifest 已是终态
    或已不可读」只可能是崩溃遗留的 unlink，回收它不会侵入仍在工作的 run。phase 仍
    是中间态时该 run 可能活着，只能停止并给出精确的 ``abort`` 回收命令由人确认。
    """
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, "the lock file is not readable JSON"
    if not isinstance(lock, dict):
        return True, "the lock file is not a JSON object"
    run_id = lock.get("run_id") or "<unknown>"
    manifest_raw = lock.get("manifest")
    if not isinstance(manifest_raw, str) or not manifest_raw:
        return True, f"run {run_id} did not record a run manifest path"
    manifest_path = Path(manifest_raw)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True, f"run {run_id} has no readable manifest left: {manifest_path}"
    phase = manifest.get("phase") if isinstance(manifest, dict) else None
    if phase in TERMINAL_LOCK_PHASES:
        return True, f"run {run_id} already reached terminal phase '{phase}'"
    created_at = manifest.get("started_at") or lock.get("created_at") or "unknown"
    return False, (
        f"run {run_id} (started {created_at}, phase '{phase or 'unknown'}') may still be "
        f"active. Confirm no initialize is running, then release the lock with: "
        f"controller.py abort --manifest {manifest_path} "
        '--reason "release stale initialize lock"'
    )


def acquire_state_lock(root: Path, run_id: str) -> tuple[Path, Path, Path, bool, list[str], int]:
    """Create project-local state and acquire its exclusive initialize lock.

    The normal state location is outside ``.git``. Some hosted runners mount
    the repository read-only, so EROFS/EACCES/EPERM is an explicit, visible
    downgrade to a private temporary state directory. FileExistsError remains
    a real concurrent-run conflict.

    Repeated initialize is the supported path, so a conflicting lock is not
    treated as a stop by itself: a lock whose run is already terminal or whose
    manifest is gone is a crashed leftover and is reclaimed with a warning
    (see ``inspect_conflicting_lock``). Only a run that may still be active
    stops, and its error names the manifest and the ``abort`` command that
    releases it.
    """
    preferred_root = root / ".codekit"
    preferred_parent = preferred_root / "initialize"
    try:
        require_repository_target(root, preferred_root)
        preferred_parent.mkdir(parents=True, exist_ok=True)
        lock_path = preferred_root / "initialize.lock"
        reclaimed: list[str] = []
        for attempt in (1, 2):
            try:
                descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError as exc:
                reclaimable, detail = inspect_conflicting_lock(lock_path)
                if not reclaimable:
                    raise PrepareError(
                        f"another initialize run holds {lock_path}: {detail}"
                    ) from exc
                if attempt == 2:
                    raise PrepareError(
                        f"another initialize run re-acquired {lock_path}: {detail}"
                    ) from exc
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                warning = (
                    f"reclaimed the stale initialize lock {lock_path} left behind by "
                    f"an interrupted run ({detail})."
                )
                print(f"initialize warning: {warning}", file=sys.stderr)
                reclaimed.append(warning)

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
        return preferred_root, lock_path, run_dir, False, reclaimed, descriptor
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


def _batch_python_target(lines: list[str]) -> list[str]:
    """Resolve the interpreter behind a Windows batch launcher (npm/venv shim).

    ``graphify.CMD`` runs ``"<venv>\\Scripts\\graphify.exe" %*`` and carries no
    shebang, so the entry-point exe names the venv that owns Graphify.
    """
    for line in lines:
        for quoted, bare in re.findall(r'"([^"]+)"|([^\s"]+\.exe)', line, re.IGNORECASE):
            target = (quoted or bare).strip()
            if not target.lower().endswith(".exe") or not os.path.isabs(target):
                continue
            path = Path(target)
            if path.name.lower().startswith("python"):
                return [str(path)]
            sibling_python = path.parent / "python.exe"
            if path.parent.name.lower() == "scripts" and sibling_python.is_file():
                return [str(sibling_python)]
    return []


def _shebang_python(graphify_path: Path) -> list[str] | None:
    """Return the interpreter that actually runs Graphify, or None if undecidable."""
    try:
        lines = graphify_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    first_line = lines[0] if lines else ""
    if first_line.startswith("#!"):
        # posix=False so a Windows ``#!C:\...\python.exe`` keeps its backslashes;
        # stripping quotes restores what POSIX mode would already have removed.
        parts = [part.strip('"') for part in shlex.split(first_line[2:].strip(), posix=False)]
    else:
        parts = _batch_python_target(lines)
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


def gitnexus_command() -> tuple[list[str], str]:
    """解析 GitNexus runner，返回 ``(argv 前缀, 类型)``。

    只接受真实安装（显式覆盖或 ``PATH`` 上的 ``gitnexus``）。仓库内的
    ``.gitnexus/run.cjs`` 是兜底 shim，在没有真实安装时按 ``gitnexus@latest``
    走 npx/bunx/pnpm dlx：每次解析到的版本、Node 版本和依赖树都可能不同，
    GitNexus 因而判定分析器身份变化并强制**全量重建**；那也违反「不通过
    npx/dlx 下载」的约束。需要 shim 时用 ``CODEKIT_GITNEXUS_RUNNER`` 显式指定。
    """
    override = os.environ.get("CODEKIT_GITNEXUS_RUNNER", "").strip()
    if override:
        # posix=False so a Windows ``C:\...\python.exe`` keeps its backslashes.
        parts = [part.strip('"') for part in shlex.split(override, posix=False)]
        if parts:
            return parts, "override"
    installed = shutil.which("gitnexus")
    if installed:
        return [installed], "global"
    return [], "unresolved"


def gitnexus_cli_path(command: list[str]) -> Path | None:
    """定位 runner 真正的 CLI 入口，用于找安装根和探测可选依赖。"""
    if not command:
        return None
    if len(command) == 1:
        return Path(command[0]).resolve()
    for item in reversed(command[1:]):
        candidate = Path(item)
        if candidate.suffix in {".js", ".cjs", ".mjs"} or candidate.is_file():
            return candidate.resolve()
    return None


def gitnexus_install_root(cli: Path | None) -> Path | None:
    """从 CLI 入口向上找 ``name == "gitnexus"`` 的包根。"""
    if cli is None:
        return None
    for candidate in (cli, *list(cli.parents)[:6]):
        try:
            data = json.loads((candidate / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("name") == "gitnexus":
            return candidate
    return None


def gitnexus_local_embeddings(cli: Path | None) -> bool:
    """探测 runner 自带的本地 embedding 栈。

    这是可选依赖，安装时可能因为 onnxruntime 的 CUDA 下载被墙而被跳过；缺少它
    时任何全量重建都会在「重算已有向量」这一步直接失败。
    """
    root = gitnexus_install_root(cli)
    if root is None:
        return False
    modules = root / "node_modules"
    return (modules / "onnxruntime-node").is_dir() and (
        modules / "@huggingface" / "transformers"
    ).is_dir()


def gitnexus_index_modes(root: Path) -> dict[str, object]:
    """读取现有索引记录的分析模式；重建时必须镜像，否则触发全量重建。"""
    try:
        data = json.loads((root / ".gitnexus" / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": False, "pdg": None, "embeddings": 0, "branch": None}
    stats = data.get("stats")
    stats = stats if isinstance(stats, dict) else {}
    try:
        embeddings = int(stats.get("embeddings") or 0)
    except (TypeError, ValueError):
        embeddings = 0
    return {
        "present": True,
        "pdg": "pdg" in data,
        "embeddings": embeddings,
        "branch": data.get("branch") if isinstance(data.get("branch"), str) else None,
    }


def gitnexus_embedding_plan(
    index: dict[str, object], local_embeddings: bool, requested: bool, authorized: bool
) -> tuple[str, list[str]]:
    """决定本次运行的 embedding 策略，返回 ``(模式, warnings)``。

    索引里已有向量而 runner 缺 embedding 栈时，一次全量重建必然 exit 1；此时
    要么按用户授权先安装，要么显式 ``--drop-embeddings`` 降级，不能撞上才失败。

    - ``preserve``：正常分析，保留既有向量（GitNexus 默认行为）。
    - ``drop``：追加 ``--drop-embeddings``，放弃语义检索以换取可完成的重建。
    - ``install-authorized``：先执行 ``gitnexus embeddings install``（全局写入，
      需用户显式授权）。
    """
    present = bool(index.get("embeddings"))
    if requested and not local_embeddings:
        if authorized:
            return "install-authorized", [
                "GitNexus 需要本地 embedding 栈；已获授权，将先安装再分析。"
            ]
        raise PrepareError(
            "--embeddings 需要本地 embedding 栈，但当前 runner 未安装它；"
            "请授权全局写入（--allow-global-writes）后重试，或去掉 --embeddings"
        )
    if not present or local_embeddings:
        return "preserve", []
    if authorized:
        return "install-authorized", [
            "索引含 embedding 而 runner 缺少本地 embedding 栈：已获授权，将先安装再分析。"
        ]
    return "drop", [
        f"索引含 {index.get('embeddings')} 条 embedding，但当前 GitNexus runner 没有本地 "
        "embedding 栈；全量重建会失败，因此本次追加 --drop-embeddings 降级"
        "（语义检索失效，补齐 embedding 栈后可重新构建）。",
    ]


def gitnexus_registration_plan(
    registry: dict[str, object], runner: object, authorized: bool, allow_partial: bool
) -> list[str]:
    """判定 GitNexus 全局注册前提，返回 warnings；未授权且未接受降级时报错。

    查询类命令（``list``/``query``/``context``/``impact``/``trace``/``cypher``/
    ``check``/``detect-changes``）与 MCP 图查询只从 ``<GITNEXUS_HOME>/registry.json``
    解析仓库，所以注册不是可选优化：只写仓库内索引而不注册时，``status`` 一切正常，
    查询却全部报 “No indexed repositories found”。与 embedding 前提一样，该判定在
    任何写入之前完成：未获用户授权时零业务写入地失败，由主 Agent 询问后重跑。
    """
    if registry.get("registered") or runner in {None, "unresolved"} or authorized:
        return []
    if allow_partial:
        return [
            "已按 --allow-partial 接受 GitNexus 注册缺失：本次不写全局注册表 "
            f"{registry.get('path')}，gitnexus 阶段记为 partial/registry-missing，"
            "查询类命令与 MCP 图查询在注册前不可用。",
        ]
    raise PrepareError(
        f"GitNexus 全局注册表 {registry.get('path')} 中没有本仓库："
        "list/query/context/impact/trace/cypher/check/detect-changes 与 MCP 图查询"
        "只能从该注册表解析仓库，缺少注册时索引虽然 status 正常但一律报 "
        "“No indexed repositories found”。请询问用户是否允许在仓库之外写入注册表："
        "允许则带 --allow-global-writes 重新运行；不允许则带 --allow-partial "
        "明确接受 gitnexus 阶段降级为 partial/registry-missing。"
    )


def tool_capabilities(root: Path) -> dict[str, object]:
    command, runner = gitnexus_command()
    cli = gitnexus_cli_path(command)
    if command:
        gitnexus = probe(command, root)
        gitnexus["command"] = command
        gitnexus["runner"] = runner
        gitnexus["cli"] = str(cli) if cli else None
        gitnexus["identity_stable"] = gitnexus_install_root(cli) is not None
        gitnexus["local_embeddings"] = gitnexus_local_embeddings(cli)
    else:
        gitnexus = {
            "available": False,
            "runner": runner,
            "command": [],
            "cli": None,
            "identity_stable": False,
            "local_embeddings": False,
            "supports": {},
            "resolution_hint": (
                "未在 PATH 上找到 gitnexus，且不会回落到 .gitnexus/run.cjs "
                "(它按 gitnexus@latest 走 npx，会让索引身份漂移并触发全量重建)。"
                "请把 <npm 前缀>/bin 加入 PATH（安装器的 shell 配置步骤可自动完成），"
                "或用 CODEKIT_GITNEXUS_RUNNER 显式指定 runner。"
            ),
        }

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
                "git_hooks": "hook install" in help_text,
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
    parser.add_argument("--allow-global-writes", action="store_true")
    parser.add_argument(
        "--no-git-hooks",
        action="store_true",
        help="skip `graphify hook install` in this repository (git hooks are on by default)",
    )
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
    # 索引模式与 embedding 前提在任何写入之前判定：不满足时零业务写入地失败。
    index_modes = gitnexus_index_modes(root)
    embedding_mode, embedding_warnings = gitnexus_embedding_plan(
        index_modes,
        bool(tools.get("gitnexus", {}).get("local_embeddings")),
        args.embeddings,
        args.allow_global_writes,
    )
    # 注册前提与 embedding 前提同样在任何写入之前判定：查询类命令与 MCP 图查询只
    # 从全局 registry 解析仓库，缺注册时索引可用但查不到，必须由用户明确取舍。
    registry = gitnexus_registry_state(root)
    registry_warnings = gitnexus_registration_plan(
        registry,
        tools.get("gitnexus", {}).get("runner"),
        args.allow_global_writes,
        args.allow_partial,
    )
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
            "allow_global_writes": args.allow_global_writes,
            "graph_git_hooks": not args.no_git_hooks,
        },
        "baseline_status": baseline_status,
        "baseline_files": baseline_files,
        "controller_visibility_delta": [],
        "worker_tree_roots": WORKER_TREE_ROOTS,
        "worker_scope_baseline": snapshot_worker_scopes(root, WORKER_TREE_ROOTS),
        "managed_paths": sorted(managed),
        "required_standard_paths": sorted(required_standard_paths),
        "tools": tools,
        "gitnexus_index": index_modes,
        "gitnexus_embedding_mode": embedding_mode,
        "gitnexus_registry": registry,
        "warnings": [*state_warnings, *embedding_warnings, *registry_warnings],
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
        if atomic_write(gitignore, replace_managed_block(gitignore.read_text(encoding="utf-8") if gitignore.exists() else "", IGNORE_START, IGNORE_END, list(MANAGED_GITIGNORE_ENTRIES))):
            changed.append(".gitignore")
        graphifyignore = root / ".graphifyignore"
        require_repository_target(root, graphifyignore)
        graphify_rules = [".codekit/", ".standard/", "AGENTS.md", "CLAUDE.md", ".gitnexus/", ".claude/", ".agents/", "graphify-out/"]
        if atomic_write(graphifyignore, replace_managed_block(graphifyignore.read_text(encoding="utf-8") if graphifyignore.exists() else "", GRAPHIFY_START, GRAPHIFY_END, graphify_rules)):
            changed.append(".graphifyignore")
        # Rewriting the managed block can un-ignore paths an earlier run had
        # ignored, which flips their git visibility without anyone writing them.
        # Only untracked paths can be affected: ignore rules never apply to a
        # tracked file, so a tracked path that appears here was genuinely
        # written and stays a declared change.  Recorded so the barrier does not
        # report the un-ignored files as unreported changes.
        visibility_delta = sorted(
            path
            for path, status in status_entries(root).items()
            if status == "??" and path not in baseline_status
        )

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
        manifest["controller_visibility_delta"] = visibility_delta
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
