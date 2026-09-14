#!/usr/bin/env python3
"""Canonical component registry and install-strategy table for CodeKit.

This module is the single source of truth for:

* which optional components the installer offers (``COMPONENTS``);
* which acquisition channels each one may use (``Component.channels``);
* the global channel priority the installer probes (``CHANNEL_ORDER``);
* the channel table for coding-agent CLIs (``AGENT_CLI_CHANNELS``).

Installer, uninstaller, state detection and the coding-agent helpers all read
these definitions instead of carrying their own copies, so a channel change is
made in exactly one place.  The human-readable mirror of this table lives in
``INSTALL-STRATEGY.md``.

Channels are expressed as ``kind -> target``:

* ``npm``        target is an official npm package name (whitelisted below);
* ``brew``       target is a Homebrew formula;
* ``brew_cask``  target is a Homebrew cask;
* ``apt``        target is a Debian/Ubuntu package name;
* ``winget``     target is a winget package id (Windows only);
* ``venv``       target is a PyPI distribution installed into a private venv;
* ``script``     target is an official installer-script URL, run with sh;
* ``powershell`` target is a Windows-only PowerShell installer-script URL;
* ``release``    target is resolved from ``Component.release`` (release assets).

``Component.entry_kind`` decides *where* a component may land.  Empty means the
host's own user-level directories (``~/.npm-global``, ``~/.local/bin``), which a
path-selective sandbox may not be able to read.  Non-empty means CodeKit's own
toolchain root owns the tool, and each value admits exactly one channel — the
only one whose target directory is inside that root (``ENTRY_KIND_CHANNELS``).
That makes "a root-owned tool can never be installed outside the root" a
property of this table rather than of a call site.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence


COMPONENT_ID = re.compile(r"^[a-z][a-z0-9_]*$")
# A requirement is either a bare command that must exist ("unzip") or a
# command with a minimum version ("node>=24.11.0").
REQUIREMENT = re.compile(r"^[a-z][a-z0-9_-]*(>=[0-9]+(\.[0-9]+)*)?$")

# Global channel priority. Every platform probes this same order; a channel is
# only used when it is both declared by the component and currently available.
# ``release`` precedes ``script``: a release asset is resolved by an exact file
# name and verified against a digest, while ``script`` pipes a remote script
# into a shell. When a component offers both, the verifiable path goes first.
# ``winget`` and ``powershell`` are Windows-only channels; their availability
# probes are false everywhere else, so they occupy the slot of the equivalent
# native channel without affecting macOS/Linux ordering.
CHANNEL_ORDER: tuple[str, ...] = (
    "npm",
    "brew",
    "brew_cask",
    "apt",
    "winget",
    "venv",
    "release",
    "script",
    "powershell",
)

# 由 CodeKit 自持工具链根接管的入口类型 → 该类型唯一允许的获取通道。
# 每个通道的落点都必须在根内（``<根>/npm`` 与 ``<根>/venvs/<命令>``），否则受限宿主
# 读不到自己会话里要启动的东西。新增一个根接管工具时只需在这里加一行并给组件写上
# ``entry_kind``，落点约束由 ``validate_registry`` 自动生效。
ENTRY_KIND_CHANNELS: Mapping[str, frozenset[str]] = {
    "npm-private": frozenset({"npm"}),
    "python-venv": frozenset({"venv"}),
}

# 落点必然在工具链根内的通道：非根接管的组件不得声明它，否则工具会静默出现在清单之外，
# 技能侧只能靠猜路径找到它。``npm`` 不在其列——同一个通道按 ``entry_kind`` 可以落用户
# 前缀（钉钉/飞书 CLI），也可以落私有前缀（gitnexus），落点由 entry_kind 决定。
ROOT_ONLY_CHANNELS: frozenset[str] = frozenset({"venv"})


def root_bound_ids(components: Optional[Sequence[Component]] = None) -> tuple[str, ...]:
    """声明了 ``entry_kind``（即由工具链根接管）的组件 ID，按注册表顺序。"""
    items = COMPONENTS if components is None else components
    return tuple(item.component_id for item in items if item.entry_kind)


# Only these npm package names may ever be installed. The allowlist exists
# because several well-known CLI names are squatted or owned by unrelated
# projects on npm (`gh`, `rtk`, `glab`, `@git-ai/cli`); installing a bare name
# would silently pull the wrong package. Adding a component to the npm channel
# requires adding its exact official package name here first.
OFFICIAL_NPM_PACKAGES: frozenset[str] = frozenset(
    {
        "@anthropic-ai/claude-code",
        "@openai/codex",
        "@larksuite/cli",
        "dingtalk-workspace-cli",
        "gitnexus",
        "opencode-ai",
    }
)


@dataclass(frozen=True)
class ReleaseSpec:
    """Release-asset download plan for the ``release`` channel."""

    repo: str  # "github:owner/repo" or "gitlab:<project-id>"
    asset: str  # template with {version} {os} {arch} {ext} {target}
    binary: str  # executable name inside the archive
    os_tokens: Mapping[str, str] = field(default_factory=dict)
    arch_tokens: Mapping[str, str] = field(default_factory=dict)
    ext_tokens: Mapping[str, str] = field(default_factory=dict)
    # Rust-style target triple, keyed by "<platform.system()>-<release_arch()>"
    # (e.g. "Darwin-arm64"), for assets named after the triple instead of the
    # separate os/arch tokens.
    target_tokens: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Component:
    component_id: str
    description: str
    group: str
    # ``command`` is the executable used for presence/version checks; plugin
    # components ship no command of their own and set ``plugin`` instead.
    command: str = ""
    version_args: tuple[str, ...] = ()
    channels: Mapping[str, str] = field(default_factory=dict)
    # Hard prerequisites enforced per component, e.g. ("node>=24.11.0",).
    requires: tuple[str, ...] = ()
    # Version source for upgrade checks: "github:owner/repo", "gitlab:<id>",
    # "npm:<package>" or "pypi:<package>". Empty means "derive from channels".
    version_source: str = ""
    release: Optional[ReleaseSpec] = None
    plugin: bool = False
    # ``entry_kind`` 决定落点：空串 = 宿主默认用户目录；非空 = CodeKit 自持工具链根，
    # 取值必须是 ``ENTRY_KIND_CHANNELS`` 的键，它同时给出唯一允许的通道与该组件在
    # ``toolchain.json`` 里用哪个 argv 构建器。
    entry_kind: str = ""


GROUPS: Dict[str, str] = {
    "ai": "── AI 开发套件 ──",
    "skills": "── CodeKit Skills ──",
    "intel": "── 代码智能 ──",
    "third": "── 三方工具 ──",
}


# Marker executable that proves an npm install produced a usable CLI.
COMPONENTS: Sequence[Component] = (
    Component(
        "rtk",
        "Rust Token Killer (节省token·推荐)",
        "ai",
        command="rtk",
        version_args=("--version",),
        channels={
            "brew": "rtk",
            "release": "github:rtk-ai/rtk",
            "script": "https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh",
        },
        version_source="github:rtk-ai/rtk",
        # 官方脚本下载的资产名就是 `rtk-<Rust target triple>.<ext>`（版本只出现在
        # 下载路径的 v{version} 里），这里按同一命名直接取发布资产，省掉 `curl|bash`。
        release=ReleaseSpec(
            repo="github:rtk-ai/rtk",
            asset="rtk-{target}.{ext}",
            binary="rtk",
            target_tokens={
                "Darwin-arm64": "aarch64-apple-darwin",
                "Darwin-x86_64": "x86_64-apple-darwin",
                # x86_64 与 arm64 的 libc 不同名：前者 musl、后者 gnu（实测 v0.49.0 资产）。
                "Linux-x86_64": "x86_64-unknown-linux-musl",
                "Linux-arm64": "aarch64-unknown-linux-gnu",
                "Windows-x86_64": "x86_64-pc-windows-msvc",
            },
            ext_tokens={"Darwin": "tar.gz", "Linux": "tar.gz", "Windows": "zip"},
        ),
    ),
    Component("claude_plugin", "CodeKit Plugin (Claude)", "skills", plugin=True),
    Component("opencode_plugin", "CodeKit Plugin (OpenCode)", "skills", plugin=True),
    Component("codex_plugin", "CodeKit Plugin (Codex)", "skills", plugin=True),
    Component("qoder_plugin", "CodeKit Plugin (Qoder)", "skills", plugin=True),
    Component("qodercn_plugin", "CodeKit Plugin (Qoder CN)", "skills", plugin=True),
    Component(
        "gitnexus",
        "GitNexus (代码结构分析·推荐)",
        "intel",
        command="gitnexus",
        version_args=("--version",),
        channels={"npm": "gitnexus"},
        requires=("node>=24.11.0",),
        entry_kind="npm-private",
    ),
    Component(
        "graphify",
        "Graphify (代码知识图谱·推荐)",
        "intel",
        command="graphify",
        version_args=("--version",),
        # 只有 venv：pipx 的 shim 落在工具链根之外，受限宿主（Codex 桌面端）读不到，
        # 装上了也等于没装。历史上它同时声明过 pipx，那条通道正是漏进根外的入口。
        channels={"venv": "graphifyy"},
        requires=("python>=3.10",),
        version_source="pypi:graphifyy",
        entry_kind="python-venv",
    ),
    Component(
        "gh",
        "GitHub CLI (推荐)",
        "third",
        command="gh",
        version_args=("--version",),
        channels={"brew": "gh", "apt": "gh", "winget": "GitHub.cli", "release": "github:cli/cli"},
        version_source="github:cli/cli",
        release=ReleaseSpec(
            repo="github:cli/cli",
            asset="gh_{version}_{os}_{arch}.{ext}",
            binary="gh",
            os_tokens={"Darwin": "macOS", "Linux": "linux", "Windows": "windows"},
            arch_tokens={"x86_64": "amd64", "arm64": "arm64"},
            ext_tokens={"Darwin": "zip", "Linux": "tar.gz", "Windows": "zip"},
        ),
    ),
    Component(
        "glab",
        "GitLab CLI (推荐)",
        "third",
        command="glab",
        version_args=("version",),
        channels={"brew": "glab", "apt": "glab", "winget": "GLab.GLab", "release": "gitlab:34675721"},
        version_source="gitlab:34675721",
        release=ReleaseSpec(
            repo="gitlab:34675721",
            asset="glab_{version}_{os}_{arch}.{ext}",
            binary="glab",
            os_tokens={"Darwin": "darwin", "Linux": "linux", "Windows": "windows"},
            arch_tokens={"x86_64": "amd64", "arm64": "arm64"},
            ext_tokens={"Darwin": "tar.gz", "Linux": "tar.gz", "Windows": "zip"},
        ),
    ),
    Component(
        "dingtalk_cli",
        "钉钉 CLI",
        "third",
        command="dws",
        version_args=("version",),
        channels={"npm": "dingtalk-workspace-cli"},
        # 解压工具的前置由 installer.ensure_unzip 负责，而不是这里的 requires：
        # Windows 上钉钉的 install.js 改用 PowerShell 的 Expand-Archive，不需要
        # unzip，写成静态 requires 会把整个组件在 Windows 上误判为不可安装。
    ),
    Component(
        "feishu_cli",
        "飞书 CLI",
        "third",
        command="lark-cli",
        version_args=("--version",),
        channels={"npm": "@larksuite/cli"},
    ),
)


# Coding-agent CLIs are installed on demand as plugin prerequisites. They share
# the same channel priority and table shape as the optional components.
AGENT_CLI_CHANNELS: Dict[str, Mapping[str, str]] = {
    "claude": {
        "npm": "@anthropic-ai/claude-code",
        "brew_cask": "claude-code",
        "winget": "Anthropic.ClaudeCode",
    },
    "codex": {"npm": "@openai/codex", "brew_cask": "codex"},
    "opencode": {"npm": "opencode-ai", "brew": "opencode"},
    # Qoder 官方按平台分发：macOS/Linux 走 install.sh，Windows 走 install.ps1。
    "qoder": {
        "script": "https://qoder.com/install",
        "powershell": "https://qoder.com/install.ps1",
    },
    "qodercn": {
        "script": "https://static.qoder.com.cn/qoder-cli-cn/install.sh",
        "powershell": "https://static.qoder.com.cn/qoder-cli-cn/install.ps1",
    },
}


COMPONENTS_BY_ID = {component.component_id: component for component in COMPONENTS}


def component_by_id(component_id: str) -> Optional[Component]:
    """Return a component definition by id, or None when unknown."""
    return COMPONENTS_BY_ID.get(component_id)


def validate_registry(
    components: Sequence[Component] = COMPONENTS,
    groups: Dict[str, str] = GROUPS,
    agent_channels: Mapping[str, Mapping[str, str]] = AGENT_CLI_CHANNELS,
) -> None:
    if not components:
        raise ValueError("component registry is empty")
    if not groups:
        raise ValueError("component group registry is empty")

    seen = set()
    group_order = {group: index for index, group in enumerate(groups)}
    previous_group_index = -1
    for component in components:
        if not COMPONENT_ID.fullmatch(component.component_id):
            raise ValueError(f"invalid component id: {component.component_id!r}")
        if component.component_id in seen:
            raise ValueError(f"duplicate component id: {component.component_id}")
        seen.add(component.component_id)
        if component.group not in groups:
            raise ValueError(
                f"unknown group {component.group!r} for {component.component_id}"
            )
        current_group_index = group_order[component.group]
        if current_group_index < previous_group_index:
            raise ValueError("components must follow the declared group order")
        previous_group_index = current_group_index
        for field_name, value in (
            ("description", component.description),
            ("group title", groups[component.group]),
        ):
            if not value or any(character in value for character in "\t\r\n"):
                raise ValueError(
                    f"invalid {field_name} for component {component.component_id}"
                )
        _validate_channels(component.component_id, component.channels)
        _validate_requirements(component.component_id, component.requires)
        if component.plugin and component.channels:
            raise ValueError(
                f"plugin component must not declare channels: {component.component_id}"
            )
        if not component.plugin and not component.channels:
            raise ValueError(
                f"non-plugin component must declare channels: {component.component_id}"
            )
        if "release" in component.channels and component.release is None:
            raise ValueError(
                f"release channel without a release spec: {component.component_id}"
            )
        if component.release is not None and component.channels.get("release") != component.release.repo:
            raise ValueError(
                f"release spec/repo mismatch: {component.component_id}"
            )
        _validate_entry_kind(component)

    for key, channels in agent_channels.items():
        _validate_channels(f"agent:{key}", channels)


def _validate_entry_kind(component: Component) -> None:
    """落点不变量：根接管的组件只能经由根内通道获取，根外的组件拿不到根内通道。"""
    kind = component.entry_kind
    if not kind:
        leaked = sorted(set(component.channels) & ROOT_ONLY_CHANNELS)
        if leaked:
            raise ValueError(
                f"channel(s) {leaked} land inside the CodeKit toolchain root and "
                f"require an entry_kind: {component.component_id}"
            )
        return
    allowed = ENTRY_KIND_CHANNELS.get(kind)
    if allowed is None:
        raise ValueError(
            f"unknown entry_kind {kind!r} for {component.component_id} "
            f"(known: {sorted(ENTRY_KIND_CHANNELS)})"
        )
    outside = sorted(set(component.channels) - allowed)
    if outside or not component.channels:
        raise ValueError(
            f"{component.component_id} owns entry_kind {kind!r}; its channels must "
            f"be exactly {sorted(allowed)}, found {sorted(component.channels)}"
        )


def _validate_channels(owner: str, channels: Mapping[str, str]) -> None:
    for kind, target in channels.items():
        if kind not in CHANNEL_ORDER:
            raise ValueError(f"unknown channel {kind!r} for {owner}")
        if not target or any(character in target for character in "\t\r\n"):
            raise ValueError(f"invalid {kind} target for {owner}")
        if kind == "npm" and target not in OFFICIAL_NPM_PACKAGES:
            raise ValueError(
                f"npm package is not on the official allowlist: {target} ({owner})"
            )


def _validate_requirements(owner: str, requires: Sequence[str]) -> None:
    for requirement in requires:
        if not REQUIREMENT.fullmatch(requirement):
            raise ValueError(f"invalid requirement {requirement!r} for {owner}")


def export_tsv(components: Sequence[Component] = COMPONENTS) -> None:
    for component in components:
        print(
            "\t".join(
                (
                    component.component_id,
                    component.description,
                    component.group,
                    GROUPS[component.group],
                )
            )
        )


def export_json(components: Sequence[Component] = COMPONENTS) -> None:
    payload: List[dict] = []
    for component in components:
        item = asdict(component)
        item["group_title"] = GROUPS[component.group]
        payload.append(item)
    print(
        json.dumps(
            {
                "channel_order": list(CHANNEL_ORDER),
                "components": payload,
                "agent_clis": {key: dict(value) for key, value in AGENT_CLI_CHANNELS.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate or export CodeKit components")
    parser.add_argument(
        "--format",
        choices=("tsv", "json"),
        default="tsv",
        help="registry output format (default: tsv)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate without printing the registry",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        validate_registry()
    except ValueError as exc:
        print(f"component registry: {exc}", file=sys.stderr)
        return 1
    if args.check:
        return 0
    if args.format == "json":
        export_json()
    else:
        export_tsv()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
