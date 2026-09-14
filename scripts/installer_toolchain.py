#!/usr/bin/env python3
"""CodeKit's private toolchain root: bundled Python runtime and toolchain.json.

Why this exists: a hosted sandbox can be *path-selective*.  Measured on Codex
Desktop (Windows), the sandbox denies `%APPDATA%` and `%LOCALAPPDATA%\\Programs`
outright while still allowing reads and execution under the user data directory.
CodeKit therefore cannot rely on npm's global prefix or on a system Python:

* `gitnexus` installed only into `%APPDATA%\\npm` is unreadable, so its shim
  cannot even be stat-ed;
* `graphify`'s venv is reachable, but a venv is not portable -- its
  `python.exe` re-execs the base interpreter recorded in `pyvenv.cfg`, which
  lives in a denied directory, so launching it fails with rc=101.

Both problems have the same fix: own a toolchain root that the consuming session
can read and execute, and give every Python-based tool an interpreter that
travels inside it.  The runtime is a self-contained CPython build
(python-build-standalone), so a venv created from it never depends on anything
outside the root.

`toolchain.json` is the other half: the installer is the only writer, and the
skills read it instead of guessing paths.  It is a *hint*, never an authority --
consumers re-validate every entry and fall back when it does not hold.  The
document carries only the fields a consumer actually reads (``schema_version``,
and per tool ``state`` / ``runner`` / ``version`` plus the landing directory);
everything else belongs in the install log, because a field nobody reads still
has to be kept in sync forever.

Two boundaries are deliberate, not gaps:

* **The interpreter behind an ``npm-private`` tool is the host's Node.**  Only
  Python is bundled, because a Python venv is the one artifact that re-execs a
  base interpreter recorded outside itself and so cannot survive a
  path-selective sandbox.  ``node`` + ``<abs>/cli.js`` has no such chain: the
  host ``node.exe`` is reachable from the measured sandboxes, and gitnexus's
  native modules are all N-API (``prebuilds/<platform>-<arch>``, ``bin/napi-v6``),
  hence stable across Node majors.  Bundling Node later is a ``RUNTIME_SPECS``
  entry away, not a redesign.
* **What each host has registered is measured, not unified.**  ``gitnexus setup``
  decides how it writes Claude's and Codex's MCP entry; CodeKit owns only the
  Qoder registration.  The same tool can therefore carry a different launch
  expression per host, so a report must quote what the host in question actually
  got.  The manifest states CodeKit's own measured expression, nothing more.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
import installer_platform as platform_layer  # noqa: E402

# 与 installer_state 同一个模块名：注册表在进程里只有一份实例，「组件的 entry_kind
# 与通道」在清单构建处和状态检测处才是同一件事。
registry = config.load_module(SCRIPT_DIR / "component-registry.py", "codekit_component_registry")

# python-build-standalone ships self-contained CPython builds.  Only the
# ``install_only`` flavour is usable here: the Windows *embeddable* package has
# no ``venv`` and no ``ensurepip``, and the installer's own preflight requires
# both.
RUNTIME_REPO = "astral-sh/python-build-standalone"
# 有内置运行时资产的 platform.key 集合。资产名里的三元组片段（pc-windows-msvc 等）
# 已经写在 RUNTIME_SPECS 的 asset 里，这里不再抄第二遍。
RUNTIME_OS_KEYS = (platform_layer.WINDOWS, platform_layer.LINUX, platform_layer.MACOS)
RUNTIME_ARCH_TOKENS = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}
MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RuntimeSpec:
    """A pinned runtime release asset.

    ``sha256`` is the verified digest of the archive.  An empty digest means the
    pin has not been resolved yet: the download still happens, but it is
    unverified, which is why :func:`ensure_runtime` reports it as a warning and
    :func:`describe_signature` exists to fill the value in.
    """

    tag: str
    asset: str
    sha256: str = ""


# Pinned per (os, arch), at python-build-standalone release ``20260901``
# (CPython 3.13.15, ~45 MB per asset).  Bump deliberately: a new tag means a new
# download and a rebuild of every venv, so it is a release decision rather than a
# side effect of re-running the installer.  Digests are the release's own asset
# digests; resolve a replacement with ``--describe <archive>``.
RUNTIME_SPECS: dict[tuple[str, str], RuntimeSpec] = {
    ("windows", "x86_64"): RuntimeSpec(
        tag="20260901",
        asset="cpython-3.13.15+20260901-x86_64-pc-windows-msvc-install_only.tar.gz",
        sha256="9bcc038a0bf180612ed56dec93d4977d035e80b8d9320ef51a38c287baf134b7",
    ),
    ("linux", "x86_64"): RuntimeSpec(
        tag="20260901",
        asset="cpython-3.13.15+20260901-x86_64-unknown-linux-gnu-install_only.tar.gz",
        sha256="0651dd7157d3debf769e15a52c1de9de7fbcdc36ba72faf79fde3c44f14d9461",
    ),
    ("linux", "aarch64"): RuntimeSpec(
        tag="20260901",
        asset="cpython-3.13.15+20260901-aarch64-unknown-linux-gnu-install_only.tar.gz",
        sha256="76ed18125286d7dc96ce24023d1e319dbd55a89a767102411b1ea23846113f69",
    ),
    ("macos", "x86_64"): RuntimeSpec(
        tag="20260901",
        asset="cpython-3.13.15+20260901-x86_64-apple-darwin-install_only.tar.gz",
        sha256="49f0d97f506b855eed60b74a8ac138595c5b39799a6aa5e0d7ca8abe1019a4d4",
    ),
    ("macos", "aarch64"): RuntimeSpec(
        tag="20260901",
        asset="cpython-3.13.15+20260901-aarch64-apple-darwin-install_only.tar.gz",
        sha256="b9054a9d3d54f4cb5573d44907fddb29874b08909bde73f29f2868cf872223ee",
    ),
}

RUNTIME_OK = "available"
RUNTIME_INCOMPLETE = "incomplete"
RUNTIME_ABSENT = "absent"


@dataclass
class RuntimeState:
    """Outcome of :func:`ensure_runtime`."""

    state: str = RUNTIME_ABSENT
    python: Optional[Path] = None
    version: str = ""
    tag: str = ""
    sha256: str = ""
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.state == RUNTIME_OK and self.python is not None

    def require_python(self) -> Path:
        """可用的内置解释器；不可用时抛错。

        这里没有回退项是刻意的：用工具链根外的解释器建出来的 venv，在受限宿主里连
        基础解释器都读不到（实测 rc=101），装出一个跑不起来的副本比如实失败更糟。
        所以「要建 venv 就必须有内置运行时」由返回类型表达，而不是由调用点的注释表达。
        """
        if self.state != RUNTIME_OK or self.python is None:
            raise ValueError(self.reason or "内置 Python 运行时不可用")
        return self.python


def runtime_spec(platform, arch: Optional[str] = None) -> Optional[RuntimeSpec]:
    """Resolve the pinned asset for this platform, or None when unsupported."""
    if platform.key not in RUNTIME_OS_KEYS:
        return None
    return RUNTIME_SPECS.get((platform.key, runtime_arch_token(arch)))


def runtime_arch_token(arch: Optional[str] = None) -> str:
    """机器名 → 支持矩阵里的架构标记；认不出时返回空串。"""
    machine = (arch or platform_module.machine()).lower()
    return RUNTIME_ARCH_TOKENS.get(machine, "")


def supported_runtime_targets() -> tuple[str, ...]:
    """内置运行时覆盖的 ``<os>/<arch>`` 组合；pin 表就是支持矩阵，不再另存一份。"""
    return tuple(f"{os_key}/{arch_key}" for os_key, arch_key in sorted(RUNTIME_SPECS))


def unsupported_runtime_reason(platform, arch: Optional[str] = None) -> str:
    """不在支持矩阵里时的说法：点名矩阵，而不是含糊的「找不到资产」。

    机器名按宿主自己声称的那样报（``arm64`` 而不是归一后的 ``aarch64``）：读这条理由的
    人是在 ``uname -m`` 的输出里看到过它的人。矩阵一侧继续用归一后的键。
    """
    machine = (arch or platform_module.machine()).lower()
    return (
        f"CodeKit 不为 {platform.key}/{machine} 内置 Python 运行时"
        f"（支持矩阵：{'、'.join(supported_runtime_targets())}）。"
        "需要它的组件不会退到工具链根外的解释器，因此如实失败。"
    )


def runtime_archive_url(spec: RuntimeSpec) -> str:
    """Release asset URL.

    The asset name carries a literal ``+`` which the release API itself reports
    percent-encoded; quoting keeps the request identical to the published URL
    instead of relying on a server tolerating the bare character.
    """
    quoted = urllib.parse.quote(spec.asset, safe="")
    return f"https://github.com/{RUNTIME_REPO}/releases/download/{spec.tag}/{quoted}"


def _read_current(home: Path, platform) -> str:
    """Tag recorded as active, or an empty string when unreadable."""
    try:
        document = config.read_json(platform.runtime_current(home))
    except ValueError:
        return ""
    if not isinstance(document, dict):
        return ""
    tag = document.get("tag")
    return tag if isinstance(tag, str) else ""


def _write_current(home: Path, platform, tag: str, python: Path) -> None:
    """发布当前生效的运行时。只写有读者的字段：``tag`` 与 ``python``。

    ``schema_version`` 保留为兼容位。资产名、摘要与版本属于安装日志，写在这里就得
    永远有人负责同步它们，而没有任何读者。
    """
    config.write_json(
        platform.runtime_current(home),
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tag": tag,
            "python": str(python),
        },
    )


def _probe_runtime(python: Path) -> tuple[bool, str]:
    """Cheap reuse probe: the interpreter must start and expose venv/ensurepip."""
    try:
        completed = subprocess.run(
            [str(python), "-c", "import venv, ensurepip, sys; print(sys.version.split()[0])"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
            **platform_layer.text_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if completed.returncode != 0:
        return False, ""
    lines = (completed.stdout or "").strip().splitlines()
    return True, (lines[-1] if lines else "")


def _extract_archive(archive: Path, destination: Path) -> None:
    """Extract a tar.gz/zip asset into ``destination``."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            package.extractall(destination)
        return
    with tarfile.open(archive) as package:
        try:
            package.extractall(destination, filter="data")
        except TypeError:
            # Python < 3.12 has no extraction filter.
            package.extractall(destination)


