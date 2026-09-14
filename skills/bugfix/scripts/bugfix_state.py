#!/usr/bin/env python3
"""Bugfix 阶段的确定性状态控制器：冻结快照、绑定审批、校验写入范围、执行验证。

本脚本存在的唯一理由是把「审批前只读、审批绑定快照、修改不越界、验证真实执行」
这四条本来只写在文档里的要求，变成可复现的机器门禁；主 Agent 不再靠自述通过门禁。

子命令：
    start           冻结仓库快照并创建 RUN_DIR，写 state.json
    check-snapshot  复查当前快照是否与冻结时一致（HEAD / 工作树 / 文档）
    record-proposal 记录提案文件摘要、方案 ID 与版本，并校验审查模式与已冻结资料的一致性
    verify-approval 校验提案摘要未变、快照未漂移、审批原文回显了方案 ID 和版本
    verify-scope    用 git 计算实际变更文件，与获批允许文件做严格差集
    run-validation  以 shell=False 执行验证命令，记录真实退出码、输出尾部和错误指纹

产物目录固定为 ``<repo>/.codekit/bugfix/<run-id>/``，该目录已被 .gitignore 排除，
因此审批前保存提案不会产生受版本控制的文件变化。

退出码：0 门禁通过；1 门禁不通过（漂移、越界、验证失败）；2 用法、安全或 IO 错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
STATE_NAME = "state.json"
VALIDATION_NAME = "validation-record.json"
FORBIDDEN_ARG = re.compile(r"[;&|`$()<>{}\n\r]")
DIGITS = re.compile(r"\d+")
HEX_RUN = re.compile(r"\b[0-9a-fA-F]{7,}\b")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OUTPUT_TAIL_LINES = 40
DEFAULT_MAX_REPEATS = 3
UNTRACKED_HASH_LIMIT = 500
UNTRACKED_HASH_MAX_BYTES = 2 * 1024 * 1024


class StateError(Exception):
    """用法、安全或 IO 层面的错误，对应退出码 2。"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode != 0:
        raise StateError(f"git {' '.join(args)} 失败：{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc
    return "sha256:" + hashlib.sha256(data).hexdigest()


def status_entries(root: Path, exclude: str | None = None) -> list[str]:
    output = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].strip('"')
        if not path or is_under(path, exclude):
            continue
        if path not in paths:
            paths.append(path)
    return sorted(paths)


