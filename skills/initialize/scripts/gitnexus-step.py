#!/usr/bin/env python3
"""Deterministic GitNexus step for initialize.

GitNexus 这一路没有需要 LLM 判断的成分：按冻结的 runner 与既有索引模式执行
``status`` / ``analyze``，再申报写入范围。因此它不再作为独立 Sub-agent 派发，
而由控制器直接执行。产出与 subagent 完全相同的 Worker result schema，Phase 2
的门禁、reconcile 与 manifest 结构都不需要改动。

参数（``--pdg``、``--drop-embeddings``、``--registry real|disposable`` 等）一律由
manifest 推导，避免由模型拼装标志位而产生模式漂移。

注册不再是「先问人再写」的预检门槛，而是**先真的尝试一次**：``gitnexus index
<root>`` 幂等且只动 ``registry.json``，成功即继续、宿主拒绝才升级申请。判定只认可
真实写入的结果（``run-gitnexus.py`` 的 ``--registry real`` 返回 77），因为预检用的
``os.access`` 在按路径拒绝访问的沙箱里会报可写。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from _common import (
    ERROR_CODE_EXECUTION_FAILED,
    ERROR_CODE_INDEX_CURRENT,
    ERROR_CODE_REGISTRY_MISSING,
    ERROR_CODE_TOOL_MISSING,
    InitializeError,
    atomic_json,
    git_status,
    gitnexus_registry_state,
    load_json,
    snapshot_trees,
    tool_runnable,
    utc_now,
)

STEP_TREES = (".gitnexus", ".claude/skills", ".agents/skills")
ANALYZE_BASE = ("analyze", "--skills", "--skip-agents-md", "--verbose")
WARNING_PREFIX = "initialize warning:"
REGISTRY_WARNING = (
    "GitNexus 索引未注册到全局 registry（{path}）：list/query/context/impact/trace/"
    "cypher/check/detect-changes 与 MCP 图查询都会报 “No indexed repositories found”。"
)


REGISTRY_DENIED_EXIT = 77
REGISTRY_INDEX_COMMAND = "index"


class RegistryWriteDenied(InitializeError):
    """The host refused the single out-of-repo write (``registry.json``).

    Not a failed run: the write either succeeds on a retry with the host's
    escalation channel or the user declines, so the caller reports it as
    recoverable and keeps the run and its lock.
    """

    def __init__(
        self,
        *,
        registry_path: str,
        exit_code: int,
        retry_command: list[str],
        justification: str,
    ) -> None:
        super().__init__(
            f"GitNexus 全局注册表 {registry_path} 需要一次仓库之外写入，宿主拒绝了该写入"
            f"（退出码 {exit_code}）"
        )
        self.registry_path = registry_path
        self.exit_code = exit_code
        self.retry_command = list(retry_command)
        self.justification = justification


def registry_warning(root: Path) -> str:
    return REGISTRY_WARNING.format(path=gitnexus_registry_state(root)["path"])


def tool_snapshot(manifest: dict) -> dict:
    tool = manifest.get("tools", {}).get("gitnexus", {})
    return tool if isinstance(tool, dict) else {}


def global_writes_authorized(manifest: dict) -> bool:
    options = manifest.get("options")
    options = options if isinstance(options, dict) else {}
    return bool(options.get("allow_global_writes"))


def allowed_paths(root: Path, tool: dict) -> list[str]:
    """本路可写的路径：``.gitnexus/`` 与探测到的具体 GitNexus skills 目录。"""
    allowed = [".gitnexus/"]
    supports = tool.get("supports")
    supports = supports if isinstance(supports, dict) else {}
    if not supports.get("skills"):
        return allowed
    for relative in (".claude/skills", ".agents/skills"):
        directory = root / relative
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.is_dir() and child.name.startswith("gitnexus"):
                allowed.append(child.relative_to(root).as_posix() + "/")
    return allowed


def analyze_argv(manifest: dict, embedding_mode: str) -> tuple[list[str], list[str]]:
    """按冻结的索引模式与 embedding 策略拼装 analyze 参数。"""
    tool = tool_snapshot(manifest)
    argv = [*tool.get("command", []), *ANALYZE_BASE]
    warnings: list[str] = []
    index = manifest.get("gitnexus_index")
    index = index if isinstance(index, dict) else {}
    if index.get("pdg"):
        argv.append("--pdg")
    options = manifest.get("options")
    options = options if isinstance(options, dict) else {}
    if options.get("embeddings"):
        argv.append("--embeddings")
    if options.get("force"):
        argv.append("--force")
    if embedding_mode == "drop":
        argv.append("--drop-embeddings")
        warnings.append(
            "已追加 --drop-embeddings：当前 runner 缺少本地 embedding 栈，"
            "放弃语义向量以换取可完成的分析。"
        )
    supports = tool.get("supports")
    supports = supports if isinstance(supports, dict) else {}
    if manifest.get("gitnexus_skill_mode") == "community-only" and supports.get("skip_skills"):
        argv.append("--skip-skills")
        warnings.append(
            "GitNexus skills 镜像目录不可写：已追加 --skip-skills，仅保留社区 skills。"
        )
    return argv, warnings


def invoke(skill_dir: Path, root: Path, command: list[str], *, registry: str = "disposable"):
    """经 run-gitnexus adapter 执行；``registry`` 决定是否动用真实全局目录。"""
    wrapper = skill_dir / "scripts" / "run-gitnexus.py"
    argv = [
        sys.executable, str(wrapper), "--root", str(root), "--registry", registry, "--", *command
    ]
    return subprocess.run(argv, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)


def registration_retry_command(manifest_path: Path, skill_dir: Path) -> list[str]:
    """升级重试用的同一条命令：只做那次仓库外写入，别的一律不碰。"""
    return [
        sys.executable,
        str(skill_dir / "scripts" / "controller.py"),
        "register",
        "--manifest",
        str(manifest_path),
    ]


def denied_write_hint(root: Path, manifest_path: Path, skill_dir: Path, denied: list[str]) -> str:
    """宿主拒绝了某次仓库外写入时的可操作说明：目标文件、失败的命令、重试路径。

    ``analyze`` 与 ``embeddings install`` 的被拒不是 ``attempt_registration`` 抛出的
    那条 ``RegistryWriteDenied``（它们已经被调用过，拿不到重试循环），所以这里必须把
    同样的信息补进 result，否则调用方只会看到「退出码 77」。
    """
    registry = gitnexus_registry_state(root)
    return (
        f"宿主拒绝了这次仓库外写入（{registry['path']}）：{' '.join(denied)} 未完成。"
        "按宿主提权通道重跑同一条命令；若只需要补注册，可只跑 "
        + " ".join(registration_retry_command(manifest_path, skill_dir))
        + "。"
    )


def attempt_registration(
    skill_dir: Path, root: Path, command: list[str], *, manifest_path: Path
) -> tuple[bool, list[str], int | None]:
    """真实尝试一次注册，返回 ``(registered, warnings, exit)``。

    ``gitnexus index <root>`` 只登记既有 ``.gitnexus/``，不重新分析，幂等且开销小，
    所以它是唯一值得先试的仓库外写入——成功就完全不需要人点头。宿主拒绝（adapter
    的退出码 77）与「GitNexus 自己失败」必须分开：前者抛出可恢复的
    ``RegistryWriteDenied``（保持 run 与锁，由主 Agent 走提权通道重试同一条命令），
    后者照常按运行失败处理。
    """
    completed = invoke(
        skill_dir, root, [*command, REGISTRY_INDEX_COMMAND, str(root)], registry="real"
    )
    forward(completed)
    if gitnexus_registry_state(root)["registered"]:
        return True, adapter_warnings(completed), completed.returncode
    if completed.returncode == REGISTRY_DENIED_EXIT:
        registry = gitnexus_registry_state(root)
        raise RegistryWriteDenied(
            registry_path=str(registry["path"]),
            exit_code=completed.returncode,
            retry_command=registration_retry_command(manifest_path, skill_dir),
            justification=(
                "需要把本仓库登记进 GitNexus 全局注册表（一次仓库之外的写入），否则索引"
                "有效但 list/query/context/impact/trace 等查询全部不可用。允许这次写入吗？"
            ),
        )
    return False, adapter_warnings(completed), completed.returncode


def resolve_missing_registration(
    manifest: dict,
    manifest_path: Path,
    skill_dir: Path,
    root: Path,
    command: list[str],
    warnings: list[str],
) -> tuple[str, list[str], int | None]:
    """缺注册时的唯一决策点，返回 ``(outcome, warnings, exit_code)``。

    这个判定被复用路径与 analyze 路径共用，两条都必须处理，否则仓库会长期停在
    「索引有效但查询全废」。

    - ``present``：已注册，无需动作。
    - ``registered``：本次尝试已把注册写进真实 ``GITNEXUS_HOME``。
    - ``partial``：不写注册表且不失败——用户已用 ``--allow-partial`` 明确接受降级，
      或尝试因非权限原因失败（那是环境事实，不是需要二次授权的取舍）。
    - ``failed``：用户已授权（``--allow-global-writes``）而注册仍不落地。
    """
    if gitnexus_registry_state(root)["registered"]:
        return "present", warnings, None
    options = manifest.get("options")
    options = options if isinstance(options, dict) else {}
    if options.get("allow_partial") and not options.get("allow_global_writes"):
        return "partial", [*warnings, registry_warning(root)], None
    registered, extra, exit_code = attempt_registration(
        skill_dir, root, command, manifest_path=manifest_path
    )
    warnings = [*warnings, *extra]
    if registered:
        return "registered", [*warnings, "索引已注册到 GitNexus 全局 registry。"], exit_code
    if options.get("allow_global_writes"):
        return "failed", [*warnings, registry_warning(root)], exit_code
    return (
        "partial",
        [
            *warnings,
            registry_warning(root),
            "尝试注册未成功（非权限原因），未写入全局注册表：命令 "
            f"{' '.join([*command, REGISTRY_INDEX_COMMAND, str(root)])} 退出码 {exit_code}。",
        ],
        exit_code,
    )


def adapter_warnings(completed: subprocess.CompletedProcess) -> list[str]:
    warnings: list[str] = []
    for line in (completed.stderr or "").splitlines():
        line = line.strip()
        if line.startswith(WARNING_PREFIX):
            warnings.append(line[len(WARNING_PREFIX):].strip())
    return warnings


def forward(completed: subprocess.CompletedProcess) -> None:
    """把 adapter 与 GitNexus 的输出透传给宿主，便于报告与排障。"""
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)


def meta_artifacts(root: Path) -> list[dict]:
    """完整复用与成功都要求携带可解析的 ``.gitnexus/meta.json``。"""
    if (root / ".gitnexus" / "meta.json").is_file():
        return [{"path": ".gitnexus/meta.json", "validation": "json-valid"}]
    return []


def parse_status(stdout: str) -> dict | None:
    """从 ``status --json`` 的输出里取状态；夹杂日志时取第一个 JSON 对象。"""
    text = stdout or ""
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload = json.loads(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def covered_by(raw: str, prefixes: list[str]) -> bool:
    """与门禁一致的路径包含判定：前缀必须按路径分量匹配。"""
    try:
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            return False
    except TypeError:
        return False
    for prefix_raw in prefixes:
        prefix = PurePosixPath(prefix_raw.rstrip("/"))
        if path == prefix or prefix in path.parents:
            return True
    return False


def changed_paths(
    root: Path, before_git: set[str], before_trees: dict, allowed: list[str]
) -> list[str]:
    """申报本步产生的变更：Git 可见的逐条，被 ignore 的产物按顶层条目。"""
    changed: set[str] = set()
    for path in set(git_status(root)) - before_git:
        if covered_by(path, allowed):
            changed.add(path)
    after_trees = snapshot_trees(root, list(STEP_TREES))
    for path in set(before_trees) | set(after_trees):
        if before_trees.get(path) == after_trees.get(path):
            continue
        if covered_by(path, allowed):
            changed.add(path)
    return sorted(changed)


def sealed(result: dict, started_clock: float, **fields) -> dict:
    result.update(fields)
    result["warnings"] = list(dict.fromkeys(result.get("warnings", [])))
    result["finished_at"] = utc_now()
    result["duration_ms"] = int((time.monotonic() - started_clock) * 1000)
    return result


def main(argv: list[str] | None = None, *, emit: bool = True) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    root = Path(manifest["root"]).resolve()
    if manifest.get("phase") != "prepared":
        raise InitializeError("gitnexus step requires a prepared run manifest")
    run_dir = Path(manifest["state_run_dir"])
    skill_dir = Path(__file__).resolve().parents[1]
    tool = tool_snapshot(manifest)
    command = [item for item in tool.get("command", []) if isinstance(item, str)]
    started_clock = time.monotonic()

    def registry_mode() -> str:
        """只有用户已授权才动用真实 ``GITNEXUS_HOME``。

        「已注册」不等于授权：``analyze`` 会在提交边界无条件重写 ``registry.json``，
        只读的 ``GITNEXUS_HOME`` 下这会直接失败，而既有的注册并不需要刷新——查询解析
        的入口指向仓库内 ``.gitnexus/``，不随一次 analyze 变化。补注册由
        ``attempt_registration`` 自己显式指定 ``real``，与这里的读/分析路径无关。
        """
        return "real" if global_writes_authorized(manifest) else "disposable"

    result: dict = {
        "schema_version": 1,
        "run_id": manifest.get("run_id"),
        "task": "gitnexus",
        "status": "failed",
        "required": True,
        "runner": "gitnexus",
        "command": [],
        "exit_code": None,
        "changed_files": [],
        "allowed_paths": [],
        "artifacts": [],
        "warnings": [],
        "started_at": utc_now(),
        "finished_at": utc_now(),
        "duration_ms": 0,
        "error": {"code": None, "message": None},
    }
    result_path = run_dir / "gitnexus-result.json"

    def publish(**fields) -> int:
        # 注册状态每次现测：它是查询路径能否解析到本仓库的唯一判据，不能缓存，
        # 也不能由退出码代替。
        registry = gitnexus_registry_state(root)
        result["registry"] = {
            "dir": registry["dir"],
            "registered": registry["registered"],
        }
        sealed(result, started_clock, **fields)
        atomic_json(result_path, result)
        if emit:
            print(json.dumps({
                "task": "gitnexus",
                "status": result["status"],
                "error_code": result["error"]["code"],
                "registry": result["registry"],
                "changed_files": result["changed_files"],
                "warnings": result["warnings"],
                "result": str(result_path),
            }, ensure_ascii=False, indent=2))
        return 0

    if not tool_runnable(tool):
        # 「入口解析到了」不等于「能执行」：沙箱拒绝读取入口或它依赖的解释器时，
        # 探测（``--version``）已经失败。这是本机跑不了，不是工具坏了，按
        # tool-missing 诚实跳过，让门禁走 partial 而不是把 run 拖进 execution-failed。
        hint = tool.get("resolution_hint")
        warnings = [hint] if isinstance(hint, str) and hint else []
        return publish(
            status="skipped",
            exit_code=None,
            error={"code": ERROR_CODE_TOOL_MISSING, "message": "GitNexus runner 不可用"},
            warnings=warnings,
        )

    before_git = set(git_status(root))
    before_trees = snapshot_trees(root, list(STEP_TREES))

    status_probe = invoke(
        skill_dir, root, [*command, "status", "--json"], registry=registry_mode()
    )
    probe_warnings = adapter_warnings(status_probe)
    payload = parse_status(status_probe.stdout) if status_probe.returncode == 0 else None
    # status 说 current 但没有 artifact 时不能声称复用：那会让门禁拿到不完整的
    # 「完整跳过」，所以退回到 analyze 自愈。
    if (
        isinstance(payload, dict)
        and payload.get("status") == "current"
        and (root / ".gitnexus" / "meta.json").is_file()
    ):
        # 复用路径是最容易漏掉注册的一支：analyze 不会运行，注册只能在这里补。
        warnings = list(probe_warnings)
        allowed = allowed_paths(root, tool)
        outcome, warnings, register_exit = resolve_missing_registration(
            manifest, args.manifest, skill_dir, root, command, warnings
        )
        if outcome == "partial":
            return publish(
                status="partial",
                exit_code=0,
                command=[*command, "status", "--json"],
                allowed_paths=allowed,
                artifacts=meta_artifacts(root),
                error={
                    "code": ERROR_CODE_REGISTRY_MISSING,
                    "message": "GitNexus 索引未注册到全局 registry，查询路径不可用",
                },
                warnings=warnings,
            )
        if outcome == "failed":
            return publish(
                status="failed",
                exit_code=register_exit if register_exit else 1,
                command=[*command, REGISTRY_INDEX_COMMAND, str(root)],
                allowed_paths=allowed,
                changed_files=changed_paths(root, before_git, before_trees, allowed),
                error={
                    "code": ERROR_CODE_EXECUTION_FAILED,
                    "message": (
                        "已获授权全局写入，但 GitNexus 全局 registry 仍缺少本仓库；"
                        "查询类命令与 MCP 图查询不可用"
                    ),
                },
                warnings=warnings,
            )
        return publish(
            status="skipped",
            exit_code=None,
            command=[*command, "status", "--json"],
            error={"code": ERROR_CODE_INDEX_CURRENT, "message": None},
            artifacts=meta_artifacts(root),
            allowed_paths=allowed,
            warnings=warnings,
        )

    embedding_mode = manifest.get("gitnexus_embedding_mode")
    embedding_mode = embedding_mode if isinstance(embedding_mode, str) else "preserve"
    install_warnings: list[str] = []
    if embedding_mode == "install-authorized":
        install = invoke(
            skill_dir, root, [*command, "embeddings", "install"], registry=registry_mode()
        )
        forward(install)
        install_warnings.extend(adapter_warnings(install))
        options = manifest.get("options")
        options = options if isinstance(options, dict) else {}
        if install.returncode == 0:
            install_warnings.append("已按授权补齐 GitNexus 本地 embedding 栈。")
        elif options.get("embeddings"):
            allowed = allowed_paths(root, tool)
            denied = install.returncode == REGISTRY_DENIED_EXIT
            message = "已授权安装本地 embedding 栈但安装失败，--embeddings 无法满足"
            denied_warnings: list[str] = []
            if denied:
                install_argv = [*command, "embeddings", "install"]
                message = (
                    f"已授权，但宿主拒绝了本地 embedding 栈安装的仓库外写入"
                    f"（退出码 {install.returncode}）"
                )
                denied_warnings = [denied_write_hint(root, args.manifest, skill_dir, install_argv)]
            return publish(
                status="failed",
                exit_code=install.returncode,
                command=[*command, "embeddings", "install"],
                allowed_paths=allowed,
                changed_files=changed_paths(root, before_git, before_trees, allowed),
                error={"code": ERROR_CODE_EXECUTION_FAILED, "message": message},
                warnings=[*probe_warnings, *install_warnings, *denied_warnings],
            )
        else:
            embedding_mode = "drop"
            install_warnings.append(
                "本地 embedding 栈安装失败：降级为 --drop-embeddings。"
            )

    argv, mode_warnings = analyze_argv(manifest, embedding_mode)
    completed = invoke(skill_dir, root, argv, registry=registry_mode())
    forward(completed)
    allowed = allowed_paths(root, tool)
    changed = changed_paths(root, before_git, before_trees, allowed)
    warnings = [*probe_warnings, *install_warnings, *mode_warnings, *adapter_warnings(completed)]

    artifacts = meta_artifacts(root)

    if completed.returncode != 0:
        message = f"gitnexus analyze 退出码 {completed.returncode}"
        if completed.returncode == REGISTRY_DENIED_EXIT:
            # 已授权还走到这里，说明真实 GITNEXUS_HOME 的写入被宿主拒绝了：这算失败，
            # 但必须点名被拒的文件与重试路径，不能只报一个孤零零的退出码。
            message = (
                f"已授权，但宿主拒绝了 GitNexus 全局注册表 "
                f"{gitnexus_registry_state(root)['path']} 的写入（退出码 {completed.returncode}）："
                "analyze 未完成，注册没有落地"
            )
            warnings = [*warnings, denied_write_hint(root, args.manifest, skill_dir, argv)]
        return publish(
            status="failed",
            exit_code=completed.returncode,
            command=argv,
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={"code": ERROR_CODE_EXECUTION_FAILED, "message": message},
            warnings=warnings,
        )

    if not artifacts:
        return publish(
            status="failed",
            exit_code=completed.returncode,
            command=argv,
            allowed_paths=allowed,
            changed_files=changed,
            error={
                "code": ERROR_CODE_EXECUTION_FAILED,
                "message": "analyze 成功但缺少 .gitnexus/meta.json artifact",
            },
            warnings=warnings,
        )

    # analyze 在未授权时把注册写进一次性 registry 并随之删除，所以「退出码 0」并不
    # 等于查询路径可用；这里实测注册状态，缺注册就补，补不上就不许记 success。
    outcome, warnings, register_exit = resolve_missing_registration(
        manifest, args.manifest, skill_dir, root, command, warnings
    )
    if outcome == "partial":
        return publish(
            status="partial",
            exit_code=completed.returncode,
            command=argv,
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={
                "code": ERROR_CODE_REGISTRY_MISSING,
                "message": "GitNexus 索引未注册到全局 registry，查询路径不可用",
            },
            warnings=warnings,
        )
    if outcome == "failed":
        return publish(
            status="failed",
            exit_code=register_exit if register_exit else 1,
            command=[*command, REGISTRY_INDEX_COMMAND, str(root)],
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={
                "code": ERROR_CODE_EXECUTION_FAILED,
                "message": (
                    "已获授权全局写入，但 GitNexus 全局 registry 仍缺少本仓库；"
                    "查询类命令与 MCP 图查询不可用"
                ),
            },
            warnings=warnings,
        )

    return publish(
        status="success",
        exit_code=completed.returncode,
        command=argv,
        allowed_paths=allowed,
        changed_files=changed,
        artifacts=artifacts,
        warnings=warnings,
    )


def register_main(argv: list[str] | None = None, *, emit: bool = True) -> int:
    """只做那一次仓库外写入的命令（``controller.py register``）。

    首次尝试与升级重试用的是同一条命令，所以宿主若弹窗，弹的永远是同一条 argv、
    同一个目标文件。它既不推进 manifest 阶段，也不因为成功而改变本次运行的其它
    判断：主 Agent 拿到 success 后重跑 ``analyze``，那里会重测注册并落在真实目录。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    root = Path(manifest["root"]).resolve()
    if manifest.get("phase") != "prepared":
        raise InitializeError("register requires a prepared run manifest")
    skill_dir = Path(__file__).resolve().parents[1]
    tool = tool_snapshot(manifest)
    command = [item for item in tool.get("command", []) if isinstance(item, str)]
    if not command or not tool_runnable(tool):
        raise InitializeError("register requires a runnable GitNexus runner in the manifest")

    registered, warnings, exit_code = attempt_registration(
        skill_dir, root, command, manifest_path=args.manifest
    )
    registry = gitnexus_registry_state(root)
    payload = {
        "task": "gitnexus-register",
        "status": "registered" if registered else "failed",
        "exit_code": exit_code,
        "command": [*command, REGISTRY_INDEX_COMMAND, str(root)],
        "registry": {
            "dir": registry["dir"],
            "path": registry["path"],
            "registered": registered,
        },
        "warnings": list(dict.fromkeys(warnings)),
        "next_action": None if registered else "inspect the GitNexus output above",
    }
    if emit:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if registered else 1


if __name__ == "__main__":
    print(
        "initialize gitnexus step: internal module; use controller.py analyze|register",
        file=sys.stderr,
    )
    raise SystemExit(2)
