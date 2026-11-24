#!/usr/bin/env python3
"""Channel engine for the CodeKit installer.

Owns the channel description table (availability probe + install action per
channel), the package-manager install/upgrade semantics, remote script and
release-asset acquisition, and the manual-install guidance. Functions take the
installer session as their first argument — the same convention as
``installer_toolchain`` — so ``Installer`` keeps thin delegates and the
uninstaller inherits them unchanged.

This module is a library: ``installer.py`` imports it; nothing runs it as a
program.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import installer_agents as agents
import installer_config as config
import installer_platform as platform_layer
import installer_state as detector
from installer_platform import RunResult


# ---- 通道描述表 ------------------------------------------------------
# 可用性探测与安装动作只在这里登记：channel_available、install_channel 与
# ensure_agent_cli（CLI 前置安装）都从这张表分发。新增通道只需在这里加一行，
# 并在注册表的 CHANNEL_ORDER 里给出优先序。


def _venv_channel_available(installer) -> bool:
    # 内置运行时才是会被使用的那一个：用系统 Python 建出的 venv 在受限宿主里
    # 起不来（pyvenv.cfg 的 home 落在读不到的目录）。运行时不可用时诚实返回
    # False，让 install_via_channels 把这个组件如实判失败，而不是产出跑不起来
    # 的 venv，也不是退到根外的落点。
    if not installer.toolchain.usable:
        return False
    return installer.run_capture(
        [installer.toolchain_python(), "-c", "import venv, ensurepip"]
    ).returncode == 0


def _script_channel_available(installer) -> bool:
    # 官方 .sh 安装脚本只在类 Unix 环境里跑。Git for Windows 也会把 bash 放进
    # PATH，所以不能只判断"有没有 shell"：否则 Windows 上会退化成用 Git Bash
    # 执行 Unix 安装脚本，把文件装进 MSYS 目录树而不是 Windows 的 npm 目录。
    return not installer.platform.is_windows and bool(
        platform_layer.remote_script_command("bash")
    )


def _powershell_channel_available(installer) -> bool:
    # 严格限定 Windows：pwsh 在 macOS/Linux 上也可能存在，但那些 .ps1
    # 是 Windows 专用的安装脚本，不能跨平台执行。
    return installer.platform.is_windows and bool(
        platform_layer.remote_script_command("powershell")
    )


def _install_npm_channel(installer, component, target: str) -> bool:
    if component.entry_kind == "npm-private":
        return installer.install_npm_package_in_root(component)
    return installer.install_npm_package(target, component.command)


def _install_script_channel(installer, component, target: str) -> bool:
    return installer.run_remote_script(target) and installer.command_exists(component.command)


def _install_powershell_channel(installer, component, target: str) -> bool:
    return installer.run_remote_script(target, shell="powershell") and installer.command_exists(component.command)


@dataclass(frozen=True)
class Channel:
    """一根获取通道：可用性探测 + 安装动作。"""

    probe: Callable[..., bool]
    install: Callable[..., bool]


CHANNELS: dict[str, Channel] = {
    "npm": Channel(lambda i: i.command_exists("npm"), _install_npm_channel),
    "brew": Channel(
        lambda i: i.command_exists("brew"),
        lambda i, c, t: i.brew_install_or_upgrade(t, c.command),
    ),
    "brew_cask": Channel(
        lambda i: i.command_exists("brew"),
        lambda i, c, t: i.install_brew_cask(t, c.command),
    ),
    # apt 通道的 target 是 Debian/Ubuntu 包名，只在该包管理器下可用。
    "apt": Channel(
        lambda i: i.linux_package_manager() == "apt-get",
        lambda i, c, t: i.install_apt_package(t, c.command),
    ),
    # winget 只在 Windows 上存在；其他平台的同名命令即使有也不应使用。
    "winget": Channel(
        lambda i: i.platform.is_windows and i.command_exists("winget"),
        lambda i, c, t: i.install_winget_package(t, c.command),
    ),
    "venv": Channel(
        _venv_channel_available,
        lambda i, c, t: i.install_venv_package(t, c.command),
    ),
    "release": Channel(
        lambda i: i.command_exists("curl") or i.command_exists("wget"),
        lambda i, c, t: i.install_release(c),
    ),
    "script": Channel(_script_channel_available, _install_script_channel),
    "powershell": Channel(_powershell_channel_available, _install_powershell_channel),
}


# ---- 通道引擎 --------------------------------------------------------


def requirements_met(installer, component) -> tuple[bool, str]:
    """校验组件的硬前置（如 node>=24.11.0），返回 (是否满足, 原因)。"""
    for requirement in component.requires:
        name, separator, minimum = requirement.partition(">=")
        if name == "node":
            current = installer.command_version("node", ["-v"])
            if not separator or not current or not config.version_greater_or_equal(current, minimum):
                return False, f"需要 Node.js >= {minimum}，当前 {current or '未安装'}"
            continue
        if name == "python":
            current = installer.python_dependency_version()
            if not separator or not config.version_greater_or_equal(current, minimum):
                return False, f"需要 Python >= {minimum}，当前 {current}"
            continue
        if not installer.command_exists(name):
            return False, f"需要命令 {name}"
    return True, ""


def channel_available(installer, kind: str) -> bool:
    """判断通道在当前平台是否可用；只做探测，不触发安装。"""
    channel = CHANNELS.get(kind)
    return channel.probe(installer) if channel is not None else False


def channel_order(installer, component) -> tuple[str, ...]:
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


def install_via_channels(installer, component_id: str) -> bool:
    """按全局通道优先级，选用第一个可用通道安装组件。"""
    component = installer.component(component_id)
    if component is None:
        return False
    met, reason = installer.requirements_met(component)
    if not met:
        installer.logger.error(f"{component.description} 已跳过：{reason}")
        return False
    # 工具链根接管的组件只能落在根内：受限宿主（Codex 桌面端）读不到根外的落点，
    # 装上了也起不来。所以候选通道就是 entry_kind 允许的那几根，一根都不可用时如实
    # 失败——循环里不存在退到根外的第二条通道。
    candidates = installer.channel_order(component)
    if component.entry_kind and not any(
        installer.channel_available(kind) for kind in candidates
    ):
        if "venv" in candidates and not installer.toolchain.usable:
            reason = f"内置 Python 运行时不可用（{installer.toolchain.reason or '原因未知'}）"
        else:
            reason = f"根内通道 {'、'.join(candidates) or '（未声明）'} 当前不可用"
        installer.logger.error(
            f"{component.description} 已跳过：{reason}。"
            f"它的落点必须在 {installer.platform.toolchain_root(installer.home)} 内"
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
        if not installer.channel_available(kind):
            continue
        installer.logger.info(f"使用 {kind} 通道安装 {component.description}（{label}）")
        if installer.install_channel(component, kind):
            return True
        installer.logger.warn(f"{kind} 通道未成功，尝试下一通道。")
    installer.logger.error(
        f"{component.description} 安装失败；请手动安装：{installer.manual_note(component)}"
    )
    return False


def install_channel(installer, component, kind: str) -> bool:
    """执行单个通道的安装动作，并以组件命令校验最终可用性。"""
    channel = CHANNELS.get(kind)
    if channel is None:
        return False
    return channel.install(installer, component, component.channels.get(kind, ""))


def manual_note(installer, component) -> str:
    """生成组件的手动安装指引，供所有通道都失败时提示。

    Windows 上没有可用的 Unix shell，`curl … | bash` 这条提示会把人引到不存在的
    解释器上；按 Windows 真正可用的通道给指引。文案模板与 CLI 侧共用同一份。
    """
    order = (
        ("winget", "npm", "powershell", "release")
        if installer.platform.is_windows
        else ("script", "powershell", "npm", "winget", "brew", "apt")
    )
    return agents.manual_install_note(
        component.channels,
        order,
        release=component.release,
        default="请参考官方文档",
        proxy_script=True,
        brew_suffix="（建议先按 INSTALL-STRATEGY.md §2.3 配置中科大镜像）",
    )


# ---- npm / venv 落点 -------------------------------------------------


def install_npm_package(installer, package: str, command: str) -> bool:
    """使用用户态 npm 前缀安装或升级一个全局 CLI，镜像源由 set_npm 注入的环境变量提供。"""
    installer.set_npm()
    result = installer.run_command(["npm", "install", "-g", f"{package}@latest"])
    return result.returncode == 0 and installer.command_exists(command)


def install_npm_package_into_prefix(installer, prefix: Path, package: str, command: str) -> bool:
    """把全局 CLI 装进 CodeKit 私有 npm 前缀并让本进程可见。

    与默认路径分开而不是加参数，是为了让既有组件（钉钉/飞书 CLI 等）的调用与
    行为完全不变：私有前缀只服务于必须在受限宿主里可读可执行的 gitnexus。
    """
    prefix = Path(prefix)
    previous = os.environ.get("npm_config_prefix")
    os.environ["npm_config_prefix"] = str(prefix)
    os.environ["npm_config_registry"] = config.NPM_REGISTRY
    try:
        result = installer.run_command(
            ["npm", "install", "-g", "--prefix", str(prefix), f"{package}@latest"]
        )
    finally:
        if previous is None:
            os.environ.pop("npm_config_prefix", None)
        else:
            os.environ["npm_config_prefix"] = previous
    if result.returncode != 0:
        return False
    installer.prepend_path(installer.platform.npm_bin_dir(prefix))
    return installer.command_exists(command)


def install_npm_package_in_root(installer, component) -> bool:
    """把根接管的 npm 组件装进 CodeKit 私有前缀，并给人类 shell 留一个转发入口。

    私有前缀落在工具链根内（实测可读可执行）。转发入口只是给人用的：它在受限宿主里
    读不到，CodeKit 自己的代码路径一律走 toolchain.json 的绝对 argv，所以建不出来
    不影响工具本身——POSIX 下需要符号链接能力，Windows 上没有开发者模式/管理员权限
    时必然失败。
    """
    prefix = installer.platform.npm_prefix_private(installer.home)
    if not installer.install_npm_package_into_prefix(
        prefix, component.channels["npm"], component.command
    ):
        return False
    try:
        installer.platform.expose_command(
            installer.bin_dir,
            component.command,
            installer.platform.npm_bin_dir(prefix) / installer.platform.shim_name(component.command),
        )
    except OSError as exc:
        installer.logger.warn(
            f"未能为交互式 shell 暴露 {component.command} 转发入口（{exc}）；"
            f"不影响 CodeKit 使用它，安装器走 toolchain.json 里的绝对 argv。"
        )
    return True


def install_venv_package(installer, package: str, command: str) -> bool:
    """把 PyPI 包装进独立 venv，并把它的命令暴露到用户 bin 目录。

    venv 一律用内置运行时创建，并加 ``--copies``：用系统 Python 建出的 venv 会把
    ``pyvenv.cfg`` 的 ``home`` 指向可能被沙箱拒绝的目录，入口存在却启动不起来；
    实体解释器留在 venv 内则不受符号链接解析的限制。
    """
    venv = installer.platform.venv_dir(installer.home, command)
    if venv.exists():
        platform_layer.rmtree(venv, ignore_errors=True)
    pip = installer.platform.venv_pip(venv)
    if installer.run_command(
        [installer.toolchain_python(), "-m", "venv", "--copies", str(venv)]
    ).returncode != 0:
        return False
    if not pip.exists() or installer.run_command([str(pip), "install", "-U", package]).returncode != 0:
        return False
    executable = installer.platform.venv_exe(venv, command)
    if not executable.exists():
        return False
    # Windows 上不能靠符号链接暴露 venv 命令，适配层会改写成 .cmd 转发脚本。
    installer.platform.expose_command(installer.bin_dir, command, executable)
    return installer.command_exists(command)


# ---- 包管理器通道 ----------------------------------------------------


def _managed_install_or_upgrade(
    installer,
    probe: str,
    *,
    manages: Callable[[], bool],
    install: Callable[[], RunResult],
    upgrade: Callable[[], RunResult],
    outside_note: str,
    upgrade_fail_note: Callable[[int], str],
    after_run: Optional[Callable[[], None]] = None,
) -> bool:
    """包管理器通道共用的三段式：缺失才 install；非本管理器管理则让位；升级失败保持现状。

    只有「命令缺失」或「命令由本包管理器管理」时该通道才是正确的通道：命令已
    存在但不由它管理（例如官方脚本装的 rtk）时直接让位，否则只会装出第二份拷贝；
    升级没成功也不降级到其他通道，否则会把本管理器的拷贝遮住。brew/cask/winget
    的差异只在各自的命令与文案，控制流只有这一份。
    """
    if not installer.command_exists(probe):
        result = install()
        if after_run is not None:
            after_run()
        return result.returncode == 0 and installer.command_exists(probe)
    if not manages():
        installer.logger.info(outside_note)
        return False
    result = upgrade()
    if after_run is not None:
        after_run()
    if result.returncode != 0:
        installer.logger.warn(upgrade_fail_note(result.returncode))
    return True


def brew_install_or_upgrade(installer, formula: str, probe: Optional[str] = None) -> bool:
    """经 Homebrew 安装或升级一个 formula，并返回该通道是否已接管该组件。"""
    probe = probe or formula
    return installer._managed_install_or_upgrade(
        probe,
        manages=lambda: installer.brew_manages(formula),
        install=lambda: installer.brew("install", formula),
        upgrade=lambda: installer.brew("upgrade", formula),
        outside_note=f"{formula} 不在 Homebrew 管理下，跳过 brew 通道交给原通道升级。",
        upgrade_fail_note=lambda _rc: f"{formula} 升级未成功，保持现有 Homebrew 安装。",
    )


def install_brew_cask(installer, cask: str, command: str) -> bool:
    """经 Homebrew cask 安装或升级；cask 不在 brew 管理下时让位给下一通道。"""
    return installer._managed_install_or_upgrade(
        command,
        manages=lambda: installer.brew_manages(cask, cask=True),
        install=lambda: installer.brew("install", "--cask", cask),
        upgrade=lambda: installer.brew("upgrade", "--cask", cask),
        outside_note=f"{cask} 不在 Homebrew cask 管理下，跳过该通道交给原通道升级。",
        upgrade_fail_note=lambda _rc: f"{cask} 升级未成功，保持现有 Homebrew cask 安装。",
    )


def install_apt_package(installer, package: str, command: str) -> bool:
    """经 sudo 安装 Debian/Ubuntu 包；包不在源中时返回 False 以便降级。"""
    if installer.run_capture(["apt-cache", "show", package]).returncode != 0:
        installer.logger.warn(f"apt 源中不存在 {package}，跳过 apt 通道。")
        return False
    if installer.run_linux_package("install", package).returncode != 0:
        return False
    return installer.command_exists(command)


def winget_manages(installer, package: str) -> bool:
    """判断该包是否由 winget 自己管理；本地查询，不触发安装。"""
    result = installer.run_capture(
        [
            "winget", "list", "--id", package, "--exact",
            "--accept-source-agreements", "--disable-interactivity",
        ],
        timeout=180,
    )
    return result.returncode == 0 and package.lower() in result.stdout.lower()


def install_winget_package(installer, package: str, command: str) -> bool:
    """经 winget 安装或升级一个组件，控制流与 brew 通道共用同一骨架。

    非交互场景必须显式接受包协议与源协议，否则 winget 会挂起等待输入。upgrade
    返回"已是最新"（实测退出码 43）不算失败 —— 否则会降级到下一通道装出第二份
    拷贝。winget 只改注册表：每次执行后不刷新 PATH 会把"装好了"误判成"通道失败"。
    """
    common = (
        "--id", package, "--exact", "--silent",
        "--accept-package-agreements", "--accept-source-agreements",
        "--disable-interactivity",
    )
    return installer._managed_install_or_upgrade(
        command,
        manages=lambda: installer.winget_manages(package),
        install=lambda: installer.run_command(["winget", "install", *common], timeout=1200),
        upgrade=lambda: installer.run_command(["winget", "upgrade", *common], timeout=1200),
        outside_note=f"{package} 不在 winget 管理下，跳过 winget 通道交给原通道升级。",
        upgrade_fail_note=lambda rc: (
            f"{package} 升级未返回成功（退出码 {rc}），保持现有 winget 安装。"
        ),
        after_run=installer.refresh_path,
    )


# ---- 远程抓取与脚本通道 ----------------------------------------------


def fetch_text(installer, url: str) -> str:
    """获取远程脚本文本；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
    text = config.fetch_first(
        config.download_urls(url),
        timeout=config.REMOTE_FETCH_TIMEOUT,
        headers={"User-Agent": "CodeKit-installer"},
        parse=lambda response: response.read().decode("utf-8"),
    )
    return text or ""