def _interpreter_root(staging: Path, platform) -> Optional[Path]:
    """Locate the ``python/`` root inside an extracted runtime archive.

    The ``install_only`` layout is ``python/`` at the top level, but that is an
    upstream convention rather than part of any contract, so it is located by
    finding the interpreter itself instead of assumed.
    """
    name = platform.binary_filename("python") if platform.is_windows else "python3"
    for candidate in sorted(staging.rglob(name)):
        root = candidate.parent if platform.is_windows else candidate.parent.parent
        if root.is_dir() and root.name == "python":
            return root
    return None


def _install_archive(
    installer, spec: RuntimeSpec, archive: Path, target: Path
) -> tuple[bool, str]:
    """Extract ``archive`` and atomically publish it as ``target``."""
    platform = installer.platform
    staging_parent = platform.runtime_root(installer.home)
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".staging-{spec.tag}-", dir=str(staging_parent))
    )
    try:
        raw = staging / "raw"
        raw.mkdir()
        try:
            _extract_archive(archive, raw)
        except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
            return False, f"运行时压缩包无法解开：{exc}"
        root = _interpreter_root(raw, platform)
        if root is None:
            return False, "运行时压缩包里找不到解释器"
        staged_python = staging / "python"
        try:
            os.replace(root, staged_python)
        except OSError as exc:
            return False, f"运行时目录无法归一：{exc}"
        platform_layer.rmtree(raw, ignore_errors=True)
        executable = platform.runtime_python(staging)
        if not executable.is_file():
            return False, f"归一后的运行时缺少解释器：{executable}"
        if not platform.is_windows:
            for binary in staged_python.rglob("*"):
                if binary.is_file() and binary.parent.name == "bin":
                    binary.chmod(binary.stat().st_mode | 0o755)
        # 冒烟测试必须在发布之前做完整：一个 starts-but-cannot-create-venv 的运行时
        # 一旦被发布，后续每次安装都会在更晚、更难归因的地方失败。
        ok, version = _probe_runtime(executable)
        if not ok:
            return False, "运行时冒烟测试失败（import venv, ensurepip）"
        if not _venv_smoke_test(installer, executable, staging):
            return False, "运行时无法创建 venv（冒烟测试失败）"
        if target.exists():
            if not target.is_dir():
                return False, f"运行时目标已存在且不是目录：{target}"
            platform_layer.rmtree(target, ignore_errors=True)
        os.replace(staging, target)
        published = platform.runtime_python(target)
        return True, version if published.is_file() else ""
    finally:
        platform_layer.rmtree(staging, ignore_errors=True)


