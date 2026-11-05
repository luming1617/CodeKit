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
    REPLY_LANGUAGE_RULE,
    InitializeError,
    atomic_write,
    git_status,
    gitnexus_registry_state,
    known_tool_dirs,
    replace_managed_block,
    runtime,
    snapshot_trees,
    tool_runnable,
    utc_now,
    utc_run_id_timestamp,
)

LANGUAGES = ("java", "python", "go", "cpp")
# 语言识别的证据表：工程清单证明该语言确实是一个工程，源码后缀补足"清单在子目录"的多模块仓库。
LANGUAGE_MANIFESTS: dict[str, tuple[str, ...]] = {
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle"),
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    "go": ("go.mod",),
    "cpp": ("CMakeLists.txt",),
}
LANGUAGE_SUFFIXES: dict[str, frozenset[str]] = {
    "java": frozenset({".java"}),
    "python": frozenset({".py"}),
    "go": frozenset({".go"}),
    # 裸 ``.h`` 不算：它同样属于 C/Objective-C，会把只带一个头文件的仓库判成 cpp。
    "cpp": frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh"}),
}
# 有源码但没有对应规范模板的语言，只用于"仓库里只找到非支持语言源码"这句说明。
UNSUPPORTED_SUFFIXES = frozenset({
    ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".kt", ".kts", ".scala", ".rb", ".php", ".cs", ".rs", ".swift", ".m", ".mm",
    ".ex", ".exs", ".dart", ".lua", ".pl", ".sh", ".ml", ".zig",
})
# 仓库根下的文档目录不计入语言证据：PRD/TDD/示例工程片段住在这里，
# 把它们当源码会让一个空项目被判成已有工程（与 technical-doc 的 greenfield 探针同口径）。
DOC_DIR_NAMES = frozenset({"doc", "docs"})
# 构建产物、工具目录与 vendored 代码：里面常有生成出来的源码，不作为语言证据。
# 根下的 ``doc``/``docs`` 不在这里——它们要遍历、但单独计数，见 scan_language_evidence。
PRUNED_DIR_NAMES = frozenset({
    ".git", ".gitnexus", ".gradle", ".idea", ".mypy_cache", ".pytest_cache", ".standard",
    ".venv", "build", "dist", "graphify-out", "node_modules", "out", "target", "venv",
})
SOURCE_SCAN_LIMIT = 50_000
MANIFEST_EVIDENCE_LIMIT = 3
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
    completed = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if check and completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise PrepareError(f"{' '.join(command)}: {message}")
    return completed


def repository_root(path: Path) -> Path:
    output = run(["git", "rev-parse", "--show-toplevel"], path).stdout.strip()
    return Path(output).resolve()


def parse_languages(raw: str | None, root: Path) -> tuple[list[str], str, str]:
    """返回 ``(语言列表, 来源, 判定依据)``；``来源`` 是 ``explicit`` 或 ``detected``。

    检测不到时不再静默回落到某一种语言：仓库里有什么语言是测量结果，不是可以替用户做的决定，
    而复制错语言的规范模板会一路带进 AGENTS.md 索引和后续所有 subagent。这里抛错，让主 Agent
    在任何写入发生前停下来问人。
    """
    if raw is not None:
        parts = [item.strip().lower() for item in raw.split(",")]
        if not parts or any(not item for item in parts):
            raise PrepareError("--language contains an empty item")
        if len(parts) != len(set(parts)):
            raise PrepareError("--language contains duplicate values")
        invalid = [item for item in parts if item not in LANGUAGES]
        if invalid:
            raise PrepareError(f"unsupported language(s): {', '.join(invalid)}")
        return parts, "explicit", f"用户指定：{'、'.join(parts)}"

    evidence = scan_language_evidence(root)
    found = [language for language in LANGUAGES if evidence["languages"].get(language)]
    if not found:
        raise PrepareError(unrecognized_language_message(evidence, root))
    return found, "detected", describe_language_evidence(evidence)


