#!/usr/bin/env python3
"""Shared subagent-capability probe for every CodeKit skill.

CodeKit's dispatch gate is a hard phase gate, so discovering mid-run that the
host exposes no agent tools wastes every write made before it.  This probe runs
first and costs nothing: on Codex it dumps the model-visible prompt locally,
which never reaches a model call.

Default mode is read-only — it writes nothing, to the repository or to any host
configuration, and reports on stdout only.  Verdicts are ``available`` /
``unavailable`` / ``unknown``, and only ``unavailable`` may block a run: a probe
that could not run is not evidence of a missing capability.

``--enable`` is the repair path, and passing it *is* the consent record: the
caller must have asked the user first.  It only ever touches the single
``[features] multi_agent_v2`` key in ``~/.codex/config.toml``, re-probes after
writing, and rolls the file back byte-for-byte if the tools still are not there.
Hosts read that file at startup, so enabling never unblocks the current session.

Exit codes: 0 for available/unknown and for a completed enable, 1 for
unavailable, 2 for a bad invocation or an enable that could not be verified.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"
ENABLED = "enabled"
ROLLED_BACK = "rolled-back"

HOSTS = ("auto", "claude", "codex", "opencode", "qoder", "qodercn", "unknown")
# The dispatch surface the subagent mandate depends on.
COLLAB_TOOLS = (
    "spawn_agent",
    "list_agents",
    "wait_agent",
    "send_message",
    "interrupt_agent",
)
# 「成组出现」的最小规模。技能自己的正文里最多出现过这族中的 1 个名字（spawn_agent），
# 而 flag 打开时这一族是一起暴露的，所以 2 已经能把「文档提到」和「工具面存在」分开。
MIN_TOOL_NAMES = 2
FEATURES_TABLE = "features"
MULTI_AGENT_KEY = "multi_agent_v2"
PROBE_TIMEOUT = 45.0

Runner = Callable[[Sequence[str], float], Tuple[int, str]]

# 宿主身份只能靠约定俗成的环境变量猜，猜错会导致误阻断，所以主 Agent 必须显式
# 传 --host；这里的自动探测只是缺省兜底，认不出来时返回 unknown（不阻断）。
HOST_ENV_MARKERS = (
    (("CODEX_SANDBOX", "CODEX_CI", "CODEX_HOME"), "codex"),
    (("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"), "claude"),
    (("OPENCODE", "OPENCODE_CLIENT"), "opencode"),
    (("QODERCN_CONFIG_DIR",), "qodercn"),
    (("QODER_CONFIG_DIR",), "qoder"),
)
# 实测只有 Codex 有可开启的多代理 flag；Claude / OpenCode / Qoder 的派发工具由宿主
# 内置（settings 里没有等价键），对它们写"开关"只会留下无人读取的无效配置。
ENABLED_HOSTS = ("codex",)


def configure_utf8_streams() -> None:
    """Windows 默认按 ANSI 代码页编码 stdout，中文 JSON 会被调用方读成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def detect_host(environ: dict[str, str]) -> str:
    for markers, host in HOST_ENV_MARKERS:
        if any(environ.get(marker) for marker in markers):
            return host
    return "unknown"


