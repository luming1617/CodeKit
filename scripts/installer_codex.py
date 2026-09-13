#!/usr/bin/env python3
"""Codex host capability layer for the CodeKit installer.

CodeKit skills dispatch named subagents, and on Codex that tool surface only
exists when ``[features] multi_agent_v2`` is on.  ``codex features list`` is not
evidence for this: it can report ``multi_agent stable true`` while the
model-visible prompt carries zero agent tools.  So the probe reads the prompt
dump itself, which costs no tokens because it never reaches a model call.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence, Tuple

FEATURES_TABLE = "features"
MULTI_AGENT_KEY = "multi_agent_v2"
# The dispatch surface CodeKit's subagent mandate depends on.
COLLAB_TOOLS = (
    "spawn_agent",
    "list_agents",
    "wait_agent",
    "send_message",
    "interrupt_agent",
)

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"


def config_path(home: Path) -> Path:
    """Codex 用户级配置文件；桌面版与 CLI 在同一 OS 上共享这一份。"""
    return home / ".codex" / "config.toml"


def probe_command() -> list[str]:
    """导出模型可见 prompt 的本地命令；配置改动即时生效，无需重启。"""
    return ["codex", "debug", "prompt-input", "hi"]


def exposed_tools(output: str) -> Tuple[str, ...]:
    return tuple(
        tool for tool in COLLAB_TOOLS if re.search(rf"\b{re.escape(tool)}\b", output)
    )


def classify_probe(returncode: int, output: str) -> str:
    """只有「命令跑通且工具面为空」才算不可用；跑不通一律 unknown，不据此阻断。"""
    if returncode != 0:
        return UNKNOWN
    return AVAILABLE if exposed_tools(output) else UNAVAILABLE


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
        is_header = text.startswith("[")
        if header is not None and is_header:
            return header + 1, index
        if is_header and not text.startswith("[[") and text.endswith("]"):
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


def enable_multi_agent(text: str) -> Tuple[str, bool]:
    """把 ``[features] multi_agent_v2 = true`` 写进 TOML，其余内容逐字节保留。"""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    table = _table_range(lines)
    if table is not None:
        key_pattern = re.compile(
            rf"^(\s*){re.escape(MULTI_AGENT_KEY)}\s*=\s*[^#]*?(\s*#.*)?$"
        )
        for index in range(table[0], table[1]):
            body, terminator = _split_terminator(lines[index])
            match = key_pattern.match(body)
            if not match:
                continue
            if read_value(text, MULTI_AGENT_KEY) == "true":
                return text, False
            lines[index] = f"{match.group(1)}{MULTI_AGENT_KEY} = true{match.group(2) or ''}{terminator}"
            return "".join(lines), True
        _, header_term = _split_terminator(lines[table[0] - 1])
        lines.insert(table[0], f"{MULTI_AGENT_KEY} = true{header_term or newline}")
        return "".join(lines), True

    prefix = "" if not text or text.endswith(("\n", "\r")) else newline
    separator = newline if text else ""
    block = f"{prefix}{separator}[{FEATURES_TABLE}]{newline}{MULTI_AGENT_KEY} = true{newline}"
    return text + block, True
