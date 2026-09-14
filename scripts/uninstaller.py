#!/usr/bin/env python3
"""CodeKit uninstaller orchestrator.

The uninstaller owns component removal, cleanup of CodeKit-managed global
configuration, and uninstall result handling. Shared command execution,
state detection, and platform helpers are inherited from :mod:`installer`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402

# 导入期门禁：与 installer.py 共用同一份 helper 清单，缺失时给出单行受控错误。
try:
    config.require_helper_files(SCRIPT_DIR)
except ValueError as exc:
    print(f"  ✗  {exc}", file=sys.stderr)
    raise SystemExit(1)

import installer_agents as agents  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_qoder as qoder  # noqa: E402
import installer_state as detector  # noqa: E402
from installer import (  # noqa: E402
    CLAUDE_MARKETPLACE,
    CLAUDE_PLUGIN,
    CODEX_MARKETPLACE,
    CODEX_PLUGIN,
    Installer,
    InstallerError,
    RunResult,
)

# npm 在 Windows 上先给包目录改名成临时名再删除；包内原生模块（.node）被运行中的
# 进程内存映射时改名必定失败，npm 以 EBUSY 退出（uv errno -4082，终端显示为
# 4294963214）。这类失败只能让占用进程退出，换参数或原地重试都无解。
NPM_LOCK_MARKERS = ("EBUSY", "resource busy or locked")


class Uninstaller(Installer):
    """执行 CodeKit 组件选择、卸载和全局配置清理。"""

    def __init__(self, auto_yes: bool, *, purge: bool = False) -> None:
        super().__init__(auto_yes)
        self.uninstall_records: list[tuple[str, str, str]] = []
        self.purge = purge
        self.toolchain_purged = False

    def component_tag(self, component_id: str) -> str:
        """生成卸载组件选择器中的状态提示。"""
        item = self.states[component_id]
        if item.status == "not_installed":
            if item.leftover:
                return " (组件未安装，但发现卸载残留，勾选后清理)"
            return " (组件未安装)"
        return f" (组件已安装 {item.installed_version or 'unknown'}，勾选后卸载)"

    def select_components(self) -> list[str]:
        """按当前安装状态生成默认选择，并在交互模式下回填用户选择。"""
        default = [
            item.component_id
            for item in self.components
            if self.states[item.component_id].status != "not_installed"
            or self.states[item.component_id].leftover
        ]
        if not self.auto_yes and self.platform.can_interact():
            answer = input("确认要卸载 CodeKit Plugin 及相关组件？[y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                self.logger.info("Cancelled.")
                raise SystemExit(0)

        # 非交互执行必须可用于 CI：采用默认选择，并跳过终端选择器。
        if self.auto_yes or not self.platform.can_interact():
            self.selected = default
            if not self.auto_yes:
                self.announce_default_selection(default)
            return default

        # 选择器只消费临时 TSV；finally 保证异常、取消和正常返回都会清理它。
        manifest = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="codekit-selector-",
            suffix=".tsv",
            delete=False,
        )
        manifest_path = Path(manifest.name)
        try:
            for index, component in enumerate(self.components, 1):
                fields = (
                    str(index),
                    component.description,
                    component.group,
                    detector.registry.GROUPS[component.group],
                    self.component_tag(component.component_id),
                    "1" if component.component_id in default else "0",
                )
                manifest.write("\t".join(value.replace("\t", " ") for value in fields) + "\n")
            manifest.close()
            configured = self.shell_config()
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "component-selector.py"),
                    "--mode",
                    "uninstall",
                    "--manifest",
                    str(manifest_path),
                    "--path-selected",
                    "0",
                    "--shell-config",
                    str(configured[0]) if configured else "",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                **platform_layer.text_kwargs(),
            )
        finally:
            manifest_path.unlink(missing_ok=True)
        if result.returncode == 2:
            # 选择器拿不到可用终端：显式降级为默认选择，不当作失败。
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
        selected_text, _ = payload.removeprefix("selected=").split(";path=", 1)
        indices = [int(value) for value in selected_text.split(",") if value]
        self.selected = [self.components[index - 1].component_id for index in indices]
        return self.selected

    def uninstall_platform_agents(self, tool: str) -> bool:
        """随对应插件移除该平台的全局 subagent。"""
        return self.run_platform_agents("uninstall", tool)

    def uninstall_plugin_component(self, component_id: str) -> bool:
        """插件组件的统一卸载骨架：平台登记清理 → 该平台全局 subagent。

        与安装侧同样只有「平台清理」这一段不同；先清登记再删缓存，登记清不掉时不碰
        文件（插件可能仍被宿主加载，删缓存等于毁掉一份在用的安装）。
        """
        entry = agents.component_agent(component_id)
        if entry is None:
            return False
        teardown = self._plugin_teardown(entry.key)
        if teardown is None:
            return False
        return bool(teardown()) and self.uninstall_platform_agents(entry.key)

    def _plugin_teardown(self, tool: str) -> Optional[Callable[[], bool]]:
        """按平台键取清理实现；Qoder 两个发行版共用同一份（共享配置根）。"""
        if tool in qoder.PLATFORMS:
            return lambda: self._uninstall_qoder_plugin(tool)
        return {
            "claude": self._uninstall_claude_plugin,
            "codex": self._uninstall_codex_plugin,
            "opencode": self._uninstall_opencode_plugin,
        }.get(tool)

    def _uninstall_claude_plugin(self) -> bool:
        """卸载 Claude 插件及其 marketplace 注册；CLI 缺失时也清理共享缓存。"""
        plugin_ok = True
        if self.command_exists("claude"):
            package = f"{CLAUDE_PLUGIN}@{CLAUDE_MARKETPLACE}"
            if package in self.run_capture(["claude", "plugin", "list"]).stdout:
                self.run_command(["claude", "plugin", "uninstall", package, "--scope", "user"])
            self.run_command(["claude", "plugin", "marketplace", "remove", CLAUDE_MARKETPLACE])
            remaining = self.run_capture(["claude", "plugin", "list"]).stdout
            marketplace = self.run_capture(["claude", "plugin", "marketplace", "list", "--json"]).stdout
            # CLI 只负责标记/登记的内容也要检查，避免界面显示卸载成功但仍可被加载。
            plugin_ok = package not in remaining and f'"{CLAUDE_MARKETPLACE}"' not in marketplace
        # The plugin cache is shared with the desktop app, so it is always
        # removed; a missing CLI must not leave a loadable plugin behind.
        platform_layer.rmtree(
            self.home / ".claude" / "plugins" / "cache" / CLAUDE_MARKETPLACE, ignore_errors=True
        )
        return plugin_ok

    def _uninstall_opencode_plugin(self) -> bool:
        """从 OpenCode 配置中删除 CodeKit 插件，并再次读取配置确认删除结果。"""
        path = config.opencode_config_file(self.home / ".config" / "opencode")
        try:
            if not config.opencode_has_plugin(path):
                return True
            config.remove_opencode_plugins(path)
            return not config.opencode_has_plugin(path)
        except ValueError:
            return False

    def _uninstall_codex_plugin(self) -> bool:
        """移除 Codex 中的 CodeKit 插件和 marketplace，并删除旧版兼容技能目录。"""
        if self.command_exists("codex"):
            self.run_command(["codex", "plugin", "remove", f"{CODEX_PLUGIN}@{CODEX_MARKETPLACE}"])
            self.run_command(["codex", "plugin", "marketplace", "remove", CODEX_MARKETPLACE])
        for path in (self.home / ".agents" / "skills").glob("keel-*"):
            if path.is_dir():
                platform_layer.rmtree(path, ignore_errors=True)
        return True

    def _uninstall_qoder_plugin(self, platform_key: str) -> bool:
        """移除 Qoder 共享配置根中的插件；CLI 可用时先走 CLI 登记。"""
        descriptor = qoder.PLATFORMS[platform_key]
        try:
            return qoder.uninstall(descriptor, self.home, runner=self.qoder_run)
        except ValueError as exc:
            self.logger.error(str(exc))
            return False

    def uninstall_rtk(self) -> bool:
        """撤销 rtk 的全局配置，并卸载残留。"""
        if self.command_exists("rtk"):
            self.run_command(["rtk", "init", "-g", "--uninstall"])
            self.run_command(["rtk", "init", "-g", "--codex", "--uninstall"])
        if self.command_exists("brew") and self.run_capture(["brew", "list", "rtk"]).returncode == 0:
            self.brew("uninstall", "rtk")
        # 发布包解出的是 rtk / rtk.exe；npm 风格入口在 Windows 上是 rtk.cmd。
        for name in dict.fromkeys(
            (self.platform.binary_filename("rtk"), self.platform.shim_name("rtk"))
        ):
            (self.bin_dir / name).unlink(missing_ok=True)
        return True

    def uninstall_winget_package(self, winget_id: str) -> bool:
        """经 winget 移除一个包；机器级 MSI 需要管理员时只对这一条命令提权。

        winget 的 uninstall 不会自己提权：机器级 MSI（gh 就是）会被 Windows Installer
        直接拒绝（事件 11730，Error 1730），winget 再把 1603 包成 ``0x8A150030`` 返回。
        这里按 §3 的 sudo 策略处理：用户态包（glab 这类）静默试一次就成功，不必打扰
        用户；只有确实失败、而且当前会话弹得出 UAC 时才提权重跑同一条命令。
        """
        if not self.winget_manages(winget_id):
            self.logger.info(f"{winget_id} 不在 winget 管理下，跳过 winget 卸载。")
            return True
        command = [
            "winget", "uninstall", "--id", winget_id, "--exact", "--silent",
            "--accept-source-agreements", "--disable-interactivity",
        ]
        if platform_layer.is_admin():
            return self.run_command(command, timeout=600).returncode == 0
        if self.run_command(command, timeout=600, quiet=True).returncode == 0:
            # 探针式调用不打印开始/结束行；成功这一条要自己补，日志才读得通。
            self.logger.success(f"完成：{self.logger.command_text(command)}")
            return True
        if not self.platform.can_interact():
            # 无终端可弹 UAC：给出可操作提示，而不是一个看不懂的退出码。
            self.logger.error(
                f"{winget_id} 是机器级安装，卸载需要管理员权限，当前会话无法提权；"
                "请在管理员终端重新运行卸载。"
            )
            return False
        self.logger.info(
            f"{winget_id} 是机器级安装，卸载需要管理员权限；"
            "请在 UAC 对话框中选择「是」完成卸载。"
        )
        code = platform_layer.run_elevated(command, timeout=600)
        if code == platform_layer.ELEVATION_CANCELLED:
            self.logger.error(f"提权被取消，{winget_id} 未卸载；请在管理员终端重新运行卸载。")
            return False
        if code != 0:
            self.logger.error(f"winget 卸载 {winget_id} 失败（退出码 {code}）。")
            return False
        self.logger.success(f"完成（已提权）：{self.logger.command_text(command)}")
        return True

    def uninstall_package_component(self, component_id: str) -> bool:
        """按平台卸载由包管理器接管的 gh/glab，并清理 CodeKit 用户 bin 残留。"""
        for name in dict.fromkeys(
            (self.platform.shim_name(component_id), self.platform.binary_filename(component_id))
        ):
            (self.bin_dir / name).unlink(missing_ok=True)
        if self.command_exists("brew") and self.run_capture(["brew", "list", component_id]).returncode == 0:
            return self.brew("uninstall", component_id).returncode == 0
        if self.os_name == "Linux" and self.linux_package_installed(component_id):
            return self.run_linux_package("remove", component_id).returncode == 0
        # Windows 上 gh/glab 由 winget 接管，必须走 winget uninstall，否则会留下
        # 一份仍在 PATH 上的安装。
        definition = self.component(component_id)
        winget_id = definition.channels.get("winget", "") if definition else ""
        if winget_id and self.channel_available("winget"):
            return self.uninstall_winget_package(winget_id)
        return True

    def unregister_gitnexus_from_qoder(self) -> None:
        """回收 CodeKit 为 Qoder 写入的 GitNexus MCP、hooks 与脚本副本。

        这些落点不在 ``gitnexus uninstall`` 的目标表里，只能自己收：漏掉的话宿主
        会继续去加载一个已经不存在的 MCP server 和 hook 脚本。
        """
        for root in qoder.existing_roots(qoder.PLATFORMS.values(), self.home):
            try:
                qoder.remove_mcp_server(root, qoder.GITNEXUS_MCP_NAME)
                qoder.remove_gitnexus_hooks(root)
            except ValueError as exc:
                self.logger.error(f"Qoder（{root}）清理 GitNexus 注册失败：{exc}")
                continue
            platform_layer.rmtree(qoder.hook_script_dir(root), ignore_errors=True)

    def uninstall_gitnexus(self) -> bool:
        """清理 gitnexus 自身配置，并删除私有前缀副本与人类 shell 的转发入口。

        安装期 gitnexus 落在 CodeKit 私有 npm 前缀里（受限宿主读不到 npm 全局目录），
        所以只在全局前缀里卸载是空操作；bin_dir 里的转发垫片也必须一并清掉——它留在
        PATH 上会让 ``command_exists`` 一直为真，把「已卸干净」误判成「没卸干净」。
        """
        if self.command_exists("gitnexus"):
            self.run_command(["gitnexus", "uninstall", "--force"])
        self.unregister_gitnexus_from_qoder()
        platform_layer.rmtree(
            self.platform.npm_prefix_private(self.home), ignore_errors=True
        )
        for name in dict.fromkeys(
            (self.platform.shim_name("gitnexus"), self.platform.binary_filename("gitnexus"))
        ):
            (self.bin_dir / name).unlink(missing_ok=True)
        return self.uninstall_npm_component("gitnexus")

    def uninstall_npm_component(self, component_id: str) -> bool:
        """从用户态和默认 npm 前缀卸载一个全局 npm CLI 组件。"""
        definition = detector.NPM_COMPONENTS.get(component_id)
        if not definition:
            return False
        package = definition["package"]
        command = definition["command"]
        self.set_npm()
        results = [self.run_command(["npm", "uninstall", "-g", package])]
        env = os.environ.copy()
        env.pop("npm_config_prefix", None)
        results.append(self.run_with_input(["npm", "uninstall", "-g", package], "", env))
        if not self.command_exists(command):
            return True
        return self.resolve_unremoved_npm_package(component_id, package, command, results)

    def resolve_unremoved_npm_package(
        self,
        component_id: str,
        package: str,
        command: str,
        results: Sequence[RunResult],
    ) -> bool:
        """npm 卸载后包还在 PATH 上时，定位原因并给出可操作的处置。

        Windows 上最常见的原因是包里带原生模块（如 gitnexus 的 ``lbugjs.node``）
        正被智能体宿主拉起的进程占用，此时 npm 只会报 EBUSY 和一个退出码；卸载器
        自己把占用进程找出来，交互模式下经用户确认后终止它们再重试，非交互模式
        只报告，绝不擅自结束用户进程。
        """
        holders = (
            self.npm_package_process_holders(package)
            if self.npm_package_locked(results)
            else []
        )
        if not holders:
            self.logger.error(
                f"{component_id} 卸载后仍在 PATH 上（{command}）；"
                f"请手动执行：npm uninstall -g {package}"
            )
            return False

        self.logger.warn(
            f"{package} 正被 {len(holders)} 个运行中的进程占用，npm 无法删除其包目录："
        )
        for pid, command_line in holders:
            self.logger.info(f"    PID {pid}  {command_line}")
        retry_hint = (
            f"请退出正在使用 {package} 的程序（宿主 IDE 里的 MCP / 智能体）后重新运行卸载，"
            f"或手动执行：npm uninstall -g {package}"
        )
        if self.auto_yes or not self.platform.can_interact():
            self.logger.error(retry_hint)
            return False

        answer = input(f"是否终止这些进程并重试卸载 {package}？[y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            self.logger.info(f"已跳过。{retry_hint}")
            return False

        self.terminate_processes(holders)
        self.set_npm()
        self.run_command(["npm", "uninstall", "-g", package])
        if not self.command_exists(command):
            return True
        self.logger.error(
            f"{package} 仍未能卸载：占用进程可能还在，也可能已被宿主重新拉起。{retry_hint}"
        )
        return False

    def npm_package_locked(self, results: Sequence[RunResult]) -> bool:
        """判断 npm 卸载失败是否由「包内文件被运行中进程占用」引起。"""
        if not self.platform.is_windows:
            return False
        return any(
            marker in (result.stdout or "")
            for result in results
            for marker in NPM_LOCK_MARKERS
        )

    def npm_package_process_holders(self, package: str) -> list[tuple[str, str]]:
        """列出命令行指向该全局包目录的进程，返回 (PID, 命令行) 列表。

        Windows 上拿不到「谁打开了这个文件」，改用命令行做启发式匹配：从包目录内部
        启动的进程正是加载了其原生模块、也是让 npm 改名失败的那一个。

        排除 powershell/pwsh：探测脚本自身的命令行里就带着这个片段，不排除就会把
        自己列成占用者，而那个 PID 事后复用时会误杀无关进程。
        """
        if not self.platform.is_windows:
            return []
        powershell = platform_layer.resolve_exe("powershell") or platform_layer.resolve_exe(
            "pwsh"
        )
        if not powershell:
            return []
        fragment = "*node_modules" + os.sep + package.replace("/", os.sep) + "*"
        pattern = platform_layer.powershell_literal(fragment)
        condition = (
            f"$_.CommandLine -like {pattern} "
            "-and $_.Name -notin @('powershell.exe', 'pwsh.exe')"
        )
        script = (
            "Get-CimInstance Win32_Process | "
            f"Where-Object {{ {condition} }} | "
            'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
        )
        result = self.run_capture(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script], timeout=30
        )
        if result.returncode != 0:
            return []
        holders: list[tuple[str, str]] = []
        for line in (result.stdout or "").splitlines():
            pid, _, command_line = line.partition("\t")
            if pid.strip().isdigit():
                holders.append((pid.strip(), command_line.strip()))
        return holders

    def terminate_processes(self, holders: Sequence[tuple[str, str]]) -> None:
        """终止占用进程，让 Windows 能改名并删除被内存映射的原生模块。

        不解读 PowerShell 的退出码：同一批 PID 里只要有一个已经自行退出，
        ``Stop-Process`` 就返回 1，而其余进程其实已经终止；能否卸载以随后的 npm
        重试为准。
        """
        pids = [pid for pid, _ in holders if pid.isdigit()]
        if not pids:
            return
        platform_layer.run_powershell_script(
            "Stop-Process -Id " + ",".join(pids) + " -Force -ErrorAction SilentlyContinue"
        )

    def uninstall_dingtalk_cli(self) -> bool:
        """卸载钉钉 npm CLI。"""
        return self.uninstall_npm_component("dingtalk_cli")

    def uninstall_feishu_cli(self) -> bool:
        """卸载飞书 npm CLI。"""
        return self.uninstall_npm_component("feishu_cli")

    def uninstall_graphify(self) -> bool:
        """卸载 pipx/pip 中的 graphifyy，并删除 CodeKit venv、用户级软链接与技能注册。"""
        if self.command_exists("pipx") and "graphifyy" in self.run_capture(["pipx", "list"]).stdout.lower():
            self.run_command(["pipx", "uninstall", "graphifyy"])
        if self.command_exists("pip3"):
            self.run_command(["pip3", "uninstall", "-y", "graphifyy"])
        platform_layer.rmtree(self.platform.venv_dir(self.home, "graphify"), ignore_errors=True)
        # 技能注册是安装期 CodeKit 自己写的（agents 目标 + Qoder 各根镜像），
        # 留着的话卸载后宿主仍然会把 /graphify 当成可用技能。
        platform_layer.rmtree(
            self.home / ".agents" / "skills" / "graphify", ignore_errors=True
        )
        for root in qoder.existing_roots(qoder.PLATFORMS.values(), self.home):
            qoder.remove_mirrored_skill(root, "graphify")
        for name in dict.fromkeys(
            (self.platform.shim_name("graphify"), self.platform.binary_filename("graphify"))
        ):
            (self.bin_dir / name).unlink(missing_ok=True)
        return True

    def clear_leftovers(self, component_id: str) -> bool:
        """物理删除组件仍留在磁盘上的插件缓存，返回是否已清空。

        插件 CLI 的卸载普遍是软删除：清掉注册表条目后在缓存目录里写一个
        ``.orphaned_at`` 标记，目录本身留着。handler 报成功只说明登记状态干净
        了，用户看到的文件还在。这里以文件为准再收一次尾，否则「成功」是假的，
        宿主还会把那些文件逐条列成待确认的残留。
        """
        for path in detector.component_leftovers(component_id, self.context):
            if path.is_dir() and not path.is_symlink():
                platform_layer.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        remaining = detector.component_leftovers(component_id, self.context)
        for path in remaining:
            self.logger.warn(f"{component_id} 的残留目录未能删除：{path}")
        return not remaining

    def uninstall_component(self, component_id: str) -> bool:
        """按组件注册 ID 分发卸载处理器，并把单项异常转换为失败结果。"""
        handlers = {
            "rtk": self.uninstall_rtk,
            "claude_plugin": lambda: self.uninstall_plugin_component("claude_plugin"),
            "opencode_plugin": lambda: self.uninstall_plugin_component("opencode_plugin"),
            "codex_plugin": lambda: self.uninstall_plugin_component("codex_plugin"),
            "qoder_plugin": lambda: self.uninstall_plugin_component("qoder_plugin"),
            "qodercn_plugin": lambda: self.uninstall_plugin_component("qodercn_plugin"),
            "gitnexus": self.uninstall_gitnexus,
            "dingtalk_cli": self.uninstall_dingtalk_cli,
            "feishu_cli": self.uninstall_feishu_cli,
            "graphify": self.uninstall_graphify,
            "gh": lambda: self.uninstall_package_component("gh"),
            "glab": lambda: self.uninstall_package_component("glab"),
        }
        handler = handlers.get(component_id)
        if not handler:
            return False
        try:
            ok = bool(handler())
        except (OSError, ValueError, InstallerError) as exc:
            self.logger.error(f"{component_id} failed: {exc}")
            return False
        # 只有在登记状态确实清理成功后才动文件：注册表或配置删不掉时，插件可能
        # 仍被宿主加载，此时删缓存等于毁掉一份正在使用的安装。
        return self.clear_leftovers(component_id) if ok else False

    def run_uninstalls(self) -> None:
        """按用户选择逐项卸载；未安装项视为幂等成功，最后清理全局配置。"""
        for component_id in self.selected:
            item = self.states[component_id]
            # 卸载是幂等操作：组件确实不存在时才跳过破坏性处理器。只剩残留时仍
            # 要走处理器，否则重跑卸载永远清不掉上一轮遗留的文件。
            prior = (
                "absent"
                if item.status == "not_installed" and not item.leftover
                else "installed"
            )
            self.logger.info(f"\n--- 卸载: {component_id} ---")
            ok = True if prior == "absent" else self.uninstall_component(component_id)
            self.uninstall_records.append((component_id, prior, "1" if ok else "0"))
        self.cleanup_global_config()

    def shell_rcs(self) -> Iterable[Path]:
        """返回卸载时需要检查的 shell 配置文件集合；Windows 上没有。"""
        return self.platform.shell_rc_files(self.home)

    def purge_toolchain(self) -> None:
        """``--purge`` 时移除整个工具链根，未指定时原样保留。

        默认保留是刻意的：根里是内置 Python 运行时、私有 npm 前缀与各 venv，同机多仓库
        共用一份，删掉会让其它仓库的 ``codekit:initialize`` 立刻降级。只有显式要求才动它，
        并在结果里说清下一次使用前要重跑安装器。
        """
        if not self.purge:
            return
        root = self.platform.toolchain_root(self.home)
        if not root.exists():
            return
        platform_layer.rmtree(root, ignore_errors=True)
        if root.exists():
            self.logger.warn(f"工具链根未能完全删除：{root}")
            return
        self.toolchain_purged = True
        self.logger.info(f"已移除工具链根 {root}（内置 Python 运行时、私有 npm 前缀与 venv）")

    def remove_matching_lines(self, path: Path, fragment: str) -> None:
        """从 shell 配置中删除包含指定片段的行，并在有变化时原子保存。"""
        if not path.is_file():
            return
        old = path.read_text(encoding="utf-8")
        new = "\n".join(line for line in old.splitlines() if fragment not in line) + ("\n" if old.endswith("\n") else "")
        if new != old:
            config.atomic_write(path, new)

    def cleanup_legacy_git_ai(self) -> None:
        """清理历史版本安装过的 git-ai 全局 hook 与残留；该组件已下线。"""
        candidate = shutil.which("git-ai") or str(self.home / ".git-ai" / "bin" / "git-ai")
        if Path(candidate).is_file():
            self.run_command(["git", "ai", "uninstall-hooks"])
        platform_layer.rmtree(self.home / ".git-ai", ignore_errors=True)
        (self.bin_dir / "git-ai").unlink(missing_ok=True)
        for path in self.shell_rcs():
            self.remove_matching_lines(path, "git-ai/bin")

    def unset_git_config(self, key: str) -> None:
        """撤销 CodeKit 写入的全局 Git 选项；键本来就不存在时不算失败。

        ``git config --unset`` 对不存在的键返回 **5**（"you try to unset an option
        which does not exist"）。安装中断、用户手工删过、或重复卸载都会走到这里，
        把 5 当成失败会让每次卸载都亮一条红字，而实际上配置本来就是干净的。
        """
        code = self.run_capture(["git", "config", "--global", "--unset", key]).returncode
        if code not in (0, 5):
            self.logger.warn(f"未能撤销 git 全局配置 {key}（退出码 {code}）。")

    def cleanup_global_config(self) -> None:
        """清理 Git 全局配置、CodeKit PATH 块和版本状态文件。"""
        self.cleanup_legacy_git_ai()
        for key in ("advice.addIgnoredFile", "push.autoSetupRemote"):
            self.unset_git_config(key)
        for path in self.shell_rcs():
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            output: list[str] = []
            skip = False
            # PATH 块由固定 marker 加下一行组成，只删除这两行，避免误伤其他配置。
            for line in lines:
                if "Added by CodeKit Plugin" in line:
                    skip = True
                    continue
                if skip:
                    skip = False
                    continue
                output.append(line)
            config.atomic_write(path, "\n".join(output).rstrip("\n") + ("\n" if output else ""))
        self.version_file.unlink(missing_ok=True)

    def run(self) -> int:
        """执行完整卸载生命周期：状态检测、组件卸载和全局配置清理。"""
        self.logger.info(f"=== CodeKit Plugin Uninstall v{self.plugin_version} ===")
        if self.os_name not in {"Darwin", "Linux", "Windows"}:
            raise InstallerError(f"Unsupported OS: {self.os_name}. Only macOS/Linux/Windows.")
        detector.registry.validate_registry()
        # npm 全局包默认落在用户前缀；卸载前挂载用户 bin，确保未 source shell
        # 配置时也能检测到并清理 GitNexus、钉钉 CLI、飞书 CLI。
        self.ensure_user_bin()
        self.load_states()
        self.select_components()
        if not self.selected:
            self.logger.info("No components selected. Nothing to uninstall.")
            self.purge_toolchain()
            try:
                detector.render_recap("uninstall", (), self.context)
            except (OSError, ValueError, RuntimeError) as exc:
                self.logger.warn(f"Could not render uninstall recap: {exc}")
            return 0
        self.run_uninstalls()
        self.purge_toolchain()
        root = self.platform.toolchain_root(self.home)
        if self.toolchain_purged:
            toolchain_note = (
                f"{root} was removed by --purge; re-run the installer before the next "
                "codekit:initialize."
            )
        elif root.exists():
            toolchain_note = (
                f"{root} also remains: it holds the bundled Python runtime and "
                "toolchain.json, so remove it by hand only once no repository still runs "
                "codekit:initialize (or re-run this uninstaller with --purge)."
            )
        else:
            toolchain_note = f"{root} is already gone."
        self.logger.info(
            f"Note: {self.state_dir} and {self.npm_prefix} remain. {toolchain_note}"
        )
        try:
            detector.render_recap("uninstall", ("\t".join(row) for row in self.uninstall_records), self.context)
        except (OSError, ValueError, RuntimeError) as exc:
            self.logger.warn(f"Could not render uninstall recap: {exc}")
        return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """解析独立卸载器命令行参数。"""
    parser = argparse.ArgumentParser(description="CodeKit Plugin uninstaller")
    parser.add_argument("-y", "--yes", action="store_true", help="proceed with default selections")
    parser.add_argument(
        "--purge",
        action="store_true",
        help=(
            "also remove the CodeKit toolchain root (bundled Python runtime, "
            "private npm prefix, venvs and toolchain.json)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """作为进程入口运行卸载器，并将取消或预期异常转换为退出码。"""
    args = parse_args(argv)
    try:
        config.require_python()
        return Uninstaller(args.yes, purge=args.purge).run()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (InstallerError, OSError, ValueError, RuntimeError) as exc:
        print(f"  ✗  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
