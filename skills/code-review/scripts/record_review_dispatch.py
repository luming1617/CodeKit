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


def compose_record(entry: dict[str, object], *, label: str = "") -> dict[str, object]:
    """单条与批量共用的记录校验（P0-3）：任何一条失败即抛 LedgerError。

    调用方决定落盘粒度：单条模式逐条追加，批量模式先全量校验再原子追加，
    两条路径走同一份谓词，批量不是绕过校验的运输层。
    """
    step = str(entry.get("step", ""))
    role = entry.get("role")
    instance_id = entry.get("instance_id")
    input_diff_digest = entry.get("input_diff_digest")
    returned_status = entry.get("returned_status")
    started_at = entry.get("started_at")
    ended_at = entry.get("ended_at")
    if role not in ROLES:
        raise LedgerError(f"{label}--role must be one of {sorted(ROLES)}")
    if returned_status not in RETURNED_STATUSES:
        raise LedgerError(f"{label}--returned-status must be one of {sorted(RETURNED_STATUSES)}")
    if not re.fullmatch(r"\d+", step) or not 1 <= int(step) <= 22:
        raise LedgerError(f"{label}--step must be an integer between 01 and 22 (the SKILL step number)")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise LedgerError(f"{label}--instance-id must be a non-empty identifier used verbatim as coverage owner")
    if not isinstance(input_diff_digest, str) or not DIGEST_RE.fullmatch(input_diff_digest):
        raise LedgerError(f"{label}--input-diff-digest must be the lowercase SHA-256 diff_digest of the frozen snapshot")
    if not isinstance(started_at, str) or not isinstance(ended_at, str):
        raise LedgerError(f"{label}--started-at and --ended-at are required UTC timestamps")
    started = parse_timestamp(started_at, f"{label}--started-at")
    ended = parse_timestamp(ended_at, f"{label}--ended-at")
    if ended < started:
        raise LedgerError(f"{label}--ended-at must not precede --started-at")
    input_paths = entry.get("inputs") or []
    if not isinstance(input_paths, list) or not all(isinstance(item, str) for item in input_paths):
        raise LedgerError(f"{label}--input must be a list of file paths handed to the role")
    return {
        "step": int(step),
        "role": role,
        "instance_id": instance_id,
        "input_diff_digest": input_diff_digest,
        "inputs": input_artifacts(input_paths),
        "returned_status": returned_status,
        "started_at": started_at,
        "ended_at": ended_at,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_record(args: argparse.Namespace) -> dict[str, object]:
    return compose_record({
        "step": args.step,
        "role": args.role,
        "instance_id": args.instance_id,
        "input_diff_digest": args.input_diff_digest,
        "returned_status": args.returned_status,
        "started_at": args.started_at,
        "ended_at": args.ended_at,
        "inputs": list(getattr(args, "input", None) or []),
    })


def build_batch(path: Path) -> list[dict[str, object]]:
    """读批量文件并逐条过 compose_record；时间戳必须显式给出，不默认 now。"""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"--batch must name a readable JSON file: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise LedgerError("--batch must contain a non-empty JSON array of dispatch records")
    records: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        label = f"batch[{index}]: "
        if not isinstance(entry, dict):
            raise LedgerError(f"{label}each batch entry must be a JSON object")
        if not entry.get("started_at") or not entry.get("ended_at"):
            raise LedgerError(f"{label}--started-at and --ended-at are required UTC timestamps")
        records.append(compose_record(entry, label=label))
    return records


def check_budget_and_duplicates(
    ledger: Path, existing: list[dict[str, object]], records: list[dict[str, object]]
) -> str | None:
    """预算与去重谓词，单条与批量共用；返回错误文案或 None。"""
    if len(existing) + len(records) > MAX_DISPATCHES:
        return (
            f"{ledger} already holds {len(existing)} dispatch(es); the budget is "
            f"MAX_DISPATCHES={MAX_DISPATCHES}. Do not dispatch again — write the report as BLOCKED "
            "and list the dimensions, candidates and checks that stayed uncovered (step 22 fallback)."
        )
    seen = {(prior.get("role"), prior.get("instance_id")) for prior in existing}
    for record in records:
        key = (record["role"], record["instance_id"])
        if key in seen:
            return (
                f"{record['role']}/{record['instance_id']} is already in {ledger}; "
                "use a new --instance-id for a fresh dispatch instead of reusing one."
            )
        seen.add(key)
    return None


SINGLE_FLAGS = (
    "step", "role", "instance_id", "input_diff_digest", "returned_status", "started_at", "ended_at",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True, help="path to dispatch-ledger.jsonl")
    parser.add_argument("--step", help="SKILL step number of this dispatch, e.g. 08")
    parser.add_argument("--role", help=f"dispatched role, one of {sorted(ROLES)}")
    parser.add_argument("--instance-id", help="unique id for this role instance, e.g. SP-01")
    parser.add_argument("--input-diff-digest", help="diff_digest the subagent was given")
    parser.add_argument("--returned-status", help=f"one of {sorted(RETURNED_STATUSES)}")
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
    parser.add_argument(
        "--batch",
        type=Path,
        default=None,
        metavar="FILE",
        help="JSON array of dispatch records (same fields as the single-record flags, timestamps "
        "required); validated as a whole and appended atomically — any invalid entry rejects the "
        "entire batch. Mutually exclusive with the single-record flags",
    )
    args = parser.parse_args()
    _reconfigure_streams()
    single_given = [name for name in SINGLE_FLAGS if getattr(args, name) is not None] + (
        ["input"] if args.input else []
    )
    if args.batch is not None:
        if single_given:
            print(
                "ERROR: --batch cannot be combined with single-record flags "
                f"({', '.join('--' + name for name in single_given)}); use one mode per invocation.",
                file=sys.stderr,
            )
            return 2
        try:
            records = build_batch(args.batch)
            existing = read_ledger(args.ledger)
        except (LedgerError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    else:
        missing = [
            name for name in ("step", "role", "instance_id", "input_diff_digest", "returned_status")
            if getattr(args, name) is None
        ]
        if missing:
            print(
                "ERROR: missing required flag(s) "
                f"{', '.join('--' + name for name in missing)} (or use --batch FILE).",
                file=sys.stderr,
            )
            return 2
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        args.started_at = args.started_at or now
        args.ended_at = args.ended_at or now
        try:
            records = [build_record(args)]
            existing = read_ledger(args.ledger)
        except (LedgerError, OSError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    problem = check_budget_and_duplicates(args.ledger, existing, records)
    if problem is not None:
        print(f"ERROR: {problem}", file=sys.stderr)
        return 2
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
            ))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        print(f"ERROR: unable to append to {args.ledger}: {exc}", file=sys.stderr)
        return 2
    if args.batch is not None:
        print(json.dumps({"ledger": str(args.ledger), "recorded": len(records)}, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(records[0], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