def is_under(path: str, prefix: str | None) -> bool:
    return bool(prefix) and (path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/"))


def head_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def ensure_run_dir_ignored(root: Path) -> None:
    """让 RUN_DIR 自我 ignore，避免产物被当成仓库变更而污染快照与越界判定。

    目标仓库的 .gitignore 不在本 Skill 的写入授权内，因此只在 `.codekit/bugfix/`
    下放置一份局部规则；已存在的仓库级 ignore 仍优先。
    """
    directory = root / ".codekit" / "bugfix"
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".gitignore"
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != "*":
        marker.write_text("*\n", encoding="utf-8")


def untracked_entries(root: Path, exclude: str | None) -> list[str]:
    """未跟踪文件的路径 + 内容摘要。

    只看文件清单会让「悄悄改写未跟踪文件」逃过漂移检测，所以这里补上内容摘要；
    文件数或单文件体积超预算时退化为只记清单，并在摘要里留下可审计的降级标记。
    """
    paths = sorted(
        line.strip() for line in git(root, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip() and not is_under(line.strip(), exclude)
    )
    if len(paths) > UNTRACKED_HASH_LIMIT:
        return [f"untracked-count-only:{len(paths)}"]
    entries: list[str] = []
    for rel in paths:
        file = root / rel
        try:
            if file.is_file() and file.stat().st_size <= UNTRACKED_HASH_MAX_BYTES:
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                entries.append(f"{rel}\1sha256:{digest}")
            else:
                entries.append(f"{rel}\1too-large")
        except OSError as exc:
            entries.append(f"{rel}\1unreadable:{type(exc).__name__}")
    return entries


def capture_snapshot(root: Path, documents: list[Path], exclude: str | None = None) -> dict:
    """冻结仓库与工作树状态。

    工作树摘要覆盖 `git status` 条目、已跟踪文件的二进制 diff 和未跟踪文件内容；
    写入范围是否越界另由 verify-scope 用 git 精确判定。
    """
    diff = ""
    base = head_sha(root)
    if base:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        diff = result.stdout
    document_snapshots = []
    for document in documents:
        if not document.is_file():
            raise StateError(f"文档不可读，无法冻结：{document}")
        document_snapshots.append({
            "path": str(document),
            "digest": sha256_file(document),
            "size": document.stat().st_size,
        })
    return {
        "captured_at": now(),
        "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "head_sha": base,
        "working_tree_digest": sha256_text(
            "\0".join(status_entries(root, exclude))
            + "\1" + "\0".join(untracked_entries(root, exclude))
            + "\2" + diff
        ),
        "preexisting_changes": status_entries(root, exclude),
        "documents": document_snapshots,
    }


def state_path(value: str | Path) -> Path:
    path = Path(value)
    if path.name != STATE_NAME:
        raise StateError(f"--manifest 必须指向 {STATE_NAME}：{path}")
    return path


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StateError(f"{path} 不是合法 JSON：{exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise StateError(f"{path} 的 schema_version 不受支持：{data.get('schema_version')}")
    return data


def save_state(path: Path, data: dict) -> None:
    data["updated_at"] = now()
    temporary = path.parent / f".{path.name}.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("verdict") in {"OK", "PASS"} else 1


def relative_to_root(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


# --------------------------------------------------------------------------- 子命令

def cmd_start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        raise StateError(f"{root} 不是 git 仓库根目录")
    if not SAFE_NAME.fullmatch(args.run_id):
        raise StateError(f"--run-id 只能是目录安全名称：{args.run_id}")
    run_dir = root / ".codekit" / "bugfix" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ensure_run_dir_ignored(root)
    manifest = run_dir / STATE_NAME
    if manifest.exists() and not args.force:
        raise StateError(f"{manifest} 已存在；用 --force 覆盖，或换一个 --run-id")
    documents = [relative_to_root(root, value) for value in (args.documents or [])]
    relative_run_dir = run_dir.relative_to(root).as_posix()
    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run_id,
        "root": str(root),
        "run_dir": relative_run_dir,
        "created_at": now(),
        "snapshot": capture_snapshot(root, documents, relative_run_dir),
        "proposal": None,
        "approval": None,
    }
    save_state(manifest, state)
    return emit({"verdict": "OK", "manifest": str(manifest), "snapshot": state["snapshot"]})


def cmd_check_snapshot(args: argparse.Namespace) -> int:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    reference = state.get("proposal") or state["snapshot"]
    exclude = state.get("run_dir")
    current = capture_snapshot(Path(state["root"]), [], exclude)
    drift: list[str] = []
    if current["head_sha"] != reference["head_sha"]:
        drift.append("head")
    if current["working_tree_digest"] != reference["working_tree_digest"]:
        drift.append("working-tree")
    frozen_docs = {doc["path"]: doc["digest"] for doc in state["snapshot"].get("documents", [])}
    for path, digest in frozen_docs.items():
        document = Path(path)
        if not document.is_file():
            drift.append(f"document-missing:{path}")
        elif sha256_file(document) != digest:
            drift.append(f"document-changed:{path}")
    return emit({
        "verdict": "OK" if not drift else "DRIFT",
        "drift": drift,
        "reference": "proposal" if state.get("proposal") else "start",
        "current": current,
    })


def cmd_record_proposal(args: argparse.Namespace) -> int:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    proposal = Path(args.proposal)
    if not re.fullmatch(r"BF-\d{8}-[A-Za-z0-9][A-Za-z0-9-]*", args.proposal_id):
        raise StateError(f"--proposal-id 必须符合 BF-YYYYMMDD-short-name：{args.proposal_id}")
    if not re.fullmatch(r"\d+", str(args.version)):
        raise StateError(f"--version 必须是整数：{args.version}")
    state["proposal"] = {
        "path": str(proposal),
        "digest": sha256_file(proposal),
        "proposal_id": args.proposal_id,
        "version": str(args.version),
        "review_mode": args.review_mode,
        "recorded_at": now(),
        "head_sha": state["snapshot"]["head_sha"],
        "working_tree_digest": state["snapshot"]["working_tree_digest"],
    }
    save_state(manifest, state)
    return emit({"verdict": "OK", "proposal": state["proposal"]})


def normalized(text: str) -> str:
    return re.sub(r"[`\s]+", "", text)


def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StateError(f"无法读取 {path}: {exc}") from exc


def cmd_verify_approval(args: argparse.Namespace) -> int:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    record = state.get("proposal")
    if not record:
        raise StateError("尚未执行 record-proposal，无法校验审批")
    proposal_path = Path(args.proposal or record["path"])
    checks: dict[str, object] = {}
    checks["proposal_digest_match"] = sha256_file(proposal_path) == record["digest"]
    current = capture_snapshot(Path(state["root"]), [], state.get("run_dir"))
    checks["head_sha_match"] = current["head_sha"] == record["head_sha"]
    checks["working_tree_match"] = current["working_tree_digest"] == record["working_tree_digest"]
    text = read_text_file(Path(args.approval_file))
    phrase = normalized(f"批准执行 {record['proposal_id']} v{record['version']}")
    checks["phrase_bound"] = phrase in normalized(text)
    state["approval"] = {
        "approval_file": str(args.approval_file),
        "proposal_digest": record["digest"],
        "approved_snapshot": {"head_sha": current["head_sha"], "working_tree_digest": current["working_tree_digest"]},
        "bound": f"批准执行 {record['proposal_id']} v{record['version']}",
        "approved_at": now(),
    }
    save_state(manifest, state)
    failed = [name for name, ok in checks.items() if not ok]
    return emit({
        "verdict": "OK" if not failed else "FAIL",
        "failed_checks": failed,
        "checks": checks,
        "review_mode": record.get("review_mode"),
        "note": "审批原文必须逐字包含批准执行 <方案 ID> v<版本>；快照或提案摘要漂移时必须重新审批",
    })


def matches_allowed(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/"):
            if path.startswith(pattern) or path == pattern.rstrip("/"):
                return True
        elif pattern == path:
            return True
    return False


def git_names(root: Path, exclude: str | None, *args: str) -> set[str]:
    return {
        line for line in git(root, *args).splitlines()
        if line.strip() and not is_under(line.strip(), exclude)
    }


def cmd_verify_scope(args: argparse.Namespace) -> int:
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    root = Path(state["root"])
    exclude = state.get("run_dir")
    reference = (state.get("approval") or {}).get("approved_snapshot") or state.get("proposal") or state["snapshot"]
    base = reference["head_sha"] or head_sha(root)
    changed = set(status_entries(root, exclude))
    if base:
        tracked = git_names(root, exclude, "diff", "--name-only", base)
        staged = git_names(root, exclude, "diff", "--name-only", "--cached", base)
    else:  # 仓库还没有任何提交，只能以工作树状态为准
        tracked, staged = set(), git_names(root, exclude, "diff", "--name-only", "--cached")
    untracked = git_names(root, exclude, "ls-files", "--others", "--exclude-standard")
    produced = sorted(tracked | staged | (untracked & changed))
    preexisting = set(state["snapshot"]["preexisting_changes"])
    allowed = list(args.allowed or [])
    if args.allowed_file:
        allowed += [
            line.strip() for line in read_text_file(Path(args.allowed_file)).splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if not allowed:
        raise StateError("verify-scope 需要 --allowed 或 --allowed-file，否则越界判定无意义")
    attributed = [path for path in produced if path not in preexisting]
    out_of_scope = [path for path in attributed if not matches_allowed(path, allowed)]
    # 冻结时就已修改、现在又出现在 diff 里的已跟踪文件：本次改动无法与用户原有修改区分。
    ambiguous = sorted(preexisting & (tracked | staged))
    verdict = "OK" if not out_of_scope and not ambiguous else "FAIL"
    return emit({
        "verdict": verdict,
        "base_sha": base,
        "changed_files": produced,
        "out_of_scope": out_of_scope,
        "preexisting_excluded": sorted(preexisting & set(produced)),
        "preexisting_touched": ambiguous,
        "allowed": allowed,
    })


def validate_command(command: list[str]) -> list[str]:
    if not command:
        raise StateError("run-validation 需要在 `--` 之后给出完整命令")
    for token in command:
        if FORBIDDEN_ARG.search(token):
            raise StateError(f"验证命令包含禁止的 shell 元字符：{token!r}")
        if ".." in Path(token).parts:
            raise StateError(f"验证命令不允许路径上跳：{token!r}")
    return command


def resolve_program(root: Path, program: str) -> str:
    if os.sep in program or program.startswith("./"):
        candidate = relative_to_root(root, program)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise StateError(f"命令不存在或不可执行：{program}")
    resolved = shutil.which(program)
    if not resolved:
        raise StateError(f"命令不在 PATH 上：{program}")
    return resolved


def fingerprint(program: str, exit_code: int | None, output: str) -> str:
    normalized_output = HEX_RUN.sub("#", DIGITS.sub("#", output))[-400:]
    digest = hashlib.sha256(f"{program}\1{exit_code}\1{normalized_output}".encode()).hexdigest()
    return digest[:12]


def tail(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-OUTPUT_TAIL_LINES:])


def load_record(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"{path} 损坏，无法追加验证记录：{exc}") from exc
    return {"schema_version": SCHEMA_VERSION, "checks": []}


def cmd_run_validation(args: argparse.Namespace) -> int:
    if args.manifest:
        manifest = state_path(args.manifest)
        state = load_state(manifest)
        root = Path(state["root"])
        default_record = manifest.parent / VALIDATION_NAME
    else:
        root = Path(args.cwd or Path.cwd())
        default_record = None
    if not args.record and default_record is None:
        raise StateError("run-validation 需要 --record 或 --manifest 才能落盘验证记录")
    record_path = Path(args.record) if args.record else default_record
    command = validate_command(list(args.command or []))
    try:
        program = resolve_program(root, command[0])
    except StateError as exc:
        payload = {
            "verdict": "NOT_AVAILABLE", "command": command, "cwd": str(root),
            "exit_code": None, "result": "NOT_AVAILABLE", "reason": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    argv = [program, *command[1:]]
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(root), timeout=args.timeout, check=False,
        )
        exit_code: int | None = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = None
        output = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    duration_ms = int((time.monotonic() - started) * 1000)
    result = "TIMEOUT" if timed_out else ("PASS" if exit_code == 0 else "FAIL")
    entry = {
        "command": command,
        "cwd": str(root),
        "exit_code": exit_code,
        "result": result,
        "duration_ms": duration_ms,
        "proves": args.proves,
        "fingerprint": fingerprint(command[0], exit_code, output),
        "output_tail": tail(output),
        "executed_at": now(),
        "source": "executed",
    }
    record = load_record(record_path)
    consecutive = 1
    if result == "FAIL":
        for previous in reversed(record["checks"]):
            if previous.get("result") != "FAIL" or previous.get("fingerprint") != entry["fingerprint"]:
                break
            consecutive += 1
    record["checks"].append(entry)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    payload = dict(entry)
    payload["repeat_count"] = consecutive
    payload["record"] = str(record_path)
    if result == "FAIL" and consecutive >= args.max_repeats:
        payload["verdict"] = "RETRY_BUDGET_EXCEEDED"
        payload["result"] = "RETRY_BUDGET_EXCEEDED"
        payload["note"] = f"同一错误指纹已连续 {consecutive} 次，超过重试预算 {args.max_repeats}，必须停止并报告"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 3
    payload["verdict"] = "PASS" if result == "PASS" else "FAIL"
    return emit(payload)


# ----------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command_name", required=True)

    start = sub.add_parser("start", help="冻结仓库快照并创建 RUN_DIR")
    start.add_argument("--root", required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--documents", action="extend", nargs="+", default=[],
                       help="要一并冻结摘要的 PRD/TDD/RPD 文件，可重复传入")
    start.add_argument("--force", action="store_true")
    start.set_defaults(handler=cmd_start)

    check = sub.add_parser("check-snapshot", help="复查快照是否相对提案/审批时漂移")
    check.add_argument("--manifest", required=True)
    check.set_defaults(handler=cmd_check_snapshot)

    record = sub.add_parser("record-proposal", help="记录提案摘要、方案 ID 与版本")
    record.add_argument("--manifest", required=True)
    record.add_argument("--proposal", required=True)
    record.add_argument("--proposal-id", required=True)
    record.add_argument("--version", type=int, required=True)
    record.add_argument("--review-mode", choices=("FULL", "LITE", "WAIVED"), default="FULL")
    record.add_argument(
        "--confirm-review-mode", action="store_true",
        help="确认 FULL（无文档）或 WAIVED 这两种降级已经取得用户明确批准",
    )
    record.set_defaults(handler=cmd_proposal_review_mode)

    approval = sub.add_parser("verify-approval", help="校验提案未改、快照未漂移、审批口令绑定版本")
    approval.add_argument("--manifest", required=True)
    approval.add_argument("--approval-file", required=True)
    approval.add_argument("--proposal")
    approval.set_defaults(handler=cmd_verify_approval)

    scope = sub.add_parser("verify-scope", help="用 git 校验实际变更是否越界")
    scope.add_argument("--manifest", required=True)
    scope.add_argument("--allowed", action="extend", nargs="+", default=[],
                       help="获批仓库相对路径，可重复传入；以 / 结尾表示目录前缀")
    scope.add_argument("--allowed-file", help="每行一个允许路径的文件")
    scope.set_defaults(handler=cmd_verify_scope)

    validation = sub.add_parser("run-validation", help="以 shell=False 执行验证命令并记录证据")
    validation.add_argument("--manifest", help="提供 cwd 与默认记录路径；省略时必须给 --cwd 和 --record")
    validation.add_argument("--record")
    validation.add_argument("--cwd")
    validation.add_argument("--proves", default=None)
    validation.add_argument("--timeout", type=float, default=600.0)
    validation.add_argument("--max-repeats", type=int, default=DEFAULT_MAX_REPEATS)
    validation.add_argument("command", nargs=argparse.REMAINDER)
    validation.set_defaults(handler=cmd_run_validation)
    return parser


def cmd_proposal_review_mode(args: argparse.Namespace) -> int:
    """校验审查模式与已冻结资料的一致性，再转入 record-proposal。

    LITE 是由事实决定的（start 时没有冻结到任何有效文档），不要求用户批准；
    FULL 在无文档时、WAIVED 在任何时候都是「对门禁的降级」，必须用户明确批准。
    """
    manifest = state_path(args.manifest)
    state = load_state(manifest)
    has_documents = bool(state["snapshot"].get("documents"))
    if args.review_mode == "LITE" and has_documents:
        raise StateError(
            "快照已冻结到 PRD/TDD/RPD，不得降级为 LITE；请修正 --documents 或改用 FULL"
        )
    if args.review_mode == "WAIVED" and not args.confirm_review_mode:
        raise StateError(
            "审查模式 WAIVED 表示跳过系统 code-review，必须已经取得用户明确批准"
            "（见 SKILL.md 的『审查模式与降级门禁』），确认后才加 --confirm-review-mode"
        )
    if args.review_mode == "FULL" and not has_documents and not args.confirm_review_mode:
        raise StateError(
            "快照没有冻结到任何文档，声称 FULL 意味着依靠仓库规范完成规格追踪，"
            "必须由用户确认后加 --confirm-review-mode，否则使用 --review-mode LITE"
        )
    return cmd_record_proposal(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command_name == "run-validation" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    return args.handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except StateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:  # pragma: no cover
        raise SystemExit(130)