def describe_language_evidence(evidence: dict) -> str:
    """把一次实测写成一句可复核的依据：语言、源码计数与点名的清单文件。"""
    parts = []
    for language in LANGUAGES:
        if not evidence["languages"].get(language):
            continue
        detail = evidence["languages"][language]
        chunk = f"{language}（源码 {detail['sources']} 个"
        manifests = detail["manifests"]
        if manifests:
            listed = "、".join(f"`{item}`" for item in manifests[:MANIFEST_EVIDENCE_LIMIT])
            more = "" if len(manifests) <= MANIFEST_EVIDENCE_LIMIT else f" 等 {len(manifests)} 份"
            chunk += f"、工程清单 {len(manifests)} 份：{listed}{more}"
        else:
            chunk += "、无工程清单"
        parts.append(chunk + "）")
    basis = "实测识别到：" + "；".join(parts)
    for note in evidence["notes"]:
        basis += f"；{note}"
    if evidence["doc_source_files"] or evidence["doc_manifests"]:
        basis += (
            f"；`doc`/`docs` 内 {evidence['doc_source_files']} 个源码、"
            f"{evidence['doc_manifests']} 份清单不计入"
        )
    if evidence["truncated"]:
        basis += f"；枚举在第 {evidence['scanned_entries']} 个条目截断"
    return basis


def unrecognized_language_message(evidence: dict, root: Path) -> str:
    """识别失败时给人机都能读的一段：测到了什么、为什么不算、下一步问什么。"""
    lines = [
        "无法识别仓库的编程语言：根下的 `doc`/`docs` 之外，没有 java、python、go、cpp 的源码或工程清单。",
        f"实测范围：{evidence['scanned_entries']} 个仓库条目"
        + ("（已截断，后面还有文件）" if evidence["truncated"] else "（完整）")
        + f"；`doc`/`docs` 内另有 {evidence['doc_source_files']} 个源码、{evidence['doc_manifests']} 份清单，按文档排除。",
    ]
    unsupported = sorted(
        evidence["unsupported"].items(), key=lambda item: (-item[1], item[0])
    )
    if unsupported:
        counted = "、".join(f"{suffix.lstrip('.')} {count} 个" for suffix, count in unsupported[:6])
        more = "" if len(unsupported) <= 6 else f" 等 {len(unsupported)} 类"
        lines.append(
            f"仓库里只找到非支持语言源码（{counted}{more}），"
            f"当前只支持 {len(LANGUAGES)} 种语言：{'、'.join(LANGUAGES)}。"
        )
    else:
        lines.append(
            f"仓库里没有可识别的源码，可能只有文档或尚未初始化；当前只支持 {len(LANGUAGES)} 种语言："
            f"{'、'.join(LANGUAGES)}。"
        )
    for note in evidence["notes"]:
        # 差一点就算成功的证据要说出来，否则用户只会看到"没识别出来"，而仓库里明明有 .cpp
        lines.append(f"另有：{note}。")
    lines.append(
        "必须由主 Agent 停下来向用户确认要初始化哪种语言的规范，再带 --language 重跑同一条 start："
    )
    lines.append(
        f"  controller.py start --root {root} --language <" + "|".join(LANGUAGES) + "[,...]>"
    )
    lines.append("在得到答复前不得写入任何托管文件。")
    return "\n".join(lines)


