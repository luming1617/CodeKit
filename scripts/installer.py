#!/usr/bin/env python3
"""CodeKit installer orchestrator.

The shell entry point is intentionally small. This module owns the installer
state machine, component dispatch, package-manager commands, and installation
result handling. Third-party CLIs remain external commands; Python provides
the lifecycle and error boundary around them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Union

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402

# 导入期门禁：残缺检出必须以单行受控错误结束，而不是让下面的 import 抛出
# ModuleNotFoundError。这也是卸载器复用的同一份 helper 清单。
try:
    config.require_helper_files(SCRIPT_DIR)
except ValueError as exc:
    print(f"  ✗  {exc}", file=sys.stderr)
    raise SystemExit(1)

import installer_agents as agents  # noqa: E402
import installer_codex as codex  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_preflight as preflight  # noqa: E402
import installer_qoder as qoder  # noqa: E402
import installer_sources as source_builder  # noqa: E402
import installer_state as detector  # noqa: E402
import installer_toolchain as toolchain  # noqa: E402

# 由 CodeKit 自持工具链根接管落点的组件：它们的命令必须能被受限宿主（沙箱按路径
# 拒绝访问）在会话里读到并启动，因此安装位置与启动表达式都由 toolchain.json 描述。
# 名单派生自注册表的 ``entry_kind``，这里不再另存一份——「哪些工具归根管」不能有两个答案。
TOOLCHAIN_COMPONENTS = detector.registry.root_bound_ids()

# graphify 的注册目标：CLI 存在的宿主才注册；``agents`` 恒注册——graphify install
# --platform agents 既不往 CLAUDE.md 写托管段、也不装 hook，而它的落点 ~/.agents/skills
# 恰好是 Qoder IDE 唯一扫描的共享技能目录。安装与回收共用这一份名单。
GRAPHIFY_HOST_TARGETS = ("claude", "codex", "opencode")
GRAPHIFY_ALWAYS_TARGETS = ("agents",)


class InstallerError(RuntimeError):
    """An expected installer failure with a user-facing message."""


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""


class Logger:
    def __init__(self, state_dir: Path) -> None:
        """初始化安装日志器，并确保状态目录已经存在。"""
        platform_layer.configure_stdout_encoding()
        self.state_dir = state_dir
        self.log_path = state_dir / "install.log"
        self.use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        state_dir.mkdir(parents=True, exist_ok=True)

    def _color(self, value: str, code: str) -> str:
        """在交互终端中为关键状态增加颜色，重定向输出保持纯文本。"""
        return f"\033[{code}m{value}\033[0m" if self.use_color else value

    @staticmethod
    def command_text(command: Sequence[str]) -> str:
        """以可复制且安全的形式展示外部命令。"""
        return shlex.join([str(item) for item in command])

    def _log(self, level: str, message: str) -> None:
        """将带级别的消息追加到日志文件；日志不可写时不阻断主流程。"""
        try:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{level}] {message}\n")
        except OSError:
            pass

    def info(self, message: str = "") -> None:
        """输出普通信息，并同步写入安装日志。"""
        print(message)
        self._log("INFO ", message)

    def section(self, message: str) -> None:
        """输出一个醒目的阶段标题，避免安装步骤挤在连续命令输出中。"""
        text = f"\n━━━ {message} ━━━"
        print(self._color(text, "1;36"))
        self._log("STEP ", message)

    def command_start(self, command: Sequence[str]) -> None:
        """只显示命令开始信息；命令的逐行输出写入文件而不刷屏。"""
        text = f"  · 执行：{self.command_text(command)}"
        print(self._color(text, "2"))
        self._log("CMD  ", text)

    def command_finished(self, command: Sequence[str], returncode: int, output: str = "") -> None:
        """显示外部命令的单行结果，并在失败时给出最后一条错误线索。"""
        command_text = self.command_text(command)
        if returncode == 0:
            self.success(f"完成：{command_text}")
            return

        detail = next(
            (
                line.strip()
                for line in reversed(output.splitlines())
                if line.strip()
                and not line.lstrip().startswith(("npm warn", "npm notice"))
            ),
            "",
        )
        if len(detail) > 180:
            detail = f"{detail[:177]}..."
        suffix = f"：{detail}" if detail else ""
        self.error(f"失败（退出码 {returncode}）：{command_text}{suffix}")

    def warn(self, message: str) -> None:
        """输出警告信息，并同步写入安装日志。"""
        print(self._color(f"  ⚠  {message}", "0;33"))
        self._log("WARN ", message)

    def error(self, message: str) -> None:
        """输出错误信息，并同步写入安装日志。"""
        print(self._color(f"  ✗  {message}", "0;31"))
        self._log("ERROR", message)

    def success(self, message: str) -> None:
        """输出成功信息，并同步写入安装日志。"""
        print(self._color(f"  ✓  {message}", "0;32"))
        self._log("OK   ", message)

    def command_output(self, line: str) -> None:
        """仅记录外部命令的原始输出，避免安装过程被依赖工具刷屏。"""
        self._log("CMD  ", line.rstrip("\n"))


class Installer:
    # 外部命令超时后返回的退出码，与 GNU timeout(1) 保持一致。
    TIMEOUT_RETURNCODE = 124

    def __init__(self, auto_yes: bool) -> None:
        """创建一次安装会话，并初始化路径、状态和结果记录。"""
        self.auto_yes = auto_yes
        # 每个选中插件平台的 subagent 派发能力结论，由 configure_agent_capabilities 填写。
        self.agent_capabilities: list[dict] = []
        self.home = Path.home()
        # 平台差异（npm 前缀、venv 布局、状态目录、临时目录、交互能力）全部由
        # 适配层给出，安装/卸载/状态检测三处不再各自硬编码 POSIX 假设。
        self.platform = platform_layer.platform_for()
        self.os_name = self.platform.os_name
        # Windows 上 winget 与官方 .ps1 安装器只把 PATH 写进注册表，不会更新已经运行的
        # 进程；先同步一次，状态检测才能看到用户此前装好的工具。
        platform_layer.refresh_process_path()
        probed_prefix = platform_layer.probe_npm_prefix() if self.platform.is_windows else ""
        self.npm_prefix = self.platform.npm_prefix(self.home, probed_prefix)
        self.npm_bin_dir = self.platform.npm_bin_dir(self.npm_prefix)
        self.bin_dir = self.platform.bin_dir(self.home, self.npm_bin_dir)
        self.state_dir = self.platform.state_dir(self.home)
        self.version_file = self.state_dir / "installed_versions"
        self.logger = Logger(self.state_dir)
        self.changed_paths: list[str] = []
        self.states: dict[str, detector.ComponentState] = {}
        self.selected: list[str] = []
        self.install_records: list[tuple[str, str, str]] = []
        # 本次是否由 CodeKit 把飞书 skills 同步进全局技能库；save_versions 落档，
        # 卸载侧凭它决定能不能回收 ~/.agents/skills/lark-*。
        self.feishu_skills_synced = False
        # 内置 Python 运行时的结论，由 setup_toolchain 填写；venv 通道与技能侧都读它。
        self.toolchain: toolchain.RuntimeState = toolchain.RuntimeState()
        self.marketplace_local = self._detect_marketplace()
        self.market_dir: Optional[Path] = None
        self.opencode_source_dir: Optional[Path] = None
        self.qoder_source_dir: Optional[Path] = None
        self.context = detector.Context(
            home=self.home,
            marketplace_local=self.marketplace_local,
            version_file=self.version_file,
        )

    @property
    def components(self):
        """返回注册表中的组件定义，供检测、选择和分发共用。"""
        return detector.registry.COMPONENTS

    @property
    def plugin_version(self) -> str:
        """读取并校验仓库根目录中的插件版本唯一配置源。"""
        try:
            return config.validate_plugin_version(REPO_DIR / "plugin-version.json")
        except (OSError, ValueError) as exc:
            raise InstallerError(f"Could not read plugin version: {exc}") from exc

    def _detect_marketplace(self) -> Optional[Path]:
        """检查当前仓库是否具备生成兼容插件源所需的完整文件。

        清单只有一份（``installer_sources.required_paths``）：这里判定可用的仓库，构建
        阶段不会再报缺文件。
        """
        return (
            REPO_DIR
            if all(path.exists() for path in source_builder.required_paths(REPO_DIR))
            else None
        )

    def track(self, path: Union[Path, str]) -> None:
        """记录本次运行可能修改的路径，并去重以便安装后汇总展示。"""
        value = str(path)
        if value and value not in self.changed_paths:
            self.changed_paths.append(value)

    def prepend_path(self, path: Path) -> None:
        """把用户态工具目录放到当前进程 PATH 首位，供后续命令立即可用。"""
        value = str(path)
        current = os.environ.get("PATH", "")
        if not current.split(os.pathsep)[:1] == [value]:
            os.environ["PATH"] = f"{value}{os.pathsep}{current}"

    def ensure_user_bin(self) -> None:
        """创建用户级 bin 目录，并让 npm 全局 bin 与本地 bin 立即生效。"""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.npm_bin_dir.mkdir(parents=True, exist_ok=True)
        self.prepend_path(self.bin_dir)
        self.prepend_path(self.npm_bin_dir)

    def set_npm(self) -> None:
        """统一下发 npm 镜像源，并在需要时切换到用户态前缀。

        macOS/Linux 把前缀指到用户目录（`~/.npm-global`），避免 sudo 写系统目录；
        Windows 沿用 npm 自己的前缀，它已经被 Node 安装包写进 PATH，覆写反而要
        额外维护一份 PATH 配置。
        """
        if not self.platform.is_windows:
            os.environ["npm_config_prefix"] = str(self.npm_prefix)
        os.environ["npm_config_registry"] = config.NPM_REGISTRY
        self.prepend_path(self.npm_bin_dir)

    # ---- CodeKit 自持工具链根 ------------------------------------------

    def toolchain_components(self) -> list[str]:
        """本次选中的、由工具链根接管的组件 ID。"""
        return [component_id for component_id in self.selected if component_id in TOOLCHAIN_COMPONENTS]

    def toolchain_component_installed(self, component_id: str) -> bool:
        """该组件在本机是否已经装过（不区分装在哪）。"""
        definition = self.component(component_id)
        command = getattr(definition, "command", "") if definition is not None else ""
        if command and self.command_exists(command):
            return True
        if definition is not None and definition.entry_kind == "python-venv":
            # venv 存在但没暴露成命令也算装过：迁移就是把这份副本搬进根里。
            venv = self.platform.venv_dir(self.home, command)
            return (Path(venv) / "pyvenv.cfg").is_file()
        return False

    def toolchain_component_is_local(self, component_id: str) -> bool:
        """落点是否已经在工具链根内、并且指向根内的解释器。

        这是「迁移是否已完成」的判据，按 ``entry_kind`` 分：npm-private 看清单条目能否
        解析（它的落点由前缀位置决定），python-venv 看 ``pyvenv.cfg`` 的 ``home`` 是否
        落在工具链根里——旧安装的 venv 指向 ``%LOCALAPPDATA%\\Programs\\Python``，
        在受限宿主里启动必失败（实测 rc=101）。
        """
        definition = self.component(component_id)
        kind = getattr(definition, "entry_kind", "")
        if kind == "npm-private":
            return toolchain.entry(self, definition).get("state") == "ok"
        if kind != "python-venv":
            return False
        root = self.platform.toolchain_root(self.home)
        venv = self.platform.venv_dir(self.home, definition.command)
        base = toolchain.venv_base_state(venv)
        if base["state"] != "ok":
            return False
        home_value = base["values"].get("home", "")
        if not home_value:
            return False
        try:
            Path(home_value).resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return True

    def toolchain_migration_targets(self) -> list[str]:
        """已装、但落点不在工具链根内的工具链组件。

        自动迁移是刻意的：只对**已经装过**的组件生效——安装器不该凭空装上用户从来没要
        的组件；但它必须把「CodeKit 装过、而宿主沙箱读不到」的落点搬到可读可执行的根
        里。没有这一步，「重跑安装器」这句修复建议对已装用户不成立，而那正是最初的问题：
        用户重跑安装器只勾了插件，于是工具链根始终是空的。
        """
        return [
            component_id
            for component_id in TOOLCHAIN_COMPONENTS
            if self.toolchain_component_installed(component_id)
            and not self.toolchain_component_is_local(component_id)
        ]

    def toolchain_targets(self) -> list[str]:
        """本次要物化到工具链根的组件：选中的 + 需要迁移的已装组件。"""
        targets = list(self.toolchain_components())
        for component_id in getattr(self, "toolchain_migration", []):
            if component_id not in targets:
                targets.append(component_id)
        return targets

    def needs_bundled_runtime(self) -> bool:
        """是否有目标组件依赖内置解释器建 venv；只有 ``python-venv`` 需要。"""
        for component_id in self.toolchain_targets():
            definition = self.component(component_id)
            if getattr(definition, "entry_kind", "") == "python-venv":
                return True
        return False

    def setup_toolchain(self) -> None:
        """准备工具链根，并在需要时物化内置 Python 运行时。

        没有目标组件时不下载运行时（约 45MB），但工具链根照常维护：技能侧读的是实测
        状态，缺了清单就只能退回猜路径。
        """
        self.platform.toolchain_root(self.home).mkdir(parents=True, exist_ok=True)
        selected = self.toolchain_components()
        self.toolchain_migration = [
            component_id
            for component_id in self.toolchain_migration_targets()
            if component_id not in selected
        ]
        if self.toolchain_migration:
            self.logger.info(
                "检测到已安装、但落点不在 CodeKit 工具链根内的组件："
                + "、".join(self.toolchain_migration)
                + f"。将重新物化到 {self.platform.toolchain_root(self.home)}"
                "（受限宿主如 Codex 桌面端只在该根内可读可执行）。"
            )
        if not self.needs_bundled_runtime():
            self.toolchain = toolchain.RuntimeState(
                reason="本次没有需要 Python 运行时的工具链组件，未获取内置运行时"
            )
            return
        self.toolchain = toolchain.ensure_runtime(self)
        for warning in self.toolchain.warnings:
            self.logger.warn(warning)
        if self.toolchain.usable:
            self.logger.info(
                f"内置 Python {self.toolchain.version or '?'} 就绪：{self.toolchain.python}"
            )
            return
        self.logger.warn(
            "内置 Python 运行时不可用（"
            + (self.toolchain.reason or "原因未知")
            + "）；需要它的组件会如实失败，不会退到工具链根外的落点——那种副本在受限"
            "沙箱（如 Codex 桌面端）里连读都读不到。"
        )

    def toolchain_python(self) -> str:
        """建 venv 用的解释器：只有内置运行时这一个答案。

        没有 ``sys.executable`` 这一条回退是刻意的：用它建出的 venv 把 ``pyvenv.cfg``
        的 ``home`` 指到工具链根外，在受限宿主里连基础解释器都读不到，等于装一份跑不
        起来的副本。边界写在 :meth:`installer_toolchain.RuntimeState.require_python`
        的返回类型里，调用点不再各自决定怎么退化。
        """
        return str(self.toolchain.require_python())

    def python_dependency_version(self) -> str:
        """比对 ``python>=x`` 依赖时使用的版本。

        该依赖的语义是「能建出可用的 venv」，而真正会被使用的解释器是内置运行时，
        所以优先用它比对，而不是当前进程的解释器。
        """
        if self.toolchain.usable and self.toolchain.version:
            return self.toolchain.version
        return f"{sys.version_info.major}.{sys.version_info.minor}"

    def manifest_components(self) -> list[str]:
        """本次要写进 ``toolchain.json`` 的组件 ID，按注册表顺序。

        不止「本次勾选的」：清单是整份重写的，只按本次选择构造会让上一轮接管、这一轮
        没勾的工具从清单里消失，技能侧于是退回按命令名猜路径——正是这套设计要消灭的
        形态。所以三类的并集都要写：本次目标（含失败项，报出实测态才有依据）、本机实测
        装着的根接管组件、以及清单里已有记录的组件（「装过又卸了」与「从来没这个工具」
        是两件事，条目保留才能分开）。
        """
        wanted = set(self.toolchain_targets())
        previous = toolchain.read_manifest(
            self.platform.toolchain_manifest(self.home)
        )
        recorded = previous.get("tools")
        wanted.update(
            component_id
            for component_id in (recorded if isinstance(recorded, dict) else {})
            if component_id in TOOLCHAIN_COMPONENTS
        )
        for component_id in TOOLCHAIN_COMPONENTS:
            if component_id in wanted:
                continue
            definition = self.component(component_id)
            if definition is not None and toolchain.entry_state(
                self.platform, self.home, definition
            ) != "not-installed":
                wanted.add(component_id)
        return [component_id for component_id in TOOLCHAIN_COMPONENTS if component_id in wanted]

    def write_toolchain_manifest(self) -> None:
        """写出技能侧读取的 ``toolchain.json``，并留给报告渲染使用。"""
        targets = self.manifest_components()
        if not targets:
            return
        path = toolchain.write_manifest(self, selected=targets)
        if path is None:
            self.logger.warn("工具链根不可写，未能写出 toolchain.json。")
            return
        self.toolchain_manifest = toolchain.read_manifest(path)

    def toolchain_state(self, component_id: str) -> str:
        """某个工具链工具在本会话的实测落点态。

        本次报告过的组件直接复用实测结果；没勾选过的组件仍要能回答「这台机器上它到底
        能不能用」，所以按注册表现测一次。判据是落点本身，不是命令在不在 PATH 上——
        受限宿主里 PATH 条目齐全而 ``stat`` 被拒，用 ``command_exists`` 会把已装说成没装。
        """
        for component, state in getattr(self, "toolchain_health", []):
            if component == component_id:
                return state
        definition = self.component(component_id)
        if definition is None or not definition.entry_kind:
            return "not-installed"
        return toolchain.entry_state(self.platform, self.home, definition)

    def report_toolchain_health(self) -> None:
        """按清单实测每个托管工具的 read/exec 状态，并把出路写清楚。

        取代原先「命令是否只靠用户级 PATH 可见」的自检：那个判据已被实测证伪——
        PATH 里用户级条目是全的，真正被拒绝的是对这些目录的文件系统访问。
        """
        document = getattr(self, "toolchain_manifest", None) or {}
        entries = document.get("tools") if isinstance(document, dict) else None
        entries = entries if isinstance(entries, dict) else {}
        self.toolchain_health = []
        for component_id in self.manifest_components():
            entry = entries.get(component_id)
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("state", ""))
            self.toolchain_health.append((component_id, state))
            if state == "ok":
                continue
            self.logger.warn(
                f"{component_id} 落点不可用（{state}）：{self._toolchain_state_hint(state)}"
            )

    def repair_command(self) -> str:
        """重跑安装器以重物化工具链根的命令写法（Windows 只有 .cmd 入口）。"""
        return "install.cmd install" if self.platform.is_windows else "install.sh install"

    def _toolchain_state_hint(self, state: str) -> str:
        """把实测状态翻成一句可执行的出路。"""
        command = self.repair_command()
        if state == "not-readable":
            return (
                "当前会话被拒绝读取该目录（不是未安装）。用提权/非沙箱会话重跑安装器"
                f"（{command}）会重建到 CodeKit 工具链根；只改 PATH 无效。"
            )
        if state == "not-executable":
            return (
                "入口存在但本会话启动不起来（多见于 venv 的基础解释器落在读不到的目录）。"
                f"重跑安装器（{command}）可用内置运行时重建。"
            )
        return f"未安装；运行安装器（{command}）并勾选该组件。"

    def install_npm_package_into_prefix(self, prefix: Path, package: str, command: str) -> bool:
        """把全局 CLI 装进 CodeKit 私有 npm 前缀并让本进程可见。

        与默认路径分开而不是加参数，是为了让既有组件（钉钉/飞书 CLI 等）的调用与
        行为完全不变：私有前缀只服务于必须在受限宿主里可读可执行的 gitnexus。
        """
        prefix = Path(prefix)
        previous = os.environ.get("npm_config_prefix")
        os.environ["npm_config_prefix"] = str(prefix)
        os.environ["npm_config_registry"] = config.NPM_REGISTRY
        try:
            result = self.run_command(
                ["npm", "install", "-g", "--prefix", str(prefix), f"{package}@latest"]
            )
        finally:
            if previous is None:
                os.environ.pop("npm_config_prefix", None)
            else:
                os.environ["npm_config_prefix"] = previous
        if result.returncode != 0:
            return False
        self.prepend_path(self.platform.npm_bin_dir(prefix))
        return self.command_exists(command)

    @staticmethod
    def _resolve_command(command: Sequence[str]) -> list[str]:
        """把 argv[0] 解析成可执行文件的全路径。

        Windows 的 CreateProcess 不按 PATHEXT 展开裸命令名，`["npm", ...]` 会直接抛
        WinError 2（npm 实际是 npm.CMD）；换成全路径后可以直接启动，既不必动用
        `shell=True`（避免引号与注入风险），也不用关心具体是 .exe 还是 .cmd。
        """
        return platform_layer.resolve_argv(command)

    def run_command(
        self,
        args: Sequence[str],
        *,
        check: bool = False,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
        quiet: bool = False,
    ) -> RunResult:
        """执行外部命令并显示摘要；完整输出写入安装日志。

        timeout 到期后终止子进程并返回 124，避免挂起的 CLI 让安装/卸载无限阻塞。
        quiet 用于「先试一次、失败就换策略」的探针式调用：终端不打印开始/结束行，
        免得留下一条其实不算失败的红字；完整输出照样写进安装日志。
        """
        command = self._resolve_command(args)
        if not quiet:
            self.logger.command_start(command)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                env=os.environ.copy(),
                stdin=None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                **platform_layer.text_kwargs(),
            )
        except OSError as exc:
            self.logger.command_finished(command, 127, str(exc))
            if check:
                raise InstallerError(f"could not run {' '.join(command)}: {exc}") from exc
            return RunResult(127)
        timed_out = False
        try:
            output_text, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            output_text, _ = process.communicate()
        output_text = output_text or ""
        for line in output_text.splitlines(True):
            self.logger.command_output(line)
        returncode = self.TIMEOUT_RETURNCODE if timed_out else process.returncode
        if timed_out:
            self.logger.warn(
                f"命令超时（{timeout:g}s），已终止：{self.logger.command_text(command)}"
            )
        if not quiet:
            self.logger.command_finished(command, returncode, output_text)
        if check and returncode != 0:
            raise InstallerError(f"command failed ({returncode}): {' '.join(command)}")
        return RunResult(returncode, output_text)

    def run_capture(
        self, args: Sequence[str], *, timeout: float = 15, cwd: Optional[Path] = None
    ) -> RunResult:
        """静默执行探测命令并返回结果，失败或超时统一返回失败结果。"""
        try:
            result = subprocess.run(
                self._resolve_command(args),
                cwd=str(cwd) if cwd else None,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                **platform_layer.text_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return RunResult(1)
        return RunResult(result.returncode, result.stdout.strip())

    def run_with_input(self, args: Sequence[str], content: str, env: Optional[dict[str, str]] = None) -> RunResult:
        """向外部命令写入文本输入并显示摘要；完整输出写入安装日志。"""
        command = self._resolve_command(args)
        self.logger.command_start(command)
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            result = subprocess.run(
                command,
                input=content,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=merged_env,
                check=False,
                **platform_layer.text_kwargs(),
            )
        except OSError:
            self.logger.command_finished(command, 127)
            return RunResult(127)
        for line in result.stdout.splitlines(True):
            self.logger.command_output(line)
        self.logger.command_finished(command, result.returncode, result.stdout)
        return RunResult(result.returncode, result.stdout)

    def command_exists(self, command: str) -> bool:
        """判断命令是否能通过当前进程 PATH 找到。"""
        return shutil.which(command) is not None

    def command_version(self, command: str, args: Sequence[str]) -> str:
        """调用统一的状态检测逻辑读取命令版本；命令不存在时返回空字符串。"""
        return detector.command_version(command, list(args))

    def refresh_path(self) -> None:
        """把注册表 PATH 同步进本进程，让刚装好的命令立即可见（Windows）。"""
        added = platform_layer.refresh_process_path()
        if added:
            self.logger.info(f"PATH 已刷新，新增 {len(added)} 个目录：{added[0]}")

    def machine_path(self) -> Optional[str]:
        """机器级 PATH；非 Windows 上不存在这个区分，返回 None。"""
        entries = platform_layer.machine_path_entries()
        return os.pathsep.join(entries) if entries else None

    def visible_without_user_path(self, command: str) -> bool:
        """命令是否只靠机器级 PATH 也能解析到。

        非 Windows 或读不到机器级 Path 时返回 True：这条自检只用来发现「宿主
        shell 看不到用户级 PATH」，读不到数据时不能报假警。
        """
        machine_path = self.machine_path()
        if machine_path is None:
            return True
        return shutil.which(command, path=machine_path) is not None

    def codex_can_run_rtk(self) -> bool:
        """Codex 会话能否执行 ``rtk``：只有桌面端会丢掉用户级 PATH。

        装了桌面版且 ``rtk`` 不在机器级 PATH 上时，``rtk init -g --codex`` 写出的
        ``RTK.md`` 前缀约束在 Codex 里永远无法执行；CLI 从终端启动时会继承完整
        PATH，因此不跳过它的注册。
        """
        path = codex.config_path(self.home)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return True
        if not codex.desktop_app_present(text):
            return True
        return self.visible_without_user_path("rtk")

    def set_brew_mirror(self) -> None:
        """把 Homebrew 切到中科大镜像并关闭自动更新。

        镜像写入进程环境：brew 及其所有子进程都会继承，因此这一步对安装与卸载
        路径、以及 `brew list` 这类探测命令同样生效。
        """
        os.environ.update(config.HOMEBREW_MIRROR_ENV)

    def brew(self, *args: str, capture: bool = False) -> RunResult:
        """调用 Homebrew，并统一注入中科大镜像与关闭自动更新的环境。

        capture=True 用于 `brew list` 这类只读本地状态的探测，不产生终端输出。
        """
        self.set_brew_mirror()
        command = ["brew", *args]
        return self.run_capture(command) if capture else self.run_command(command)

    def brew_manages(self, name: str, *, cask: bool = False) -> bool:
        """判断 name 是否由 Homebrew 自己管理；本地查询，不触发安装动作。"""
        args = ["list", "--cask", name] if cask else ["list", name]
        return self.brew(*args, capture=True).returncode == 0

    def brew_install_or_upgrade(self, formula: str, probe: Optional[str] = None) -> bool:
        """经 Homebrew 安装或升级一个 formula，并返回该通道是否已接管该组件。

        只有「命令缺失」或「命令由 brew 自己管理」时 brew 才是正确的通道：命令已存在
        但不由 brew 管理（例如官方脚本装的 rtk）时直接让位，否则只会装出第二份拷贝，
        而原来的 script/release 通道才是能真正完成升级的那个。
        """
        probe = probe or formula
        if not self.command_exists(probe):
            result = self.brew("install", formula)
            return result.returncode == 0 and self.command_exists(probe)
        if not self.brew_manages(formula):
            self.logger.info(f"{formula} 不在 Homebrew 管理下，跳过 brew 通道交给原通道升级。")
            return False
        result = self.brew("upgrade", formula)
        if result.returncode != 0:
            # brew 管着这个命令、只是这次升级没成功：命令仍可用，不降级到其他通道，
            # 否则会在 script/release 里再装一份把 brew 的拷贝遮住。
            self.logger.warn(f"{formula} 升级未成功，保持现有 Homebrew 安装。")
        return True

    def linux_package_manager(self) -> Optional[str]:
        """返回本机的 Linux 包管理器；非 Linux 一律返回 None。"""
        if self.os_name != "Linux":
            return None
        for candidate in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk"):
            if shutil.which(candidate):
                return candidate
        return None

    def linux_package_command(self, action: str, package: str) -> Optional[list[str]]:
        """构造 Linux 包管理命令，不把系统包操作误用于 macOS。"""
        manager = self.linux_package_manager()
        if not manager:
            return None
        if manager == "apt-get":
            return ["apt-get", action, "-y", package]
        if manager in {"dnf", "yum"}:
            return [manager, action, "-y", package]
        if manager == "pacman":
            return ["pacman", "-R" if action == "remove" else "-S", "--noconfirm", package]
        if manager == "zypper":
            return ["zypper", "--non-interactive", "remove" if action == "remove" else "install", package]
        return ["apk", "del" if action == "remove" else "add", package]

    def run_linux_package(self, action: str, package: str) -> RunResult:
        """以 root 或 sudo 执行 Linux 包操作，并复用统一日志输出。"""
        command = self.linux_package_command(action, package)
        if not command:
            return RunResult(127)
        if not platform_layer.is_admin():
            if not self.command_exists("sudo"):
                self.logger.error("Linux package operation requires root privileges or sudo.")
                return RunResult(127)
            command.insert(0, "sudo")
        return self.run_command(command)

    def linux_package_installed(self, package: str) -> bool:
        """判断目标 Linux 包是否由当前系统包管理器管理。"""
        manager = self.linux_package_manager()
        if not manager:
            return False
        probes = {
            "apt-get": ["dpkg-query", "-W", "-f=${Status}", package],
            "dnf": ["rpm", "-q", package],
            "yum": ["rpm", "-q", package],
            "pacman": ["pacman", "-Q", package],
            "zypper": ["rpm", "-q", package],
            "apk": ["apk", "info", "-e", package],
        }
        result = self.run_capture(probes[manager])
        if manager == "apt-get":
            return result.returncode == 0 and "install ok installed" in result.stdout.lower()
        return result.returncode == 0

    def fetch_text(self, url: str) -> str:
        """获取远程脚本文本；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
        for candidate in config.download_urls(url):
            try:
                request = urllib.request.Request(candidate, headers={"User-Agent": "CodeKit-installer"})
                with urllib.request.urlopen(request, timeout=config.REMOTE_FETCH_TIMEOUT) as response:
                    return response.read().decode("utf-8")
            except (OSError, UnicodeError):
                continue
        return ""

    def agent_notice_box(self, lines: Sequence[str]) -> str:
        """按终端显示宽度渲染提示边框，避免中英文混排时边框错位。"""
        width = max(detector.terminal_width(line) for line in lines) + 4
        border = "═" * (width - 2)
        rows = "\n".join(
            "║" + detector.terminal_pad(f"  {line}", width - 2) + "║"
            for line in lines
        )
        return f"╔{border}╗\n{rows}\n╚{border}╝"

    def render_table(self, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
        """按终端显示宽度渲染带列分隔的表格，避免中英文混排时列错位。"""
        widths = [
            max(
                detector.terminal_width(header),
                *(detector.terminal_width(row[index]) for row in rows),
            )
            for index, header in enumerate(headers)
        ]
        border = "  ╔" + "╤".join("═" * (width + 2) for width in widths) + "╗"
        separator = "  ╟" + "┼".join("─" * (width + 2) for width in widths) + "╢"
        bottom = "  ╚" + "╧".join("═" * (width + 2) for width in widths) + "╝"

        def render_row(values: Sequence[str]) -> str:
            cells = " │ ".join(
                detector.terminal_pad(value, widths[index])
                for index, value in enumerate(values)
            )
            return f"  ║ {cells} ║"

        lines = [border, render_row(headers), separator]
        lines.extend(render_row(row) for row in rows)
        lines.append(bottom)
        return "\n".join(lines)

    def preflight_agents(self) -> bool:
        """只检查五个平台的 CLI，缺失时提示后续由勾选的插件自动安装 CLI。"""
        rows = []
        for entry in agents.AGENT_CLIS:
            source, version = self.agent_cli_status(entry.key)
            if not source:
                continue
            rows.append(
                (entry.key, version or "unknown", agents.cli_command_path(entry, self.home))
            )
        if rows:
            self.logger.success("\nCoding agent CLI(s) detected:")
            self.logger.info(f"\n{self.render_table(('平台', '版本', '路径'), rows)}\n")
            return True
        self.logger.warn("未检测到任何编程智能体 CLI。")
        self.logger.info(
            "\n" + self.agent_notice_box(
                (
                    "未检测到编程智能体 CLI：Claude Code / Codex / OpenCode / Qoder / Qoder CN",
                    "在选择器中勾选某个平台的插件后，安装器会先自动安装该平台 CLI，再安装插件。",
                    "未勾选任何平台插件时，安装器不会自动安装 CLI。",
                )
            )
        )
        return True

    def prepare_sources(self) -> None:
        """从本地仓库构建 Claude/Codex/OpenCode/Qoder 使用的兼容插件源。"""
        if not self.marketplace_local:
            return
        try:
            (
                self.market_dir,
                self.opencode_source_dir,
                self.qoder_source_dir,
            ) = source_builder.prepare(self.marketplace_local, self.state_dir)
        except (OSError, ValueError) as exc:
            raise InstallerError(f"Could not prepare compatibility plugin sources: {exc}") from exc

    def load_states(self) -> None:
        """检测所有注册组件的安装状态，并缓存结果供选择器和执行阶段使用。"""
        self.logger.info("Checking all components...")
        try:
            self.states = {item.component_id: item for item in detector.all_states(self.context)}
        except (OSError, RuntimeError, ValueError) as exc:
            raise InstallerError(f"Component state detection failed: {exc}") from exc

    def agent_cli_status(self, platform_key: str) -> tuple[str, str]:
        """返回平台 CLI 的发现来源与版本；同一次运行内复用探测结果。"""
        cache = getattr(self, "agent_cli_cache", None)
        if cache is None:
            cache = self.agent_cli_cache = {}
        if platform_key not in cache:
            source = detector.agent_cli_presence(platform_key, self.home)
            version = agents.cli_version(platform_key, self.home) if source else ""
            cache[platform_key] = (source, version)
        return cache[platform_key]

    def refresh_agent_cli_status(self, platform_key: str) -> tuple[str, str]:
        """安装 CLI 后丢弃缓存，让后续状态展示读取最新探测结果。"""
        cache = getattr(self, "agent_cli_cache", None)
        if cache is not None:
            cache.pop(platform_key, None)
        return self.agent_cli_status(platform_key)

    def component_tag(self, component_id: str) -> str:
        """生成安装组件选择器中的状态提示，插件额外区分 CLI 本体的状态与版本。"""
        item = self.states[component_id]
        entry = agents.component_agent(component_id)
        if entry is not None:
            return self.plugin_component_tag(item, entry)
        installed_version = item.installed_version or "unknown"
        if component_id == "gh":
            # gh --version 会附带日期、平台和构建信息，选择器只保留三段版本号。
            installed_version = config.extract_version(installed_version) or installed_version
        if item.status == "not_installed":
            return " (组件未安装，勾选后安装)"
        if item.status == "installed":
            return f" (组件有更新，勾选后升级 {installed_version} → {item.latest_version})"
        return f" (组件已最新 {installed_version}，勾选后重装)"

    def plugin_component_tag(self, item, entry) -> str:
        """插件条目同时显示插件状态/版本与对应平台 CLI 的状态/版本。"""
        source, cli_version = self.agent_cli_status(entry.key)
        if source:
            cli_text = f"CLI 已安装 {cli_version or 'unknown'}"
        else:
            cli_text = f"CLI 未安装（勾选后自动安装 {entry.display_name} CLI）"
        plugin_version = item.installed_version or "unknown"
        if item.status == "not_installed":
            return f" (插件未安装 · {cli_text}，勾选后安装插件)"
        if item.status == "installed":
            if item.latest_version and item.latest_version != plugin_version:
                return f" (插件有更新 {plugin_version} → {item.latest_version} · {cli_text}，勾选后升级)"
            # 版本未变但插件源内容已更新（本地 marketplace 换了提交）：只播报
            # 插件版本，绝不把提交号当作版本号展示。
            return f" (插件有更新 {plugin_version} · {cli_text}，勾选后升级)"
        return f" (插件已最新 {plugin_version} · {cli_text}，勾选后重装)"

    def shell_config(self) -> Optional[tuple[Path, str]]:
        """返回需要写入 PATH 的 shell 配置；Windows 上没有这样的文件。"""
        return self.platform.shell_config(self.home)

    def shell_path_configs(self) -> tuple[tuple[Path, str], ...]:
        """返回全部需要写入 PATH 的 shell 配置；Windows 上没有。"""
        return self.platform.shell_path_configs(self.home)

    def path_configured(self) -> bool:
        """检查所有 shell 配置是否都已包含 CodeKit 用户工具目录。

        只检查当前 ``$SHELL`` 的交互配置会漏掉 agent 实际使用的 login shell
        （``~/.profile``），于是工具在 agent 里不可见而回落到 ``npx @latest``。
        Windows 没有可写的 shell 配置，改为检查 npm 全局目录是否已在 PATH 上
        （Node 安装包通常会写入），避免把「配置缺失」误报成需要处理。
        """
        configs = self.shell_path_configs()
        if not configs:
            return self.tool_dir_on_path()
        for path, line in configs:
            try:
                if line not in path.read_text(encoding="utf-8"):
                    return False
            except (OSError, UnicodeDecodeError):
                return False
        return True

    def tool_dir_on_path(self) -> bool:
        """判断本平台需要的用户工具目录是否已经出现在 PATH 中。"""
        entries = {os.path.normcase(os.path.normpath(value)) for value in os.environ.get("PATH", "").split(os.pathsep) if value}
        for directory in (self.bin_dir, self.npm_bin_dir):
            if os.path.normcase(os.path.normpath(str(directory))) not in entries:
                return False
        return True

    def cli_available_for(self, component_id: str) -> bool:
        """判断组件是否没有 CLI 前置要求，或该前置已经满足。"""
        entry = agents.component_agent(component_id)
        if entry is None:
            return True
        return bool(self.agent_cli_status(entry.key)[0])

    def announce_default_selection(self, default: Sequence[str]) -> None:
        """显式播报默认选择，替代不可用的交互选择器。

        静默采用默认值比报错更糟：用户会以为自己在交互，实际已经被决定。这里把
        与选择器等价的清单（分组、状态标签、勾选标记）直接打印出来。
        """
        self.logger.warn(
            "交互式选择器暂不可用（需要 termios 与 /dev/tty），本次使用默认选择。"
        )
        previous_group = ""
        lines = ["\n  本次安装选择（● = 勾选，○ = 跳过）："]
        for index, item in enumerate(self.components, 1):
            title = detector.registry.GROUPS[item.group]
            if item.group != previous_group:
                if previous_group:
                    lines.append("")
                lines.append(f"  {title}")
                previous_group = item.group
            mark = "●" if item.component_id in default else "○"
            lines.append(
                f"   {mark} {index:>2}. {item.description}{self.component_tag(item.component_id)}"
            )
        self.logger.info("\n".join(lines))

    def select_components(self) -> list[str]:
        """按安装状态生成默认选择，并在交互模式下调用选择器回填用户选择。"""
        default = [
            item.component_id
            for item in self.components
            if self.states[item.component_id].status != "up_to_date"
            # 缺 CLI 的插件不默认勾选：自动安装 CLI 必须来自用户的显式勾选。
            and self.cli_available_for(item.component_id)
        ]
        path_selected = not self.path_configured()
        # 非交互执行必须可用于 CI：采用默认选择，并跳过终端选择器。
        if self.auto_yes or not self.platform.can_interact():
            self.setup_path_selected = path_selected
            self.selected = default
            if not self.auto_yes:
                self.announce_default_selection(default)
            return default

        # 选择器只消费临时 TSV；finally 保证异常、取消和正常返回都会清理它。
        manifest = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="codekit-selector-", suffix=".tsv", delete=False)
        manifest_path = Path(manifest.name)
        try:
            for index, component in enumerate(self.components, 1):
                fields = (
                    str(index), component.description, component.group,
                    detector.registry.GROUPS[component.group],
                    self.component_tag(component.component_id),
                    "1" if component.component_id in default else "0",
                )
                manifest.write("\t".join(value.replace("\t", " ") for value in fields) + "\n")
            manifest.close()
            configured = self.shell_config()
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "component-selector.py"), "--mode", "install", "--manifest", str(manifest_path), "--path-selected", "1" if path_selected else "0", "--shell-config", str(configured[0]) if configured else ""],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                **platform_layer.text_kwargs(),
            )
        finally:
            manifest_path.unlink(missing_ok=True)
        if result.returncode == 2:
            # 选择器拿不到可用终端（例如没有控制台的 Windows 会话）：显式降级，
            # 不把缺少终端当成失败。
            self.setup_path_selected = path_selected
            self.selected = default
            self.announce_default_selection(default)
            return default
        if result.returncode == 10:
            raise SystemExit(0)
        if result.returncode == 130:
            raise KeyboardInterrupt
        if result.returncode != 0:
            raise InstallerError(f"Interactive selector failed: {result.stderr.strip()}")
        payload = result.stdout.strip()
        if not payload.startswith("selected=") or ";path=" not in payload:
            raise InstallerError("Interactive selector returned an invalid result.")
        selected_text, path_text = payload.removeprefix("selected=").split(";path=", 1)
        indices = [int(value) for value in selected_text.split(",") if value]
        self.setup_path_selected = path_text == "1"
        self.selected = [self.components[index - 1].component_id for index in indices]
        return self.selected

    def marketplace_sha(self) -> str:
        """读取本地 marketplace 仓库 HEAD，用于判断插件源是否需要刷新。"""
        return detector.marketplace_sha(self.context)

    def record_marketplace_sha(self) -> None:
        """把当前 marketplace 提交号保存到版本状态文件，供下次检测比较。"""
        sha = self.marketplace_sha()
        if sha:
            config.set_key_value(self.version_file, "marketplace_sha", sha)
            self.logger.success(f"Marketplace version recorded: {sha[:7]}")

    def component(self, component_id: str):
        """按 ID 读取策略表中的组件定义；未知组件返回 None。"""
        return detector.registry.component_by_id(component_id)

    def requirements_met(self, component) -> tuple[bool, str]:
        """校验组件的硬前置（如 node>=24.11.0），返回 (是否满足, 原因)。"""
        for requirement in component.requires:
            name, separator, minimum = requirement.partition(">=")
            if name == "node":
                current = self.command_version("node", ["-v"])
                if not separator or not current or not config.version_greater_or_equal(current, minimum):
                    return False, f"需要 Node.js >= {minimum}，当前 {current or '未安装'}"
                continue
            if name == "python":
                current = self.python_dependency_version()
                if not separator or not config.version_greater_or_equal(current, minimum):
                    return False, f"需要 Python >= {minimum}，当前 {current}"
                continue
            if not self.command_exists(name):
                return False, f"需要命令 {name}"
        return True, ""

    def channel_available(self, kind: str) -> bool:
        """判断通道在当前平台是否可用；只做探测，不触发安装。"""
        if kind == "npm":
            return self.command_exists("npm")
        if kind in {"brew", "brew_cask"}:
            return self.command_exists("brew")
        if kind == "apt":
            # apt 通道的 target 是 Debian/Ubuntu 包名，只在该包管理器下可用。
            return self.linux_package_manager() == "apt-get"
        if kind == "winget":
            # winget 只在 Windows 上存在；其他平台的同名命令即使有也不应使用。
            return self.platform.is_windows and self.command_exists("winget")
        if kind == "venv":
            # 内置运行时才是会被使用的那一个：用系统 Python 建出的 venv 在受限宿主里
            # 起不来（pyvenv.cfg 的 home 落在读不到的目录）。运行时不可用时诚实返回
            # False，让 install_via_channels 把这个组件如实判失败，而不是产出跑不起来
            # 的 venv，也不是退到根外的落点。
            if not self.toolchain.usable:
                return False
            return self.run_capture(
                [self.toolchain_python(), "-c", "import venv, ensurepip"]
            ).returncode == 0
        if kind == "script":
            # 官方 .sh 安装脚本只在类 Unix 环境里跑。Git for Windows 也会把 bash 放进
            # PATH，所以不能只判断"有没有 shell"：否则 Windows 上会退化成用 Git Bash
            # 执行 Unix 安装脚本，把文件装进 MSYS 目录树而不是 Windows 的 npm 目录。
            return not self.platform.is_windows and bool(
                platform_layer.remote_script_command("bash")
            )
        if kind == "powershell":
            # 严格限定 Windows：pwsh 在 macOS/Linux 上也可能存在，但那些 .ps1
            # 是 Windows 专用的安装脚本，不能跨平台执行。
            return self.platform.is_windows and bool(
                platform_layer.remote_script_command("powershell")
            )
        if kind == "release":
            return self.command_exists("curl") or self.command_exists("wget")
        return False

    def manual_note(self, component) -> str:
        """生成组件的手动安装指引，供所有通道都失败时提示。"""
        channels = component.channels
        # Windows 上没有可用的 Unix shell，`curl … | bash` 这条提示会把人引到不存在的
        # 解释器上；按 Windows 真正可用的通道给指引。
        if self.platform.is_windows:
            if "winget" in channels:
                return f"winget install --id {channels['winget']} --exact"
            if "npm" in channels:
                registry = " ".join(config.NPM_REGISTRY_ARGS)
                return f"npm install -g {channels['npm']} {registry}"
            if "powershell" in channels:
                return (
                    "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                    f"\"irm {channels['powershell']} | iex\""
                )
            if component.release is not None:
                return (
                    f"从 {component.release.repo} 的 Releases 下载对应的 Windows 资产"
                    f"（{component.release.binary}）手工安装并加入 PATH"
                )
            return "请参考官方文档"
        if "script" in channels:
            return f"curl -fsSL {config.github_proxy_url(channels['script'])} | bash"
        if "powershell" in channels:
            return (
                "powershell -NoProfile -ExecutionPolicy Bypass -Command "
                f"\"irm {channels['powershell']} | iex\""
            )
        if "npm" in channels:
            registry = " ".join(config.NPM_REGISTRY_ARGS)
            return f"npm install -g {channels['npm']} {registry}"
        if "winget" in channels:
            return f"winget install --id {channels['winget']} --exact"
        if "brew" in channels:
            return f"brew install {channels['brew']}（建议先按 INSTALL-STRATEGY.md §2.3 配置中科大镜像）"
        if "apt" in channels:
            return f"sudo apt-get install -y {channels['apt']}"
        return "请参考官方文档"

    def channel_order(self, component) -> tuple[str, ...]:
        """通道优先级；根接管的组件只剩 ``entry_kind`` 允许的那一根根内通道。

        过滤而不是排序是刻意的：留下的候选全部指向工具链根内，venv 失败之后没有第二
        根通道可以漏到根外去。
        """
        allowed = detector.registry.ENTRY_KIND_CHANNELS.get(component.entry_kind)
        order = tuple(
            kind for kind in detector.registry.CHANNEL_ORDER if kind in component.channels
        )
        if allowed is None:
            return order
        return tuple(kind for kind in order if kind in allowed)

    def install_via_channels(self, component_id: str) -> bool:
        """按全局通道优先级，选用第一个可用通道安装组件。"""
        component = self.component(component_id)
        if component is None:
            return False
        met, reason = self.requirements_met(component)
        if not met:
            self.logger.error(f"{component.description} 已跳过：{reason}")
            return False
        # 工具链根接管的组件只能落在根内：受限宿主（Codex 桌面端）读不到根外的落点，
        # 装上了也起不来。所以候选通道就是 entry_kind 允许的那几根，一根都不可用时如实
        # 失败——循环里不存在退到根外的第二条通道。
        candidates = self.channel_order(component)
        if component.entry_kind and not any(
            self.channel_available(kind) for kind in candidates
        ):
            if "venv" in candidates and not self.toolchain.usable:
                reason = f"内置 Python 运行时不可用（{self.toolchain.reason or '原因未知'}）"
            else:
                reason = f"根内通道 {'、'.join(candidates) or '（未声明）'} 当前不可用"
            self.logger.error(
                f"{component.description} 已跳过：{reason}。"
                f"它的落点必须在 {self.platform.toolchain_root(self.home)} 内"
                "才能在受限宿主里启动；修复网络或补齐依赖后重跑安装器。"
            )
            return False
        for kind in candidates:
            if kind == "release":
                if component.release is None:
                    continue
                label = component.release.repo
            else:
                label = component.channels.get(kind, "")
                if not label:
                    continue
            if not self.channel_available(kind):
                continue
            self.logger.info(f"使用 {kind} 通道安装 {component.description}（{label}）")
            if self.install_channel(component, kind):
                return True
            self.logger.warn(f"{kind} 通道未成功，尝试下一通道。")
        self.logger.error(
            f"{component.description} 安装失败；请手动安装：{self.manual_note(component)}"
        )
        return False

    def install_channel(self, component, kind: str) -> bool:
        """执行单个通道的安装动作，并以组件命令校验最终可用性。"""
        target = component.channels.get(kind, "")
        if kind == "npm":
            if component.entry_kind == "npm-private":
                return self.install_npm_package_in_root(component)
            return self.install_npm_package(target, component.command)
        if kind == "brew":
            return self.brew_install_or_upgrade(target, component.command)
        if kind == "brew_cask":
            return self.install_brew_cask(target, component.command)
        if kind == "apt":
            return self.install_apt_package(target, component.command)
        if kind == "winget":
            return self.install_winget_package(target, component.command)
        if kind == "venv":
            return self.install_venv_package(target, component.command)
        if kind == "script":
            return self.run_remote_script(target) and self.command_exists(component.command)
        if kind == "powershell":
            return self.run_remote_script(target, shell="powershell") and self.command_exists(component.command)
        if kind == "release":
            return self.install_release(component)
        return False

    def install_npm_package(self, package: str, command: str) -> bool:
        """使用用户态 npm 前缀安装或升级一个全局 CLI，镜像源由 set_npm 注入的环境变量提供。"""
        self.set_npm()
        result = self.run_command(["npm", "install", "-g", f"{package}@latest"])
        return result.returncode == 0 and self.command_exists(command)

    def install_brew_cask(self, cask: str, command: str) -> bool:
        """经 Homebrew cask 安装或升级；cask 不在 brew 管理下时让位给下一通道。"""
        if not self.command_exists(command):
            result = self.brew("install", "--cask", cask)
            return result.returncode == 0 and self.command_exists(command)
        if not self.brew_manages(cask, cask=True):
            self.logger.info(f"{cask} 不在 Homebrew cask 管理下，跳过该通道交给原通道升级。")
            return False
        result = self.brew("upgrade", "--cask", cask)
        if result.returncode != 0:
            self.logger.warn(f"{cask} 升级未成功，保持现有 Homebrew cask 安装。")
        return True

    def install_apt_package(self, package: str, command: str) -> bool:
        """经 sudo 安装 Debian/Ubuntu 包；包不在源中时返回 False 以便降级。"""
        if self.run_capture(["apt-cache", "show", package]).returncode != 0:
            self.logger.warn(f"apt 源中不存在 {package}，跳过 apt 通道。")
            return False
        if self.run_linux_package("install", package).returncode != 0:
            return False
        return self.command_exists(command)

    def winget_manages(self, package: str) -> bool:
        """判断该包是否由 winget 自己管理；本地查询，不触发安装。"""
        result = self.run_capture(
            [
                "winget", "list", "--id", package, "--exact",
                "--accept-source-agreements", "--disable-interactivity",
            ],
            timeout=180,
        )
        return result.returncode == 0 and package.lower() in result.stdout.lower()

    def install_winget_package(self, package: str, command: str) -> bool:
        """经 winget 安装或升级一个组件。

        非交互场景必须显式接受包协议与源协议，否则 winget 会挂起等待输入。语义与 brew
        通道对齐：命令缺失才 install；命令已存在时先确认由 winget 管理再 upgrade，且
        upgrade 返回"已是最新"（实测退出码 43）不算失败 —— 否则会降级到下一通道装出
        第二份拷贝。
        """
        common = (
            "--id", package, "--exact", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity",
        )
        if not self.command_exists(command):
            result = self.run_command(["winget", "install", *common], timeout=1200)
            # winget 只改注册表：不刷新会把"装好了"误判成"通道失败"。
            self.refresh_path()
            return result.returncode == 0 and self.command_exists(command)
        if not self.winget_manages(package):
            self.logger.info(f"{package} 不在 winget 管理下，跳过 winget 通道交给原通道升级。")
            return False
        result = self.run_command(["winget", "upgrade", *common], timeout=1200)
        self.refresh_path()
        if result.returncode != 0:
            self.logger.warn(
                f"{package} 升级未返回成功（退出码 {result.returncode}），保持现有 winget 安装。"
            )
        return True

    def install_npm_package_in_root(self, component) -> bool:
        """把根接管的 npm 组件装进 CodeKit 私有前缀，并给人类 shell 留一个转发入口。

        私有前缀落在工具链根内（实测可读可执行）。转发入口只是给人用的：它在受限宿主里
        读不到，CodeKit 自己的代码路径一律走 toolchain.json 的绝对 argv，所以建不出来
        不影响工具本身——POSIX 下需要符号链接能力，Windows 上没有开发者模式/管理员权限
        时必然失败。
        """
        prefix = self.platform.npm_prefix_private(self.home)
        if not self.install_npm_package_into_prefix(
            prefix, component.channels["npm"], component.command
        ):
            return False
        try:
            self.platform.expose_command(
                self.bin_dir,
                component.command,
                self.platform.npm_bin_dir(prefix) / self.platform.shim_name(component.command),
            )
        except OSError as exc:
            self.logger.warn(
                f"未能为交互式 shell 暴露 {component.command} 转发入口（{exc}）；"
                f"不影响 CodeKit 使用它，安装器走 toolchain.json 里的绝对 argv。"
            )
        return True

    def install_venv_package(self, package: str, command: str) -> bool:
        """把 PyPI 包装进独立 venv，并把它的命令暴露到用户 bin 目录。

        venv 一律用内置运行时创建，并加 ``--copies``：用系统 Python 建出的 venv 会把
        ``pyvenv.cfg`` 的 ``home`` 指向可能被沙箱拒绝的目录，入口存在却启动不起来；
        实体解释器留在 venv 内则不受符号链接解析的限制。
        """
        venv = self.platform.venv_dir(self.home, command)
        if venv.exists():
            platform_layer.rmtree(venv, ignore_errors=True)
        pip = self.platform.venv_pip(venv)
        if self.run_command(
            [self.toolchain_python(), "-m", "venv", "--copies", str(venv)]
        ).returncode != 0:
            return False
        if not pip.exists() or self.run_command([str(pip), "install", "-U", package]).returncode != 0:
            return False
        executable = self.platform.venv_exe(venv, command)
        if not executable.exists():
            return False
        # Windows 上不能靠符号链接暴露 venv 命令，适配层会改写成 .cmd 转发脚本。
        self.platform.expose_command(self.bin_dir, command, executable)
        return self.command_exists(command)

    def release_arch(self) -> str:
        """把平台机器名归一化为发布资产的架构标记。"""
        return {
            "x86_64": "x86_64",
            "amd64": "x86_64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }.get(platform.machine().lower(), "")

    def release_asset_name(self, spec, version: str) -> str:
        """按 release 模板展开当前平台的资产文件名。"""
        arch = self.release_arch()
        return spec.asset.format(
            version=version,
            os=spec.os_tokens.get(self.os_name, ""),
            arch=spec.arch_tokens.get(arch, ""),
            ext=spec.ext_tokens.get(self.os_name, ""),
            target=spec.target_tokens.get(f"{self.os_name}-{arch}", ""),
        )

    def fetch_json_url(self, url: str):
        """获取发布接口 JSON；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
        for candidate in config.download_urls(url):
            try:
                request = urllib.request.Request(
                    candidate,
                    headers={"Accept": "application/json", "User-Agent": "CodeKit-installer"},
                )
                with urllib.request.urlopen(request, timeout=config.REMOTE_FETCH_TIMEOUT) as response:
                    return json.load(response)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return None

    def resolve_release(self, spec) -> Optional[tuple[str, str, str]]:
        """解析最新发布版本，返回 (版本, 下载地址, sha256)；失败返回 None。"""
        kind, _, ident = spec.repo.partition(":")
        if kind == "github":
            document = self.fetch_json_url(
                f"https://api.github.com/repos/{ident}/releases/latest"
            )
            if not document:
                return None
            version = str(document.get("tag_name", "")).lstrip("v")
            asset_name = self.release_asset_name(spec, version)
            for asset in document.get("assets", []):
                if asset.get("name") == asset_name:
                    digest = str(asset.get("digest", "")).split(":", 1)[-1]
                    return version, str(asset.get("browser_download_url", "")), digest
            return None
        if kind == "gitlab":
            document = self.fetch_json_url(
                f"https://gitlab.com/api/v4/projects/{ident}/releases/permalink/latest"
            )
            if not document:
                return None
            version = str(document.get("tag_name", "")).lstrip("v")
            asset_name = self.release_asset_name(spec, version)
            links = (document.get("assets") or {}).get("links", [])
            for link in links:
                if link.get("name") == asset_name:
                    return version, str(link.get("direct_asset_url", "")), ""
            return None
        return None

    def download_release(self, url: str, digest: str) -> Optional[Path]:
        """下载发布资产并校验摘要；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
        options = [
            "--fail", "--location", "--connect-timeout", "10", "--speed-limit", "51200",
            "--speed-time", "20", "--retry", "2", "--retry-max-time", "180",
            "--continue-at", "-", "--progress-bar",
        ]
        fd, name = tempfile.mkstemp(prefix="codekit-release-", dir=self.platform.temp_dir())
        os.close(fd)
        temporary = Path(name)
        result = RunResult(1)
        for candidate in config.download_urls(url):
            # --continue-at - 会续传上一个地址留下的半成品，切换地址前必须清空。
            temporary.write_bytes(b"")
            result = self.run_command(["curl", *options, "-o", str(temporary), candidate])
            if result.returncode == 0:
                break
        if result.returncode != 0:
            temporary.unlink(missing_ok=True)
            return None
        if digest and hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            temporary.unlink(missing_ok=True)
            self.logger.error("发布包摘要校验失败，已删除下载文件。")
            return None
        return temporary

    def extract_release_binary(self, archive: Path, binary: str) -> Optional[Path]:
        """从 tar.gz/zip 发布包中定位目标可执行文件。

        Windows 构建的可执行文件带 ``.exe``，因此按平台文件名与裸名各找一次，
        这样同一个发布说明在 macOS/Linux 与 Windows 上都能命中。
        """
        directory = Path(tempfile.mkdtemp(prefix="codekit-extract-"))
        try:
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as package:
                    package.extractall(directory)
            else:
                with tarfile.open(archive) as package:
                    try:
                        package.extractall(directory, filter="data")
                    except TypeError:
                        # Python < 3.12 has no extraction filter.
                        package.extractall(directory)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError):
            return None
        for name in dict.fromkeys((self.platform.binary_filename(binary), binary)):
            for candidate in directory.rglob(name):
                if candidate.is_file():
                    return candidate
        return None

    def install_release(self, component) -> bool:
        """按 release 通道解析、下载并安装组件的最新发布包。"""
        resolved = self.resolve_release(component.release)
        if not resolved or not resolved[1]:
            self.logger.warn(f"未能解析 {component.description} 的发布包地址。")
            return False
        version, url, digest = resolved
        self.logger.info(f"下载 {component.description} {version}：{url}")
        archive = self.download_release(url, digest)
        if archive is None:
            return False
        try:
            binary = self.extract_release_binary(archive, component.release.binary)
        finally:
            archive.unlink(missing_ok=True)
        if binary is None:
            return False
        target = self.platform.release_target(self.bin_dir, component.release.binary)
        target.unlink(missing_ok=True)
        shutil.move(str(binary), str(target))
        if not self.platform.is_windows:
            target.chmod(0o755)
        self.track(target)
        return self.command_exists(component.command)

    def run_remote_script(self, url: str, shell: str = "bash", env: Optional[dict[str, str]] = None) -> bool:
        """下载远程安装脚本后交给平台原生的 shell 执行。

        shell="powershell" 用于 Windows 专用的 .ps1 安装器（Qoder 等官方只提供
        .ps1），脚本同样经 stdin 喂入，避免落盘与执行策略的额外处理。
        """
        script = self.fetch_text(url)
        if not script:
            return False
        command = platform_layer.remote_script_command(shell)
        if not command:
            return False
        if self.run_with_input(list(command), script, env).returncode != 0:
            return False
        # 官方 .ps1 安装器同样把 PATH 写进注册表，刷新后才能校验命令可用性。
        self.refresh_path()
        return True

    # rtk 的按宿主参数表：(注册用的完整参数, 撤销时附加在 `rtk init -g … --uninstall`
    # 上的平台选择器)。撤销只带选择器——``--auto-patch`` 是注册时的模式参数，带着它去
    # 撤销会改变 rtk 自己的收尾行为。安装与撤销共用这一张表，两边不再各抄一份名单
    # （历史上卸载侧就漏过 opencode）。
    RTK_PLATFORMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        "claude": (("--auto-patch",), ()),
        "codex": (("--codex",), ("--codex",)),
        "opencode": (("--opencode", "--auto-patch"), ("--opencode",)),
    }

    def rtk_targets(self) -> list[str]:
        """本机装了哪些 CLI，就注册/撤销哪些 rtk 目标。"""
        return [
            platform for platform in self.RTK_PLATFORMS if self.command_exists(platform)
        ]

    def configure_rtk(self) -> bool:
        """为已存在的 AI 编程工具注册 rtk 的全局 token 优化配置。"""
        results = []
        for platform in self.rtk_targets():
            if platform == "codex" and not self.codex_can_run_rtk():
                # Codex 使用 AGENTS.md/RTK.md 指令集，必须走它自己的初始化模式；但桌面版
                # 的 shell 连 rtk 本身都启动不了，写出的约束只会每条会话都做不到。
                self.logger.warn(
                    "跳过 codex 侧 rtk 注册：检测到 Codex 桌面版，而它的 shell 只继承机器级 "
                    "PATH，rtk 不在其中。此时写出的 RTK.md 前缀约束在 Codex 会话里无法执行，"
                    "只会让每次会话都多出一条做不到的约束。注意只把 rtk 所在目录加入机器级 "
                    "PATH 往往还不够——Codex 桌面版的沙箱会在命令层面拒绝读取用户级目录；"
                    "确认 rtk 真的能在 Codex 会话里跑起来之后再重跑安装即可。只想消除该约束"
                    "时删除 ~/.codex/AGENTS.md 里的 @RTK.md 引用。"
                )
                continue
            results.append(
                self.run_command(["rtk", "init", "-g", *self.RTK_PLATFORMS[platform][0]])
            )
        return all(result.returncode == 0 for result in results)

    def install_rtk(self) -> bool:
        """按策略表安装 rtk（brew → 官方脚本），再写入各智能体配置。"""
        self.logger.info("Installing rtk (Rust Token Killer)...")
        if not self.install_via_channels("rtk"):
            return False
        return self.configure_rtk()

    def install_gitnexus(self) -> bool:
        """安装 gitnexus（落点由注册表的 ``entry_kind`` 决定），再做它自己的初始化。

        装进 CodeKit 私有 npm 前缀而不是 npm 全局目录：受限宿主（Codex 桌面端）拒绝
        读取 ``%APPDATA%\\npm`` 下的任何文件，装在那里的 gitnexus 在会话里连 stat 都
        失败。这一步与其他根接管组件共用同一套通道逻辑，这里只补 gitnexus 特有的两项。
        """
        if not self.install_via_channels("gitnexus"):
            return False
        if self.run_command(["gitnexus", "setup"]).returncode != 0:
            return False
        return self.register_gitnexus_for_qoder()

    def gitnexus_hook_source(self) -> Optional[Path]:
        """GitNexus 自带 hook 脚本所在目录。

        包内 ``hooks/claude`` 是 ``gitnexus setup`` 自己的规范源；只有从 PATH 上的
        shim 定位不到包目录时，才退回 setup 已经复制到 ``~/.claude/hooks/gitnexus``
        的那一份，避免 Qoder 依赖一次成功的 Claude 注册。
        """
        candidates: list[Path] = []
        resolved = platform_layer.resolve_exe("gitnexus")
        if resolved:
            bin_dir = Path(resolved).parent
            candidates.extend(
                bin_dir.joinpath(*parts)
                for parts in (
                    ("node_modules", "gitnexus", "hooks", "claude"),
                    ("..", "node_modules", "gitnexus", "hooks", "claude"),
                    ("lib", "node_modules", "gitnexus", "hooks", "claude"),
                )
            )
        candidates.append(self.home / ".claude" / "hooks" / "gitnexus")
        return next(
            (path for path in candidates if (path / "gitnexus-hook.cjs").is_file()),
            None,
        )

    def prune_dead_qoder_mcp_file(self) -> None:
        """删掉 ``gitnexus setup`` 写下的 ``~/.qoder.json``。

        Qoder 的用户级 MCP 只从共享配置根的 settings.json 读取，这个文件没有任何
        读者；留着会让「已注册」的假象在下一轮排查里反复出现。只有整份文档就是
        gitnexus 一条时才删，避免动到第三方写进去的内容。
        """
        path = self.home / ".qoder.json"
        if not path.is_file():
            return
        try:
            document = config.read_json(path)
        except ValueError:
            return
        if not isinstance(document, dict) or set(document) != {"mcpServers"}:
            return
        servers = document.get("mcpServers")
        if not isinstance(servers, dict) or set(servers) != {qoder.GITNEXUS_MCP_NAME}:
            return
        path.unlink(missing_ok=True)
        self.logger.info(
            f"已删除 {path}：Qoder 不读这个文件，MCP 已注册进共享配置根的 settings.json。"
        )

    def gitnexus_mcp_argv(self) -> list[str]:
        """启动 GitNexus MCP 的 argv：优先私有前缀里的 ``[<node>, <cli.js>]``。

        MCP server 是宿主在**会话里**拉起的，正是 npm 垫片不可达的那个环境，所以注册
        进宿主配置的必须是绝对可执行 + 绝对脚本，而不是 ``gitnexus`` 这个名字。
        """
        entry = toolchain.entry(self, self.component("gitnexus"))
        runner = entry.get("runner") if isinstance(entry, dict) else None
        if entry.get("state") == "ok" and isinstance(runner, list) and runner:
            return [str(item) for item in runner]
        resolved = platform_layer.resolve_exe("gitnexus")
        if resolved:
            # 按名字的命令在受限宿主里可能连 stat 都做不到；注册它只是不让 MCP 缺席，
            # 真要用还得重跑安装器把落点搬回工具链根内。
            self.logger.warn(
                f"GitNexus 清单态为 {entry.get('state')}，MCP 暂按 PATH 解析出的 "
                f"{resolved} 注册；该入口在受限宿主里可能不可读，重跑安装器可修复。"
            )
        return [str(resolved)] if resolved else []

    def register_gitnexus_for_qoder(self) -> bool:
        """把 GitNexus 注册到 Qoder 真正加载的位置（MCP 与 hooks）。

        ``gitnexus setup`` 的 Qoder MCP 目标指向无人读取的 ``~/.qoder.json``，而它的
        hook 目标表里根本没有 Qoder，所以两端都由 CodeKit 补齐。宿主看不见等同没装，
        因此这里失败必须报错，不能静默降级成「只注册了一半」。
        """
        roots = qoder.existing_roots(qoder.PLATFORMS.values(), self.home)
        if not roots:
            return True
        mcp_argv = self.gitnexus_mcp_argv()
        if not mcp_argv:
            self.logger.error(
                "无法解析 gitnexus 的启动命令，无法为 Qoder 注册 MCP 与 hooks；"
                "请重新安装 gitnexus 后重试。"
            )
            return False
        source = self.gitnexus_hook_source()
        if source is None:
            self.logger.error(
                "未找到 GitNexus 自带的 hook 脚本（hooks/claude），Qoder 侧的图谱增强"
                "无法注册；请重新安装 gitnexus 后再试。"
            )
            return False
        ok = True
        for root in roots:
            try:
                qoder.register_mcp_server(
                    root,
                    qoder.GITNEXUS_MCP_NAME,
                    {"command": mcp_argv[0], "args": [*mcp_argv[1:], "mcp"]},
                )
                script = qoder.install_gitnexus_hook_scripts(root, source)
                qoder.register_gitnexus_hooks(root, script)
            except (OSError, ValueError) as exc:
                self.logger.error(f"Qoder（{root}）注册 GitNexus 失败：{exc}")
                ok = False
                continue
            self.track(qoder.settings_path(root))
            self.track(qoder.hook_script_dir(root))
            self.logger.info(
                f"GitNexus 已注册到 Qoder：MCP + hooks（{detector.display_path(root, self.home)}）"
            )
        if ok:
            self.prune_dead_qoder_mcp_file()
        return ok

    def ensure_unzip(self) -> bool:
        """确保钉钉 CLI 的安装脚本可以调用解压工具。

        Windows 上不需要：钉钉的 install.js 在 win32 分支改用 PowerShell 的
        Expand-Archive 解压 zip，Windows 也没有默认的 unzip 命令。
        """
        if self.platform.is_windows or self.command_exists("unzip"):
            return True

        self.logger.info("unzip not found; installing it before DingTalk CLI...")
        if self.os_name == "Darwin":
            installed = self.command_exists("brew") and self.brew_install_or_upgrade(
                "unzip"
            )
        elif self.os_name == "Linux":
            installed = (
                self.run_linux_package("install", "unzip").returncode == 0
                and self.command_exists("unzip")
            )
        else:
            installed = False

        if not installed:
            self.logger.error("unzip is required to install the DingTalk CLI.")
        return installed

    def install_dingtalk_cli(self) -> bool:
        """按策略表安装钉钉 CLI，并在安装前准备其所需的 unzip 命令。"""
        if not self.ensure_unzip():
            return False
        return self.install_via_channels("dingtalk_cli")

    def graphify_targets(self) -> list[str]:
        """graphify 要注册到的宿主名单；卸载侧回收的是同一份。"""
        present = [name for name in GRAPHIFY_HOST_TARGETS if self.command_exists(name)]
        return (present or ["claude"]) + [
            name for name in GRAPHIFY_ALWAYS_TARGETS if name not in present
        ]

    def install_graphify(self) -> bool:
        """安装 graphify（内置运行时建出的根内 venv），再为已装智能体注册。

        注册目标固定包含 `agents`，并把产物镜像进每个 Qoder 配置根，否则 graphify
        在 Qoder 两端都不可见（见 `mirror_graphify_skill`）。

        graphify 的注册命令按「当前项目」落盘，因此必须显式指定工作目录：opencode
        平台无条件往 cwd 写 `.opencode/plugins/graphify.js`（没有只装用户级的开关），
        `hook install` 更是要求 cwd 在 git 仓库内（`hooks.py` 从 cwd 向上找 `.git`）。
        而 cwd 取决于用户怎么启动安装器 —— 双击快捷方式时可能是 `C:\\Windows`，
        那里既不可写也没有仓库，注册会直接抛 PermissionError。
        """
        if not self.install_via_channels("graphify"):
            return False
        platforms = self.graphify_targets()
        repo_root = self.git_repo_root()
        workdir = repo_root or self.graphify_project_dir()
        results = [
            self.run_command(["graphify", "install", "--platform", platform], cwd=workdir)
            for platform in platforms
        ]
        if repo_root is None:
            # 提交钩子是「按仓库」安装的附加步骤：安装器是全局工具链，无从得知用户
            # 想在哪个仓库里用图谱，因此只提示、不判组件失败。
            self.logger.warn(
                "当前目录不在 git 仓库内，跳过 graphify hook install；"
                "该步骤按仓库安装，请在需要图谱提交钩子的仓库根目录执行："
                "graphify hook install"
            )
        else:
            results.append(self.run_command(["graphify", "hook", "install"], cwd=workdir))
        if not all(result.returncode == 0 for result in results):
            return False
        return self.mirror_graphify_skill()

    def mirror_graphify_skill(self) -> bool:
        """把 graphify 的 Agent-Skills 产物镜像进每个 Qoder 配置根。

        两个 Qoder 运行时的技能扫描面并不一致：桌面 IDE 读 ``~/.agents/skills``，
        而 CLI 的 ``skills list`` 只列 ``<root>/skills`` 与插件自带技能。双写才能
        让同一个 graphify 在两端都可见。
        """
        roots = qoder.existing_roots(qoder.PLATFORMS.values(), self.home)
        if not roots:
            return True
        source = self.home / ".agents" / "skills" / "graphify"
        if not source.is_dir():
            self.logger.error(
                f"未找到 graphify 的 Agent-Skills 产物（{source}）；"
                "请确认 `graphify install --platform agents` 是否成功。"
            )
            return False
        ok = True
        for root in roots:
            try:
                target = qoder.mirror_skill(root, source)
            except (OSError, ValueError) as exc:
                self.logger.error(f"graphify 技能镜像到 {root} 失败：{exc}")
                ok = False
                continue
            self.track(target)
        return ok

    def git_repo_root(self) -> Optional[Path]:
        """启动目录所在的 git 仓库根目录；不在任何仓库内时返回 None。"""
        result = self.run_capture(["git", "rev-parse", "--show-toplevel"])
        root = (result.stdout or "").strip() if result.returncode == 0 else ""
        return Path(root) if root else None

    def graphify_project_dir(self) -> Path:
        """仓库外启动 graphify 注册命令时的落点。

        opencode 平台的项目级产物无法关闭，只能改落点：放进安装器自己的状态目录，
        既不污染用户目录，也不会因为启动目录不可写而失败。
        """
        directory = self.state_dir / "graphify-project"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def install_feishu_cli(self) -> bool:
        """按策略表安装飞书 CLI，并向已装智能体同步 skills。

        skills 由第三方提供（`npx skills add` 需要 clone GitHub 仓库），只是 CLI 之外
        的附加项：同步失败不影响 CLI 本身可用，所以只告警、不判组件失败。技能全局库
        （`-g` 的落盘位置）已有这些 skills 时直接跳过，升级路径不再依赖 GitHub 可达。
        """
        if not self.install_via_channels("feishu_cli"):
            return False

        # `skills add -g` 会自动探测当前目录中的所有 Agent。若探测到
        # PromptScript，skills CLI 会因其不支持全局安装而让整批技能失败。
        # 只传递本安装器管理的目标，避免不兼容的 Agent 参与安装。
        agents = [
            agent
            for command, agent in (
                ("claude", "claude-code"),
                ("codex", "codex"),
                ("opencode", "opencode"),
            )
            if self.command_exists(command)
        ]
        if not agents:
            self.logger.warn("No supported coding agent found; skipped Feishu skills.")
            return True
        if self.feishu_skills_installed():
            self.logger.info("飞书 skills 已安装，跳过同步。")
            # 技能库已有时不再写，但上一次由 CodeKit 同步过的事实必须留档：
            # 抹掉它会让卸载侧不敢回收，也不该凭它去删别人装的副本。
            self.feishu_skills_synced = bool(
                config.read_key_value(self.version_file, "feishu_skills")
            )
            return True
        result = self.run_command(
            [
                "npx",
                "skills",
                "add",
                "larksuite/cli",
                "-y",
                "-g",
                "--agent",
                *agents,
            ]
        )
        if result.returncode != 0:
            self.logger.warn(
                "飞书 skills 未同步（飞书 CLI 本身已就绪）；可稍后手动重试："
                "npx skills add larksuite/cli -y -g"
            )
            return True
        # 只有这一步真的写过，卸载时才有权删掉 ~/.agents/skills/lark-*。
        self.feishu_skills_synced = True
        return True

    def feishu_skills_installed(self) -> bool:
        """判断 skills CLI 的全局技能库中是否已装飞书 skills。

        `npx skills add larksuite/cli -g` 把技能写进 `~/.agents/skills`（配一个
        `~/.agents/.skill-lock.json` 记录来源），与具体智能体无关，因此这一处就是
        「已装」的权威依据。
        """
        return any((self.home / ".agents" / "skills").glob("lark-*"))

    def platform_agents_plan(self, action: str, tool: str) -> Optional[tuple[list[str], Path]]:
        """构造平台全局 subagent 的同步/清理命令与目标目录；无可执行动作时返回 None。"""
        helper = SCRIPT_DIR / "manage-global-agents.py"
        if not helper.is_file():
            return None
        args = [sys.executable, str(helper), action, "--home", str(self.home), "--tool", tool]
        if action == "install":
            source = self.marketplace_local / "skills" if self.marketplace_local else None
            if source is None or not source.is_dir():
                return None
            args.extend(["--source", str(source)])
        flags, agents_dir = agents.global_agent_plan(tool, self.home)
        args.extend(flags)
        return args, agents_dir

    def run_platform_agents(self, action: str, tool: str) -> bool:
        """同步或清理某平台的全局 subagent；安装与卸载共用同一实现与路径解析。"""
        try:
            plan = self.platform_agents_plan(action, tool)
        except ValueError as exc:
            self.logger.error(str(exc))
            return False
        if plan is None:
            return True
        args, agents_dir = plan
        result = self.run_command(args)
        if result.returncode == 0:
            self.track(agents_dir)
        return result.returncode == 0

    def install_platform_agents(self, tool: str) -> bool:
        """随对应插件同步该平台的全局 subagent。"""
        return self.run_platform_agents("install", tool)

    def ensure_agent_cli(self, platform_key: str) -> bool:
        """安装插件前确保平台 CLI 可用；缺失时自动安装 CLI 本体。"""
        entry = agents.agent_cli(platform_key)
        if entry is None:
            return False
        source, _version = self.agent_cli_status(platform_key)
        if source:
            self.logger.info(f"{entry.display_name} CLI already available: {source}")
            return True
        if not agents.supports_install(entry, self.os_name):
            self.logger.error(
                f"未检测到 {entry.display_name} CLI，且当前平台没有可用的自动安装渠道。"
            )
            return False

        self.logger.section(f"安装智能体 CLI：{entry.display_name}")
        self.logger.info(
            f"未检测到 {entry.display_name} CLI，先自动安装 CLI，随后再安装对应插件。"
        )
        # 官方脚本把 CLI 放进配置根下的 entry/bin 目录，只有先补进 PATH，
        # 后续的可用性校验和插件安装才能在同一进程里直接调用它。
        for directory in reversed(agents.cli_extra_paths(entry, self.home)):
            self.prepend_path(directory)
        installed = False
        for kind, target in agents.channel_plan(entry):
            if not self.channel_available(kind):
                continue
            if kind == "npm":
                self.set_npm()
                self.run_command(["npm", "install", "-g", f"{target}@latest"])
            elif kind == "brew":
                self.brew_install_or_upgrade(target, entry.commands[0])
            elif kind == "brew_cask":
                self.install_brew_cask(target, entry.commands[0])
            elif kind == "winget":
                self.install_winget_package(target, entry.commands[0])
            elif kind == "script":
                self.run_remote_script(target)
            elif kind == "powershell":
                self.run_remote_script(target, shell="powershell")
            if detector.agent_cli_presence(platform_key, self.home):
                installed = True
                break
        if not installed:
            note = agents.fallback_note(entry, windows=self.platform.is_windows)
            self.logger.error(
                f"{entry.display_name} CLI 安装失败；请手动执行：{note}，然后重新运行安装。"
            )
            return False
        source, version = self.refresh_agent_cli_status(platform_key)
        self.logger.success(
            f"{entry.display_name} CLI 已就绪：{version or 'unknown'} ({source})"
        )
        return True

    def announce_plugin_install(self, component_id: str) -> None:
        """播报插件的真实安装版本与落盘位置，与 Qoder 的收尾提示保持一致。"""
        version = detector.installed_plugin_version(component_id, self.context)
        path = detector.component_install_path(component_id, self.context)
        self.logger.success(
            f"{detector.component_description(component_id)} v{version} 已安装（{path}）"
        )

    # 每个插件组件的构建产物属性名：为空说明 prepare_sources 没产出对应源，注册表不能
    # 继续写，否则会留下指向不存在源的注册项。
    PLUGIN_SOURCE_ATTR = {
        "claude": "market_dir",
        "codex": "market_dir",
        "opencode": "opencode_source_dir",
        "qoder": "qoder_source_dir",
        "qodercn": "qoder_source_dir",
    }

    def install_plugin_component(self, component_id: str) -> bool:
        """插件组件的统一安装骨架：CLI 前置 → 全局 subagent → 平台注册 → 播报。

        四个平台的差别只在 `_register_<platform>_plugin` 这一段，其余顺序与前置检查
        完全一致。抄四遍的结果是任何一处新增前置都要记得改四个地方。
        """
        entry = agents.component_agent(component_id)
        if entry is None:
            return False
        source_attr = self.PLUGIN_SOURCE_ATTR.get(entry.key)
        if source_attr is None or not getattr(self, source_attr, None):
            return False
        registrar = self._plugin_registrar(entry.key)
        if registrar is None:
            return False
        # 勾选插件但缺少 CLI 时先自动安装 CLI，再安装插件。
        if not self.ensure_agent_cli(entry.key):
            return False
        # 先同步全局 subagent：失败即中止且不写注册表，避免“插件已写入并启用、组件却
        # 上报失败”的半安装状态。
        if not self.install_platform_agents(entry.key):
            return False
        if not registrar():
            return False
        self.announce_plugin_install(component_id)
        return True

    def _plugin_registrar(self, tool: str) -> Optional[Callable[[], bool]]:
        """按平台键取注册实现；Qoder 两个发行版共用同一份（共享配置根）。"""
        if tool in qoder.PLATFORMS:
            return lambda: self._register_qoder_plugin(tool)
        return {
            "claude": self._register_claude_plugin,
            "codex": self._register_codex_plugin,
            "opencode": self._register_opencode_plugin,
        }.get(tool)

    def _register_claude_plugin(self) -> bool:
        """注册本地 Claude marketplace，并按已安装状态执行插件安装或更新。"""
        if self.run_command(["claude", "plugin", "marketplace", "add", str(self.market_dir)]).returncode != 0:
            return False
        self.track(self.home / ".claude" / "plugins" / "known_marketplaces.json")
        self.run_command(["claude", "plugin", "marketplace", "update", config.MARKETPLACE_NAME])
        listing = self.run_capture(["claude", "plugin", "list"]).stdout
        package = f"{config.PLUGIN_NAME}@{config.MARKETPLACE_NAME}"
        result = self.run_command(["claude", "plugin", "update", package]) if package in listing else self.run_command(["claude", "plugin", "install", package, "--scope", "user"])
        if result.returncode != 0:
            return False
        self.track(self.home / ".claude" / "plugins" / "installed_plugins.json")
        self.record_marketplace_sha()
        return True

    def _register_opencode_plugin(self) -> bool:
        """把本地 OpenCode 插件源写入全局配置；已存在时尝试强制刷新插件。"""
        # 写 opencode 自己解析到的那份配置：它按 opencode.json → opencode.jsonc
        # 的顺序取第一个存在的文件，写错文件会让同一个插件出现在两份配置里。
        path = config.opencode_config_file(self.home / ".config" / "opencode")
        # 必须用 file URI 而不是直接拼路径：Windows 盘符路径含冒号与反斜杠，
        # `file://C:\...` 会被解析成主机名为盘符的 UNC 路径。
        plugin = f"codekit@{self.opencode_source_dir.as_uri()}"
        try:
            if config.opencode_has_plugin(path):
                result = self.run_command(["opencode", "plugin", plugin, "-g", "-f"]) if self.run_capture(["opencode", "plugin", "--help"]).returncode == 0 else RunResult(0)
            else:
                config.add_opencode_plugin(path, plugin)
                result = RunResult(0)
        except ValueError:
            return False
        if result.returncode != 0:
            return False
        self.track(path)
        return True

    def _register_codex_plugin(self) -> bool:
        """注册本地 Codex marketplace，清理旧技能残留后添加 CodeKit 插件。"""
        # 旧版本可能把兼容技能放进 ~/.agents；安装前清掉旧生成物，避免重复加载。
        for path in (self.home / ".agents" / "skills").glob("keel-*"):
            if path.is_dir():
                platform_layer.rmtree(path)
        added = self.run_command(["codex", "plugin", "marketplace", "add", str(self.market_dir)]).returncode == 0
        if not added and config.MARKETPLACE_NAME in self.run_capture(["codex", "plugin", "marketplace", "list"]).stdout:
            added = True
        if not added:
            return False
        self.track(self.home / ".codex" / "config.toml")
        result = self.run_command(["codex", "plugin", "add", f"{config.PLUGIN_NAME}@{config.MARKETPLACE_NAME}"])
        if result.returncode != 0:
            return False
        self.record_marketplace_sha()
        return True

    def _register_qoder_plugin(self, platform_key: str) -> bool:
        """把插件写进 Qoder 的共享配置根，并确认注册表里确实已安装且启用。"""
        descriptor = qoder.PLATFORMS[platform_key]
        try:
            root = qoder.resolve_config_root(descriptor, self.home)
        except ValueError as exc:
            raise InstallerError(str(exc)) from exc
        if qoder.foreign_os_roots(descriptor) and not (root / "plugins").is_dir():
            self.logger.warn(
                "Qoder keeps a separate configuration root per OS; this install "
                f"only reaches {root}, so a Qoder running on Windows stays unaware."
            )
        command = qoder.capable_cli(descriptor)
        try:
            if command:
                qoder.install_via_cli(command, root, self.qoder_source_dir, runner=self.qoder_run)
            else:
                # Desktop-only machines get the same registry the CLI would write.
                qoder.install_direct(root, self.qoder_source_dir, self.plugin_version)
            self.track(qoder.registry_path(root))
            self.track(qoder.settings_path(root))
            state = qoder.inspect(descriptor, self.home)
        except ValueError as exc:
            raise InstallerError(str(exc)) from exc
        if not state.installed or not state.enabled:
            raise InstallerError(
                f"{qoder.PLUGIN_ID} is not enabled in {qoder.registry_path(state.root)} "
                "after install; the shared Qoder registry was not updated"
            )
        return True

    def codex_probe_runner(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        """在空目录里跑一次探测。

        prompt dump 会带上**当前目录**的仓库上下文，而 CodeKit 自己的技能正文里就有
        ``spawn_agent`` 这个词；安装器常常就跑在这类仓库里，不隔离就可能把文档读成工具面。
        """
        with codex.neutral_cwd() as directory:
            result = self.run_capture(argv, timeout=timeout, cwd=Path(directory))
        return result.returncode, result.stdout

    def codex_collab_verdict(self) -> tuple[str, str]:
        """零 token 探测：模型可见 prompt 里到底有没有派发工具。

        ``codex features list`` 不算证据——它可以在工具面为空时仍报 stable true。判定规则
        （成组出现才算数）与技能侧共用同一份实现，见 probe-collab.classify。
        """
        command = codex.probe_command()
        if not command:
            return codex.UNKNOWN, ""
        returncode, output = self.codex_probe_runner(command, codex.PROBE_TIMEOUT)
        return codex.classify_probe(returncode, output), output

    # 多代理能力的结论分级：无需用户动作的结论不占版面（`enabled` 只给一行 10 字提示；
    # `already-on` 与「宿主没有开关」完全静默）。失败类结论各打一条 warn，文案来自共享的
    # 开启编排本身（它知道是哪一步、哪个路径失败的），写入现场不再自己播报一遍。
    MULTI_AGENT_NOTICE = "已开启多代理，需重启"
    MULTI_AGENT_UNCONFIRMED = {
        "unknown": "探测未正常返回，未改动配置",
        "no-cli": "未找到 codex 命令，未做探测",
        "noop": "开关已是 true 却看不到派发工具，未改动配置",
        "rolled-back": "写入后复验仍看不到派发工具，已回滚",
        "failed": "配置未能落盘",
        "refused": "该宿主没有已实测的开关，未改动配置",
    }
    # 满足即无需用户动作的结论；其余都要在「安装后操作」里留一行。
    MULTI_AGENT_RESOLVED = {"enabled", "already-on", "no-switch"}

    def enable_codex_multi_agent(self) -> tuple[str, str]:
        """探测 Codex 子代理工具面，缺失就直接开启并复验；返回（结论, 说明）。

        读→改→写→复验→逐字节回滚这套编排只有技能侧一份实现（``probe-collab.enable``），
        安装器把自己的 runner 交给它；结论沿用那套 action 名，不再另立一套语义把 noop
        折叠成 rolled-back。
        """
        if not self.command_exists("codex"):
            return "no-cli", ""
        verdict, _ = self.codex_collab_verdict()
        if verdict == codex.AVAILABLE:
            return "already-on", ""
        if verdict == codex.UNKNOWN:
            return "unknown", ""
        outcome = codex.enable("codex", self.home, self.codex_probe_runner)
        action = str(outcome.get("action", "failed"))
        if action == codex.ENABLED:
            self.track(codex.config_path(self.home))
            return "enabled", ""
        return action, str(outcome.get("reason") or "")

    def configure_agent_capabilities(self) -> None:
        """勾选了哪个平台的 CodeKit 插件，就为哪个平台确认 subagent 派发能力。

        勾选即授权：Codex 缺开关时直接写入并复验，失败逐字节回滚。写入只对
        **下一次**启动生效，所以这里只声称"已改配置"，不声称当前会话已可用。
        """
        self.agent_capabilities = []
        for component_id in self.selected:
            entry = agents.component_agent(component_id)
            if entry is None:
                continue
            # 只有 Codex 有可开启的多代理 feature flag；其余平台的派发工具由宿主内置，
            # 真正的前置是全局角色文件已装到该平台目录，没有键可写。
            outcome, detail = (
                self.enable_codex_multi_agent() if entry.key == "codex" else ("no-switch", "")
            )
            self.agent_capabilities.append(
                {"platform": entry.key, "outcome": outcome, "detail": detail}
            )
        self.report_agent_capabilities()

    def report_agent_capabilities(self) -> None:
        """按结论分级报告：无需动作时最多一行 10 字，需要动作时才一条 warn。

        原实现无论结果都打印一个 7 行汇总框加三行重启说明，每次安装/升级都会刷一遍，
        把「确实需要重启」这条真信息埋掉了。重启本身不需要在安装器里预告：技能侧在
        派发失败时会自己判定 collab-unavailable 并说明。
        """
        if not self.agent_capabilities:
            return
        if any(entry["outcome"] == "enabled" for entry in self.agent_capabilities):
            self.logger.info(self.MULTI_AGENT_NOTICE)
        for entry in self.agent_capabilities:
            if entry["outcome"] in self.MULTI_AGENT_RESOLVED:
                continue
            label = self._agent_label(entry["platform"])
            detail = entry.get("detail") or self.MULTI_AGENT_UNCONFIRMED.get(
                entry["outcome"], "结论未知"
            )
            self.logger.warn(f"{label} 多代理能力未确认：{detail}")

    @staticmethod
    def _agent_label(platform_key: str) -> str:
        """平台键的展示名；平台键本身来自 installer_agents，这里不另存一份对照表。"""
        entry = agents.agent_cli(platform_key)
        return entry.display_name if entry is not None else platform_key

    def qoder_run(self, args: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
        """执行 Qoder CLI 命令；到期即终止子进程，避免安装/卸载无限阻塞。"""
        result = self.run_command(args, timeout=timeout)
        return result.returncode, result.stdout

    def install_component(self, component_id: str) -> bool:
        """按组件注册 ID 分发安装处理器，并隔离单个组件的失败。"""
        handlers = {
            "rtk": self.install_rtk,
            "claude_plugin": lambda: self.install_plugin_component("claude_plugin"),
            "opencode_plugin": lambda: self.install_plugin_component("opencode_plugin"),
            "codex_plugin": lambda: self.install_plugin_component("codex_plugin"),
            "qoder_plugin": lambda: self.install_plugin_component("qoder_plugin"),
            "qodercn_plugin": lambda: self.install_plugin_component("qodercn_plugin"),
            "gitnexus": self.install_gitnexus,
            "dingtalk_cli": self.install_dingtalk_cli,
            "feishu_cli": self.install_feishu_cli,
            "graphify": self.install_graphify,
            "gh": lambda: self.install_via_channels("gh"),
            "glab": lambda: self.install_via_channels("glab"),
        }
        handler = handlers.get(component_id)
        if not handler:
            return False
        try:
            return bool(handler())
        except (OSError, ValueError, InstallerError) as exc:
            self.logger.error(f"{component_id} failed: {exc}")
            return False

    def run_installs(self) -> None:
        """按用户选择逐项执行安装/升级/重装，并补齐需要迁移的已装组件。"""
        for component_id in self.selected:
            status = self.states[component_id].status
            action = "安装" if status == "not_installed" else "升级" if status == "installed" else "重装"
            label = detector.component_description(component_id)
            self.logger.section(f"{action}：{label}")
            # 单个组件失败不会中断其他组件，最终状态由收尾阶段统一呈现。
            ok = self.install_component(component_id)
            if ok:
                self.logger.success(f"{label}已完成")
            else:
                self.logger.error(f"{label}未完成，请查看日志：{self.logger.log_path}")
            self.install_records.append((component_id, action, "1" if ok else "0"))
        for component_id in getattr(self, "toolchain_migration", []):
            label = detector.component_description(component_id)
            self.logger.section(f"迁移到工具链根：{label}")
            ok = self.install_component(component_id)
            if ok:
                self.logger.success(
                    f"{label} 已重新物化到 {self.platform.toolchain_root(self.home)}"
                )
            else:
                self.logger.error(f"{label} 迁移未完成，请查看日志：{self.logger.log_path}")
            self.install_records.append((component_id, "迁移", "1" if ok else "0"))

    def configure_git(self) -> None:
        """写入 CodeKit 需要的全局 Git 选项，并记录 Git 配置文件路径。"""
        self.run_command(["git", "config", "--global", "advice.addIgnoredFile", "false"])
        self.run_command(["git", "config", "--global", "push.autoSetupRemote", "true"])
        self.track(self.home / ".gitconfig")

    def save_versions(self) -> None:
        """收集注册表全部组件的版本、插件源提交号和插件版本，原子写入状态文件。"""
        values = [
            (
                component.component_id,
                detector.component_version(component.component_id, self.context) or "N/A",
            )
            for component in self.components
        ]
        sha = config.read_key_value(self.version_file, "marketplace_sha")
        config.write_versions(self.version_file, values, sha)
        if self.feishu_skills_synced:
            config.set_key_value(self.version_file, "feishu_skills", "synced")
        self.track(self.version_file)

    def setup_shell_path(self) -> None:
        """向所有 shell 配置追加一次性 PATH 配置，已存在时保持文件不变。

        写的不只是当前 ``$SHELL`` 的交互配置：agent 派生的 login / 非交互 bash
        只读 ``~/.profile``，漏掉它会让用户态 CLI 在 agent 会话里不可见。

        Windows 没有可写的 shell 配置（PowerShell profile 路径本地化且可能被
        OneDrive 重定向），改为校验 PATH 并在缺失时打印手动指引。
        """
        configs = self.shell_path_configs()
        if not configs:
            if not self.tool_dir_on_path():
                self.logger.warn(self.platform.path_hint(self.home, self.npm_bin_dir))
            return
        marker = "# Added by CodeKit Plugin"
        for path, line in configs:
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            if line in old:
                continue
            config.atomic_write(path, old.rstrip("\n") + f"\n\n{marker}\n{line}\n")
            self.track(path)

    def print_next_steps(self) -> None:
        """用醒目的表格打印后续操作及其作用。"""
        rows = [("重启智能体应用", "让本次安装的插件、技能和配置立即生效")]
        configs = self.shell_path_configs()
        if not configs:
            rows.append(("新开一个终端", "让本次安装的用户级命令立即生效"))
        else:
            names = []
            for path, _ in configs:
                try:
                    names.append(f"~/{path.relative_to(self.home)}")
                except ValueError:
                    names.append(str(path))
            rows.append(
                (f"source {' '.join(names)}", "让本次安装的用户级命令立即在当前终端生效")
            )
        if self.toolchain_state("gitnexus") == "ok":
            rows.append(("仓库根目录执行：gitnexus analyze --embeddings --skills --verbose", "对当前仓库建立gitnexus索引"))
        # 工具链落点的实测结论：非 ok 的组件给出一步就能照做的修复。
        repair = self.repair_command()
        for component_id, state in getattr(self, "toolchain_health", []):
            if state == "ok":
                continue
            rows.append(
                (
                    f"{repair}（重新安装 {component_id}）",
                    f"{component_id} 的落点实测为 {state}；"
                    "codekit:initialize 会如实降级为 partial 并在报告里点明这一条",
                )
            )
        if self.command_exists("claude"):
            rows.append(("Claude 中输入：/codekit:initialize", "初始化当前仓库"))
        if self.command_exists("opencode"):
            rows.append(("OpenCode 中输入：/initialize", "初始化当前仓库"))
        if self.command_exists("codex"):
            rows.append(("Codex 中输入：$initialize", "初始化当前仓库"))
        if any(
            entry["outcome"] not in self.MULTI_AGENT_RESOLVED
            for entry in self.agent_capabilities
        ):
            rows.append(
                (
                    "手工确认所选平台的子代理派发能力",
                    "安装器未能确认或已回滚该配置，CodeKit 技能会停在 collab-unavailable",
                )
            )
        qoder_labels = [
            descriptor.display_name.removeprefix("CodeKit Plugin (").removesuffix(")")
            for descriptor in (
                qoder.PLATFORMS[key]
                for key in ("qoder", "qodercn")
                if detector.agent_presence(key, self.home, self.os_name)
            )
        ]
        for label in qoder_labels:
            rows.append(
                (
                    f"{label} 中输入：/codekit:initialize",
                    "初始化当前仓库",
                )
            )

        if self.command_exists("gh"):
            rows.append(("gh auth login", "推荐浏览器登录；Git 操作选择 HTTPS，并确认使用 GitHub 凭据"))
        if self.command_exists("glab"):
            rows.append(("glab auth login", "GitLab.com 直接回车；自托管 GitLab 输入实例 URL"))
        if self.command_exists("dws"):
            rows.append(("dws auth login", "登录钉钉并完成 CLI 认证"))
        if self.command_exists("lark-cli"):
            rows.append(("lark-cli config init", "创建或选择飞书应用并保存 App ID 和 App Secret"))
            rows.append(("lark-cli auth login --recommend", "登录飞书，启用飞书 CLI 的业务操作"))

        headers = ("安装后操作", "作用")
        self.logger.info(
            "\n\n\n  ⚠ 安装后操作（建议按顺序执行）\n"
            + self.render_table(headers, rows)
            + f"\n\n日志：{self.logger.log_path}"
        )

    def run(self) -> int:
        """执行完整安装生命周期：前置检查、安装、配置和结果收尾。"""
        self.logger.info(f"\n \n === CodeKit Plugin Install v{self.plugin_version} ===")
        if self.os_name not in {"Darwin", "Linux", "Windows"}:
            raise InstallerError(f"Unsupported OS: {self.os_name}. Only macOS/Linux/Windows.")
        # 系统依赖检查在所有组件动作之前，且三平台共用同一份实现（installer_preflight）。
        if not preflight.check(self):
            return 1
        detector.registry.validate_registry()
        self.ensure_user_bin()
        if not self.marketplace_local:
            self.logger.warn("Marketplace repo not detected; plugin components may be skipped.")
        self.prepare_sources()
        if not self.preflight_agents():
            return 0
        self.load_states()
        self.select_components()
        # 工具链根必须在 run_installs 之前就绪：venv 通道要用内置运行时建 venv，装配
        # 阶段也会依据它判断通道可用性。空选择也要先过这一步——需要迁移的组件与本次勾选
        # 无关，「只勾插件」和「什么都没勾」正是落点留在根外的那批人。
        self.setup_toolchain()
        migration = list(getattr(self, "toolchain_migration", []))
        if not self.selected and not migration:
            self.logger.info("No components selected. Nothing to do.")
            try:
                detector.render_recap("install", (), self.context)
            except (OSError, ValueError, RuntimeError) as exc:
                self.logger.warn(f"Could not render installation recap: {exc}")
            return 0
        self.run_installs()
        self.write_toolchain_manifest()
        self.report_toolchain_health()
        self.configure_git()
        self.save_versions()
        if getattr(self, "setup_path_selected", False):
            self.setup_shell_path()
        self.configure_agent_capabilities()
        self.print_next_steps()
        try:
            detector.render_recap("install", ("\t".join(row) for row in self.install_records), self.context)
        except (OSError, ValueError, RuntimeError) as exc:
            self.logger.warn(f"Could not render installation recap: {exc}")
        failed = [row for row in self.install_records if row[2] == "0"]
        if failed:
            self.logger.error(
                f"{len(failed)} 个组件未完成："
                + "、".join(row[0] for row in failed)
                + f"；详见日志 {self.logger.log_path}"
            )
            return 1
        return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """解析安装器命令行参数。"""
    parser = argparse.ArgumentParser(description="CodeKit Plugin installer")
    parser.add_argument(
        "action",
        nargs="?",
        default="install",
        choices=("install",),
        help="安装或升级所选组件（升级与重装是同一件事）",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="proceed with default selections")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """作为进程入口运行安装器，并将取消或预期异常转换为退出码。"""
    args = parse_args(argv)
    try:
        config.require_python()
        return Installer(args.yes).run()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (InstallerError, OSError, ValueError, RuntimeError) as exc:
        print(f"  ✗  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
