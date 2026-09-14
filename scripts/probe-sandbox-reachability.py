#!/usr/bin/env python3
"""Characterise what the current host session's sandbox lets CodeKit touch.

Answers, per candidate toolchain root: can this session (a) stat, (b) read,
(c) enumerate, and (d) EXECUTE a native binary living there?  Run it from a
normal, NON-escalated command inside the sandbox you want to characterise --
"the command ran" is not evidence that the tool it launched was reachable, and
a run that was approved/elevated measures a different environment entirely.

Stdlib only, so the interpreter that runs it need not come from anywhere special.
Writes one self-cleaning copy of a dependency-free Win32 executable into each
candidate root (that write *is* part of the measurement) and nothing else.
Emits one JSON object on stdout.  It never raises: a path the host refuses to
read is recorded as that refusal, because a denied probe is data, not a crash.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

TIMEOUT = 8.0
PROBE_EXE_NAME = "codekit-reachability-probe.exe"


def errno_name(exc: BaseException) -> str:
    """``PermissionError:EACCES:13`` 这类精确失败形态，区分「读不到」与「不存在」。"""
    if isinstance(exc, OSError) and exc.errno is not None:
        code = errno.errorcode.get(exc.errno, str(exc.errno))
        return f"{exc.__class__.__name__}:{code}:{exc.errno}"
    return exc.__class__.__name__


def guard(fn: Callable[[], object]) -> object:
    """任何单点探测失败都降级成一条记录，绝不带走整份报告。"""
    try:
        return fn()
    except BaseException as exc:
        return {"__error__": errno_name(exc)}


def list_subdirectories(root: Path) -> list[Path]:
    """列出 ``root`` 的子目录；被拒绝枚举时返回空列表。

    宿主可以只放行 ``ReadAttributes`` 而不放行 ``ListDirectory``（Codex 桌面端的
    沙箱就是这样：``is_dir()`` 为真，``iterdir()`` 抛 ``WinError 5``）。探测属于
    诊断手段，「列不出来」必须等价于「这个候选不存在」，不能把枚举失败升级成
    整轮失败。``Path.glob`` 不吞 ``OSError``，所以这里必须显式包住。
    """
    try:
        entries = sorted(root.iterdir())
    except BaseException:
        return []
    directories: list[Path] = []
    for path in entries:
        try:
            if path.is_dir():
                directories.append(path)
        except BaseException:
            continue
    return directories


def probe_path(path: Path) -> dict:
    """stat / read-one-byte / listdir，各自记录精确失败原因。

    三项分开测是刻意的：宿主可以只放行 ``ReadAttributes``（``stat`` 成功）而不放行
    读取内容或列目录，把三者混成一个 ``is_file()`` 正是既有诊断失真的原因。

    ``read`` 只对**文件**有意义：Windows 上 ``open(目录)`` 一律抛 EACCES，把它当成
    「被拒绝读取」会得出与事实相反的结论（workspace 自己也会被标成拒绝）。目录只看
    ``listdir``。
    """
    out: dict = {"path": str(path), "stat": None, "read": None, "listdir": None}

    def stat_once() -> dict:
        info = path.stat()
        return {"mode": oct(info.st_mode), "size": info.st_size}

    out["stat"] = guard(stat_once)
    reached = isinstance(out["stat"], dict) and "__error__" not in out["stat"]
    is_dir = reached and os.path.isdir(path)
    if reached and is_dir:
        out["listdir"] = guard(lambda: len(list(path.iterdir())))
    elif reached:
        out["read"] = guard(lambda: len(path.open("rb").read(1)))
    return out


def probe_exec(argv: list[str]) -> dict:
    def attempt() -> dict:
        done = subprocess.run(
            argv, capture_output=True, timeout=TIMEOUT, check=False,
            encoding="utf-8", errors="replace",
        )
        return {
            "argv": argv,
            "returncode": done.returncode,
            "stdout_head": (done.stdout or "").strip()[:200],
            "stderr_head": (done.stderr or "").strip()[:200],
        }

    result = guard(attempt)
    if isinstance(result, dict) and "__error__" in result:
        return {"argv": argv, "raised": result["__error__"]}
    return {"argv": argv, **result}


def probe_exec_from(root: Path) -> dict:
    """分别测「能不能往这个根写」与「能不能执行这个根里的二进制」。

    两者必须分开：先 ``copy`` 再 ``exec`` 的实现会把「写入被拒」报成「不能执行」，
    而真正要回答的是「放进这个根的工具能否启动」。写入失败时 ``exec`` 记为
    ``not-measured``，不判它失败——写不进去不代表跑不起来，``entries``/``execs``
    里对**已存在**入口的执行结果才是那一问的答案。
    """
    if os.name != "nt":
        return {"skipped": "exec-from-root probe is Windows-specific"}
    source = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    if not source.is_file():
        return {"skipped": f"missing {source}"}

    target = root / PROBE_EXE_NAME
    try:
        root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except BaseException as exc:
        return {"write": errno_name(exc), "exec": "not-measured"}
    try:
        return {"write": "ok", "exec": probe_exec([str(target), "where.exe"])}
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def ok(value: object) -> bool:
    """探测结果是否代表「成功」，而不是一条被拒绝/失败的记录。"""
    return value is not None and not (isinstance(value, dict) and "__error__" in value)


def root_verdict(item: dict) -> dict:
    probe = item.get("path_probe") or {}
    reached = ok(probe.get("stat"))
    readable = "ok" if (reached and (ok(probe.get("listdir")) or ok(probe.get("read")))) else probe.get("stat")
    outcome = item.get("exec_from_root") or {}
    if "skipped" in outcome:
        executable: object = outcome["skipped"]
    elif isinstance(outcome.get("exec"), dict):
        executable = "ok" if outcome["exec"].get("returncode") == 0 else outcome["exec"]
    else:
        executable = outcome.get("exec")
    return {"read": readable, "write": outcome.get("write"), "exec": executable}


def candidate_roots(home: Path) -> list[Path]:
    roots = [home / ".local" / "share" / "CodeKit"]
    if os.name == "nt":
        programdata = os.environ.get("ProgramData")
        if programdata:
            roots.append(Path(programdata) / "CodeKit")
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "npm")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "Programs" / "Python")
    else:
        roots += [home / ".local" / "bin", home / ".npm-global" / "bin"]
    return roots


def concrete_entries(home: Path) -> list[Path]:
    """真实存在的候选入口：解析层现在会猜的那些路径，逐条如实测。"""
    scripts = "Scripts" if os.name == "nt" else "bin"
    codekit = home / ".local" / "share" / "CodeKit"
    branch = "python.exe" if os.name == "nt" else "python"
    entries = [
        Path(sys.executable),
        codekit / "venvs" / "graphify" / "pyvenv.cfg",
        codekit / "venvs" / "graphify" / scripts / branch,
        codekit / "venvs" / "graphify" / scripts / ("graphify.exe" if os.name == "nt" else "graphify"),
    ]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for name in sorted(
                list_subdirectories(Path(local) / "Programs" / "Python"),
                key=lambda path: path.name,
                reverse=True,
            ):
                if name.name.startswith("Python3"):
                    entries.append(name / "python.exe")
        entries.append(Path(r"C:\Program Files\nodejs\node.exe"))
        appdata = os.environ.get("APPDATA")
        if appdata:
            entries.append(Path(appdata) / "npm" / "gitnexus.cmd")
            entries.append(Path(appdata) / "npm" / "graphify.cmd")
    return entries


def executable_entries(entries: list[Path]) -> list[Path]:
    suffixes = {".exe", ".cmd", ".bat"} if os.name == "nt" else {"", ".sh"}
    return [entry for entry in entries if entry.suffix.lower() in suffixes]


def build_report() -> dict:
    home = Path.home()
    workspace = Path.cwd()
    entries = guard(lambda: concrete_entries(home))
    entries = entries if isinstance(entries, list) else []
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    roots = guard(lambda: [
        {"root": str(root), "path_probe": probe_path(root), "exec_from_root": probe_exec_from(root)}
        for root in candidate_roots(home)
    ])
    roots = roots if isinstance(roots, list) else []
    return {
        "probe_version": 3,
        "os": os.name,
        "platform": sys.platform,
        "cwd": str(workspace),
        "python": {"executable": sys.executable, "version": sys.version},
        "environment": {
            "PATH": os.environ.get("PATH", ""),
            "resolved": {
                name: shutil.which(name)
                for name in ("py", "python", "python3", "node", "gitnexus", "graphify")
            },
        },
        "self_read": probe_path(Path(sys.executable)),
        "roots": roots,
        "workspace_control": {
            "path_probe": probe_path(workspace),
            "exec_from_root": probe_exec_from(workspace),
        },
        "entries": [probe_path(entry) for entry in entries],
        "execs": [
            probe_exec([sys.executable, "-c", "print('self-ok')"]),
            probe_exec([str(system32), "where.exe"]),
            *[probe_exec([str(entry), "--version"]) for entry in executable_entries(entries)],
        ],
        "verdict": {item["root"]: root_verdict(item) for item in roots},
    }


def main() -> int:
    print(json.dumps(guard(build_report), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
