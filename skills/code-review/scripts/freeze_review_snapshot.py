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
    paths = [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item]
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
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--target-type", required=True)
    parser.add_argument("--target-locator", required=True)
    parser.add_argument("--preexisting-change", action="append", default=[])
    args = parser.parse_args()
    try:
        manifest = build_snapshot(args)
    except SnapshotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: unable to read the working tree: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
