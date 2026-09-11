#!/usr/bin/env python3
"""Component detection, state calculation, and result summaries.

Package managers and vendor CLIs remain external commands.  This module owns
the data model around them: detection, version state, presence checks, and
recap rendering.  The installer shell uses the TSV output for compatibility
with its existing terminal selector.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "installer_config.py"
REGISTRY_PATH = SCRIPT_DIR / "component-registry.py"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


import installer_agents as agents  # noqa: E402
import installer_qoder as qoder  # noqa: E402


def load_module(path: Path, name: str) -> Any:
    """按文件路径动态加载注册表或配置模块，供状态检测复用其定义。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


config = load_module(CONFIG_PATH, "codekit_installer_config")
registry = load_module(REGISTRY_PATH, "codekit_component_registry")


CODEX_MARKETPLACE = "co-daming"
CODEX_PLUGIN = "codekit"
CLAUDE_MARKETPLACE = "co-daming"
CLAUDE_PLUGIN = "codekit"
ANSI_RED = "\033[0;31m"
ANSI_RESET = "\033[0m"


# Desktop applications expose the same coding-agent capability as their CLI
# counterparts, but they do not consistently install a command on PATH. Keep
# these probes explicit and conservative: a config/data directory alone is not
# proof that the application is installed. Qoder is the exception documented in
# installer_qoder.py: its shared configuration root *is* the plugin storage both
# surfaces read, so an existing root does count as availability.
AGENT_COMMANDS = {
    "claude": ("claude", "claude-desktop"),
    "codex": ("codex",),
    "opencode": ("opencode", "opencode-desktop"),
}

MACOS_AGENT_APPS = {
    "claude": ("Claude.app",),
    # The Codex app was merged into the new ChatGPT desktop app. Keep the old
    # Codex bundle name as a compatibility probe for installations not yet
    # migrated.
    "codex": ("ChatGPT.app", "Codex.app"),
    "opencode": ("OpenCode.app", "OpenCode Beta.app"),
}

LINUX_DESKTOP_FILES = {
    "claude": ("claude.desktop", "claude-desktop.desktop"),
    "codex": (
        "chatgpt.desktop",
        "codex.desktop",
        "com.openai.chatgpt.desktop",
        "com.openai.codex.desktop",
    ),
    "opencode": (
        "opencode.desktop",
        "opencode-desktop.desktop",
        "ai.opencode.desktop.desktop",
        "com.anomalyco.opencode.desktop",
    ),
}

LINUX_AGENT_PATHS = {
    "claude": (
        Path("/opt/Claude"),
        Path("/opt/claude-desktop"),
        Path("/usr/lib/claude-desktop"),
    ),
    "codex": (
        Path("/opt/ChatGPT"),
        Path("/opt/Codex"),
    ),
    "opencode": (
        Path("/opt/OpenCode"),
        Path("/opt/opencode-desktop"),
        Path("/usr/lib/opencode-desktop"),
    ),
}


# npm 包名、可执行命令和版本参数从策略表派生，供状态检测、安装路径和
# 安装/卸载流程共同复用，避免出现第二份 npm 包名定义。
NPM_COMPONENTS = {
    component.component_id: {
        "package": component.channels["npm"],
        "command": component.command,
        "version_args": list(component.version_args),
    }
    for component in registry.COMPONENTS
    if "npm" in component.channels
}


@dataclass
class ComponentState:
    component_id: str
    status: str = "not_installed"
    installed_version: str = ""
    latest_version: str = ""

    @property
    def state(self) -> str:
        """把内部三态状态映射为选择器使用的 absent/upgradable/up_to_date。"""
        if self.status == "not_installed":
            return "absent"
        if self.status == "installed":
            return "upgradable"
        return "up_to_date"


@dataclass
class Context:
    home: Path
    marketplace_local: Optional[Path]
    version_file: Path