def scan_language_evidence(root: Path) -> dict:
    """Walk the repository once and collect the language evidence the detection rests on.

    Root-level ``doc``/``docs`` trees are walked but counted separately: their example snippets are
    this suite's own deliverables, and counting them would let a project that has not picked a
    language yet look like an existing one -- which is exactly how a wrong ``.standard/02-stack``
    template set gets copied in.
    """
    root = Path(root).resolve()
    languages: dict[str, dict] = {
        language: {"sources": 0, "manifests": []} for language in LANGUAGES
    }
    suffix_owner = {
        suffix: language for language, suffixes in LANGUAGE_SUFFIXES.items() for suffix in suffixes
    }
    manifest_owner = {
        name: language for language, names in LANGUAGE_MANIFESTS.items() for name in names
    }
    unsupported: dict[str, int] = {}
    doc_source_files = 0
    doc_manifests = 0
    scanned = 0
    truncated = False
    root_has_makefile = (root / "Makefile").is_file()
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if name.lower() not in PRUNED_DIR_NAMES)
        in_docs = _first_relative_segment(directory, root) in DOC_DIR_NAMES
        for name in sorted(filenames):
            scanned += 1
            if scanned > SOURCE_SCAN_LIMIT:
                truncated = True
                break
            path = directory / name
            owner = manifest_owner.get(name)
            if owner is not None:
                if in_docs:
                    doc_manifests += 1
                else:
                    languages[owner]["manifests"].append(path.relative_to(root).as_posix())
                continue
            suffix = path.suffix.lower()
            if suffix in suffix_owner:
                if in_docs:
                    doc_source_files += 1
                else:
                    languages[suffix_owner[suffix]]["sources"] += 1
            elif suffix in UNSUPPORTED_SUFFIXES and not in_docs:
                unsupported[suffix] = unsupported.get(suffix, 0) + 1
        if truncated:
            break

    active: dict[str, dict] = {}
    notes: list[str] = []
    for language in LANGUAGES:
        detail = languages[language]
        if language == "cpp" and not detail["manifests"] and not root_has_makefile:
            # 单个 .cpp（JNI 桥、benchmark）撑不起一个 C++ 工程，cpp 的证据必须带构建文件
            if detail["sources"]:
                notes.append(
                    f"{detail['sources']} 个 C/C++ 源码没有配 CMakeLists.txt/Makefile，未计为 cpp 工程"
                )
            continue
        if detail["sources"] or detail["manifests"]:
            active[language] = detail
    return {
        "languages": active,
        "unsupported": unsupported,
        "doc_source_files": doc_source_files,
        "doc_manifests": doc_manifests,
        "notes": notes,
        "scanned_entries": min(scanned, SOURCE_SCAN_LIMIT),
        "truncated": truncated,
    }


def _first_relative_segment(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ""
    return relative.parts[0].lower() if relative.parts else ""


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
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                check=False,
                timeout=3,
            )
        return completed.returncode == 0
    except Exception:
        return False


def gitnexus_command() -> tuple[list[str], str]:
    """解析 GitNexus runner，返回 ``(argv 前缀, 类型)``。

    解析顺序：``CODEKIT_GITNEXUS_RUNNER``、``PATH`` 上的 ``gitnexus``、CodeKit
    已知安装目录（``installed``）。仓库内的 ``.gitnexus/run.cjs`` 是兜底 shim，
    在没有真实安装时按 ``gitnexus@latest`` 走 npx/bunx/pnpm dlx：每次解析到的
    版本、Node 版本和依赖树都可能不同，GitNexus 因而判定分析器身份变化并强制
    **全量重建**；那也违反「不通过 npx/dlx 下载」的约束。已知目录这一段是给
    「宿主把用户级 ``PATH`` 丢掉」的情况兜底的：Codex 桌面端的 shell 只继承
    机器级 ``PATH``，而安装器把 ``gitnexus`` 落在 ``%APPDATA%\\npm``。需要 shim
    时用 ``CODEKIT_GITNEXUS_RUNNER`` 显式指定。
    """
    command, runner, _ = gitnexus_resolution()
    return command, runner


