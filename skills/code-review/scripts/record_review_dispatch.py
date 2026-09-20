#!/usr/bin/env python3
"""Append one subagent dispatch record to a code-review dispatch ledger.

The ledger is the mechanical proof that a dispatched review role actually ran:
validate_review_report.py --dispatch-ledger cross-checks the coverage matrix
owners and the step ordering against it. Records are append-only JSON Lines.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from freeze_review_snapshot import _reconfigure_streams, sha256  # noqa: E402

DIGEST_RE = re.compile(r"[0-9a-f]{64}")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
ROLES = {
    "review-context",
    "review-traceability",
    "review-specialist",
    "review-evidence",
    "review-failure",
}
RETURNED_STATUSES = {"COMPLETED", "PARTIAL", "FAILED"}
LEDGER_FIELDS = (
    "step",
    "role",
    "instance_id",
    "input_diff_digest",
    "returned_status",
    "started_at",
    "ended_at",
)
INPUT_FIELDS = ("path", "digest")
# CW4-a：一轮审查的派发预算（审核报告 E1）。正常路径 03/05/07/08(≥2)/10/16/18 ≈7-9 条，
# 14 条留给补证与失败归因；超出预算说明本轮已经无法在预算内收敛，登记脚本直接拒绝，
# 剩下的只能是步骤 22 的 BLOCKED 兜底报告，而不是继续派新角色。
MAX_DISPATCHES = 14


class LedgerError(RuntimeError):
    pass


def parse_timestamp(value: str, field: str) -> datetime:
    if not TIMESTAMP_RE.fullmatch(value):
        raise LedgerError(f"{field} must be UTC in the form 2026-01-02T03:04:05Z")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_ledger(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise LedgerError(f"{path}:{lineno} must contain a JSON object")
        records.append(value)
    return records


def input_artifacts(paths: list[str]) -> list[dict[str, str]]:
    """把本次派发真正交给角色的中间契约落成 {path, digest}。

    摘要由脚本读文件算出，模型只负责点名路径：这样"我把 Context Contract 给了
    角色"要么有据可查，要么在写台账时就直接失败（文件不存在/不是文件），
    而不是等到步骤 18 才发现角色读的是复述。
    """
    entries: dict[str, dict[str, str]] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise LedgerError(f"--input must name an existing file handed to the role: {raw}")
        resolved = str(path.resolve())
        entries[resolved] = {"path": resolved, "digest": sha256(path.read_bytes())}
    return [entries[key] for key in sorted(entries)]


def build_record(args: argparse.Namespace) -> dict[str, object]:
    if args.role not in ROLES:
        raise LedgerError(f"--role must be one of {sorted(ROLES)}")
    if args.returned_status not in RETURNED_STATUSES:
        raise LedgerError(f"--returned-status must be one of {sorted(RETURNED_STATUSES)}")
    if not re.fullmatch(r"\d+", args.step) or not 1 <= int(args.step) <= 22:
        raise LedgerError("--step must be an integer between 01 and 22 (the SKILL step number)")
    if not args.instance_id.strip():
        raise LedgerError("--instance-id must be a non-empty identifier used verbatim as coverage owner")
    if not DIGEST_RE.fullmatch(args.input_diff_digest):
        raise LedgerError("--input-diff-digest must be the lowercase SHA-256 diff_digest of the frozen snapshot")
    started = parse_timestamp(args.started_at, "--started-at")
    ended = parse_timestamp(args.ended_at, "--ended-at")
    if ended < started:
        raise LedgerError("--ended-at must not precede --started-at")
    return {
        "step": int(args.step),
        "role": args.role,
        "instance_id": args.instance_id,
        "input_diff_digest": args.input_diff_digest,
        "inputs": input_artifacts(list(getattr(args, "input", None) or [])),
        "returned_status": args.returned_status,
        "started_at": args.started_at,
        "ended_at": args.ended_at,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True, help="path to dispatch-ledger.jsonl")
    parser.add_argument("--step", required=True, help="SKILL step number of this dispatch, e.g. 08")
    parser.add_argument("--role", required=True, help=f"dispatched role, one of {sorted(ROLES)}")
    parser.add_argument("--instance-id", required=True, help="unique id for this role instance, e.g. SP-01")
    parser.add_argument("--input-diff-digest", required=True, help="diff_digest the subagent was given")
    parser.add_argument("--returned-status", required=True, help=f"one of {sorted(RETURNED_STATUSES)}")
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="PATH",
        help="intermediate contract file passed to this role by path (steps 03/05/07/09/11/16/17 "
        "artifacts under $RUN_DIR/steps/); repeatable, digests are hashed by this script",
    )
    parser.add_argument("--started-at", default=None, help="UTC timestamp; defaults to now")
    parser.add_argument("--ended-at", default=None, help="UTC timestamp; defaults to now")
    args = parser.parse_args()
    _reconfigure_streams()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    args.started_at = args.started_at or now
    args.ended_at = args.ended_at or now
    try:
        record = build_record(args)
        existing = read_ledger(args.ledger)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if len(existing) + 1 > MAX_DISPATCHES:
        print(
            f"ERROR: {args.ledger} already holds {len(existing)} dispatch(es); the budget is "
            f"MAX_DISPATCHES={MAX_DISPATCHES}. Do not dispatch again — write the report as BLOCKED "
            "and list the dimensions, candidates and checks that stayed uncovered (step 22 fallback).",
            file=sys.stderr,
        )
        return 2
    for prior in existing:
        if prior.get("role") == record["role"] and prior.get("instance_id") == record["instance_id"]:
            print(
                f"ERROR: {record['role']}/{record['instance_id']} is already in {args.ledger}; "
                "use a new --instance-id for a fresh dispatch instead of reusing one.",
                file=sys.stderr,
            )
            return 2
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        print(f"ERROR: unable to append to {args.ledger}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