def run_command_capture(args: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """静默执行探测命令，返回 (退出码, stdout, stderr)；异常与超时按失败处理。"""
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def run_command(args: list[str], timeout: float = 15.0) -> str:
    """静默执行探测命令；命令失败、异常或超时均返回空字符串。"""
    returncode, output, _error = run_command_capture(args, timeout)
    return output if returncode == 0 else ""


def command_version(command: str, args: list[str]) -> str:
    """确认命令存在后读取第一行版本输出。"""
    if shutil.which(command) is None:
        return ""
    output = run_command([command, *args])
    return output.splitlines()[0] if output else ""


def agent_presence(name: str, home: Path, os_name: Optional[str] = None) -> str:
    """返回 agent 的发现来源；CLI 和 Desktop 均视为已安装。"""
    if name in qoder.PLATFORMS:
        # Qoder 的 CLI 与 Desktop 地位一致，任一方可用的共享配置根即视为可用。
        return qoder.agent_presence(name, home, os_name)
    if name not in AGENT_COMMANDS:
        return ""
    for command in AGENT_COMMANDS[name]:
        found = shutil.which(command)
        if found:
            return f"{command} ({found})"

    os_name = os_name or platform.system()
    if os_name == "Darwin":
        for app_name in MACOS_AGENT_APPS[name]:
            for app_dir in (Path("/Applications"), home / "Applications"):
                candidate = app_dir / app_name
                if candidate.is_dir():
                    return str(candidate)
        return ""

    if os_name == "Linux":
        for application_dir in config.linux_application_dirs(home):
            for desktop_file in LINUX_DESKTOP_FILES[name]:
                candidate = application_dir / "applications" / desktop_file
                if candidate.is_file():
                    return str(candidate)
        for candidate in LINUX_AGENT_PATHS[name]:
            if candidate.exists():
                return str(candidate)
        # User-local unpacked desktop bundles are common for AppImage-style
        # installs and do not necessarily create a desktop entry.
        for relative in (
            Path(".local") / "share" / "applications" / f"{name}-desktop",
            Path(".local") / "opt" / name,
        ):
            candidate = home / relative
            if candidate.exists():
                return str(candidate)
    return ""


def agent_present(name: str, home: Path, os_name: Optional[str] = None) -> bool:
    """判断 Claude、Codex 或 OpenCode 的 CLI/Desktop 是否可用。"""
    return bool(agent_presence(name, home, os_name))


def agent_cli_presence(
    platform_key: str,
    home: Path,
    environ: Optional[dict[str, str]] = None,
) -> str:
    """只探测 agent 平台的 CLI 本体。

    Desktop 应用、Linux 启动器和共享配置根都不算可用：插件安装以 CLI 为前置，
    缺少 CLI 时安装器会先自动安装 CLI 再装插件。
    """
    return agents.cli_presence(platform_key, home, environ)


def agent_cli_state(component_id: str, context: Context) -> tuple[bool, str]:
    """返回插件对应平台 CLI 的存在性与版本，供安装选择器和结果汇总展示。"""
    entry = agents.component_agent(component_id)
    if entry is None:
        return False, ""
    if not agents.cli_presence(entry.key, context.home):
        return False, ""
    return True, agents.cli_version(entry.key, context.home)


def plugin_listing(command: str, args: list[str]) -> str:
    """读取 Claude/Codex 插件列表，隐藏 CLI 错误以便状态探测安全降级。"""
    if shutil.which(command) is None:
        return ""
    return run_command([command, *args])


def has_line(listing: str, expected: str, exact_prefix: bool = True) -> bool:
    """在插件列表中按前缀或包含关系匹配目标组件。"""
    return any(
        line.startswith(expected) if exact_prefix else expected in line
        for line in listing.splitlines()
    )


def marketplace_sha(context: Context) -> str:
    """读取本地 marketplace 仓库 HEAD 提交号；非本地源时返回空字符串。"""
    if not context.marketplace_local:
        return ""
    return run_command(
        ["git", "-C", str(context.marketplace_local), "rev-parse", "HEAD"]
    )


def saved_marketplace_sha(context: Context) -> str:
    """从安装状态文件读取上次成功记录的 marketplace 提交号。"""
    return config.read_key_value(context.version_file, "marketplace_sha")


def fetch_json(url: str) -> Any:
    """获取版本接口 JSON；网络、格式或解析失败时返回 None。"""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "CodeKit-installer"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def version_source_url(component: str) -> tuple[str, str]:
    """返回组件版本来源的 (URL, 字段)，全部由策略表声明；无来源时返回空对。"""
    definition = registry.component_by_id(component)
    if definition is None:
        return "", ""
    source = definition.version_source
    if not source and "npm" in definition.channels:
        source = f"npm:{definition.channels['npm']}"
    kind, _, target = source.partition(":")
    if kind == "npm":
        # npm 元数据同样走镜像源：直连 registry.npmjs.org 探测会拖慢甚至超时。
        return f"{config.NPM_REGISTRY}/{target}/latest", "version"
    if kind == "pypi":
        return f"https://pypi.org/pypi/{target}/json", "version"
    if kind == "github":
        return f"https://api.github.com/repos/{target}/releases/latest", "tag_name"
    if kind == "gitlab":
        return f"https://gitlab.com/api/v4/projects/{target}/releases/permalink/latest", "tag_name"
    return "", ""


def latest_versions(components: Iterable[str]) -> dict[str, str]:
    """从各组件的版本源读取最新版本，供安装选择器判断是否可升级。"""
    result: dict[str, str] = {}
    for component in components:
        url, field = version_source_url(component)
        if not url:
            continue
        value = config.json_field(fetch_json(url), field)
        version = config.extract_version(value)
        if version:
            result[component] = f"v{version}" if component == "glab" else version
    return result


def platform_subagents_state(context: Context, tool: str) -> str:
    """读取指定平台的全局 agent 状态，作为对应插件状态的一部分。"""
    helper = SCRIPT_DIR / "manage-global-agents.py"
    if not helper.exists():
        return ""
    args = [sys.executable, str(helper), "status", "--home", str(context.home), "--tool", tool]
    try:
        # 与安装/卸载共用同一份目录解析：避免 Qoder 共享根在探测与落盘之间漂移。
        flags, _agents_dir = agents.global_agent_plan(tool, context.home)
    except ValueError:
        return ""
    args.extend(flags)
    if context.marketplace_local:
        source = context.marketplace_local / "skills"
        if source.is_dir():
            args.extend(["--source", str(source)])
    returncode, output, error = run_command_capture(args)
    if returncode != 0 or not output:
        # 区分“确实未安装”和“探测失败”：失败必须留下可见线索，
        # 否则组件状态会被静默降级为未安装并反复提示重装。
        detail = error.splitlines()[0] if error else f"exit code {returncode}"
        print(
            f"installer state: agent 状态探测失败（tool={tool}）：{detail}",
            file=sys.stderr,
        )
        return ""
    return output


def repository_plugin_version(context: Context) -> str:
    """读取插件源中的版本，作为无法从安装状态取版本时的回退。"""
    version_file = (
        context.marketplace_local / "plugin-version.json"
        if context.marketplace_local
        else SCRIPT_DIR.parent / "plugin-version.json"
    )
    try:
        return config.validate_plugin_version(version_file)
    except (OSError, ValueError):
        return "installed"


def claude_installed_version(home: Path) -> str:
    """读取 Claude 插件注册表中记录的实际安装版本。"""
    document = config.read_json(home / ".claude" / "plugins" / "installed_plugins.json")
    entries = config.json_path(document, f"plugins.{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}")
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        version = config.json_field(entry, "version")
        if version:
            return version
    return ""


def codex_installed_version(home: Path) -> str:
    """读取 Codex 插件缓存中以版本号命名的目录，取其中最新的一版。"""
    root = home / ".codex" / "plugins" / "cache" / CODEX_MARKETPLACE / CODEX_PLUGIN
    try:
        versions = [item.name for item in root.iterdir() if item.is_dir()]
    except OSError:
        return ""
    return max(versions, key=config.version_tuple) if versions else ""


def installed_plugin_version(component: str, context: Context) -> str:
    """读取平台实际落盘的插件版本；平台未记录版本时回退到插件源版本。

    OpenCode 直接从本地插件源目录加载，不写版本记录，因此始终走回退分支。
    """
    readers = {
        "claude_plugin": claude_installed_version,
        "codex_plugin": codex_installed_version,
    }
    reader = readers.get(component)
    if reader is not None:
        try:
            version = reader(context.home)
        except (OSError, ValueError):
            version = ""
        if version:
            return version
    return repository_plugin_version(context)


def qoder_present(descriptor: Any, context: Context) -> bool:
    """按共享配置根判断 Qoder 插件文件状态，不要求 CLI 或 Desktop 正在运行。"""
    try:
        state = qoder.inspect(descriptor, context.home)
    except ValueError:
        return False
    if not state.installed or not state.enabled:
        return False
    return platform_subagents_state(context, descriptor.key) in {"installed", "up_to_date"}


def component_version(component: str, context: Context) -> str:
    """按组件类型选择版本来源：CLI 命令来自策略表，插件来自自身配置。"""
    definition = registry.component_by_id(component)
    if definition is not None and definition.command:
        value = command_version(definition.command, list(definition.version_args))
        # `gh --version` includes the build date and platform after the
        # semantic version.  Result pages only need the three-part version.
        return config.extract_version(value) if component == "gh" else value
    if component in {"claude_plugin", "opencode_plugin", "codex_plugin"}:
        return installed_plugin_version(component, context)
    qoder_platform = qoder.platform_for_component(component)
    if qoder_platform is not None:
        # 共享注册表里的版本就是 CLI 与 Desktop 两端共同看到的结果。
        try:
            state = qoder.inspect(qoder_platform, context.home)
        except ValueError:
            state = None
        if state is not None and state.version:
            return state.version
        return repository_plugin_version(context)
    return ""


def claude_cache_present(home: Path) -> bool:
    """判断 Claude 插件缓存是否已由 CLI 或 Desktop 写入共享目录。"""
    return (home / ".claude" / "plugins" / "cache" / CLAUDE_MARKETPLACE / CLAUDE_PLUGIN).is_dir()


def present(component: str, context: Context) -> bool:
    """判断组件文件是否存在；插件同时要求对应平台的 agent 已同步。

    这里只看文件层面的落盘结果，卸载复核与 `--present` 子命令依赖它；
    安装状态是否成立另由 plugin_ready 追加 CLI 前置判断。
    """
    definition = registry.component_by_id(component)
    if definition is not None and definition.command:
        return shutil.which(definition.command) is not None
    if component == "claude_plugin":
        listing = plugin_listing("claude", ["plugin", "list"])
        registered = bool(listing) and has_line(
            listing, f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}", False
        )
        if not registered:
            # 只装 Desktop 的机器没有 CLI 命令，但插件缓存由两端共享写入。
            registered = claude_cache_present(context.home)
        return registered and platform_subagents_state(context, "claude") in {"installed", "up_to_date"}
    if component == "codex_plugin":
        listing = plugin_listing("codex", ["plugin", "list", "--marketplace", CODEX_MARKETPLACE])
        return bool(listing) and has_line(listing, f"{CODEX_PLUGIN}@{CODEX_MARKETPLACE}") and platform_subagents_state(context, "codex") in {"installed", "up_to_date"}
    if component == "opencode_plugin":
        return config.opencode_has_plugin(
            context.home / ".config" / "opencode" / "opencode.json"
        ) and platform_subagents_state(context, "opencode") in {"installed", "up_to_date"}
    qoder_platform = qoder.platform_for_component(component)
    if qoder_platform is not None:
        return qoder_present(qoder_platform, context)
    return False


