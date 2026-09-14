"""Shared primitives for the initialize controller scripts."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class InitializeError(RuntimeError):
    """Base error shared by all initialize phases."""


def _load_shared_runtime():
    """按路径加载跨技能共用的工具链解析模块。

    不能用普通 ``import``：本文件由 ``controller.py`` 用 ``spec_from_file_location``
    加载，不保证 ``skills/_shared/scripts`` 在 ``sys.path`` 上。加载失败属于打包缺陷
    （该文件与技能一同发布），不是运行期状况，所以直接抛错而不是降级。
    """
    path = Path(__file__).resolve().parents[2] / "_shared" / "scripts" / "codekit_runtime.py"
    spec = importlib.util.spec_from_file_location("codekit_runtime", path)
    if spec is None or spec.loader is None:
        raise InitializeError(f"missing shared toolchain resolver: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runtime = _load_shared_runtime()


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
# Repo-level reply-language anchor.  AGENTS.md / CLAUDE.md and the ``.standard``
# indexes are loaded into every session, so this is the only layer that keeps the
# reply language stable when the user triggers a skill with ``@skill`` alone and
# the turn carries no natural language for the host to mirror.
REPLY_LANGUAGE_RULE = (
    "回复语言：面向用户的说明、状态、判断和报告默认使用简体中文，"
    "用户只用 `@skill`/斜杠命令触发、本轮没有自然语言时同样适用；"
    "代码、命令、路径、参数、字段名与状态字面量保持原文，产物文件正文按各自模板与契约的语言要求执行。"
)
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
# 「工具在本宿主不可用」造成的降级：未安装/读不到/跑不起来，以及缺全局注册。这是环境
# 事实而不是运行失败——用户能做的只有重装或提权，与「是否接受 partial」无关，因此它
# 不再要求 --allow-partial，也不再 abort 释放锁（那才会逼出反复重跑）。真实失败
# （execution-failed、chunk-failures 等）不在此列，仍走原有的显式授权路径。
DEGRADED_TOOL_CODES = frozenset({ERROR_CODE_TOOL_MISSING, ERROR_CODE_REGISTRY_MISSING})
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


def known_tool_dirs() -> list[Path]:
    """CodeKit 可能放置命令入口的绝对目录，按优先级排列。

    实现已收敛到跨技能共享的解析模块：安装器与 7 个技能必须看同一份候选目录，否则
    两边的路径知识会各自漂移（历史上就漏掉了 pipx 在 Windows 的 ``~/.local/bin``）。
    顺序是工具链根内的目录在前，``%APPDATA%\\npm`` 与系统 Python 目录等历史落点在后。

    历史落点**必须保留**：正是尝试它们才把「未安装」与「已安装但读不到」区分开。
    """
    return runtime.toolchain_paths()


def executable_names(program: str) -> list[str]:
    """``program`` 在该宿主上可能出现的入口文件名，按可直接执行的程度排序。

    Windows 上 npm 会同时生成无扩展名的 POSIX 脚本、``.cmd`` 与 ``.ps1``；无
    扩展名那份是给 shell 用的，``CreateProcess`` 不能直接启动，所以 ``.exe`` /
    ``.cmd`` / ``.bat`` 排在前面。
    """
    return runtime.executable_names(program)


def declared_tool_scan(program: str) -> tuple[str | None, str]:
    """在 CodeKit 已知安装目录里扫描 ``program``，返回 ``(路径, 状态)``。

    状态取值见共享模块的四态：命中为 ``ok``；全都没命中时是 ``not-installed``，或
    「至少一个候选被拒绝读取」时更具体的 ``not-readable``。旧实现用
    ``Path.is_file()`` 的 ``except OSError`` 判定「被拒绝读取」，那条分支永远不可达
    （CPython 会把 ``OSError`` 吞成 ``False``），所以第二态实际产不出来——这正是
    诊断一直只能靠猜的原因。
    """
    argv, failure = runtime.scan_declared(program)
    if argv:
        return argv[0], runtime.OK
    return None, failure or runtime.NOT_INSTALLED


def declared_tool_path(program: str) -> str | None:
    """已知安装目录里 ``program`` 的路径，找不到返回 ``None``。"""
    path, _ = declared_tool_scan(program)
    return path


def tool_state(tool: dict) -> str:
    """工具快照的实测状态；未记录状态的旧 manifest 按 ``available`` 推断。"""
    state = tool.get("state")
    if isinstance(state, str) and state in runtime.TOOLCHAIN_STATES:
        return state
    return runtime.OK if tool.get("available", True) else runtime.NOT_EXECUTABLE


def tool_runnable(tool: dict) -> bool:
    """manifest 里的工具快照是否真的可以执行。

    「能 stat 到入口」不等于「能执行」：宿主沙箱可能拒绝读取入口或它依赖的解释
    器，此时探测（``--version``）失败。可用性以探测结论 ``available`` 与实测的
    ``state`` 共同为准，否则会把一次诚实跳过记成 ``execution-failed``，把「本机
    跑不了」误报成「工具坏了」。
    """
    command = tool.get("command")
    if not command or tool.get("runner") == "unresolved":
        return False
    if tool_state(tool) != runtime.OK:
        return False
    return bool(tool.get("available", True))


def which_with_fallback(program: str) -> str | None:
    """解析可执行程序：``python``/``py`` 这类不归 CodeKit 管的命令按四段解析。

    刻意不把内置运行时算进这条回退链：仓库验证命令（``py -3 -m pytest``）依赖的是
    **项目自己的**解释器环境，用只有标准库的内置运行时去跑会「真的失败」，把一个本
    该跳过的验证变成假失败。内置运行时只服务于 CodeKit 自己托管的两个工具。

    解析失败返回 ``None``，由调用方记为 ``skipped``——诚实跳过好过跑出假结论。
    """
    resolution = runtime.resolve_tool(program)
    return resolution.argv[0] if resolution.resolved else None


def resolve_validation_command(root: Path, command: str) -> list[str]:
    """Turn a recorded repository command into a directly executable argv.

    The Scanner invents these strings from repository content, so they are
    treated as untrusted: shell syntax is rejected, the program must be on the
    allow list (or a repository script path), and nothing is ever run through a
    shell.  The working directory is always the frozen repository root.

    ``PATH`` 之外还会查 CodeKit 已知的安装目录：宿主丢掉用户级 ``PATH`` 时，
    ``py`` / ``python`` 这类命令会在这里被找到，从而按真实退出码判定，而不是
    被误记成 ``skipped/tool-missing``。
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
    executable = which_with_fallback(program)
    if executable is None:
        raise ValidationProgramMissing(
            f"repository validation program is not installed: {program}"
        )
    return [executable, *argv[1:]]


def git_status(root: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
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