def config_path(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def probe_command(host: str) -> Optional[list[str]]:
    """目前只有 Codex 有实测过的零 token 探测；其他宿主返回 None 表示不阻断。"""
    if host == "codex":
        return ["codex", "debug", "prompt-input", "hi"]
    return None


def resolve(argv: Sequence[str]) -> Optional[list[str]]:
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    return [executable, *argv[1:]]


def exposed_tools(output: str) -> Tuple[str, ...]:
    return tuple(
        tool for tool in COLLAB_TOOLS if re.search(rf"\b{re.escape(tool)}\b", output)
    )


def classify(returncode: int, output: str) -> str:
    """只有「命令跑通且**成组**看到派发工具」才算 available。

    单个名字命中不算：技能自己的 SKILL.md 与 prompt 正文里就会出现 ``spawn_agent``
    （本仓库即有），一次词边界命中会把「文档提到了派发这件事」读成「宿主给了派发工具」，
    后果是安装器静默判定 already-on、既不写开关也不提示一个字。``MIN_TOOL_NAMES``
    要求至少这么多个不同名字同时出现——这族工具是一个 feature 一起暴露的，成组出现才是
    工具面的形状。
    """
    if returncode != 0:
        return UNKNOWN
    return (
        AVAILABLE
        if len(exposed_tools(output)) >= MIN_TOOL_NAMES
        else UNAVAILABLE
    )


def _split_terminator(line: str) -> Tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def _table_range(lines: Sequence[str]) -> Optional[Tuple[int, int]]:
    """返回 ``[features]`` 表体的 [起始, 结束) 行区间；表头本身是 start-1。"""
    header: Optional[int] = None
    for index, raw in enumerate(lines):
        body, _ = _split_terminator(raw)
        text = body.strip()
        if not text or text.startswith("#"):
            continue
        if header is not None and text.startswith("["):
            return header + 1, index
        if text.startswith("[") and not text.startswith("[[") and text.endswith("]"):
            if text[1:-1].strip() == FEATURES_TABLE:
                header = index
    return (header + 1, len(lines)) if header is not None else None


def read_value(text: str, key: str) -> Optional[str]:
    """读取 ``[features]`` 表内某个键的原始字面量；不存在时返回 None。"""
    lines = text.splitlines(keepends=True)
    table = _table_range(lines)
    if table is None:
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$")
    for raw in lines[table[0]:table[1]]:
        body, _ = _split_terminator(raw)
        match = pattern.match(body.split("#", 1)[0])
        if match:
            return match.group(1)
    return None


def multi_agent_flag(home: Path) -> Optional[str]:
    """当前 ``multi_agent_v2`` 的字面量，用于区分「没配」和「配了不生效」。"""
    try:
        text = config_path(home).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return read_value(text, MULTI_AGENT_KEY)


def enable_multi_agent(text: str) -> Tuple[str, bool]:
    """把 ``[features] multi_agent_v2 = true`` 写进 TOML，其余内容逐字节保留。"""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    table = _table_range(lines)
    if table is not None:
        key_pattern = re.compile(rf"^(\s*){re.escape(MULTI_AGENT_KEY)}\s*=\s*[^#]*?(\s*#.*)?$")
        for index in range(table[0], table[1]):
            body, terminator = _split_terminator(lines[index])
            match = key_pattern.match(body)
            if not match:
                continue
            if read_value(text, MULTI_AGENT_KEY) == "true":
                return text, False
            lines[index] = (
                f"{match.group(1)}{MULTI_AGENT_KEY} = true{match.group(2) or ''}{terminator}"
            )
            return "".join(lines), True
        _, header_term = _split_terminator(lines[table[0] - 1])
        lines.insert(table[0], f"{MULTI_AGENT_KEY} = true{header_term or newline}")
        return "".join(lines), True

    prefix = "" if not text or text.endswith(("\n", "\r")) else newline
    separator = newline if text else ""
    block = f"{prefix}{separator}[{FEATURES_TABLE}]{newline}{MULTI_AGENT_KEY} = true{newline}"
    return text + block, True


def write_config(path: Path, payload: bytes) -> None:
    """同目录临时文件 + os.replace 落盘，避免半写状态；新文件按 600 收紧。

    这是全仓唯一一份实现：安装器不再自己写一份，而是按路径加载本模块后直接调用
    （见 `scripts/installer_codex.py`）。
    """
    temporary = path.with_name(f"{path.name}.codekit-tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(payload)
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def enable(host: str, home: Path, runner: Runner) -> dict:
    """在用户已同意的前提下开启该平台的多代理能力；失败逐字节回滚。"""
    if host not in ENABLED_HOSTS:
        return {"action": "refused", "reason": f"{host} 无已实测的多代理开关，不写任何配置"}
    path = config_path(home)
    try:
        original = path.read_bytes() if path.is_file() else None
    except OSError as exc:
        return {"action": "failed", "reason": f"无法读取 {path}: {exc}"}
    try:
        text = original.decode("utf-8") if original is not None else ""
    except UnicodeDecodeError:
        return {"action": "failed", "reason": f"{path} 不是 UTF-8 文本"}
    updated, changed = enable_multi_agent(text)
    if not changed:
        return {
            "action": "noop",
            "reason": f"{path} 已声明 {MULTI_AGENT_KEY} = true 仍看不到工具，通常是 Codex "
            "版本改动了 flag 名称，未猜测新名称也未改动配置",
        }
    try:
        write_config(path, updated.encode("utf-8"))
    except OSError as exc:
        return {"action": "failed", "reason": f"无法写入 {path}: {exc}"}
    command = probe_command(host)
    resolved = resolve(command) if command else None
    verified = UNKNOWN
    if resolved:
        verified = classify(*runner(resolved, PROBE_TIMEOUT))
    if verified == AVAILABLE:
        return {"action": ENABLED, "path": str(path)}
    try:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            write_config(path, original)
    except OSError as exc:
        return {
            "action": "failed",
            "reason": f"写入 {path} 后复验为 {verified} 且回滚失败：{exc}；"
            f"请手工删除该文件中的 [features] {MULTI_AGENT_KEY} 行",
        }
    return {
        "action": ROLLED_BACK,
        "reason": f"写入后复验仍看不到派发工具（{verified}），已回滚为原内容",
    }


def build_result(host: str, collab: str, evidence: dict, flag: Optional[str]) -> dict:
    result: dict[str, object] = {"probe_version": 1, "host": host, "collab": collab}
    result["evidence"] = evidence
    if flag is not None:
        result["codex_multi_agent_v2"] = flag
    if collab == UNAVAILABLE:
        result["fix"] = (
            "Codex 未向模型暴露子代理工具，CodeKit 的 subagent 门禁无法满足。"
            "先向用户申请授权，同意后运行本脚本的 --enable（写入 "
            "[features] multi_agent_v2 = true），然后完全退出并重启 Codex 再重跑本 Skill"
            "——正在运行的会话不会获得该工具，本轮无法靠改配置继续。"
            + (
                ""
                if flag in (None, "false")
                else f" 注意：该键当前已是 {flag} 仍看不到工具，通常是 Codex 版本改动了 flag 名称。"
            )
        )
        result["restart_required"] = True
    return result


@contextlib.contextmanager
def neutral_cwd():
    """探测用的空目录。

    ``codex debug prompt-input`` 会把**当前目录**的仓库上下文渲染进 prompt，而 CodeKit
    自己的技能正文里就有 ``spawn_agent`` 这个词——在安装器/技能所在的仓库里直接探测，
    测到的可能是文档而不是工具面。空目录里没有任何可注入的上下文。
    """
    with tempfile.TemporaryDirectory(prefix="codekit-collab-probe-") as directory:
        yield directory


def subprocess_runner(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    try:
        with neutral_cwd() as directory:
            completed = subprocess.run(
                list(argv),
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return completed.returncode, completed.stdout or ""


def run_probe(host: str, runner: Runner, home: Path) -> dict:
    command = probe_command(host)
    if command is None:
        return build_result(
            host,
            UNKNOWN,
            {"reason": "no-measured-probe-for-host", "command": None, "exit_code": None},
            None,
        )
    resolved = resolve(command)
    if resolved is None:
        return build_result(
            host,
            UNKNOWN,
            {"reason": "host-cli-not-on-path", "command": command, "exit_code": None},
            None,
        )
    returncode, output = runner(resolved, PROBE_TIMEOUT)
    flag = multi_agent_flag(home) if host == "codex" else None
    return build_result(
        host,
        classify(returncode, output),
        {
            "reason": None,
            "command": command,
            "exit_code": returncode,
            "tools": list(exposed_tools(output)),
        },
        flag,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe or enable the host's subagent tools")
    parser.add_argument("--host", choices=HOSTS, default="auto")
    parser.add_argument(
        "--enable",
        action="store_true",
        help="write the host's multi-agent switch; pass only after the user agreed",
    )
    parser.add_argument("--home", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_utf8_streams()
    args = parse_args(argv)
    host = args.host if args.host != "auto" else detect_host(dict(os.environ))
    home = Path(args.home).expanduser() if args.home else Path.home()
    if args.enable:
        outcome = enable(host, home, subprocess_runner)
        print(json.dumps({"host": host, "enable": outcome}, ensure_ascii=False, indent=2))
        return 0 if outcome.get("action") in {ENABLED, "noop", "refused"} else 2
    result = run_probe(host, subprocess_runner, home)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["collab"] == UNAVAILABLE else 0


if __name__ == "__main__":
    raise SystemExit(main())