def _venv_smoke_test(installer, python: Path, parent: Path) -> bool:
    """A throwaway venv must be creatable; the bundled runtime is the only base."""
    probe = Path(tempfile.mkdtemp(prefix=".venv-smoke-", dir=str(parent)))
    try:
        return installer.run_command(
            [str(python), "-m", "venv", "--without-pip", str(probe)],
            quiet=True,
            timeout=180,
        ).returncode == 0
    finally:
        platform_layer.rmtree(probe, ignore_errors=True)


def ensure_runtime(installer, *, arch: Optional[str] = None) -> RuntimeState:
    """Make a usable bundled interpreter available, or report why not.

    Idempotent and offline-safe: an already-published runtime is reused without
    touching the network, which is what makes re-running the installer on a
    machine without egress a no-op rather than a failure.
    """
    platform = installer.platform
    spec = runtime_spec(platform, arch)
    if spec is None:
        return RuntimeState(
            state=RUNTIME_ABSENT, reason=unsupported_runtime_reason(platform, arch)
        )
    target = platform.runtime_dir(installer.home, spec.tag)
    executable = platform.runtime_python(target)
    active = _read_current(installer.home, platform)

    if target.is_dir() and executable.is_file():
        ok, version = _probe_runtime(executable)
        if ok:
            warnings: list[str] = []
            if active and active != spec.tag:
                warnings.append(
                    f"current.json 记录的 tag 是 {active}，与本次固定的 {spec.tag} 不一致，已按 {spec.tag} 更新。"
                )
            _write_current(installer.home, platform, spec.tag, executable)
            return RuntimeState(
                state=RUNTIME_OK,
                python=executable,
                version=version,
                tag=spec.tag,
                sha256=spec.sha256,
                warnings=warnings,
            )
        installer.logger.warn(f"已存在的运行时无法启动，将重新获取：{executable}")

    installer.logger.info(f"获取内置 Python 运行时 {spec.asset}")
    archive = installer.download_release(runtime_archive_url(spec), spec.sha256)
    if archive is None:
        kept = _previous_runtime(installer, active)
        if kept is not None:
            return RuntimeState(
                state=RUNTIME_OK,
                python=kept,
                tag=active,
                reason="本次未能下载新的运行时，沿用上一个可用版本",
                warnings=["运行时未更新：下载或校验失败，已沿用上一个可用版本。"],
            )
        return RuntimeState(
            state=RUNTIME_ABSENT,
            reason="运行时下载失败，且本机没有可用的旧版本",
        )
    if not spec.sha256:
        installer.logger.warn(
            f"{spec.asset} 未固定 sha256，本次下载未经校验。"
            "请在 RUNTIME_SPECS 里补上摘要（见 describe_signature）。"
        )
    try:
        ok, version = _install_archive(installer, spec, archive, target)
    finally:
        archive.unlink(missing_ok=True)
    if not ok:
        kept = _previous_runtime(installer, active)
        if kept is not None:
            return RuntimeState(
                state=RUNTIME_OK,
                python=kept,
                tag=active,
                reason=version,
                warnings=[f"运行时更新失败（{version}），已沿用上一个可用版本。"],
            )
        return RuntimeState(state=RUNTIME_INCOMPLETE, reason=version)
    published = platform.runtime_python(target)
    if not version or not published.is_file():
        return RuntimeState(
            state=RUNTIME_INCOMPLETE,
            reason="运行时已解包但发布后的解释器不可用，未更新 current.json",
        )
    _write_current(installer.home, platform, spec.tag, published)
    installer.track(platform.runtime_root(installer.home))
    return RuntimeState(
        state=RUNTIME_OK,
        python=published,
        version=version,
        tag=spec.tag,
        sha256=spec.sha256,
    )