def plugin_ready(component: str, context: Context) -> bool:
    """判断插件是否已安装且其平台 CLI 可用。

    安装状态以 CLI 为准：只有桌面端时插件视为未安装，安装器会默认勾选，
    并先自动安装 CLI 再安装插件。
    """
    entry = agents.component_agent(component)
    if entry is not None and not agent_cli_presence(entry.key, context.home):
        return False
    return present(component, context)


def detect_component(component: str, context: Context, latest: dict[str, str]) -> ComponentState:
    """计算组件当前状态，并在可取得最新版本时区分已安装和可升级。"""
    state = ComponentState(component)
    if not plugin_ready(component, context):
        return state
    state.installed_version = component_version(component, context)

    qoder_platform = qoder.platform_for_component(component)
    if qoder_platform is not None:
        if platform_subagents_state(context, qoder_platform.key) != "up_to_date":
            state.status = "installed"
            return state
        target = repository_plugin_version(context)
        if target and target != "installed":
            state.latest_version = target
            # Qoder 走本地目录安装，重装即升级，因此版本较旧就标记可升级。
            if not state.installed_version or not config.version_greater_or_equal(
                state.installed_version, target
            ):
                state.status = "installed"
                return state
        state.status = "up_to_date"
        return state
    if component in {"claude_plugin", "opencode_plugin", "codex_plugin"}:
        tool = {"claude_plugin": "claude", "opencode_plugin": "opencode", "codex_plugin": "codex"}[component]
        agents_state = platform_subagents_state(context, tool)
        if context.marketplace_local and agents_state != "up_to_date":
            state.status = "installed"
            return state
        current = saved_marketplace_sha(context)
        remote = marketplace_sha(context)
        if remote:
            # 本地插件源的最新版本就是插件源自身的版本号；提交号只用于判断
            # 源内容是否变化，不能当作版本号展示。
            target = repository_plugin_version(context)
            state.latest_version = target if target != "installed" else ""
            state.status = "up_to_date" if current and current == remote else "installed"
        else:
            state.status = "up_to_date"
        return state

    state.latest_version = latest.get(component, "")
    # 没有远端版本时保持保守状态，避免网络故障导致每次运行都误报可升级。
    if state.latest_version and config.version_greater_or_equal(
        state.installed_version, state.latest_version
    ):
        state.status = "up_to_date"
    else:
        # If the remote source is unavailable, retain the safe up-to-date state
        # without suggesting an upgrade based on incomplete information.
        state.status = "installed" if state.latest_version else "up_to_date"
    return state


