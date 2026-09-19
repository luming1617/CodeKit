#!/usr/bin/env python3
"""Run GitNexus against an explicitly chosen global registry.

GitNexus stores its local index in the repository but registers repositories in
``GITNEXUS_HOME/registry.json``; a sandbox may allow the former and reject the
latter.  The two modes make that difference observable instead of silent:

``--registry disposable`` (default)
    Isolate ``GITNEXUS_HOME`` in a temporary directory.  The directory is
    deleted when the process exits, so this mode never writes anything outside
    the repository — and never registers anything either.  An index built here
    satisfies ``status`` while ``list``/``query``/``context``/``impact``/
    ``trace``/``cypher``/``check``/``detect-changes`` and the MCP graph queries
    answer ``No indexed repositories found``.

``--registry real``
    Run against the host's real ``GITNEXUS_HOME``.  This is the *attempt*: it
    either performs the out-of-repo write or tells the caller, with a dedicated
    exit code, that the host refused it.  A silent fall back to a disposable
    registry would hide exactly the refusal that the caller has to escalate, so
    this mode never falls back.

A denied write exits ``77`` and prints ``initialize error: global registry
write denied: <dir>`` on stderr; callers must treat that as "ask the host for
permission and retry this same command", not as a GitNexus failure.  The
verdict is evidence-based: stderr naming ``EACCES``/``EPERM``/``EROFS`` (or a
Windows "access is denied") is the primary signal, and a real probe write only
backs it up for a subcommand that actually writes ``registry.json`` — a read
failure is never re-labelled as a permission problem.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import gitnexus_home

DISPOSABLE = "disposable"
REAL = "real"
WRITE_DENIED_EXIT = 77
DENIED_PREFIX = "initialize error: global registry write denied:"
# GitNexus 的报错原文随版本与 Node 版本变化，不能只认一种写法；命中即按「权限被拒」
# 归类（多一次提权重试是幂等的，比把拒绝误报成工具故障安全）。
_DENIED_MARKERS = re.compile(
    r"EACCES|EPERM|EROFS|permission denied|access is denied|read-only file system",
    re.IGNORECASE,
)
# 只有这些子命令会写全局 registry：``index`` 登记既有索引，``analyze`` 在提交边界
# 重写它，``embeddings`` 把运行时装进同一处 HOME。
_REGISTRY_WRITING_SUBCOMMANDS = frozenset({"index", "analyze", "embeddings"})


def writes_registry(command: list[str]) -> bool:
    """这条命令是否会写 ``registry.json``。

    只有会写的命令才允许用「``GITNEXUS_HOME`` 写不进去」解释它的失败：否则一个与权限
    无关的失败（子命令拼错、GitNexus 内部错误）会被误判成「写入被拒」，把调用方推去
    申请一次根本解决不了问题的授权。
    """
    return any(item in _REGISTRY_WRITING_SUBCOMMANDS for item in command)


def registry_write_probe(home: Path) -> bool:
    """Try a real create+delete inside ``home``; return whether it worked.

    ``os.access(os.W_OK)`` reports writability from the process's viewpoint and
    a path-based sandbox can still deny the write, so the only honest judge is
    an actual write.  The probe file lives in GitNexus's own directory and is
    removed immediately; callers consult it only for commands that write the
    registry (see ``writes_registry``).
    """
    probe = home / ".codekit-write-probe"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def run_real(command: list[str], root: Path, home: Path) -> int:
    """Run against the real ``GITNEXUS_HOME``; classify a refusal as exit 77."""
    # 必须捕获再转写：被拒的证据就在子进程的 stderr 里（原生 ``EACCES``/``EPERM``
    # 原文），让它直接继承 stdout/stderr 就拿不到分类依据。
    completed = subprocess.run(
        command, cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if completed.returncode == 0:
        return completed.returncode
    if _DENIED_MARKERS.search(completed.stderr or ""):
        print(f"{DENIED_PREFIX} {home}", file=sys.stderr)
        return WRITE_DENIED_EXIT
    # 没有原文证据时才用一次真实探针写复核，而且只对确实要写注册表的命令：读命令
    # 失败时「目录写不进去」解释不了它，探针还会平白在真实 HOME 里建目录。
    if writes_registry(command) and not registry_write_probe(home):
        print(f"{DENIED_PREFIX} {home}", file=sys.stderr)
        return WRITE_DENIED_EXIT
    return completed.returncode


def run_disposable(command: list[str], root: Path, home: Path) -> int:
    """Isolate ``GITNEXUS_HOME`` so nothing is written outside the repository."""
    try:
        with tempfile.TemporaryDirectory(prefix="codekit-gitnexus-home-") as temporary:
            fallback = Path(temporary) / "registry"
            env = os.environ.copy()
            env["GITNEXUS_HOME"] = str(fallback)
            print(
                "initialize warning: GitNexus global registry is intentionally "
                f"isolated ({home}); using disposable GITNEXUS_HOME={fallback}",
                file=sys.stderr,
            )
            completed = subprocess.run(
                command, cwd=root, text=True, encoding="utf-8", errors="replace", env=env
            )
            return completed.returncode
    except OSError as error:
        print(
            f"initialize error: GitNexus registry fallback could not run: {error}",
            file=sys.stderr,
        )
        return 78


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--registry",
        choices=(DISPOSABLE, REAL),
        default=DISPOSABLE,
        help=(
            "disposable isolates GITNEXUS_HOME in a temp dir (default); real targets "
            "the host's GITNEXUS_HOME and reports a refused write with exit code 77"
        ),
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a GitNexus command is required after --")

    home = gitnexus_home()
    mode = REAL if os.environ.get("CODEKIT_GITNEXUS_REGISTRY") == REAL else args.registry
    if mode == REAL:
        return run_real(command, args.root, home)
    return run_disposable(command, args.root, home)


if __name__ == "__main__":
    raise SystemExit(main())