def _previous_runtime(installer, active: str) -> Optional[Path]:
    """Interpreter of the previously active runtime, if it still works."""
    if not active:
        return None
    executable = installer.platform.runtime_python(
        installer.platform.runtime_dir(installer.home, active)
    )
    if not executable.is_file():
        return None
    ok, _ = _probe_runtime(executable)
    return executable if ok else None


def describe_signature(archive: Path) -> str:
    """Print the pin line for a downloaded asset, for a human to commit.

    The digest of a release asset cannot be derived offline, so it is resolved
    once, by hand, and pinned in :data:`RUNTIME_SPECS`.  Never called from
    :meth:`Installer.run`.
    """
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return f'    sha256="{digest}",'


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def classify_state(path: Path) -> str:
    """`ok` / `not-installed` / `not-readable` / `not-executable` for one path.

    Deliberately never uses ``Path.is_file``/``exists`` for the decision: CPython
    swallows ``OSError`` in those and returns ``False``, which is exactly how the
    old resolver lost the ability to tell "absent" from "denied".  The skills
    side carries the same classification in
    ``skills/_shared/scripts/codekit_runtime.py``: skills ship as a plugin and
    cannot import ``scripts/``, so that one is the copy they must keep; parity
    between the two is asserted by ``SharedResolverParityTests`` rather than
    claimed by a comment.
    """
    try:
        os.stat(path)
    except FileNotFoundError:
        return "not-installed"
    except NotADirectoryError:
        return "not-installed"
    except PermissionError:
        return "not-readable"
    except OSError:
        return "not-readable"
    return "ok"