def fetch_json_url(installer, url: str):
    """获取发布接口 JSON；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
    return config.fetch_first(
        config.download_urls(url),
        timeout=config.REMOTE_FETCH_TIMEOUT,
        headers={"Accept": "application/json", "User-Agent": "CodeKit-installer"},
        parse=json.load,
    )


def run_remote_script(installer, url: str, shell: str = "bash", env: Optional[dict[str, str]] = None) -> bool:
    """下载远程安装脚本后交给平台原生的 shell 执行。

    shell="powershell" 用于 Windows 专用的 .ps1 安装器（Qoder 等官方只提供
    .ps1），脚本同样经 stdin 喂入，避免落盘与执行策略的额外处理。
    """
    script = installer.fetch_text(url)
    if not script:
        return False
    command = platform_layer.remote_script_command(shell)
    if not command:
        return False
    if installer.run_with_input(list(command), script, env).returncode != 0:
        return False
    # 官方 .ps1 安装器同样把 PATH 写进注册表，刷新后才能校验命令可用性。
    installer.refresh_path()
    return True


# ---- release 资产通道 ------------------------------------------------


def release_arch(installer) -> str:
    """把平台机器名归一化为发布资产的架构标记。"""
    return {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform_module.machine().lower(), "")


def release_asset_name(installer, spec, version: str) -> str:
    """按 release 模板展开当前平台的资产文件名。"""
    arch = installer.release_arch()
    return spec.asset.format(
        version=version,
        os=spec.os_tokens.get(installer.os_name, ""),
        arch=spec.arch_tokens.get(arch, ""),
        ext=spec.ext_tokens.get(installer.os_name, ""),
        target=spec.target_tokens.get(f"{installer.os_name}-{arch}", ""),
    )


def resolve_release(installer, spec) -> Optional[tuple[str, str, str]]:
    """解析最新发布版本，返回 (版本, 下载地址, sha256)；失败返回 None。"""
    kind, _, ident = spec.repo.partition(":")
    if kind == "github":
        document = installer.fetch_json_url(
            f"https://api.github.com/repos/{ident}/releases/latest"
        )
        if not document:
            return None
        version = str(document.get("tag_name", "")).lstrip("v")
        asset_name = installer.release_asset_name(spec, version)
        for asset in document.get("assets", []):
            if asset.get("name") == asset_name:
                digest = str(asset.get("digest", "")).split(":", 1)[-1]
                return version, str(asset.get("browser_download_url", "")), digest
        return None
    if kind == "gitlab":
        document = installer.fetch_json_url(
            f"https://gitlab.com/api/v4/projects/{ident}/releases/permalink/latest"
        )
        if not document:
            return None
        version = str(document.get("tag_name", "")).lstrip("v")
        asset_name = installer.release_asset_name(spec, version)
        links = (document.get("assets") or {}).get("links", [])
        for link in links:
            if link.get("name") == asset_name:
                return version, str(link.get("direct_asset_url", "")), ""
        return None
    return None


def download_release(installer, url: str, digest: str) -> Optional[Path]:
    """下载发布资产并校验摘要；GitHub 地址优先走 gh-proxy，失败再回退直连。"""
    options = [
        "--fail", "--location", "--connect-timeout", "10", "--speed-limit", "51200",
        "--speed-time", "20", "--retry", "2", "--retry-max-time", "180",
        "--continue-at", "-", "--progress-bar",
    ]
    fd, name = tempfile.mkstemp(prefix="codekit-release-", dir=installer.platform.temp_dir())
    os.close(fd)
    temporary = Path(name)
    result = RunResult(1)
    for candidate in config.download_urls(url):
        # --continue-at - 会续传上一个地址留下的半成品，切换地址前必须清空。
        temporary.write_bytes(b"")
        result = installer.run_command(["curl", *options, "-o", str(temporary), candidate])
        if result.returncode == 0:
            break
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        return None
    if digest and hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
        temporary.unlink(missing_ok=True)
        installer.logger.error("发布包摘要校验失败，已删除下载文件。")
        return None
    return temporary


def extract_release_binary(installer, archive: Path, binary: str) -> Optional[Path]:
    """从 tar.gz/zip 发布包中定位目标可执行文件。

    Windows 构建的可执行文件带 ``.exe``，因此按平台文件名与裸名各找一次，
    这样同一个发布说明在 macOS/Linux 与 Windows 上都能命中。
    """
    directory = Path(tempfile.mkdtemp(prefix="codekit-extract-"))
    try:
        platform_layer.extract_archive(archive, directory)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError):
        return None
    for name in dict.fromkeys((installer.platform.binary_filename(binary), binary)):
        for candidate in directory.rglob(name):
            if candidate.is_file():
                return candidate
    return None


def install_release(installer, component) -> bool:
    """按 release 通道解析、下载并安装组件的最新发布包。"""
    resolved = installer.resolve_release(component.release)
    if not resolved or not resolved[1]:
        installer.logger.warn(f"未能解析 {component.description} 的发布包地址。")
        return False
    version, url, digest = resolved
    installer.logger.info(f"下载 {component.description} {version}：{url}")
    archive = installer.download_release(url, digest)
    if archive is None:
        return False
    try:
        binary = installer.extract_release_binary(archive, component.release.binary)
    finally:
        archive.unlink(missing_ok=True)
    if binary is None:
        return False
    target = installer.platform.release_target(installer.bin_dir, component.release.binary)
    target.unlink(missing_ok=True)
    shutil.move(str(binary), str(target))
    if not installer.platform.is_windows:
        target.chmod(0o755)
    installer.track(target)
    return installer.command_exists(component.command)