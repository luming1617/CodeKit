#!/usr/bin/env python3
"""Deterministic GitNexus step for initialize.

GitNexus 这一路没有需要 LLM 判断的成分：按冻结的 runner 与既有索引模式执行
``status`` / ``analyze``，再申报写入范围。因此它不再作为独立 Sub-agent 派发，
而由控制器直接执行。产出与 subagent 完全相同的 Worker result schema，Phase 2
的门禁、reconcile 与 manifest 结构都不需要改动。

参数（``--pdg``、``--drop-embeddings``、``--use-global-registry`` 等）一律由
manifest 推导，避免由模型拼装标志位而产生模式漂移。
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
    utc_now,
)

STEP_TREES = (".gitnexus", ".claude/skills", ".agents/skills")
ANALYZE_BASE = ("analyze", "--skills", "--skip-agents-md", "--verbose")
WARNING_PREFIX = "initialize warning:"
REGISTRY_WARNING = (
    "GitNexus 索引未注册到全局 registry（{path}）：list/query/context/impact/trace/"
    "cypher/check/detect-changes 与 MCP 图查询都会报 “No indexed repositories found”。"
)


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


def invoke(skill_dir: Path, root: Path, command: list[str], *, authorized: bool):
    """经 run-gitnexus adapter 执行，默认隔离全局 registry。"""
    wrapper = skill_dir / "scripts" / "run-gitnexus.py"
    argv = [sys.executable, str(wrapper), "--root", str(root)]
    if authorized:
        argv.append("--use-global-registry")
    argv += ["--", *command]
    return subprocess.run(argv, text=True, capture_output=True, check=False)


def register_index(
    skill_dir: Path, root: Path, command: list[str], *, authorized: bool
) -> tuple[bool, list[str], int | None]:
    """把已有索引登记到真实全局 registry，返回 ``(registered, warnings, exit)``。

    ``status=current`` 时 analyze 不会运行，因此复用路径上没有任何一步会写注册表；
    仓库因此可能长期处于「索引有效但查询解析不到」的状态。注册走 ``gitnexus index``：
    它只登记既有 ``.gitnexus/``，不重新分析，幂等且开销小。
    """
    if not authorized:
        return False, [], None
    completed = invoke(skill_dir, root, [*command, "index", str(root)], authorized=True)
    forward(completed)
    return (
        bool(gitnexus_registry_state(root)["registered"]),
        adapter_warnings(completed),
        completed.returncode,
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
    authorized = global_writes_authorized(manifest)
    started_clock = time.monotonic()

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

    if not command or tool.get("runner") == "unresolved":
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
        skill_dir, root, [*command, "status", "--json"], authorized=authorized
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
        if not gitnexus_registry_state(root)["registered"]:
            if not authorized:
                warnings.append(registry_warning(root))
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
            registered, register_warnings, register_exit = register_index(
                skill_dir, root, command, authorized=authorized
            )
            warnings.extend(register_warnings)
            if not registered:
                warnings.append(registry_warning(root))
                return publish(
                    status="failed",
                    exit_code=register_exit if register_exit else 1,
                    command=[*command, "index", str(root)],
                    allowed_paths=allowed,
                    changed_files=changed_paths(root, before_git, before_trees, allowed),
                    error={
                        "code": ERROR_CODE_EXECUTION_FAILED,
                        "message": (
                            "已授权全局写入，但 GitNexus 全局 registry 仍缺少本仓库；"
                            "查询类命令与 MCP 图查询不可用"
                        ),
                    },
                    warnings=warnings,
                )
            warnings.append("复用的索引已补注册到 GitNexus 全局 registry。")
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
            skill_dir, root, [*command, "embeddings", "install"], authorized=authorized
        )
        forward(install)
        install_warnings.extend(adapter_warnings(install))
        options = manifest.get("options")
        options = options if isinstance(options, dict) else {}
        if install.returncode == 0:
            install_warnings.append("已按授权补齐 GitNexus 本地 embedding 栈。")
        elif options.get("embeddings"):
            allowed = allowed_paths(root, tool)
            return publish(
                status="failed",
                exit_code=install.returncode,
                command=[*command, "embeddings", "install"],
                allowed_paths=allowed,
                changed_files=changed_paths(root, before_git, before_trees, allowed),
                error={
                    "code": ERROR_CODE_EXECUTION_FAILED,
                    "message": "已授权安装本地 embedding 栈但安装失败，--embeddings 无法满足",
                },
                warnings=[*probe_warnings, *install_warnings],
            )
        else:
            embedding_mode = "drop"
            install_warnings.append(
                "本地 embedding 栈安装失败：降级为 --drop-embeddings。"
            )

    argv, mode_warnings = analyze_argv(manifest, embedding_mode)
    completed = invoke(skill_dir, root, argv, authorized=authorized)
    forward(completed)
    allowed = allowed_paths(root, tool)
    changed = changed_paths(root, before_git, before_trees, allowed)
    warnings = [*probe_warnings, *install_warnings, *mode_warnings, *adapter_warnings(completed)]

    artifacts = meta_artifacts(root)

    if completed.returncode != 0:
        return publish(
            status="failed",
            exit_code=completed.returncode,
            command=argv,
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={
                "code": ERROR_CODE_EXECUTION_FAILED,
                "message": f"gitnexus analyze 退出码 {completed.returncode}",
            },
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
    if not gitnexus_registry_state(root)["registered"]:
        if not authorized:
            warnings.append(registry_warning(root))
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
        registered, register_warnings, register_exit = register_index(
            skill_dir, root, command, authorized=authorized
        )
        warnings.extend(register_warnings)
        if not registered:
            warnings.append(registry_warning(root))
            return publish(
                status="failed",
                exit_code=register_exit if register_exit else 1,
                command=[*command, "index", str(root)],
                allowed_paths=allowed,
                changed_files=changed,
                artifacts=artifacts,
                error={
                    "code": ERROR_CODE_EXECUTION_FAILED,
                    "message": (
                        "已授权全局写入，但 GitNexus 全局 registry 仍缺少本仓库；"
                        "查询类命令与 MCP 图查询不可用"
                    ),
                },
                warnings=warnings,
            )
        warnings.append("索引已注册到 GitNexus 全局 registry。")

    return publish(
        status="success",
        exit_code=completed.returncode,
        command=argv,
        allowed_paths=allowed,
        changed_files=changed,
        artifacts=artifacts,
        warnings=warnings,
    )


if __name__ == "__main__":
    print("initialize gitnexus step: internal module; use controller.py analyze", file=sys.stderr)
    raise SystemExit(2)