def readable_state(path: Path) -> str:
    """State of a path whose contents must also be readable, not just stat-able.

    目录只看 ``stat``：Windows 上 ``open(目录)`` 一律抛 EACCES，把它当成「被拒绝读取」
    会把完全可用的路径标成不可读。

    打不开但不是权限问题的入口（典型是 ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` 里的
    商店别名桩：``stat`` 能过、``open`` 抛 EINVAL）记为 ``not-executable``——它确实存在，
    只是不可用；报 ``not-readable`` 会把「这不是个真程序」说成「宿主不让读」。判定与
    ``codekit_runtime.readable_state`` 逐分支一致，由 ``SharedResolverParityTests`` 断言。
    """
    state = classify_state(path)
    if state != "ok":
        return state
    if path.is_dir():
        return state
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except PermissionError:
        return "not-readable"
    except OSError:
        return "not-executable"
    return "ok"


def npm_prefix_probe(
    platform, home: Path, component
) -> tuple[str, Path, Optional[Path], Optional[str]]:
    """``npm-private`` 落点的实测态：``(state, package_root, cli, node)``。

    只做 ``stat`` 与一次 PATH 查找，不起子进程：清单构建器与安装状态检测共用这一份
    判据，而检测侧不该为每个组件付一次 ``--version`` 进程开销。runner 里的 ``node``
    是宿主解释器（这条边界见模块 docstring），按名字解析一次后把绝对路径写进清单。
    """
    package_root = (
        platform.npm_prefix_private(home) / "node_modules" / component.channels["npm"]
    )
    state = classify_state(package_root)
    if state != "ok":
        return state, package_root, None, None
    cli = _package_bin(package_root, component.command)
    node = platform_layer.resolve_exe("node")
    if node is None:
        return "not-executable", package_root, cli, None
    if cli is None:
        return "not-installed", package_root, None, node
    return "ok", package_root, cli, node


def npm_prefix_entry(installer, component) -> dict[str, Any]:
    """``npm-private`` 条目：runner 记成 ``[<绝对 node>, <绝对 cli.js>]``。

    npm 生成的 shim 在沙箱下不可读，裸 ``node`` 在那里也按名字解析不出来，而
    ``C:\\Program Files\\nodejs\\node.exe`` 可以直接执行，所以存的是这一对绝对路径。
    """
    state, package_root, cli, node = npm_prefix_probe(
        installer.platform, installer.home, component
    )
    return {
        "state": state,
        "runner": [str(node), str(cli)] if state == "ok" else [],
        "version": _package_version(package_root),
        "package_root": str(package_root),
    }


