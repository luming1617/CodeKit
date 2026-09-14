#!/usr/bin/env python3
"""Shared toolchain resolution for every CodeKit skill.

Why this module exists: a hosted agent sandbox can be *path-selective*.  Measured
inside Codex Desktop on Windows, the sandbox denies `%APPDATA%` and
`%LOCALAPPDATA%\\Programs` outright while still allowing reads and execution under
the user data directory.  `PATH` itself is fine -- it even contains the user-level
entries -- but `shutil.which` has to `stat` each candidate and that `stat` is what
gets refused.  So name-based resolution fails for reasons that have nothing to do
with the tool being installed, and the old two-state answer ("found" / "not found")
could not tell a user which of the two very different situations they were in.

This module answers four states instead, and resolves in a fixed order where the
installer's own manifest outranks `PATH`:

    CODEKIT_<TOOL>_RUNNER  ->  toolchain.json  ->  PATH  ->  known directories

The manifest is a *hint*, never an authority: every entry is re-validated here, and
any entry that does not hold is downgraded to its measured state and the next stage
is tried.  Nothing in this module repairs anything -- the toolchain root is
write-denied inside the very sandbox that makes it necessary, so repair belongs to
the installer alone.  Skills only read, classify, degrade and report.

Stdlib only, no side effects at import, so every skill can load it by path.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

OK = "ok"
NOT_INSTALLED = "not-installed"
NOT_READABLE = "not-readable"
NOT_EXECUTABLE = "not-executable"
TOOLCHAIN_STATES = (OK, NOT_INSTALLED, NOT_READABLE, NOT_EXECUTABLE)

MANIFEST_SCHEMA_VERSION = 1
RUNNER_ENV_TEMPLATE = "CODEKIT_{}_RUNNER"
# 由 CodeKit 自持工具链根接管的工具。权威来源是 ``scripts/component-registry.py`` 的
# ``entry_kind`` 字段（安装器侧的名单由它派生，不再有第二份清单）；这里保留一份字面量
# 是因为技能随插件分发、不能导入 scripts/。两侧的一致性由
# ``scripts/test_installer_toolchain.py`` 的跨模块用例断言。
KNOWN_TOOLS = ("gitnexus", "graphify")
# 可直接被 CreateProcess 启动的入口扩展名；Windows 上 npm 还会生成无扩展名的 POSIX
# 脚本与 .ps1，那两份都不能直接启动，所以排在 .exe/.cmd 之后。
EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat") if os.name == "nt" else ("")


def errno_name(exc: BaseException) -> str:
    """``PermissionError:EACCES:13`` 这类精确失败形态，用于区分「读不到」与「不存在」。"""
    if isinstance(exc, OSError) and exc.errno is not None:
        import errno as errno_module

        code = errno_module.errorcode.get(exc.errno, str(exc.errno))
        return f"{exc.__class__.__name__}:{code}:{exc.errno}"
    return exc.__class__.__name__


def toolchain_root(home: Optional[Path] = None) -> Path:
    """CodeKit 自持工具链根；与 installer_platform 的 ``toolchain_root`` 同源。

    刻意不读 ``XDG_DATA_HOME``：安装器也不读它，两边必须落在同一个目录。这条契约由
    ``scripts/test_installer_toolchain.py`` 的跨模块用例保证。
    """
    base = Path(home) if home else Path.home()
    return base / ".local" / "share" / "CodeKit"


def manifest_path(home: Optional[Path] = None) -> Path:
    return toolchain_root(home) / "toolchain.json"


def runtime_root(home: Optional[Path] = None) -> Path:
    return toolchain_root(home) / "runtime"


def runtime_python(home: Optional[Path] = None) -> Optional[Path]:
    """内置运行时的解释器，未安装或指针失效时返回 None。"""
    root = runtime_root(home)
    try:
        document = json.loads((root / "current.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    python = document.get("python")
    if isinstance(python, str) and python:
        candidate = Path(python)
        if candidate.is_file():
            return candidate
    tag = document.get("tag")
    if not isinstance(tag, str) or not tag:
        return None
    suffix = Path("python") / ("python.exe" if os.name == "nt" else "bin/python3")
    candidate = root / tag / suffix
    return candidate if candidate.is_file() else None


def read_manifest(home: Optional[Path] = None) -> dict:
    """读取 ``toolchain.json``；缺失、非法或 schema 不符都降级为空字典。

    绝不能因为清单坏了就抛错：清单是提示，坏了只是退回猜路径，不是失败。
    """
    try:
        document = json.loads(manifest_path(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return {}
    return document


# --------------------------------------------------------------------------
# 状态分类
# --------------------------------------------------------------------------


def classify_state(path: Path) -> str:
    """``ok`` / ``not-installed`` / ``not-readable``，只按显式 ``stat`` 判定。

    不能用 ``Path.is_file``/``exists``：CPython 会把 ``OSError`` 吞掉返回 ``False``，
    于是「被拒绝读取」被压成「不存在」——这正是旧解析层里那个永远不可达的 ``denied``
    分支的成因，也是诊断一直说不清三态的原因。
    """
    try:
        os.stat(path)
    except FileNotFoundError:
        return NOT_INSTALLED
    except NotADirectoryError:
        return NOT_INSTALLED
    except PermissionError:
        return NOT_READABLE
    except OSError:
        return NOT_READABLE
    return OK


def readable_state(path: Path) -> str:
    """既要能 stat、也要能读到内容。

    目录只看 ``stat``：Windows 上 ``open(目录)`` 一律抛 EACCES，把它当成「被拒绝读取」
    会把完全可用的路径标成不可读。

    打不开但也不是权限问题的入口（典型是 ``%LOCALAPPDATA%\\Microsoft\\WindowsApps``
    里的商店别名桩：``stat`` 能过，``open`` 抛 EINVAL）记为 ``not-executable``——它确实
    存在，只是不可用；报 ``not-readable`` 会把「这不是个真程序」说成「宿主不让读」。
    """
    state = classify_state(path)
    if state != OK:
        return state
    if path.is_dir():
        return OK
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except PermissionError:
        return NOT_READABLE
    except OSError:
        return NOT_EXECUTABLE
    return OK


def _list_subdirectories(root: Path) -> list[Path]:
    """列出子目录；被拒绝枚举时返回空列表。

    枚举失败必须等价于「这个候选不存在」：沙箱可能只放行 ``ReadAttributes`` 而不放行
    ``ListDirectory``，一个被拒的候选不能带走整轮解析。
    """
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    directories: list[Path] = []
    for path in entries:
        try:
            if path.is_dir():
                directories.append(path)
        except OSError:
            continue
    return directories


def venv_of(entry: Path) -> Optional[Path]:
    """``entry`` 所属的 venv 根（含 ``pyvenv.cfg`` 的那层），不在 venv 里则 None。"""
    for candidate in [entry.parent, *list(entry.parents)[:4]]:
        try:
            if (candidate / "pyvenv.cfg").is_file():
                return candidate
        except OSError:
            continue
    return None


def venv_base_state(venv: Path) -> dict:
    """venv 会 re-exec 的解释器是否可达，返回 ``{"state", "values"}``。

    venv 里的 ``python.exe`` 只是转发器：它读 ``pyvenv.cfg`` 的 ``home``/``executable``
    再启动真正的解释器。这两个路径落在不可读目录时（旧 CodeKit 用系统 Python 建的
    venv 就是这样），venv 在、入口能 stat，但启动必失败（实测 rc=101
    ``Unable to create process``）。把这一态单独报出来，才不用让用户去猜一个退出码。
    """
    values: dict[str, str] = {}
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        state = NOT_READABLE if exc.errno in {13, 1} else NOT_INSTALLED
        return {"state": state, "values": values}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    targets = [values.get(key, "") for key in ("executable", "home")]
    if not any(targets):
        return {"state": NOT_EXECUTABLE, "values": values}
    for raw in targets:
        if not raw:
            continue
        state = classify_state(Path(raw))
        if state != OK:
            return {"state": state, "values": values}
    return {"state": OK, "values": values}


def classify_argv(argv: Sequence[str]) -> tuple[str, str]:
    """判定一条启动 argv 在本会话能否落地，返回 ``(state, detail)``。

    逐个元素看可读性，并对 Python 工具额外校验 venv 的基础解释器：只检查第一个元素
    会把「入口可读但解释器不可达」漏成 ok，而那种情况下命令一定跑不起来。
    """
    if not argv:
        return NOT_INSTALLED, "启动命令为空"
    for item in argv:
        state = readable_state(Path(item))
        if state != OK:
            return state, f"{item} 的状态是 {state}"
    venv = venv_of(Path(argv[0]))
    if venv is not None:
        base = venv_base_state(venv)
        if base["state"] != OK:
            # 「基础解释器被拒绝读取」和「基础解释器不存在」要分开报：前者是沙箱限制，
            # 出路是提权重装；后者是安装残缺，出路是重装。两者都不是「没装过这个工具」。
            state = NOT_READABLE if base["state"] == NOT_READABLE else NOT_EXECUTABLE
            home = base["values"].get("home") or base["values"].get("executable") or "未知"
            return state, f"venv 的基础解释器不可达（{home}）：{base['state']}"
    return OK, ""


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


@dataclass
class Resolution:
    """一次工具解析的完整结论。"""

    name: str
    argv: list[str] = field(default_factory=list)
    source: str = "unresolved"
    state: str = NOT_INSTALLED
    detail: str = ""
    # 逐个来源的实测记录，顺序即解析顺序；只有 ``explain`` 时才对外输出。
    stages: list[dict] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(self.argv) and self.state == OK

    def to_json(self, *, explain: bool = False) -> dict:
        payload = {
            "tool": self.name,
            "state": self.state,
            "source": self.source,
            "argv": list(self.argv),
            "detail": self.detail,
        }
        if explain:
            payload["stages"] = [dict(stage) for stage in self.stages]
        return payload


def runner_env_name(name: str) -> str:
    return RUNNER_ENV_TEMPLATE.format(name.upper().replace("-", "_"))


def runner_override(name: str, environ: Optional[dict] = None) -> list[str]:
    """``CODEKIT_<TOOL>_RUNNER`` 指定的启动命令（可含参数）。

    用 ``posix=False`` 解析，否则 Windows 路径里的反斜杠会被当转义符吃掉，得到一条
    不存在的路径，报出来却像「工具没装」。
    """
    import shlex

    environ = os.environ if environ is None else environ
    raw = (environ.get(runner_env_name(name)) or "").strip()
    if not raw:
        return []
    parts = [part.strip('"') for part in shlex.split(raw, posix=False)]
    return [part for part in parts if part]


def manifest_argv(document: dict, name: str) -> list[str]:
    """清单里该工具的启动 argv；条目缺失或形状不对时返回空。

    ``state`` 必须是 ``ok``：安装器写入的非 ok 条目是「实测不可用」的记录，采信它等于
    把一次失败的安装当成可用工具。
    """
    tools = document.get("tools")
    if not isinstance(tools, dict):
        return []
    entry = tools.get(name)
    if not isinstance(entry, dict) or entry.get("state") != OK:
        return []
    runner = entry.get("runner")
    if not isinstance(runner, list):
        return []
    return [str(item) for item in runner if isinstance(item, str) and item]


def toolchain_paths(home: Optional[Path] = None) -> list[Path]:
    """候选目录，工具链根内的在前，历史落点在后。

    历史落点（``%APPDATA%\\npm``、``%LOCALAPPDATA%\\Programs\\Python``）在受限宿主里被
    拒绝读取，但**必须保留在候选里**：正是尝试它们才把「未安装」与「已安装但读不到」
    区分开，去掉它们三态就退化成两态。
    """
    home_path = Path(home) if home else Path.home()
    root = toolchain_root(home_path)
    scripts = "Scripts" if os.name == "nt" else "bin"
    paths = [root / "npm", root / "bin"]
    python = runtime_python(home_path)
    if python is not None:
        paths.append(python.parent)
    for venv in _list_subdirectories(root / "venvs"):
        paths.append(venv / scripts)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "npm")
        paths.append(home_path / "AppData" / "Roaming" / "npm")
        local = Path(os.environ.get("LOCALAPPDATA") or home_path / "AppData" / "Local")
        paths.append(local / "Programs" / "Python" / "Launcher")
        # pipx 在 Windows 上也用 ~/.local/bin，而它在偏好顺序里排在 venv 之前。
        paths.append(home_path / ".local" / "bin")
        versioned = [
            path
            for path in _list_subdirectories(local / "Programs" / "Python")
            if path.name.startswith("Python3")
        ]
        paths.extend(sorted(versioned, key=lambda path: path.name, reverse=True))
    else:
        paths += [home_path / ".local" / "bin", home_path / ".npm-global" / "bin"]
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def executable_names(program: str) -> list[str]:
    """``program`` 在该宿主上可能出现的入口文件名，按可直接执行的程度排序。"""
    if os.name != "nt":
        return [program]
    return [f"{program}.exe", f"{program}.cmd", f"{program}.bat", program]


def scan_declared(name: str, home: Optional[Path] = None) -> tuple[list[str], str]:
    """在候选目录里按名字扫描，返回 ``(argv, 失败态)``；命中时失败态为空串。

    命中即返回；全都命中不到时，把**最早遇到的具体失败态**带出去——扫描顺序里
    ``%APPDATA%`` 这类被拒绝的目录排在可读目录之前时，用户看到的就是「已安装但读不到」
    而不是一句笼统的「未安装」。失败态为空串或 ``not-installed`` 时才是真的没装。
    """
    failure = ""
    for directory in toolchain_paths(home):
        for entry in executable_names(name):
            candidate = directory / entry
            state = classify_state(candidate)
            if state == OK:
                return [str(candidate)], ""
            if state != NOT_INSTALLED and not failure:
                failure = state
    return [], failure


def resolve_tool(
    name: str, *, home: Optional[Path] = None, document: Optional[dict] = None,
    environ: Optional[dict] = None,
) -> Resolution:
    """按固定顺序解析一个工具，返回带四态的结论。

    顺序是 ``override`` → ``toolchain.json`` → ``PATH`` → 已知目录。清单优先于 ``PATH``
    是刻意的：即使会话里恰好有一个能用的旧 shim，也按清单里的绝对 argv 执行，结果才
    跨宿主确定。首个 ``ok`` 胜出；全都不 ok 时报告**最早出现的最具体**那一态。
    """
    document = read_manifest(home) if document is None else document
    stages: list[tuple[str, list[str]]] = []
    override = runner_override(name, environ)
    if override:
        stages.append(("override", override))
    argv = manifest_argv(document, name)
    if argv:
        stages.append(("toolchain", argv))
    found = shutil.which(name)
    if found:
        stages.append(("path", [found]))
    declared, declared_failure = scan_declared(name, home)
    if declared:
        stages.append(("declared", declared))

    failure: Optional[Resolution] = None
    trace: list[dict] = []
    for source, candidate in stages:
        state, detail = classify_argv(candidate)
        trace.append(
            {"source": source, "state": state, "argv": list(candidate), "detail": detail}
        )
        if state == OK:
            return Resolution(name, list(candidate), source, OK, stages=trace)
        if failure is None:
            failure = Resolution(name, list(candidate), source, state, detail)
    if failure is not None:
        failure.stages = trace
        return failure

    if declared_failure and declared_failure != NOT_INSTALLED:
        detail = f"候选入口的状态是 {declared_failure}，判断为已安装但当前会话不可用"
        trace.append(
            {"source": "declared", "state": declared_failure, "argv": [], "detail": detail}
        )
        return Resolution(name, [], "declared", declared_failure, detail, trace)
    return Resolution(
        name,
        [],
        "unresolved",
        NOT_INSTALLED,
        "未在 override、toolchain.json、PATH 或 CodeKit 已知目录中找到",
        trace,
    )


def state_hint(name: str, state: str, *, home: Optional[Path] = None) -> str:
    """把状态翻成一句可执行的出路。

    刻意不再建议「把目录加进 PATH」：实测 PATH 里用户级条目是全的，被拒绝的是对这些
    目录的文件系统访问，改 PATH 无效。
    """
    if state == OK:
        return ""
    if state == NOT_INSTALLED:
        return (
            f"未安装：在 CodeKit 工具链根 {toolchain_root(home)} 及各历史安装目录里都没找到 "
            f"{name} 的入口。用安装器安装该组件后重试。"
        )
    if state == NOT_READABLE:
        return (
            f"已安装但当前会话读不到：宿主沙箱按路径拒绝了 {name} 所在目录。"
            "把目录加进 PATH 无效；请在已授权/提权（非沙箱）会话里重跑安装器"
            "（install.cmd install / install.sh install），让工具重建到 CodeKit 工具链根。"
        )
    return (
        f"入口可读但本会话启动不起来：{name} 依赖的解释器或运行时不可达（典型是 venv 的"
        "基础解释器落在读不到的目录）。重跑安装器可用内置运行时重建。"
    )


def format_report(resolution: Resolution, *, home: Optional[Path] = None) -> str:
    """一行人类可读结论，供 CLI 与报告共用。"""
    parts = [f"{resolution.name}: {resolution.state} (source={resolution.source})"]
    if resolution.argv:
        parts.append("argv=" + " ".join(resolution.argv))
    if resolution.detail:
        parts.append(resolution.detail)
    hint = state_hint(resolution.name, resolution.state, home=home)
    if hint:
        parts.append(hint)
    return " | ".join(parts)


def format_explanation(resolution: Resolution, *, home: Optional[Path] = None) -> str:
    """逐阶段展开一次解析，供 ``resolve-runner.py --explain`` 使用。

    ``format_report`` 只给结论；故障归因需要看到「试过哪些来源、各自是什么实测态」。
    受限宿主里最容易被误判的就是「PATH 命中但被拒绝读取」——结论一行说不清是哪一层
    拒绝的，逐阶段表能直接指出停顿位置。
    """
    lines = [f"{resolution.name}: {resolution.state} (source={resolution.source})"]
    if not resolution.stages:
        lines.append("  没有任何候选来源产出入口（override / toolchain.json / PATH / 已知目录）")
    for index, stage in enumerate(resolution.stages, start=1):
        state = str(stage.get("state", ""))
        verdict = "命中" if state == OK else "不可用"
        parts = [f"  {index}. {stage.get('source')}: {verdict}（{state}）"]
        argv = [str(item) for item in (stage.get("argv") or [])]
        if argv:
            parts.append(" ".join(argv))
        detail = str(stage.get("detail") or "")
        if detail:
            parts.append(f"— {detail}")
        lines.append(" ".join(parts))
    hint = state_hint(resolution.name, resolution.state, home=home)
    if hint:
        lines.append(f"  → {hint}")
    return "\n".join(lines)