def gitnexus_resolution() -> tuple[list[str], str, str]:
    """``gitnexus_command`` 的完整形态，额外带出实测状态。

    显式 override 时不再扫描：用户给定的命令就是要用的那一条，能不能跑由后续探测判定。
    """
    override = os.environ.get("CODEKIT_GITNEXUS_RUNNER", "").strip()
    if override:
        # posix=False so a Windows ``C:\...\python.exe`` keeps its backslashes.
        parts = [part.strip('"') for part in shlex.split(override, posix=False)]
        if parts:
            return parts, "override", runtime.OK
    return resolve_installed_command("gitnexus")


def resolve_installed_command(program: str) -> tuple[list[str], str, str]:
    """解析工具启动命令，返回 ``(argv, 来源, 状态)``。

    argv **保留全部元素**：清单里的 runner 可能是 ``[<node>, <cli.js>]`` 这样的多段命令，
    只取第一段会把脚本参数丢掉，等于把工具启动成一个裸解释器。来源取值 ``override`` /
    ``toolchain`` / ``path`` / ``declared`` / ``unresolved``，状态是共享模块的四态。

    **清单优先于 ``PATH``**：即使会话里恰好有一个能用的旧 shim，也按安装器写出的绝对
    argv 执行，结果才跨宿主确定。
    """
    resolution = runtime.resolve_tool(program)
    if resolution.resolved:
        return list(resolution.argv), resolution.source, runtime.OK
    return [], resolution.source, resolution.state


def graphify_command() -> tuple[list[str], str]:
    """解析 Graphify runner，返回 ``(argv 前缀, 类型)``。

    与 GitNexus 对称：``CODEKIT_GRAPHIFY_RUNNER`` 覆盖 → ``PATH`` → CodeKit 已知
    安装目录。安装器在 Windows 上把命令暴露成 ``%APPDATA%\\npm\\graphify.cmd``
    垫片（本体在私有 venv 里），两者都只在用户级 ``PATH`` 上，所以同样需要这一
    段兜底。
    """
    command, runner, _ = graphify_resolution()
    return command, runner


def graphify_resolution() -> tuple[list[str], str, str]:
    """``graphify_command`` 的完整形态，额外带出实测状态。"""
    override = os.environ.get("CODEKIT_GRAPHIFY_RUNNER", "").strip()
    if override:
        parts = [part.strip('"') for part in shlex.split(override, posix=False)]
        if parts:
            return parts, "override", runtime.OK
    return resolve_installed_command("graphify")


def known_tool_dir_hint() -> str:
    """已知安装目录的现状，拼进解析失败的提示里，让提示可操作。

    只列真实存在的目录；宿主拒绝 ``stat`` 时跳过，提示生成同样不允许失败。
    """
    directories: list[str] = []
    for path in known_tool_dirs():
        try:
            if path.is_dir():
                directories.append(str(path))
        except OSError:
            continue
    return "、".join(directories) if directories else "本机没有已存在的候选目录"


def tool_scan_clause(program: str, state: str) -> str:
    """把实测状态写成提示从句。

    ``not-readable`` 与 ``not-installed`` 必须分开说：前者代表工具可能就装在那些目录
    里、只是当前会话读不到（Codex 桌面端的沙箱会拒绝读取 ``%APPDATA%`` 下的入口），
    此时让用户去重装是错的建议。
    """
    if state == runtime.NOT_READABLE:
        return "；至少一个候选入口被宿主拒绝读取，说明它可能已安装但当前会话读不到"
    if state == runtime.NOT_EXECUTABLE:
        return "；候选入口可读，但本会话启动不起来（入口依赖的解释器或运行时不可达）"
    return "；候选入口均确认不存在，判断为本机未安装"


def escalation_hint(env_var: str) -> str:
    """工具不可用时的统一出路：重装到 CodeKit 工具链根，或显式指定 runner。

    刻意**不再建议「把目录加进 PATH」**：实测受限宿主（Codex 桌面端）的 PATH 里用户级
    条目是全的，被拒绝的是对这些目录的文件系统访问，改 PATH 无效。真正的出路是让工具
    落在已实测可读可执行的 CodeKit 工具链根里。
    """
    installer = "install.cmd install" if os.name == "nt" else "install.sh install"
    return (
        f"出路：用安装器重跑并勾选该组件（{installer}），它会按本会话实测可用的位置物化工具"
        f"到 CodeKit 工具链根（{runtime.toolchain_root()}）并写出 toolchain.json；"
        f"或用 {env_var} 显式指定当前会话真能读到且能执行的 runner。"
    )


