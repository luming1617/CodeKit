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
consumers re-validate every entry and fall back when it does not hold.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
import installer_platform as platform_layer  # noqa: E402

# python-build-standalone ships self-contained CPython builds.  Only the
# ``install_only`` flavour is usable here: the Windows *embeddable* package has
# no ``venv`` and no ``ensurepip``, and the installer's own preflight requires
# both.
RUNTIME_REPO = "astral-sh/python-build-standalone"
RUNTIME_OS_TOKENS = {
    "windows": "pc-windows-msvc",
    "linux": "unknown-linux-gnu",
    "macos": "apple-darwin",
}
RUNTIME_ARCH_TOKENS = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}
MANIFEST_SCHEMA_VERSION = 1
# PyPI 上的发行名与命令名不同（``graphifyy`` 提供 ``graphify``）。
GRAPHIFY_PACKAGE = "graphifyy"


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


def runtime_spec(platform, arch: Optional[str] = None) -> Optional[RuntimeSpec]:
    """Resolve the pinned asset for this platform, or None when unsupported."""
    os_key = "windows" if platform.is_windows else ("macos" if platform.os_name == "Darwin" else "linux")
    if os_key not in RUNTIME_OS_TOKENS:
        return None
    machine = (arch or platform_module.machine()).lower()
    arch_key = RUNTIME_ARCH_TOKENS.get(machine)
    if arch_key is None:
        return None
    return RUNTIME_SPECS.get((os_key, arch_key))


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


def _write_current(home: Path, platform, spec: RuntimeSpec, python: Path, version: str) -> None:
    config.write_json(
        platform.runtime_current(home),
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "tag": spec.tag,
            "asset": spec.asset,
            "sha256": spec.sha256,
            "python": str(python),
            "version": version,
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
            state=RUNTIME_ABSENT,
            reason=f"没有适配 {platform.os_name} 的运行时资产",
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
            _write_current(installer.home, platform, spec, executable, version)
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
    _write_current(installer.home, platform, spec, published, version)
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
    ``skills/_shared/scripts/codekit_runtime.py``; the two are kept in step by a
    contract test rather than by code sharing, because the skills ship as a
    plugin while this module does not.
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


def gitnexus_entry(installer) -> dict[str, Any]:
    """Manifest entry for GitNexus.

    The runner is recorded as ``[<abs node>, <abs cli.js>]``: the npm-generated
    shim is unreadable under the sandbox, and a bare ``node`` is not resolvable
    by name there either, while ``C:\\Program Files\\nodejs\\node.exe`` executes.
    """
    platform = installer.platform
    package_root = platform.npm_prefix_private(installer.home) / "node_modules" / "gitnexus"
    entry: dict[str, Any] = {
        "kind": "node-cli",
        "state": "absent",
        "runner": [],
        "shim": None,
        "package_root": str(package_root),
        "version": "",
        "source": "npm-private-prefix",
    }
    state = classify_state(package_root)
    if state != "ok":
        entry["state"] = state
        return entry
    cli = _package_bin(package_root, "gitnexus")
    node = platform_layer.resolve_exe("node")
    if cli is None or node is None:
        entry["state"] = "not-executable" if node is None else "not-installed"
        return entry
    entry.update(
        {
            "state": "ok",
            "runner": [node, str(cli)],
            "shim": str(platform.npm_prefix_private(installer.home) / platform.shim_name("gitnexus")),
            "node": {"path": node, "version": installer.command_version("node", ["-v"])},
            "version": _package_version(package_root),
            "identity_stable": True,
            "local_embeddings": (package_root / "node_modules" / "onnxruntime-node").is_dir(),
        }
    )
    return entry


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


def graphify_entry(installer) -> dict[str, Any]:
    """Manifest entry for Graphify.

    Its venv must be built from the bundled runtime; the previous venv was built
    from the system Python and therefore could not start inside the sandbox.
    ``pyvenv_cfg`` is recorded so a consumer can name the exact failure instead
    of reporting an opaque non-zero exit.
    """
    platform = installer.platform
    venv = platform.venv_dir(installer.home, "graphify")
    executable = platform.venv_exe(venv, "graphify")
    entry: dict[str, Any] = {
        "kind": "python-venv",
        "state": "absent",
        "runner": [],
        "venv": str(venv),
        "venv_python": str(platform.venv_exe(venv, "python")),
        "package": GRAPHIFY_PACKAGE,
        "version": "",
        "source": "venv",
    }
    state = readable_state(executable)
    if state != "ok":
        entry["state"] = state
        return entry
    base = venv_base_state(venv)
    entry["pyvenv_cfg"] = base["values"]
    if base["state"] != "ok":
        entry["state"] = base["state"]
        return entry
    entry["state"] = "ok"
    entry["runner"] = [str(executable)]
    entry["version"] = _venv_package_version(
        platform.venv_exe(venv, "python"), GRAPHIFY_PACKAGE
    )
    return entry


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


def write_manifest(
    installer, runtime: RuntimeState, *, selected: Sequence[str] = ()
) -> Optional[Path]:
    """Write ``toolchain.json`` and return its path.

    Only tools the installer actually manages are recorded: an entry for a tool
    CodeKit never installed would read as authoritative while describing
    something it does not own.
    """
    platform = installer.platform
    root = platform.toolchain_root(installer.home)
    if not root.is_dir():
        return None
    tools: dict[str, Any] = {}
    managed = set(selected)
    if "gitnexus" in managed:
        tools["gitnexus"] = gitnexus_entry(installer)
    if "graphify" in managed:
        tools["graphify"] = graphify_entry(installer)
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "installer_version": installer.plugin_version,
        "root": str(root),
        "platform": {
            "os": "windows" if platform.is_windows else ("macos" if platform.os_name == "Darwin" else "linux"),
            "arch": _arch(),
            "key": platform.key,
        },
        "runtime": {
            "state": runtime.state,
            "tag": runtime.tag,
            "python": str(runtime.python) if runtime.python else None,
            "version": runtime.version,
            "sha256": runtime.sha256,
            "venv_ok": runtime.usable,
        },
        "tools": tools,
        "warnings": list(runtime.warnings),
    }
    target = platform.toolchain_manifest(installer.home)
    config.write_json(target, document)
    if os.name != "nt":
        # 清单里是工具链根的绝对路径与各版本，同机其它用户没有读它的理由。
        os.chmod(target, 0o600)
    installer.track(target)
    return target


def _arch() -> str:
    machine = platform_module.machine().lower()
    return RUNTIME_ARCH_TOKENS.get(machine, machine)


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
