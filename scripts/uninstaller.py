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
from typing import Iterable, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
import installer_state as detector  # noqa: E402
from installer import (  # noqa: E402
    CLAUDE_MARKETPLACE,
    CLAUDE_PLUGIN,
    CODEX_MARKETPLACE,
    CODEX_PLUGIN,
    Installer,
    InstallerError,
)


class Uninstaller(Installer):
    """执行 CodeKit 组件选择、卸载和全局配置清理。"""

    def __init__(self, auto_yes: bool) -> None:
        super().__init__(auto_yes)
        self.uninstall_records: list[tuple[str, str, str]] = []

    def component_tag(self, component_id: str) -> str:
        """生成卸载组件选择器中的状态提示。"""
        item = self.states[component_id]
        if item.status == "not_installed":
            return " (组件未安装)"
        return f" (组件已安装 {item.installed_version or 'unknown'}，勾选后卸载)"

    def select_components(self) -> list[str]:
        """按当前安装状态生成默认选择，并在交互模式下回填用户选择。"""
        default = [
            item.component_id
            for item in self.components
            if self.states[item.component_id].status != "not_installed"
        ]
        if not self.auto_yes and Path("/dev/tty").exists():
            answer = input("确认要卸载 CodeKit Plugin 及相关组件？[y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                self.logger.info("Cancelled.")
                raise SystemExit(0)

        # 非交互执行必须可用于 CI：采用默认选择，并跳过终端选择器。
        if self.auto_yes or not Path("/dev/tty").exists():
            self.selected = default
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
                    str(self.shell_config()[0]),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        finally:
            manifest_path.unlink(missing_ok=True)
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
        helper = SCRIPT_DIR / "manage-global-agents.py"
        if not helper.is_file():
            return True
        args = [sys.executable, str(helper), "uninstall"]
        args.extend(["--home", str(self.home), "--tool", tool])
        return self.run_command(args).returncode == 0

    def uninstall_claude_plugin(self) -> bool:
        """卸载 Claude 插件及其 marketplace 注册，并校验注册信息是否已移除。"""
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
            shutil.rmtree(self.home / ".claude" / "plugins" / "cache" / CLAUDE_MARKETPLACE, ignore_errors=True)
        return plugin_ok and self.uninstall_platform_agents("claude")

    def uninstall_opencode_plugin(self) -> bool:
        """从 OpenCode 配置中删除 CodeKit 插件，并再次读取配置确认删除结果。"""
        path = self.home / ".config" / "opencode" / "opencode.json"
        try:
            plugin_ok = True
            if config.opencode_has_plugin(path):
                config.remove_opencode_plugins(path)
                plugin_ok = not config.opencode_has_plugin(path)
            return plugin_ok and self.uninstall_platform_agents("opencode")
        except ValueError:
            return False

    def uninstall_git_ai(self) -> bool:
        """移除 git-ai hooks、程序目录、用户 bin 链接以及 shell PATH 残留。"""
        if self.git_ai_command():
            self.run_command(["git", "ai", "uninstall-hooks"])
        shutil.rmtree(self.home / ".git-ai", ignore_errors=True)
        (self.bin_dir / "git-ai").unlink(missing_ok=True)
        for path in self.shell_rcs():
            self.remove_matching_lines(path, "git-ai/bin")
        return True

    def uninstall_rtk(self) -> bool:
        """撤销 rtk 的全局配置，并卸载残留。"""
        if self.command_exists("rtk"):
            self.run_command(["rtk", "init", "-g", "--uninstall"])
            self.run_command(["rtk", "init", "-g", "--codex", "--uninstall"])
        if self.command_exists("brew") and self.run_capture(["brew", "list", "rtk"]).returncode == 0:
            self.brew("uninstall", "rtk")
        for path in (self.bin_dir / "rtk", self.home / ".local" / "bin" / "rtk"):
            path.unlink(missing_ok=True)
        return True

    def uninstall_brew_component(self, formula: str) -> bool:
        """按平台卸载 gh/glab，并删除 CodeKit 用户 bin 残留。"""
        (self.bin_dir / formula).unlink(missing_ok=True)
        if self.command_exists("brew") and self.run_capture(["brew", "list", formula]).returncode == 0:
            return self.brew("uninstall", formula).returncode == 0
        if self.os_name == "Linux" and self.linux_package_installed(formula):
            return self.run_linux_package("remove", formula).returncode == 0
        return True

    def uninstall_gitnexus(self) -> bool:
        """清理 gitnexus 自身配置，并同时卸载用户 npm 前缀和默认 npm 前缀中的包。"""
        if self.command_exists("gitnexus"):
            self.run_command(["gitnexus", "uninstall", "--force"])
        return self.uninstall_npm_component("gitnexus")

    def uninstall_npm_component(self, component_id: str) -> bool:
        """从用户态和默认 npm 前缀卸载一个全局 npm CLI 组件。"""
        definition = detector.NPM_COMPONENTS.get(component_id)
        if not definition:
            return False
        package = definition["package"]
        command = definition["command"]
        self.set_npm()
        self.run_command(["npm", "uninstall", "-g", package])
        env = os.environ.copy()
        env.pop("npm_config_prefix", None)
        self.run_with_input(["npm", "uninstall", "-g", package], "", env)
        return not self.command_exists(command)

    def uninstall_dingtalk_cli(self) -> bool:
        """卸载钉钉 npm CLI。"""
        return self.uninstall_npm_component("dingtalk_cli")

    def uninstall_feishu_cli(self) -> bool:
        """卸载飞书 npm CLI。"""
        return self.uninstall_npm_component("feishu_cli")

    def uninstall_graphify(self) -> bool:
        """卸载 pipx/pip 中的 graphifyy，并删除 CodeKit venv 和用户级软链接。"""
        if self.command_exists("pipx") and "graphifyy" in self.run_capture(["pipx", "list"]).stdout.lower():
            self.run_command(["pipx", "uninstall", "graphifyy"])
        if self.command_exists("pip3"):
            self.run_command(["pip3", "uninstall", "-y", "graphifyy"])
        shutil.rmtree(self.home / ".local" / "share" / "CodeKit" / "venvs" / "graphify", ignore_errors=True)
        (self.bin_dir / "graphify").unlink(missing_ok=True)
        return True

    def uninstall_codex_plugin(self) -> bool:
        """移除 Codex 中的 CodeKit 插件和 marketplace，并删除旧版兼容技能目录。"""
        if self.command_exists("codex"):
            self.run_command(["codex", "plugin", "remove", f"{CODEX_PLUGIN}@{CODEX_MARKETPLACE}"])
            self.run_command(["codex", "plugin", "marketplace", "remove", CODEX_MARKETPLACE])
        for path in (self.home / ".agents" / "skills").glob("keel-*"):
            if path.is_dir():
                shutil.rmtree(path)
        return self.uninstall_platform_agents("codex")

    def uninstall_component(self, component_id: str) -> bool:
        """按组件注册 ID 分发卸载处理器，并把单项异常转换为失败结果。"""
        handlers = {
            "git_ai": self.uninstall_git_ai,
            "rtk": self.uninstall_rtk,
            "claude_plugin": self.uninstall_claude_plugin,
            "opencode_plugin": self.uninstall_opencode_plugin,
            "codex_plugin": self.uninstall_codex_plugin,
            "gitnexus": self.uninstall_gitnexus,
            "dingtalk_cli": self.uninstall_dingtalk_cli,
            "feishu_cli": self.uninstall_feishu_cli,
            "graphify": self.uninstall_graphify,
            "gh": lambda: self.uninstall_brew_component("gh"),
            "glab": lambda: self.uninstall_brew_component("glab"),
        }
        handler = handlers.get(component_id)
        if not handler:
            return False
        try:
            return bool(handler())
        except (OSError, ValueError, InstallerError) as exc:
            self.logger.error(f"{component_id} failed: {exc}")
            return False

    def run_uninstalls(self) -> None:
        """按用户选择逐项卸载；未安装项视为幂等成功，最后清理全局配置。"""
        for component_id in self.selected:
            prior = "absent" if self.states[component_id].status == "not_installed" else "installed"
            self.logger.info(f"\n--- 卸载: {component_id} ---")
            # 卸载是幂等操作：检测到组件不存在时不调用破坏性处理器。
            ok = True if prior == "absent" else self.uninstall_component(component_id)
            self.uninstall_records.append((component_id, prior, "1" if ok else "0"))
        self.cleanup_global_config()

    def shell_rcs(self) -> Iterable[Path]:
        """返回卸载时需要检查的 Bash、Zsh 和 Fish 配置文件集合。"""
        return (
            self.home / ".bashrc",
            self.home / ".bash_profile",
            self.home / ".zshrc",
            self.home / ".zprofile",
            self.home / ".config" / "fish" / "config.fish",
        )

    def remove_matching_lines(self, path: Path, fragment: str) -> None:
        """从 shell 配置中删除包含指定片段的行，并在有变化时原子保存。"""
        if not path.is_file():
            return
        old = path.read_text(encoding="utf-8")
        new = "\n".join(line for line in old.splitlines() if fragment not in line) + ("\n" if old.endswith("\n") else "")
        if new != old:
            config.atomic_write(path, new)

    def cleanup_global_config(self) -> None:
        """清理 Git 全局配置、CodeKit PATH 块和版本状态文件。"""
        self.run_command(["git", "config", "--global", "--unset", "advice.addIgnoredFile"])
        self.run_command(["git", "config", "--global", "--unset", "push.autoSetupRemote"])
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
        if self.os_name not in {"Darwin", "Linux"}:
            raise InstallerError(f"Unsupported OS: {self.os_name}. Only macOS/Linux.")
        required_helpers = (
            SCRIPT_DIR / "component-registry.py",
            SCRIPT_DIR / "installer.py",
            SCRIPT_DIR / "installer_config.py",
            SCRIPT_DIR / "installer_state.py",
            SCRIPT_DIR / "installer_sources.py",
            SCRIPT_DIR / "component-selector.py",
        )
        missing = [str(path) for path in required_helpers if not path.is_file()]
        if missing:
            raise InstallerError(f"Uninstaller helper missing: {', '.join(missing)}")
        detector.registry.validate_registry()
        # npm 全局包默认落在用户前缀；卸载前挂载用户 bin，确保未 source shell
        # 配置时也能检测到并清理 GitNexus、钉钉 CLI、飞书 CLI。
        self.ensure_user_bin()
        self.load_states()
        self.select_components()
        if not self.selected:
            self.logger.info("No components selected. Nothing to uninstall.")
            try:
                detector.render_recap("uninstall", (), self.context)
            except (OSError, ValueError, RuntimeError) as exc:
                self.logger.warn(f"Could not render uninstall recap: {exc}")
            return 0
        self.run_uninstalls()
        self.logger.info(f"Note: {self.state_dir} and {self.npm_prefix} remain.")
        try:
            detector.render_recap("uninstall", ("\t".join(row) for row in self.uninstall_records), self.context)
        except (OSError, ValueError, RuntimeError) as exc:
            self.logger.warn(f"Could not render uninstall recap: {exc}")
        return 0


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """解析独立卸载器命令行参数。"""
    parser = argparse.ArgumentParser(description="CodeKit Plugin uninstaller")
    parser.add_argument("-y", "--yes", action="store_true", help="proceed with default selections")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """作为进程入口运行卸载器，并将取消或预期异常转换为退出码。"""
    args = parse_args(argv)
    try:
        return Uninstaller(args.yes).run()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (InstallerError, OSError, ValueError, RuntimeError) as exc:
        print(f"  ✗  {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