def _shim_cli_entry(shim: Path) -> Path | None:
    """从 npm/venv 生成的转发脚本里找出真正的 CLI 入口。

    ``%APPDATA%\\npm\\gitnexus.cmd`` 只写了一句
    ``node "%dp0%\\node_modules\\gitnexus\\dist\\cli\\index.js"``，包目录并不在
    shim 的父级路径上：按文件名向上找 ``package.json`` 会一路失败，把真实安装
    误判成「分析器身份不稳定 + 缺 embedding 栈」，进而触发全量重建与向量降级。
    """
    try:
        lines = shim.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        for quoted, bare in re.findall(
            r'"([^"]+\.(?:js|cjs|mjs))"|([^\s"]+\.(?:js|cjs|mjs))', line, re.IGNORECASE
        ):
            target = (quoted or bare).strip()
            lowered = target.lower()
            anchored = "%dp0%" in lowered or "%~dp0" in lowered
            cleaned = target.replace("%~dp0%", "").replace("%dp0%", "").strip().strip('"')
            # 脚本里的 ``%dp0%`` 前缀表示「与 shim 同目录」，去掉后必须按相对路径
            # 拼接；没有该前缀的绝对路径（POSIX 生成的 shim）直接使用。
            if anchored or not os.path.isabs(cleaned):
                candidate = shim.parent / cleaned.lstrip("\\/")
            else:
                candidate = Path(cleaned)
            if candidate.is_file():
                return candidate.resolve()
    return None


def _cli_entry(path: Path) -> Path:
    """把入口折叠成真正的 CLI 文件；不是转发脚本时等价于 ``resolve()``。"""
    if path.suffix.lower() in {".cmd", ".bat"}:
        target = _shim_cli_entry(path)
        if target is not None:
            return target
    return path.resolve()


def gitnexus_cli_path(command: list[str]) -> Path | None:
    """定位 runner 真正的 CLI 入口，用于找安装根和探测可选依赖。"""
    if not command:
        return None
    if len(command) == 1:
        return _cli_entry(Path(command[0]))
    for item in reversed(command[1:]):
        candidate = Path(item)
        if candidate.suffix in {".js", ".cjs", ".mjs"} or candidate.is_file():
            return _cli_entry(candidate)
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
    registry: dict[str, object], tool: dict[str, object], authorized: bool, allow_partial: bool
) -> tuple[str, list[str]]:
    """判定 GitNexus 全局注册前提，返回 ``(action, warnings)``；不预先拦人。

    查询类命令（``list``/``query``/``context``/``impact``/``trace``/``cypher``/
    ``check``/``detect-changes``）与 MCP 图查询只从 ``<GITNEXUS_HOME>/registry.json``
    解析仓库，所以注册不是可选优化：只写仓库内索引而不注册时，``status`` 一切正常，
    查询却全部报 “No indexed repositories found”。

    但**判定方式不是预检**：``os.access`` 在按路径拒绝文件系统访问的沙箱里会谎报
    可写，用它决定「要不要问人」会把「其实写得进去」的运行也拦下来。所以这里只声明
    本次运行打算怎么处理，真正的是否有权限由一次真实尝试回答（gitnexus 步骤的
    ``gitnexus index <root>``）：

    - ``none``：已注册，或 runner 跑不起来（那类降级与注册无关）。
    - ``authorized``：用户已授权（``--allow-global-writes``），步骤直接写真实注册表。
    - ``declined``：用户用 ``--allow-partial`` 明确接受 ``partial/registry-missing``。
    - ``attempt``：先尝试真实注册；宿主拒绝时步骤以可恢复的 ``registry-denied`` 停下，
      由主 Agent 按宿主提权通道重跑同一条命令后再问用户。
    """
    if registry.get("registered") or not tool_runnable(tool):
        return "none", []
    if authorized:
        return "authorized", []
    if allow_partial:
        return "declined", [
            "已按 --allow-partial 接受 GitNexus 注册缺失：本次不写全局注册表 "
            f"{registry.get('path')}，gitnexus 阶段记为 partial/registry-missing，"
            "查询类命令与 MCP 图查询在注册前不可用。",
        ]
    return "attempt", [
        f"GitNexus 全局注册表 {registry.get('path')} 中还没有本仓库："
        "list/query/context/impact/trace/cypher/check/detect-changes 与 MCP 图查询"
        "只能从该注册表解析仓库，缺少注册时索引虽然 status 正常但一律报 "
        "“No indexed repositories found”。本次先尝试一次真实注册"
        "（gitnexus index <root>，幂等且只写 registry.json）：成功即继续，不需要任何人"
        "点头；宿主拒绝这次仓库外写入时，控制器 stop 在可恢复的 registry-denied 并给出"
        "重试命令，主 Agent 按宿主提权通道重跑同一条命令，仍失败再询问用户；用户拒绝时"
        "带 --allow-partial 接受 gitnexus 阶段降级为 partial/registry-missing。",
    ]