def all_states(context: Context) -> list[ComponentState]:
    """校验组件注册表并返回全部组件的检测结果。"""
    registry.validate_registry()
    latest = latest_versions(
        component.component_id
        for component in registry.COMPONENTS
        if version_source_url(component.component_id)[0]
        and present(component.component_id, context)
    )
    return [
        detect_component(component.component_id, context, latest)
        for component in registry.COMPONENTS
    ]


def export_states(context: Context) -> None:
    """以无颜色 TSV 输出状态，供 shell/Python 选择器之间稳定传递。"""
    def tsv_field(value: str) -> str:
        """清理 TSV 禁止字符，确保单个状态不会破坏行列结构。"""
        return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")

    for state in all_states(context):
        print(
            "\t".join(
                (
                    tsv_field(state.component_id),
                    tsv_field(state.status),
                    tsv_field(state.installed_version),
                    tsv_field(state.latest_version),
                )
            )
        )


def component_description(component_id: str) -> str:
    """根据注册表 ID 返回用户可读名称；未知 ID 原样返回。"""
    for component in registry.COMPONENTS:
        if component.component_id == component_id:
            return component.description
    return component_id


def recap_presence_version(component: str, context: Context) -> tuple[bool, str]:
    """同时读取组件存在性和版本，供安装/卸载结果汇总复核。"""
    is_present = present(component, context)
    return is_present, component_version(component, context) if is_present else ""


