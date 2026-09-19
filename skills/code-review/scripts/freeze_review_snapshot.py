#!/usr/bin/env python3
"""Create a deterministic, read-only code-review snapshot manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


class SnapshotError(RuntimeError):
    """Raised when a read-only git command fails during snapshot freezing."""


class TargetBlocked(RuntimeError):
    """Raised when --auto-target cannot determine a reviewable target.

    与脚本故障区分：这不是环境问题，而是审查目标本身不成立（步骤 02/04 的
    BLOCKED 路径），main 以独立退出码 3 报告，便于 Orchestrator 直接转 BLOCKED。
    """


# CodeKit 运行时产物目录前缀。快照/报告必须落在 <repo>/.codekit/ 下（SKILL.md
# 「产物落点」），而它们本身是未跟踪文件；若不固定排除，任何一次重冻结都会把
# 上一轮产物计入 untracked_digest，造成观测者效应式的自污染。该排除只作用于
# untracked 层，且写死在这里并同步披露进 SKILL.md，不构成藏匿通道。
RUNTIME_ARTIFACT_PREFIXES = (".codekit/",)


def _reconfigure_streams() -> None:
    # Windows cp936 控制台下 ensure_ascii=False 的 CJK 路径会让 print 直接抛
    # UnicodeEncodeError，快照因此生成失败；统一强制 UTF-8 输出流。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def run_git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # encoding 只能在文本模式下传：带上它会强制开启文本模式，二进制调用
            # （``text=False``）会因此拿到 str 而不是 bytes，把快照字节流改坏。
            **({"text": True, "encoding": "utf-8", "errors": "replace"} if text else {"text": False}),
        )
    except FileNotFoundError as exc:
        raise SnapshotError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raw_stderr = exc.stderr or (b"" if not text else "")
        detail = raw_stderr.decode("utf-8", "replace") if isinstance(raw_stderr, bytes) else str(raw_stderr)
        detail = detail.strip() or "<no stderr>"
        raise SnapshotError(
            f"git {' '.join(args)} failed with exit status {exc.returncode}: {detail}"
        ) from exc
    return result.stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def resolve_commit(repo: Path, revision: str) -> str:
    return str(run_git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", text=True)).strip()


def diff_bytes(repo: Path, *args: str) -> bytes:
    return bytes(run_git(repo, "diff", "--binary", "--no-ext-diff", *args))


def parse_name_status(raw: bytes) -> list[str]:
    parts = raw.split(b"\0")
    result: list[str] = []
    index = 0
    while index < len(parts) and parts[index]:
        status = parts[index].decode("utf-8", "surrogateescape")
        index += 1
        if not status:
            break
        path = parts[index].decode("utf-8", "surrogateescape") if index < len(parts) else ""
        index += 1
        if status[0] in {"R", "C"}:
            old_path = path
            new_path = parts[index].decode("utf-8", "surrogateescape") if index < len(parts) else ""
            index += 1
            result.append(f"{status[0]}:{old_path}->{new_path}")
        else:
            result.append(f"{status[0]}:{path}")
    return result


def changed_from_diff(repo: Path, *args: str) -> list[str]:
    raw = bytes(
        run_git(
            repo,
            "diff",
            "--name-status",
            "--find-renames",
            "-z",
            *args,
        )
    )
    return parse_name_status(raw)


def untracked_manifest(repo: Path) -> tuple[list[str], str]:
    raw = bytes(run_git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    listed = [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]
    paths = [item for item in listed if not item.startswith(RUNTIME_ARTIFACT_PREFIXES)]
    entries: list[dict[str, str]] = []
    for relative in sorted(paths):
        path = repo / relative
        stat = path.lstat()
        if os.path.islink(path):
            entries.append({"path": relative, "kind": "symlink", "value": os.readlink(path)})
        elif stat.st_mode & 0o170000 == 0o100000:
            entries.append({"path": relative, "kind": "file", "value": sha256(path.read_bytes())})
        else:
            entries.append({"path": relative, "kind": "other", "value": str(stat.st_mode)})
    return paths, sha256(canonical_json(entries))


BASELINE_CANDIDATES = ("refs/heads/master", "refs/heads/main")


def resolve_auto_branch_target(repo: Path) -> dict[str, str]:
    """按 SKILL.md 步骤 02 的独立执行规则确定 branch 审查目标。

    原先由 Orchestrator 逐条手工执行的 5 个 git 探测命令（repo root、当前分支、
    HEAD SHA、基线选择、merge-base）全部收进本函数；任何一个 BLOCKED 条件都以
    TargetBlocked 携带机器可读原因返回，不再由模型转述判定。
    """
    try:
        root = Path(str(run_git(repo, "rev-parse", "--show-toplevel", text=True)).strip())
    except SnapshotError as exc:
        raise TargetBlocked(f"not inside a git repository: {exc}") from exc
    try:
        branch = str(run_git(root, "symbolic-ref", "--short", "HEAD", text=True)).strip()
    except SnapshotError as exc:
        raise TargetBlocked(
            "detached HEAD: checkout a feature branch, or freeze with explicit --base-sha/--head-sha"
        ) from exc
    if branch in {"master", "main"}:
        raise TargetBlocked(f"current branch '{branch}' is a baseline branch; no branch diff to review")
    baseline: tuple[str, str] | None = None
    for ref in BASELINE_CANDIDATES:
        try:
            baseline = (ref, resolve_commit(root, ref))
        except SnapshotError:
            continue
        break
    if baseline is None:
        raise TargetBlocked("neither local refs/heads/master nor refs/heads/main exists to base the review on")
    ref, base_tip = baseline
    head_sha = resolve_commit(root, "HEAD")
    try:
        merge_base = str(run_git(root, "merge-base", base_tip, head_sha, text=True)).strip()
    except SnapshotError as exc:
        raise TargetBlocked(f"no common ancestor between {ref} and branch '{branch}': {exc}") from exc
    return {
        "repo": str(root),
        "base_sha": merge_base,
        "head_sha": head_sha,
        "target_type": "branch",
        # locator 记录实际选中的基线（master 与 main 同时存在时固定优先 master）。
        "target_locator": f"{ref[len('refs/heads/'):]}...{branch}",
    }


SNAPSHOT_REPORT_FIELDS = (
    "base_sha",
    "head_sha",
    "diff_digest",
    "changed_files",
    "staged_digest",
    "unstaged_digest",
    "untracked_digest",
    "preexisting_changes",
    "manifest_digest",
)
COVERAGE_DIMENSIONS = (
    "correctness",
    "security_access",
    "data_transaction",
    "distributed_reliability",
    "compatibility_contract",
    "performance_capacity",
    "test_adequacy",
    "observability",
    "release_rollback",
)


def build_report_skeleton(manifest: dict[str, object]) -> dict[str, object]:
    """用签名 manifest 机械预填报告骨架的确定性字段。

    snapshot 九个字段（含 4 个 64-hex digest）、scope 的 target 二元组和九个覆盖
    维度名都不允许再由模型手工转录；其余判断性字段一律为 null——未填写的骨架
    必然通不过 validate_review_report.py，不可能被当作成品交付。
    """
    return {
        "schema_version": "1.0",
        "review_context": {"review_mode": None, "documents_status": None},
        "verdict": None,
        "verdict_basis": [],
        "blockers": [],
        "scope": {
            "target_type": manifest["target_type"],
            "target_locator": manifest["target_locator"],
            "included": [],
            "excluded": [],
        },
        "snapshot": {field: manifest[field] for field in SNAPSHOT_REPORT_FIELDS},
        "summary": None,
        "findings": [],
        "open_questions": [],
        "traceability": [],
        "coverage": [
            {
                "dimension": dimension,
                "required": True,
                "status": None,
                "owner": None,
                "evidence": [],
                "reason": None,
            }
            for dimension in COVERAGE_DIMENSIONS
        ],
        "validation": [],
        "release_gate": {"status": None, "checks": [], "blocking_reasons": [], "conditions": []},
        "rejected_candidates": [],
        "limitations": [],
        "residual_risks": [],
    }


def build_snapshot(args: argparse.Namespace) -> dict[str, object]:
    repo = Path(args.repo).resolve()
    base_sha = resolve_commit(repo, args.base_sha)
    head_sha = resolve_commit(repo, args.head_sha)
    committed = diff_bytes(repo, base_sha, head_sha)
    staged = diff_bytes(repo, "--cached")
    unstaged = diff_bytes(repo)
    untracked, untracked_digest = untracked_manifest(repo)
    changes = set(changed_from_diff(repo, base_sha, head_sha))
    changes.update(changed_from_diff(repo, "--cached"))
    changes.update(changed_from_diff(repo))
    changes.update(f"A:{path}" for path in untracked)
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "repository_root": str(repo),
        "target_type": args.target_type,
        "target_locator": args.target_locator,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "changed_files": sorted(changes),
        "staged_digest": sha256(staged),
        "unstaged_digest": sha256(unstaged),
        "untracked_digest": untracked_digest,
        "preexisting_changes": sorted(set(args.preexisting_change)),
    }
    diff_material = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "committed_digest": sha256(committed),
        "staged_digest": manifest["staged_digest"],
        "unstaged_digest": manifest["unstaged_digest"],
        "untracked_digest": untracked_digest,
        "changed_files": manifest["changed_files"],
        "preexisting_changes": manifest["preexisting_changes"],
    }
    manifest["diff_digest"] = sha256(canonical_json(diff_material))
    manifest["manifest_digest"] = sha256(canonical_json(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=None, help="repository root; required unless --auto-target branch resolves it")
    parser.add_argument("--base-sha", default=None)
    parser.add_argument("--head-sha", default=None)
    parser.add_argument("--target-type", default=None)
    parser.add_argument("--target-locator", default=None)
    parser.add_argument(
        "--auto-target",
        choices=["branch"],
        default=None,
        help="resolve repo root, current branch, master/main baseline and merge-base deterministically (step 02); exits 3 with 'BLOCKED:' reason when no reviewable branch target exists",
    )
    parser.add_argument(
        "--emit-skeleton",
        type=Path,
        default=None,
        metavar="PATH",
        help="write a report skeleton whose snapshot/scope/coverage fields are prefilled from the manifest",
    )
    parser.add_argument("--preexisting-change", action="append", default=[])
    args = parser.parse_args()
    _reconfigure_streams()
    if args.auto_target == "branch":
        try:
            resolved = resolve_auto_branch_target(args.repo or Path.cwd())
        except TargetBlocked as exc:
            print(f"BLOCKED: {exc}", file=sys.stderr)
            return 3
        except SnapshotError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        args.repo = Path(resolved["repo"])
        args.base_sha = resolved["base_sha"]
        args.head_sha = resolved["head_sha"]
        args.target_type = resolved["target_type"]
        args.target_locator = resolved["target_locator"]
    else:
        missing = [
            flag
            for flag, value in (
                ("--repo", args.repo),
                ("--base-sha", args.base_sha),
                ("--head-sha", args.head_sha),
                ("--target-type", args.target_type),
                ("--target-locator", args.target_locator),
            )
            if not value
        ]
        if missing:
            parser.error(f"{' '.join(missing)} required unless --auto-target branch is used")
    try:
        manifest = build_snapshot(args)
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: unable to read the working tree: {exc}", file=sys.stderr)
        return 2
    if args.emit_skeleton is not None:
        try:
            if args.emit_skeleton.parent != Path(""):
                args.emit_skeleton.parent.mkdir(parents=True, exist_ok=True)
            args.emit_skeleton.write_text(
                json.dumps(build_report_skeleton(manifest), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"ERROR: unable to write report skeleton to {args.emit_skeleton}: {exc}", file=sys.stderr)
            return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
