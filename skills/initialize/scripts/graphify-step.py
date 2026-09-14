#!/usr/bin/env python3
"""Deterministic Graphify step for initialize.

Graphify 这一路的判断成分——模式判据、能力位、artifact 与缓存校验——在
``references/tool-adapters.md`` 里本来就是确定性规则，此前交给 Sub-agent 执行的唯一
收益是独立会话隔离，代价是把它放进宿主的权限分类器：Qoder CN 的 auto 模式对子代理的
每一条 Bash 调用远程 LLM 分类器，``graphify extract`` 被稳定拒绝（同一子代理里
``--version`` / ``--help`` / ``extract --help`` 都放行），而主会话有工具允许规则、
不进分类器。一次拒绝在这条路上没有人工兜底，required 阶段失败会让整轮 abort。

因此这一路与 GitNexus 同构：由控制器执行，参数一律从冻结的 manifest 推导，产出同
schema 的 Worker result，Phase 2 的门禁、reconcile 与 manifest 结构都不需要改动。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

from _common import (
    ERROR_CODE_ARTIFACT_INVALID,
    ERROR_CODE_EXECUTION_FAILED,
    ERROR_CODE_NO_CHANGE,
    ERROR_CODE_TOOL_MISSING,
    TASK_ALLOWED_PATHS,
    TASK_REQUIRED_ARTIFACTS,
    InitializeError,
    atomic_json,
    git_status,
    graphify_ignore_digest,
    load_json,
    snapshot_trees,
    tool_runnable,
    utc_now,
)

STEP_TREES = ("graphify-out",)
# 本步只做本地 AST 抽取：语义/full 模式需要 backend、model、key 的预检结论，而
# manifest 不承载这些字段，所以 mode 是固定值而不是推断值。
MODE = "code-only"
# ``graphify update`` 在无增量时以退出码 1 结束并打印这句话（cli.py 的 _rebuild_code
# 返回 False 分支）。它不是失败，而是「无需重复工作」，对应 contract 里的完整复用跳过。
NOTHING_TO_UPDATE = "Nothing to update"
ARTIFACT_RELATIVES = tuple(TASK_REQUIRED_ARTIFACTS["graphify"])


def tool_snapshot(manifest: dict) -> dict:
    tool = manifest.get("tools", {}).get("graphify", {})
    return tool if isinstance(tool, dict) else {}


def capability(tool: dict) -> dict:
    supports = tool.get("supports")
    return supports if isinstance(supports, dict) else {}


def graphify_version(tool: dict) -> str:
    """manifest 冻结的版本串，与 reconcile 写进 sidecar 的取值口径一致。"""
    versions = [item for item in tool.get("version", []) if isinstance(item, str) and item.strip()]
    return " ".join(versions).strip() or "unknown"


def json_nodes(path: Path) -> list | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return nodes if isinstance(nodes, list) else None


def artifacts_present(root: Path) -> list[dict]:
    """申报存在的 artifact 与其校验类型；真正的校验由门禁执行。"""
    artifacts = []
    for relative in ARTIFACT_RELATIVES:
        if (root / relative).is_file():
            artifacts.append({"path": relative, "validation": TASK_REQUIRED_ARTIFACTS["graphify"][relative]})
    return artifacts


def cache_is_valid(root: Path, tool: dict) -> tuple[bool, str]:
    """有效缓存判定（tool-adapters.md 的四条同时成立才复用）。"""
    graph = root / "graphify-out" / "graph.json"
    manifest_json = root / "graphify-out" / "manifest.json"
    marker = root / "graphify-out" / ".graphify_root"
    sidecar = root / "graphify-out" / ".codekit-state.json"
    nodes = json_nodes(graph)
    if not nodes:
        return False, "graphify-out/graph.json 缺失、不可解析或节点为空"
    try:
        if not isinstance(json.loads(manifest_json.read_text(encoding="utf-8")), dict):
            return False, "graphify-out/manifest.json 不是 JSON 对象"
    except (OSError, json.JSONDecodeError):
        return False, "graphify-out/manifest.json 缺失或不可解析"
    try:
        recorded = marker.read_text(encoding="utf-8-sig").strip()
        if Path(recorded).resolve() != root.resolve():
            return False, ".graphify_root 指向的不是当前仓库根"
    except (OSError, ValueError):
        return False, ".graphify_root 缺失或不可读"
    try:
        state = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, ".codekit-state.json 缺失或不可解析"
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return False, ".codekit-state.json schema 不匹配"
    if state.get("last_status") != "success":
        return False, "上次 Graphify 状态不是 success"
    try:
        if Path(state.get("root", "")).resolve() != root.resolve():
            return False, ".codekit-state.json 的 root 不是当前仓库根"
    except (OSError, ValueError, TypeError):
        return False, ".codekit-state.json 的 root 不可解析"
    if state.get("mode") != MODE:
        return False, f".codekit-state.json 的 mode 不是 {MODE}"
    if state.get("graphify_version") != graphify_version(tool):
        return False, "Graphify 版本与 sidecar 记录的不一致"
    digest = graphify_ignore_digest(root)
    if digest is None or state.get("ignore_digest") != digest:
        return False, "CodeKit managed Graphify ignore block 与 sidecar 记录的不一致"
    return True, ""


def decide_mode(root: Path, tool: dict, options: dict) -> tuple[str, list[str]]:
    """full / update 的判据；``--force`` 一律走 full force。"""
    if options.get("force"):
        return "full", ["manifest options.force=true：执行 full 抽取并追加 --force。"]
    valid, reason = cache_is_valid(root, tool)
    if valid:
        return "update", []
    return "full", [f"缓存不可复用，执行 full 抽取：{reason}"]


def build_argv(tool: dict, root: Path, mode: str, options: dict) -> list[str]:
    """按子命令拼装 argv。

    路径参数与 ``--out`` 都用仓库根的绝对路径而不是 ``.``：``extract`` 把
    ``.graphify_root`` 写成解析后的绝对路径，``update``（watch._rebuild_code）写的却是
    原样传入的路径。传 ``.`` 会让门禁的 root-match 依赖 cwd，而 ``update`` 不接受
    ``--out``，只能靠绝对路径参数把两态对齐。
    """
    command = [item for item in tool.get("command", []) if isinstance(item, str)]
    target = str(root)
    if mode == "update":
        argv = [*command, "update", target]
    else:
        argv = [*command, "extract", target, "--out", target, "--code-only"]
    if options.get("force"):
        argv.append("--force")
    return argv


def worker_env(tool: dict) -> tuple[dict, list[str]]:
    """AST 进程池不可用时的并行收敛；两态都不支持时由调用方拒绝执行。

    用环境变量而不是 ``--max-workers 1``：该 flag 只有 ``extract`` 接受，``update`` 遇到
    未知 option 直接 ``exit 2``，而 ``extract`` 内部就是把 flag 写进这个环境变量。
    """
    supports = capability(tool)
    if supports.get("ast_process_pool"):
        return dict(os.environ), []
    if supports.get("max_workers"):
        env = dict(os.environ)
        env["GRAPHIFY_MAX_WORKERS"] = "1"
        return env, [
            "AST 进程池能力位为 false 且 max_workers 为 true：以 GRAPHIFY_MAX_WORKERS=1 "
            "收敛为单进程抽取（两个子命令共用该开关，--max-workers 只有 extract 接受）。"
        ]
    return dict(os.environ), []


def process_pool_unsupported(tool: dict) -> bool:
    supports = capability(tool)
    return not supports.get("ast_process_pool") and not supports.get("max_workers")


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


def tail(text: str, limit: int = 800) -> str:
    cleaned = (text or "").strip()
    return cleaned[-limit:]


def main(argv: list[str] | None = None, *, emit: bool = True) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    root = Path(manifest["root"]).resolve()
    if manifest.get("phase") != "prepared":
        raise InitializeError("graphify step requires a prepared run manifest")
    run_dir = Path(manifest["state_run_dir"])
    tool = tool_snapshot(manifest)
    options = manifest.get("options") if isinstance(manifest.get("options"), dict) else {}
    started_clock = time.monotonic()

    result: dict = {
        "schema_version": 1,
        "run_id": manifest.get("run_id"),
        "task": "graphify",
        "status": "failed",
        "required": True,
        "runner": tool.get("runner") or "graphify",
        "version": graphify_version(tool),
        "mode": MODE,
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
    result_path = run_dir / "graphify-result.json"

    def publish(**fields) -> int:
        result.update(fields)
        result["warnings"] = list(dict.fromkeys(result.get("warnings", [])))
        result["finished_at"] = utc_now()
        result["duration_ms"] = int((time.monotonic() - started_clock) * 1000)
        atomic_json(result_path, result)
        if emit:
            print(json.dumps({
                "task": "graphify",
                "status": result["status"],
                "mode": result["mode"],
                "error_code": result["error"]["code"],
                "command": result["command"],
                "changed_files": result["changed_files"],
                "warnings": result["warnings"],
                "result": str(result_path),
            }, ensure_ascii=False, indent=2))
        return 0

    allowed = list(TASK_ALLOWED_PATHS["graphify"])

    if not tool_runnable(tool):
        # 与 GitNexus 同一判据：入口解析到了不等于能执行，跑不起来是环境事实而不是工具
        # 坏了，按 tool-missing 诚实跳过让门禁走 partial，而不是把 run 拖进 abort。
        hint = tool.get("resolution_hint")
        warnings = [hint] if isinstance(hint, str) and hint else []
        return publish(
            status="skipped",
            exit_code=None,
            error={"code": ERROR_CODE_TOOL_MISSING, "message": "Graphify runner 不可用"},
            warnings=warnings,
        )
    if not capability(tool).get("code_only"):
        # 少了 --code-only 就会退化成需要 API key 的语义抽取：那是未经用户授权的联网
        # 行为，宁可如实记为能力不支持，也不得静默换成语义模式。
        return publish(
            status="skipped",
            exit_code=None,
            error={
                "code": ERROR_CODE_TOOL_MISSING,
                "message": "Graphify runner 不支持 --code-only，本地 AST 模式不可用",
            },
            warnings=[
                "升级 Graphify 到支持 `extract --code-only` 的版本后重跑本路；"
                "initialize 不会改用需要语义后端的抽取模式来绕过这一态。"
            ],
        )
    if process_pool_unsupported(tool):
        return publish(
            status="skipped",
            exit_code=None,
            error={
                "code": ERROR_CODE_TOOL_MISSING,
                "message": "本机既不支持 AST 进程池，也不支持并行收敛开关",
            },
            warnings=[
                "能力位 ast_process_pool 与 max_workers 均为 false：无法在不静默改变"
                "命令语义的前提下执行抽取。升级 Graphify 后重跑本路。"
            ],
        )

    mode, mode_warnings = decide_mode(root, tool, options)
    env, env_warnings = worker_env(tool)
    command = build_argv(tool, root, mode, options)

    before_git = set(git_status(root))
    before_trees = snapshot_trees(root, list(STEP_TREES))

    completed = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)

    changed = changed_paths(root, before_git, before_trees, allowed)
    warnings = [*mode_warnings, *env_warnings]
    artifacts = artifacts_present(root)

    if completed.returncode != 0:
        stderr = completed.stderr or ""
        if mode == "update" and NOTHING_TO_UPDATE in stderr:
            # 无增量可重算是完整复用，不是失败：exit_code 必须为 null 才能过 skipped 校验。
            return publish(
                status="skipped",
                exit_code=None,
                command=command,
                allowed_paths=allowed,
                changed_files=changed,
                artifacts=artifacts,
                error={"code": ERROR_CODE_NO_CHANGE, "message": None},
                warnings=[*warnings, "graphify update 报告无增量：复用现有图谱。"],
            )
        return publish(
            status="failed",
            exit_code=completed.returncode,
            command=command,
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={
                "code": ERROR_CODE_EXECUTION_FAILED,
                "message": f"graphify {mode} 退出码 {completed.returncode}：{tail(stderr) or tail(completed.stdout)}",
            },
            warnings=warnings,
        )

    if len(artifacts) != len(ARTIFACT_RELATIVES):
        missing = sorted(set(ARTIFACT_RELATIVES) - {item["path"] for item in artifacts})
        return publish(
            status="failed",
            # contract 禁止 failed 配 exit_code 0（那会被读成「进程成功却谎报失败」），
            # 所以这里留空并把真实退出码写进消息，而不是伪造一个非零值。
            exit_code=None,
            command=command,
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={
                "code": ERROR_CODE_ARTIFACT_INVALID,
                "message": (
                    f"graphify 退出码 {completed.returncode} 但缺少 required artifacts: "
                    + ", ".join(missing)
                ),
            },
            warnings=warnings,
        )

    nodes = json_nodes(root / "graphify-out" / "graph.json")
    if not nodes:
        return publish(
            status="failed",
            exit_code=None,
            command=command,
            allowed_paths=allowed,
            changed_files=changed,
            artifacts=artifacts,
            error={
                "code": ERROR_CODE_ARTIFACT_INVALID,
                "message": (
                    f"graphify 退出码 {completed.returncode} 但 graphify-out/graph.json "
                    "的 nodes 为空"
                ),
            },
            warnings=warnings,
        )

    return publish(
        status="success",
        exit_code=completed.returncode,
        command=command,
        allowed_paths=allowed,
        changed_files=changed,
        artifacts=artifacts,
        warnings=warnings,
    )


if __name__ == "__main__":
    print("initialize graphify step: internal module; use controller.py graphify", file=sys.stderr)
    raise SystemExit(2)