def component_cli_note(component: str, context: Context) -> str:
    """返回插件对应平台 CLI 本体的状态与版本；非插件组件返回空字符串。"""
    entry = agents.component_agent(component)
    if entry is None:
        return ""
    is_present, version = agent_cli_state(component, context)
    if not is_present:
        return f"CLI {entry.display_name} 未安装"
    return f"CLI {version or 'unknown'}"


def terminal_width(value: str) -> int:
    """计算终端显示宽度，避免中英文混排时使用字符串长度造成错位。"""
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def terminal_pad(value: str, width: int) -> str:
    """按终端显示宽度右侧补齐字符串。"""
    return value + " " * max(0, width - terminal_width(value))


def terminal_truncate(value: str, width: int, *, from_end: bool = False) -> str:
    """按终端显示宽度截断文本；路径保留末尾，版本保留开头。"""
    if terminal_width(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    if from_end:
        result = "…" + value[-(width - 1):]
        while terminal_width(result) > width and len(result) > 1:
            result = "…" + result[2:]
        return result
    result = ""
    for character in value:
        if terminal_width(result + character + "…") > width:
            break
        result += character
    return result + "…"


def result_cell(value: str) -> str:
    """在交互式终端中用红色强调失败结果，管道输出保持纯文本。"""
    if value == "失败" and sys.stdout.isatty():
        return f"{ANSI_RED}{value}{ANSI_RESET}"
    return value


def render_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    result_column: int,
) -> None:
    """按终端显示宽度渲染表格，正确处理中文和结果颜色。"""
    widths = [
        max(terminal_width(headers[index]), *(terminal_width(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    separator = "  " + "  ".join("─" * width for width in widths)
    print("  " + "  ".join(terminal_pad(value, widths[index]) for index, value in enumerate(headers)))
    print(separator)
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            padding = " " * max(0, widths[index] - terminal_width(value))
            cell = result_cell(value) if index == result_column else value
            cells.append(cell + padding)
        print("  " + "  ".join(cells))


def display_path(path: Path, home: Path) -> str:
    """将用户目录下的路径压缩为更易读的 `~` 形式。"""
    try:
        return str(Path("~") / path.relative_to(home))
    except ValueError:
        return str(path)


def component_install_path(component: str, context: Context) -> str:
    """返回组件的实际安装路径或用户态安装的稳定回退路径。"""
    definition = registry.component_by_id(component)
    if definition is not None and definition.command:
        path = shutil.which(definition.command)
        if path:
            return display_path(Path(path), context.home)
        fallback = {
            "rtk": context.home / ".local" / "bin" / "rtk",
            "gitnexus": context.home / ".npm-global" / "bin" / "gitnexus",
            "dingtalk_cli": context.home / ".npm-global" / "bin" / "dws",
            "feishu_cli": context.home / ".npm-global" / "bin" / "lark-cli",
            "graphify": context.home / ".local" / "bin" / "graphify",
        }.get(component)
        return display_path(fallback, context.home) if fallback else "—"
    qoder_platform = qoder.platform_for_component(component)
    if qoder_platform is not None:
        try:
            root = qoder.resolve_config_root(qoder_platform, context.home)
        except ValueError:
            return "—"
        return display_path(qoder.plugin_cache_root(root), context.home)
    plugin_paths = {
        "claude_plugin": context.home / ".claude" / "plugins" / "cache" / CLAUDE_MARKETPLACE,
        "opencode_plugin": context.home / ".config" / "opencode" / "node_modules" / "codekit",
        "codex_plugin": context.home / ".codex" / "plugins" / "cache" / CODEX_MARKETPLACE,
    }
    path = plugin_paths.get(component)
    return display_path(path, context.home) if path else "—"


def render_install_table(rows: list[tuple[str, str, str, str]]) -> None:
    """按内容计算列宽并输出紧凑的安装结果表。"""
    headers = ("组件", "安装结果", "安装路径", "版本（插件 · CLI）")
    max_path_width = 48
    max_version_width = 56
    normalized_rows = [
        (
            component,
            result,
            terminal_truncate(path, max_path_width, from_end=True),
            terminal_truncate(version, max_version_width),
        )
        for component, result, path, version in rows
    ]
    render_table(headers, normalized_rows, result_column=1)


def render_uninstall_table(rows: list[tuple[str, str, str]]) -> None:
    """渲染卸载结果表，使用与安装结果相同的中文宽度计算规则。"""
    render_table(("组件", "结果", "说明"), rows, result_column=1)


def render_recap(mode: str, records: Iterable[str], context: Context) -> None:
    """根据执行记录重新探测结果，并输出安装或卸载汇总。"""
    parsed = []
    for record in records:
        fields = record.split("\t")
        if len(fields) != 3:
            raise ValueError(f"invalid recap record: {record!r}")
        parsed.append(fields)

    if mode == "install":
        print("\n===== 本次安装结果汇总 =====\n")
        record_map = {component: (expected_ok, action) for component, action, expected_ok in parsed}
        install_rows = []
        ok_count = 0
        fail_count = 0
        for registered in registry.COMPONENTS:
            component = registered.component_id
            if component not in record_map:
                install_rows.append(
                    (
                        component_description(component),
                        "未选择",
                        "---",
                        "---",
                    )
                )
                continue
            is_present, version = recap_presence_version(component, context)
            expected_ok, _action = record_map[component]
            succeeded = expected_ok == "1" and is_present
            if succeeded:
                ok_count += 1
            else:
                fail_count += 1
            # 插件组件同时给出插件本体与对应平台 CLI 的状态/版本。
            cli_note = component_cli_note(component, context)
            version_cell = version or "---"
            if cli_note:
                version_cell = f"{version_cell} · {cli_note}"
            install_rows.append(
                (
                    component_description(component),
                    "成功" if succeeded else "失败",
                    component_install_path(component, context) if succeeded else "---",
                    version_cell,
                )
            )
        render_install_table(install_rows)
    else:
        print("\n===== 本次卸载结果汇总 =====\n")
        uninstall_rows = []
        ok_count = 0
        fail_count = 0
        record_map = {
            component: (action, expected_ok)
            for component, action, expected_ok in parsed
        }
        for registered in registry.COMPONENTS:
            component = registered.component_id
            if component not in record_map:
                uninstall_rows.append((component_description(component), "未选择", "---"))
                continue
            action, expected_ok = record_map[component]
            is_present, _version = recap_presence_version(component, context)
            succeeded = expected_ok == "1" and not is_present
            if succeeded:
                note = "组件未安装，已忽略" if action == "absent" else "已卸载"
                result = "成功"
            else:
                note = "仍有残留，建议手动检查"
                result = "失败"
            uninstall_rows.append((component_description(component), result, note))
            if succeeded:
                ok_count += 1
            else:
                fail_count += 1
        render_uninstall_table(uninstall_rows)

    total = len(parsed)
    if not parsed:
        print("\n本次未选择任何组件")
    elif fail_count == 0:
        print(f"\n本次共处理 {total} 个组件：全部成功")
    else:
        print(f"\n本次共处理 {total} 个组件：成功 {ok_count}，失败 {fail_count}")


def build_parser() -> argparse.ArgumentParser:
    """构建状态检测命令行解析器。"""
    parser = argparse.ArgumentParser(description="CodeKit component state helpers")
    parser.add_argument("command", choices=("detect", "present", "recap"))
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--marketplace-local", type=Path, default=None)
    parser.add_argument("--version-file", type=Path, required=False)
    parser.add_argument("--component")
    parser.add_argument("--mode", choices=("install", "uninstall"))
    parser.add_argument("--record", action="append", default=[])
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """分发 detect/present/recap 子命令，并统一处理输入错误。"""
    args = build_parser().parse_args(argv)
    context = Context(
        home=args.home,
        marketplace_local=args.marketplace_local,
        version_file=args.version_file
        or args.home / ".local" / "state" / "CodeKit" / "installed_versions",
    )
    try:
        if args.command == "detect":
            export_states(context)
        elif args.command == "present":
            if not args.component:
                raise ValueError("--component is required for present")
            raise SystemExit(0 if present(args.component, context) else 1)
        else:
            if not args.mode:
                raise ValueError("--mode is required for recap")
            render_recap(args.mode, args.record, context)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"installer state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