def tool_capabilities(root: Path) -> dict[str, object]:
    command, runner, state = gitnexus_resolution()
    cli = gitnexus_cli_path(command)
    if command:
        gitnexus = probe(command, root)
        gitnexus["command"] = command
        gitnexus["runner"] = runner
        gitnexus["cli"] = str(cli) if cli else None
        gitnexus["identity_stable"] = gitnexus_install_root(cli) is not None
        gitnexus["local_embeddings"] = gitnexus_local_embeddings(cli)
        if not gitnexus["available"] and state == runtime.OK:
            # 「能 stat 到入口」不等于「能执行」：沙箱可能拒绝读取入口或它依赖的
            # 解释器。这一支仍是 tool-missing，提示要说清是跑不起来而不是没装。
            state = runtime.NOT_EXECUTABLE
            gitnexus["resolution_hint"] = (
                f"已解析到 GitNexus runner（{command[0]}，来源 {runner}），但探测命令未"
                "能执行，判断为本会话跑不起来。"
                + escalation_hint("CODEKIT_GITNEXUS_RUNNER")
            )
        # 状态随快照落盘：门禁与报告引用实测态，而不是各自重新推导一遍。
        gitnexus["state"] = state
    else:
        gitnexus = {
            "available": False,
            "state": state,
            "runner": runner,
            "command": [],
            "cli": None,
            "identity_stable": False,
            "local_embeddings": False,
            "supports": {},
            "resolution_hint": (
                "未在 CODEKIT_GITNEXUS_RUNNER、toolchain.json、PATH 或 CodeKit 已知安装"
                "目录中找到 gitnexus"
                + tool_scan_clause("gitnexus", state)
                + "，且不会回落到 .gitnexus/run.cjs (它按 gitnexus@latest 走 npx，"
                "会让索引身份漂移并触发全量重建)。已知安装目录："
                + known_tool_dir_hint() + "。"
                + escalation_hint("CODEKIT_GITNEXUS_RUNNER")
            ),
        }

    graphify_command_argv, graphify_runner, graphify_state = graphify_resolution()
    graphify_path = graphify_command_argv[0] if graphify_command_argv else None
    if graphify_path:
        version = run([graphify_path, "--version"], root, check=False)
        help_result = run([graphify_path, "--help"], root, check=False)
        extract_help = run([graphify_path, "extract", "--help"], root, check=False)
        help_text = help_result.stdout + help_result.stderr + extract_help.stdout + extract_help.stderr
        graphify = {
            "available": version.returncode == 0 or help_result.returncode == 0,
            "runner": graphify_path,
            "runner_type": graphify_runner,
            "command": list(graphify_command_argv),
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
        if not graphify["available"] and graphify_state == runtime.OK:
            graphify_state = runtime.NOT_EXECUTABLE
            graphify["resolution_hint"] = (
                f"已解析到 Graphify runner（{graphify_path}，来源 {graphify_runner}），"
                "但探测命令未能执行，判断为本会话跑不起来。"
                + escalation_hint("CODEKIT_GRAPHIFY_RUNNER")
            )
        graphify["state"] = graphify_state
    else:
        graphify = {
            "available": False,
            "state": graphify_state,
            "runner": None,
            "runner_type": graphify_runner,
            "command": [],
            "version": [],
            "supports": {},
            "resolution_hint": (
                "未在 CODEKIT_GRAPHIFY_RUNNER、toolchain.json、PATH 或 CodeKit 已知安装"
                "目录中找到 graphify"
                + tool_scan_clause("graphify", graphify_state)
                + "。已知安装目录：" + known_tool_dir_hint() + "。"
                + escalation_hint("CODEKIT_GRAPHIFY_RUNNER")
            ),
        }
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
                "仅在任务进入对应场景时读取相关详细文件；不要预先加载全部规范。\n\n"
                f"{REPLY_LANGUAGE_RULE}\n"
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
        "--strict-tools",
        action="store_true",
        help="gate a tool that is unavailable instead of degrading to partial",
    )
    parser.add_argument(
        "--no-git-hooks",
        action="store_true",
        help="skip `graphify hook install` in this repository (git hooks are on by default)",
    )
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    root = repository_root(Path(args.root).resolve())
    skill_dir = Path(__file__).resolve().parents[1]
    languages, language_source, language_basis = parse_languages(args.language, root)
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
    gitnexus_tool = tools.get("gitnexus")
    gitnexus_tool = gitnexus_tool if isinstance(gitnexus_tool, dict) else {}
    # 索引模式始终按仓库内既有索引读取；但两个「前提」只对真能执行的 runner 生效，
    # 否则会拿一次无意义的授权去换一个跑不起来的工具。
    index_modes = gitnexus_index_modes(root)
    registry = gitnexus_registry_state(root)
    if tool_runnable(gitnexus_tool):
        embedding_mode, embedding_warnings = gitnexus_embedding_plan(
            index_modes,
            bool(gitnexus_tool.get("local_embeddings")),
            args.embeddings,
            args.allow_global_writes,
        )
        # 注册不在这里拦人：缺注册时先声明「本次会尝试一次真实注册」，是否真有权限由
        # gitnexus 步骤的一次 ``index <root>`` 回答——预检用的 os.access 在沙箱里会
        # 谎报可写，用它决定要不要问人会把本来写得进去的运行也停下。
        registry_action, registry_warnings = gitnexus_registration_plan(
            registry,
            gitnexus_tool,
            args.allow_global_writes,
            args.allow_partial,
        )
    else:
        embedding_mode, embedding_warnings = "preserve", []
        registry_action, registry_warnings = "none", []
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
        "language_source": language_source,
        "language_basis": language_basis,
        "options": {
            "embeddings": args.embeddings,
            "force": args.force,
            "allow_partial": args.allow_partial,
            "commit": args.commit,
            "allow_global_writes": args.allow_global_writes,
            "graph_git_hooks": not args.no_git_hooks,
            "strict_tools": args.strict_tools,
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
        "gitnexus_registry": {**registry, "action": registry_action},
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
        print(json.dumps({
            "status": "success",
            "run_id": run_id,
            "manifest": str(manifest_path),
            "languages": languages,
            "language_source": language_source,
            "language_basis": language_basis,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    print("initialize prepare: internal module; use controller.py start", file=sys.stderr)
    raise SystemExit(2)
