#!/usr/bin/env python3
"""Codex host capability layer for the CodeKit installer.

CodeKit skills dispatch named subagents, and on Codex that tool surface only
exists when ``[features] multi_agent_v2`` is on.  ``codex features list`` is not
evidence for this: it can report ``multi_agent stable true`` while the
model-visible prompt carries zero agent tools.  So the probe reads the prompt
dump itself, which costs no tokens because it never reaches a model call.

The TOML reader/writer and the probe classifier are not reimplemented here: the
skills own that logic in ``skills/_shared/scripts/probe-collab.py`` and call it
directly, so this module loads that single implementation by path and re-exports
it under the names the installer uses.  Keeping a second copy meant two files
that had to be edited in lockstep, which is exactly how they drift apart.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent

# 技能侧共享脚本是安装器的真实依赖（安装器本来就要求检出里有 skills/）。缺了它是
# 残缺检出，要以单行受控错误结束，而不是让下游在运行期抛 AttributeError。
_SHARED_PROBE = REPO_DIR / "skills" / "_shared" / "scripts" / "probe-collab.py"
if not _SHARED_PROBE.is_file():
    raise ValueError(f"installer helper missing: {_SHARED_PROBE}")

import installer_config as config  # noqa: E402

_probe = config.load_module(_SHARED_PROBE, "codekit_probe_collab")

AVAILABLE = _probe.AVAILABLE
UNAVAILABLE = _probe.UNAVAILABLE
UNKNOWN = _probe.UNKNOWN
MULTI_AGENT_KEY = _probe.MULTI_AGENT_KEY

# 桌面版写入的配置段；CLI 不会写它。桌面版的 shell 只继承机器级 PATH，而 CLI 从终端
# 启动时继承完整 PATH，两者共用这一份配置，所以能力判定需要区分它们。
DESKTOP_TABLE = "desktop"

config_path = _probe.config_path
enable_multi_agent = _probe.enable_multi_agent
write_config = _probe.write_config


def probe_command() -> list[str]:
    """导出模型可见 prompt 的本地命令；配置改动即时生效，无需重启。"""
    return list(_probe.probe_command("codex") or [])


def classify_probe(returncode: int, output: str) -> str:
    """只有「命令跑通且工具面为空」才算不可用；跑不通一律 unknown，不据此阻断。"""
    return _probe.classify(returncode, output)


def desktop_app_present(text: str) -> bool:
    """这份配置是否由桌面版写过（存在 ``[desktop]`` 或它的子表）。

    桌面版与 CLI 共用 ``config.toml``，但只有桌面版会写 ``[desktop]``。判定「用户级
    命令在这个宿主里能不能执行」需要它：桌面版的 shell 只继承机器级 PATH，同样的
    命令在从终端启动的 CLI 里可见。
    """
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.startswith("[") or line.startswith("[["):
            continue
        name = line[1:-1].strip() if line.endswith("]") else ""
        if name == DESKTOP_TABLE or name.startswith(f"{DESKTOP_TABLE}."):
            return True
    return False