def _package_json(package_root: Path) -> dict[str, Any]:
    try:
        document = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _package_bin(package_root: Path, command: str) -> Optional[Path]:
    """Resolve the CLI entry declared by the package's own ``bin`` field."""
    document = _package_json(package_root)
    declared = document.get("bin")
    relative = ""
    if isinstance(declared, str):
        relative = declared
    elif isinstance(declared, dict):
        value = declared.get(command)
        relative = value if isinstance(value, str) else ""
    if not relative:
        return None
    candidate = (package_root / relative).resolve()
    return candidate if candidate.is_file() else None


def _package_version(package_root: Path) -> str:
    version = _package_json(package_root).get("version")
    return version if isinstance(version, str) else ""


def _venv_package_version(python: Path, package: str) -> str:
    """venv 里某个已安装包的真实版本；探测不到时返回空串。

    读 ``importlib.metadata`` 而不是解析 ``--version`` 的文本：入口的输出格式是第三方
    CLI 的自由，改一次措辞清单里的版本就会静默变空。探测失败只留空，不算安装失败。
    """
    if not python.is_file():
        return ""
    try:
        completed = subprocess.run(
            [str(python), "-c", f"import importlib.metadata as m; print(m.version({package!r}))"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
            **platform_layer.text_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def venv_probe(platform, home: Path, component) -> tuple[str, Path, Optional[Path]]:
    """``python-venv`` 落点的实测态：``(state, venv, executable)``；不起子进程。

    入口能 ``stat`` 还不够：venv 的 ``python.exe`` 只是转发器，``pyvenv.cfg`` 的基础
    解释器落在读不到的目录时它一定起不来（实测 rc=101），所以这里连带判定。
    """
    venv = platform.venv_dir(home, component.command)
    executable = platform.venv_exe(venv, component.command)
    state = readable_state(executable)
    if state != "ok":
        return state, venv, executable
    base = venv_base_state(venv)
    if base["state"] != "ok":
        return base["state"], venv, executable
    return "ok", venv, executable


def venv_entry(installer, component) -> dict[str, Any]:
    """``python-venv`` 条目。

    The venv must be built from the bundled runtime; the previous venv was built
    from the system Python and therefore could not start inside the sandbox.
    """
    platform = installer.platform
    state, venv, executable = venv_probe(platform, installer.home, component)
    venv_python = platform.venv_exe(venv, "python")
    return {
        "state": state,
        "runner": [str(executable)] if state == "ok" else [],
        "version": (
            _venv_package_version(venv_python, component.channels["venv"])
            if state == "ok"
            else ""
        ),
        "venv": str(venv),
    }


# entry_kind → 清单 argv 构建器。新增一个根接管工具时，这里是唯一的登记点。
ENTRY_BUILDERS = {
    "npm-private": npm_prefix_entry,
    "python-venv": venv_entry,
}
# entry_kind → 落点实测（不起子进程）。清单条目与安装状态检测必须出自同一个判据，
# 否则会出现「清单说可用、组件状态说未安装」这类互相打架的结论。
ENTRY_PROBES = {
    "npm-private": npm_prefix_probe,
    "python-venv": venv_probe,
}


def entry_state(platform, home: Path, component) -> str:
    """某个根接管组件在本机的实测落点态。"""
    probe = ENTRY_PROBES.get(getattr(component, "entry_kind", ""))
    if probe is None:
        raise ValueError(
            f"{getattr(component, 'component_id', '?')} 没有 entry_kind 对应的落点探测"
        )
    return str(probe(platform, home, component)[0])


def entry(installer, component) -> dict[str, Any]:
    """按组件的 ``entry_kind`` 构建 ``toolchain.json`` 里的那一条。"""
    builder = ENTRY_BUILDERS.get(getattr(component, "entry_kind", ""))
    if builder is None:
        raise ValueError(
            f"{getattr(component, 'component_id', '?')} 没有 entry_kind 对应的清单构建器"
        )
    return builder(installer, component)


def venv_base_state(venv: Path) -> dict[str, Any]:
    """Whether the interpreter a venv re-execs is reachable.

    A venv's ``python.exe`` is only a redirector: it reads ``pyvenv.cfg`` and
    starts ``home``/``executable`` from there.  When those live outside what the
    session may read, the venv is present but unlaunchable, which is the
    difference between ``not-readable`` and ``not-installed`` that the report
    must be able to express.
    """
    values: dict[str, str] = {}
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # EACCES/EPERM 是「被拒绝读取」；ENOENT/ENOTDIR/EINVAL 是「这里没有可用的
        # venv」。判据与 skills/_shared/scripts/codekit_runtime.venv_base_state 完全
        # 一致：同一份事实在两侧必须得到同一个态，否则清单与技能结论会互相打架。
        state = "not-readable" if exc.errno in {13, 1} else "not-installed"
        return {"state": state, "values": values}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().lower()] = value.strip()
    targets = [values.get(key, "") for key in ("executable", "home")]
    if not any(targets):
        return {"state": "not-executable", "values": values}
    for raw in targets:
        if not raw:
            continue
        state = classify_state(Path(raw))
        if state != "ok":
            return {"state": state, "values": values}
    return {"state": "ok", "values": values}


def write_manifest(installer, *, selected: Sequence[str] = ()) -> Optional[Path]:
    """Write ``toolchain.json`` and return its path.

    字段只保留有读者的：``schema_version`` 与每个工具的 ``state`` / ``runner`` /
    ``version`` / 落点目录。运行时的 tag 与解释器路径由 ``current.json`` 承载，警告
    由安装日志承载——在同一份文件里再抄一遍，只会多出一个需要长期同步、却没人读的副本。

    Only tools the installer actually manages are recorded: an entry for a tool
    CodeKit never installed would read as authoritative while describing
    something it does not own.
    """
    platform = installer.platform
    root = platform.toolchain_root(installer.home)
    if not root.is_dir():
        return None
    tools: dict[str, Any] = {}
    for component_id in selected:
        component = registry.component_by_id(component_id)
        if component is None or not component.entry_kind:
            continue
        tools[component_id] = entry(installer, component)
    document = {"schema_version": MANIFEST_SCHEMA_VERSION, "tools": tools}
    target = platform.toolchain_manifest(installer.home)
    config.write_json(target, document)
    if os.name != "nt":
        # 清单里是工具链根的绝对路径与各版本，同机其它用户没有读它的理由。
        os.chmod(target, 0o600)
    installer.track(target)
    return target


def read_manifest(path: Path) -> dict[str, Any]:
    """Read ``toolchain.json``; an unusable file degrades to an empty mapping."""
    try:
        document = config.read_json(path)
    except ValueError:
        return {}
    if not isinstance(document, dict):
        return {}
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return {}
    return document


def prune_manifest(path: Path, removed: Sequence[str]) -> bool:
    """把已卸载的工具在清单里降级为 ``not-installed``。

    卸载不能重写整份清单（那要重装才写得对），但留着一条 ``ok`` 更糟：技能侧把它当成
    可采信的证据，直到下一次安装为止。条目本身保留——「装过又卸了」和「从来没这个
    工具」是两件事，降级而不是删除就是为了让两者还能分开。
    """
    document = read_manifest(path)
    tools = document.get("tools")
    if not isinstance(tools, dict) or not removed:
        return False
    changed = False
    for component_id in removed:
        record = tools.get(component_id)
        if not isinstance(record, dict) or record.get("state") == "not-installed":
            continue
        record["state"] = "not-installed"
        record["runner"] = []
        record["version"] = ""
        changed = True
    if not changed:
        return False
    config.write_json(path, document)
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Diagnostic entry point: describe or hash a runtime asset.

    Never part of :meth:`Installer.run`; it exists so the pinned digest in
    :data:`RUNTIME_SPECS` can be produced and reviewed by a human.
    """
    import argparse

    parser = argparse.ArgumentParser(description="CodeKit bundled runtime helper")
    parser.add_argument("--describe", type=Path, metavar="ARCHIVE", help="print the sha256 pin line for an asset")
    parser.add_argument("--list", action="store_true", help="list pinned runtime assets")
    args = parser.parse_args(argv)
    if args.describe is not None:
        print(describe_signature(args.describe))
        return 0
    for (os_key, arch_key), spec in sorted(RUNTIME_SPECS.items()):
        pinned = "pinned" if spec.sha256 else "UNPINNED"
        print(f"{os_key}/{arch_key}: {spec.asset} ({pinned})")
        print(f"  {runtime_archive_url(spec)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
