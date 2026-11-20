#!/usr/bin/env python3
"""product-doc 独立评审轮次与 Finding 闭环台账。

主 Agent 用它推进状态，而不是凭记忆或子代理的一句话宣布“已审查 / 已修复”：
轮次上限、视角路由、稿件摘要一致性、阻塞项闭环与评审回执入档都由本脚本判定。

用法：
    review_state.py init   --state prd-review.json --prd product-doc.md --mode new --scale M --input SRC-001=note.md
    review_state.py record --state prd-review.json --report round1.json
    review_state.py close  --state prd-review.json --finding PR-001 --resolution fixed --evidence "已按 SRC-001 修正 4.1"
    review_state.py advance --state prd-review.json [--mode full|delta]
    review_state.py verify  --state prd-review.json [--allow-blocked]
    review_state.py show    --state prd-review.json
    review_state.py digest  --prd product-doc.md
    review_state.py render-appendix --report round1.json
    review_state.py render-report   --report round1.json
    review_state.py close   --state prd-review.json --batch closures.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

CONTRACT_VERSION = "product-review-v1"
SCHEMA_VERSION = "1.0"
MAX_ROUNDS = 5
STATUSES = ("APPROVE", "APPROVE_WITH_FOLLOW_UPS", "REQUEST_CHANGES", "BLOCKED")
SEVERITIES = ("Blocker", "High", "Medium", "Low")
BLOCKING_SEVERITIES = {"Blocker", "High"}
CATEGORIES = (
    "TEMPLATE", "EVIDENCE", "TRACEABILITY", "CONTENT", "NON_FUNCTIONAL",
    "METRICS", "ACCEPTANCE", "CHANGE_HISTORY", "RISK",
)
PERSPECTIVES = ("COMPLETENESS", "NON_FUNCTIONAL", "COMPREHENSIVE", "RECHECK")
REQUIRED_PERSPECTIVES = {
    "S": {"COMPREHENSIVE"},
    "M": {"COMPLETENESS", "NON_FUNCTIONAL"},
    "L": {"COMPLETENESS", "NON_FUNCTIONAL"},
}
DELTA_PERSPECTIVES = {"RECHECK"}
REPORT_FIELDS = (
    "review_id", "contract_version", "review_round", "perspective",
    "scale", "prd_digest", "status", "summary", "findings", "prd_appendix_markdown",
)
FINDING_FIELDS = (
    "id", "severity", "category", "location", "evidence",
    "impact", "remediation", "verification",
)
RESOLUTIONS = ("fixed", "deferred", "rejected")


class GateError(RuntimeError):
    """状态推进或报告校验失败；调用方必须以非 0 退出码停止交付。"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 文末回执区是评审完成后的追加产物，不得计入“被评审稿件”的指纹；
# 否则“把评审意见归档”本身会被当成一次修订，既让轮次门禁形同虚设，
# 又让交付校验永远报“评审后又改过稿”。
APPENDIX_HEADING = re.compile(r"^#{1,6}[ \t]*独立评审意见")


def without_appendix(lines: list[str]) -> list[str]:
    for index, raw in enumerate(lines):
        match = APPENDIX_HEADING.match(raw)
        if not match:
            continue
        level = len(raw) - len(raw.lstrip("#"))
        for cursor in range(index + 1, len(lines)):
            candidate = lines[cursor]
            if candidate.startswith("#") and len(candidate) - len(candidate.lstrip("#")) <= level:
                return lines[:index] + without_appendix(lines[cursor:])
        return lines[:index]
    return lines


def content_digest(path: Path) -> str:
    """评审对象的指纹：正文变则变，追加回执不变。"""
    body = "\n".join(without_appendix(path.read_text(encoding="utf-8").splitlines())).rstrip()
    return hashlib.sha256((body + "\n").encode("utf-8")).hexdigest()


def digest_matches(reported: str, actual: str) -> bool:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", reported or "")
    return len(cleaned) >= 12 and actual.lower().startswith(cleaned.lower())


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GateError(f"无法读取台账 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"台账 {path} 不是合法 JSON: {exc}") from exc
    if not isinstance(state, dict) or "rounds" not in state:
        raise GateError(f"台账 {path} 结构不合法")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class InputRef(NamedTuple):
    source_id: str
    path: Path


def parse_input_ref(raw: str, index: int) -> InputRef:
    if "=" in raw:
        source_id, _, value = raw.partition("=")
        if re.fullmatch(r"SRC-\d+", source_id.strip()):
            return InputRef(source_id.strip(), Path(value.strip()))
    return InputRef(f"SRC-{index:03d}", Path(raw))


def history_row_count(prd: Path) -> int:
    """统计变更历史台账的数据行数，用于检测历史被删改（倒退）。"""
    try:
        lines = prd.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError(f"无法读取 {prd}: {exc}") from exc
    start = None
    level = 0
    for number, raw in enumerate(lines):
        if raw.startswith("#") and "变更历史" in raw:
            start = number
            level = len(raw) - len(raw.lstrip("#"))
            break
    if start is None:
        return 0
    rows = 0
    for raw in lines[start + 1 :]:
        if raw.startswith("#") and len(raw) - len(raw.lstrip("#")) <= level:
            break
        stripped = raw.strip()
        if stripped.startswith("|") and not re.fullmatch(r"[|\s:-]+", stripped):
            rows += 1
    # 表头与内容行的判定：减去表头行，保留数据行数量。
    return max(rows - 1, 0)


def open_findings(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in state["findings"].values() if item["status"] == "OPEN"]


def blocking_open_findings(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in open_findings(state) if item["blocking"]]


def round_reports(state: dict[str, Any], number: int) -> list[dict[str, Any]]:
    for round_entry in state["rounds"]:
        if round_entry["round"] == number:
            return round_entry["reports"]
    return []


def round_entry_of(state: dict[str, Any], number: int) -> dict[str, Any]:
    for round_entry in state["rounds"]:
        if round_entry["round"] == number:
            return round_entry
    return {}


def required_perspectives(state: dict[str, Any], entry: dict[str, Any]) -> set[str]:
    """增量复核轮只要求 RECHECK；全量轮按规模路由（旧台账无 mode 字段时视为全量）。"""
    if entry.get("mode", "full") == "delta":
        return set(DELTA_PERSPECTIVES)
    return set(REQUIRED_PERSPECTIVES[state["scale"]])


def validate_finding(raw: Any, review_round: int, seen_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GateError(f"第 {review_round} 轮 finding 必须是对象：{raw!r}")
    missing = [field for field in FINDING_FIELDS if not str(raw.get(field, "")).strip()]
    if missing:
        raise GateError(f"Finding {raw.get('id', '?')} 缺少非空字段：{', '.join(missing)}")
    identifier = str(raw["id"]).strip()
    if not re.fullmatch(r"PR-\d{2,4}", identifier):
        raise GateError(f"Finding id 必须形如 PR-001：{identifier}")
    if identifier in seen_ids:
        raise GateError(f"同一报告内 Finding id 重复：{identifier}")
    seen_ids.add(identifier)
    severity = raw["severity"]
    category = raw["category"]
    if severity not in SEVERITIES:
        raise GateError(f"{identifier} 的 severity `{severity}` 非法（只允许 {', '.join(SEVERITIES)}）")
    if category not in CATEGORIES:
        raise GateError(f"{identifier} 的 category `{category}` 非法（只允许 {', '.join(CATEGORIES)}）")
    blocking = raw.get("blocking")
    if not isinstance(blocking, bool):
        blocking = severity in BLOCKING_SEVERITIES
    if severity in BLOCKING_SEVERITIES and not blocking:
        raise GateError(f"{identifier} 为 {severity}，不得标记 blocking=false 以绕过闭环")
    first_seen = raw.get("first_seen_round", review_round)
    if not isinstance(first_seen, int) or first_seen < 1 or first_seen > review_round:
        raise GateError(f"{identifier} 的 first_seen_round 必须落在 1..{review_round}")
    return {
        "id": identifier,
        "severity": severity,
        "category": category,
        "location": str(raw["location"]).strip(),
        "evidence": str(raw["evidence"]).strip(),
        "impact": str(raw["impact"]).strip(),
        "remediation": str(raw["remediation"]).strip(),
        "verification": str(raw["verification"]).strip(),
        "blocking": blocking,
        "first_seen_round": first_seen,
        "raised_in_round": review_round,
    }


def validate_report(payload: Any, state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise GateError("报告负载必须是 JSON 对象")
    missing = [
        field for field in REPORT_FIELDS
        if field != "findings" and (field not in payload or payload[field] in ("", None))
    ]
    if missing:
        raise GateError(f"报告缺少契约字段：{', '.join(missing)}")
    if not isinstance(payload["findings"], list):
        raise GateError("findings 必须是数组（无问题时为空数组）")
    if payload["contract_version"] != CONTRACT_VERSION:
        raise GateError(f"contract_version 必须为 {CONTRACT_VERSION}")
    if payload.get("review_round") != state["current_round"]:
        raise GateError(
            f"报告 review_round={payload.get('review_round')} 与台账当前轮次 {state['current_round']} 不一致；"
            "不得复用上轮结论"
        )
    if payload.get("scale") != state["scale"]:
        raise GateError(f"报告 scale={payload.get('scale')} 与需求规模 {state['scale']} 不一致")
    status = payload["status"]
    if status not in STATUSES:
        raise GateError(f"status `{status}` 非法（只允许 {', '.join(STATUSES)}）")
    perspective = payload["perspective"]
    if perspective not in PERSPECTIVES:
        raise GateError(f"perspective `{perspective}` 非法（只允许 {', '.join(PERSPECTIVES)}）")
    current_entry = round_entry_of(state, state["current_round"])
    if current_entry.get("mode", "full") == "delta":
        if perspective != "RECHECK":
            raise GateError(f"增量复核轮只接受视角 RECHECK，不接受 {perspective}")
    elif perspective not in REQUIRED_PERSPECTIVES[state["scale"]]:
        raise GateError(f"{state['scale']} 级需求不接受视角 {perspective}；应为 {sorted(REQUIRED_PERSPECTIVES[state['scale']])}")
    prd = Path(state["prd_path"])
    actual = content_digest(prd)
    if not digest_matches(str(payload["prd_digest"]), actual):
        raise GateError(
            f"报告 prd_digest 与当前稿件摘要不一致（评审对象已失效）：{payload['prd_digest']} != {actual[:12]}"
        )
    existing = round_reports(state, state["current_round"])
    if any(item["review_id"] == payload["review_id"] for item in existing):
        raise GateError(f"review_id {payload['review_id']} 在本轮已记录，不得重复计入")
    if any(item["perspective"] == perspective for item in existing):
        raise GateError(f"本轮已有 {perspective} 视角的报告；同一视角不得记录两次")
    seen_ids: set[str] = set()
    findings = [validate_finding(item, state["current_round"], seen_ids) for item in payload["findings"]]
    blocking = [item for item in findings if item["blocking"]]
    follow_ups = [item for item in findings if not item["blocking"]]
    still_open = blocking_open_findings(state)
    if status == "BLOCKED":
        if not str(payload.get("blocked_reason", "")).strip():
            raise GateError("status=BLOCKED 必须给出 blocked_reason")
    elif not str(payload.get("summary", "")).strip():
        raise GateError("summary 不得为空")
    if status == "REQUEST_CHANGES" and not blocking:
        raise GateError("status=REQUEST_CHANGES 要求至少一条 blocking finding")
    if status in {"APPROVE", "APPROVE_WITH_FOLLOW_UPS"} and blocking:
        raise GateError("存在 blocking finding 时不得给出 APPROVE/APPROVE_WITH_FOLLOW_UPS")
    if status == "APPROVE" and (follow_ups or (still_open and perspective != "RECHECK")):
        raise GateError(
            "status=APPROVE 要求本次无 Finding 且台账中无未闭环项；未闭环："
            + ", ".join(item["id"] for item in still_open + follow_ups)
        )
    if status == "APPROVE_WITH_FOLLOW_UPS" and not follow_ups:
        raise GateError("status=APPROVE_WITH_FOLLOW_UPS 要求存在非阻塞跟进项")
    verified = payload.get("verified_findings", [])
    if not isinstance(verified, list):
        raise GateError("verified_findings 必须是数组")
    if perspective != "RECHECK":
        if verified:
            raise GateError("verified_findings 只允许出现在 RECHECK 视角的报告中")
    else:
        # 增量复核的 APPROVE 只表示“被复核项已修复”，未闭环项随后由 close 归档。
        for identifier in verified:
            item = state["findings"].get(str(identifier))
            if not item or item["status"] != "OPEN":
                raise GateError(f"verified_findings 中的 {identifier} 必须是台账中未闭环的 Finding")
        if status == "APPROVE" and still_open and not verified:
            raise GateError("RECHECK 的 APPROVE 不得空确认：台账仍有未闭环项时 verified_findings 不得为空")
    if not str(payload.get("prd_appendix_markdown", "")).strip():
        raise GateError("prd_appendix_markdown 为空；评审意见必须可原样追加到 prd-reviews.md")
    report = {
        "review_id": str(payload["review_id"]),
        "perspective": perspective,
        "status": status,
        "prd_digest": actual,
        "summary": str(payload["summary"]),
        "blocked_reason": str(payload.get("blocked_reason", "")),
        "appendix_digest": hashlib.sha256(str(payload["prd_appendix_markdown"]).encode("utf-8")).hexdigest(),
        "verified_findings": [str(item) for item in verified],
        "recorded_at": now(),
    }
    return report, findings


def command_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    prd = Path(args.prd).resolve()
    if not prd.is_file():
        raise GateError(f"PRD 不存在或不可读：{prd}")
    if state_path.exists() and not args.force:
        raise GateError(f"台账已存在：{state_path}（继续既有轮次请直接使用 record；重跑需 --force）")
    inputs: list[dict[str, Any]] = []
    for index, raw in enumerate(args.input or [], start=1):
        ref = parse_input_ref(raw, index)
        if not ref.path.is_file():
            raise GateError(f"输入资料不存在：{ref.source_id} -> {ref.path}")
        inputs.append(
            {
                "source_id": ref.source_id,
                "path": str(ref.path.resolve()),
                "sha256": sha256_file(ref.path),
                "bytes": ref.path.stat().st_size,
            }
        )
    if not inputs:
        raise GateError("必须至少登记一份输入资料（--input SRC-001=<路径>）；无来源时不得启动 Writer")
    state = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "max_rounds": MAX_ROUNDS,
        "current_round": 1,
        "status": "IN_PROGRESS",
        "mode": args.mode,
        "scale": args.scale,
        "prd_path": str(prd),
        "prd_digest": content_digest(prd),
        "history_rows": history_row_count(prd),
        "inputs": inputs,
        "rounds": [{"round": 1, "mode": "full", "opened_at": now(), "prd_digest": content_digest(prd), "reports": []}],
        "findings": {},
        "transitions": [{"at": now(), "event": "init", "detail": f"mode={args.mode} scale={args.scale}"}],
    }
    save_state(state_path, state)
    print(f"INIT: 台账已建立 {state_path}（round=1，scale={args.scale}，inputs={len(inputs)}）")
    return 0


def command_record(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    if state["status"] != "IN_PROGRESS":
        raise GateError(f"台账状态为 {state['status']}，不得继续记录评审报告")
    try:
        payload = json.loads(sys.stdin.read() if args.report == "-" else Path(args.report).read_text(encoding="utf-8"))
    except OSError as exc:
        raise GateError(f"无法读取报告：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"报告不是合法 JSON：{exc}") from exc
    report, findings = validate_report(payload, state)
    entry = round_reports(state, state["current_round"])
    entry.append(report)
    for finding in findings:
        finding["raised_digest"] = report["prd_digest"]
        existing = state["findings"].get(finding["id"])
        if existing and existing["status"] == "OPEN":
            # 同一个 Finding 在多个轮次里反复出现且未闭环时，刷新证据但保留最早出现轮次。
            finding["first_seen_round"] = min(existing["first_seen_round"], finding["first_seen_round"])
        state["findings"][finding["id"]] = {**finding, "status": "OPEN", "resolution": None, "closed_in_round": None,
                                            "closure_evidence": "", "q_ref": ""}
    required = required_perspectives(state, round_entry_of(state, state["current_round"]))
    got = {item["perspective"] for item in entry}
    remaining = sorted(required - got)
    state["transitions"].append({"at": now(), "event": "record", "detail": f"{report['review_id']} -> {report['status']}"})
    if report["status"] == "BLOCKED":
        state["status"] = "BLOCKED"
    save_state(state_path, state)
    print(f"RECORDED: {report['review_id']} status={report['status']}；本轮仍缺少视角：{remaining or '无（可 close/advance）'}")
    return 0


def close_finding(state: dict[str, Any], identifier: str, resolution: str, evidence: str, q_ref: str) -> None:
    """校验并关闭一条 Finding；只改内存中的台账，落盘由调用方统一完成。"""
    if resolution not in RESOLUTIONS:
        raise GateError(f"resolution `{resolution}` 非法（只允许 {', '.join(RESOLUTIONS)}）")
    finding = state["findings"].get(identifier)
    if not finding:
        raise GateError(f"台账中没有 Finding {identifier}；只能关闭 Reviewer 实际提出的问题")
    if finding["status"] == "CLOSED":
        raise GateError(f"Finding {identifier} 已在第 {finding['closed_in_round']} 轮闭环，不得重复关闭")
    evidence = evidence.strip()
    if len(evidence) < 10:
        raise GateError("closure 证据过短；必须说明依据哪份资料或哪条契约做了什么修改")
    if resolution == "fixed":
        if state["current_round"] <= finding["raised_in_round"]:
            raise GateError(
                f"Finding {identifier} 提出于第 {finding['raised_in_round']} 轮，"
                "标记为 fixed 前必须先 advance 到下一轮并由独立 Reviewer 复核"
            )
        # 存在增量复核时，fixed 必须消费 RECHECK 的确认，不得由主 Agent 口头自称修复。
        recheck_reports = [
            report
            for round_entry in state["rounds"]
            if round_entry["round"] > finding["raised_in_round"]
            for report in round_entry["reports"]
            if report["perspective"] == "RECHECK"
        ]
        if recheck_reports and not any(
            identifier in report.get("verified_findings", []) for report in recheck_reports
        ):
            raise GateError(
                f"Finding {identifier} 未出现在任何后续 RECHECK 报告的 verified_findings 中，"
                "不得以 fixed 关闭；请先由增量复核确认修复"
            )
    if resolution == "deferred":
        if not re.fullmatch(r"Q-\d+", q_ref or ""):
            raise GateError("deferred 必须通过 --q-ref 指向 9.2 中已建立的 Q 编号")
        prd_text = Path(state["prd_path"]).read_text(encoding="utf-8")
        if q_ref not in prd_text:
            raise GateError(f"{q_ref} 未出现在 PRD 的 9.2 决策与待决问题中，不得以“待确认”关闭")
    if resolution == "rejected":
        if finding["severity"] in BLOCKING_SEVERITIES:
            raise GateError(f"{finding['severity']} 级 Finding 不得判定为不采纳；只能修复或转 Q 待确认")
        if len(evidence) < 20:
            raise GateError("不采纳必须给出更长理由（≥20 字符），说明为何不构成文档缺陷")
    finding.update(
        status="CLOSED",
        resolution=resolution,
        closed_in_round=state["current_round"],
        closure_evidence=evidence,
        q_ref=q_ref or "",
    )
    state["transitions"].append({"at": now(), "event": "close", "detail": f"{identifier} -> {resolution}"})


def command_close(args: argparse.Namespace) -> int:
    if args.batch and (args.finding or args.resolution or args.evidence):
        raise GateError("--batch 与 --finding/--resolution/--evidence 不得同时使用")
    if args.batch:
        try:
            items = json.loads(sys.stdin.read() if args.batch == "-" else Path(args.batch).read_text(encoding="utf-8"))
        except OSError as exc:
            raise GateError(f"无法读取批量闭环清单：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise GateError(f"批量闭环清单不是合法 JSON：{exc}") from exc
        if not isinstance(items, list) or not items:
            raise GateError("批量闭环清单必须是非空数组，每项含 finding/resolution/evidence（deferred 另需 q_ref）")
    elif not (args.finding and args.resolution and args.evidence):
        raise GateError("单条闭环需要 --finding/--resolution/--evidence；批量闭环请使用 --batch")
    state_path = Path(args.state)
    state = load_state(state_path)
    if args.batch:
        # 逐条校验与单条模式完全一致；任一条失败则整批不落盘，避免半批闭环。
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise GateError(f"批量闭环第 {index} 项必须是对象")
            close_finding(
                state,
                str(item.get("finding", "")).strip(),
                str(item.get("resolution", "")).strip(),
                str(item.get("evidence", "")),
                str(item.get("q_ref", "")).strip(),
            )
        save_state(state_path, state)
        print(f"CLOSED: 批量闭环 {len(items)} 条 Finding")
        return 0
    close_finding(state, args.finding, args.resolution, args.evidence, args.q_ref or "")
    save_state(state_path, state)
    print(f"CLOSED: {args.finding} ({args.resolution})")
    return 0


def command_advance(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    if state["status"] != "IN_PROGRESS":
        raise GateError(f"台账状态为 {state['status']}，不得推进轮次")
    number = state["current_round"]
    entry = round_reports(state, number)
    current = round_entry_of(state, number)
    required = required_perspectives(state, current)
    got = {item["perspective"] for item in entry}
    if required - got:
        raise GateError(f"第 {number} 轮尚未收齐独立评审：缺少 {', '.join(sorted(required - got))}")
    if not entry:
        raise GateError(f"第 {number} 轮没有评审记录")
    if args.mode == "delta":
        if current.get("mode", "full") != "full":
            raise GateError("增量复核不得连续进行：上一轮已是 delta，本轮修复后必须回到全量评审")
        if not open_findings(state):
            raise GateError("没有未闭环 Finding，无需增量复核；请直接 verify 或推进全量轮")
        if number + 1 >= state["max_rounds"]:
            raise GateError("最终轮必须是全量评审，不得以增量复核轮作为最后一轮")
    prd = Path(state["prd_path"])
    digest = content_digest(prd)
    rows = history_row_count(prd)
    if rows < state["history_rows"]:
        raise GateError(f"变更历史台账从 {state['history_rows']} 行减少到 {rows} 行：历史不得删除、覆盖或重排")
    state["history_rows"] = rows
    # 先判轮次上限：第 5 轮已收齐仍有未闭环阻塞项时，结论必须是 BLOCKED，
    # 不能再要求“先改稿”——那会把阻塞交付退化成无限循环。
    if number >= state["max_rounds"]:
        if blocking_open_findings(state):
            state["status"] = "BLOCKED"
            state["transitions"].append({"at": now(), "event": "blocked", "detail": f"round={number} 仍有未闭环阻塞项"})
            save_state(state_path, state)
            raise GateError(f"已达 {MAX_ROUNDS} 轮上限且仍有未闭环阻塞项，必须进入阻塞交付")
        state["transitions"].append({"at": now(), "event": "max_round", "detail": f"round={number} 完成"})
        save_state(state_path, state)
        print(f"ADVANCE: 已达第 {number} 轮（上限），无未闭环阻塞项，可直接 verify")
        return 0
    reviewed = {item["prd_digest"] for item in entry}
    if blocking_open_findings(state) and digest in reviewed:
        raise GateError(
            "稿件正文与送审时完全一致，但仍有未闭环的阻塞项；必须先按 Finding 修改正文再推进"
        )
    state["current_round"] = number + 1
    state["rounds"].append({"round": number + 1, "mode": args.mode, "opened_at": now(), "prd_digest": digest, "reports": []})
    state["transitions"].append({"at": now(), "event": "advance", "detail": f"{number} -> {number + 1} ({args.mode})"})
    save_state(state_path, state)
    print(f"ADVANCE: 进入第 {number + 1} 轮（{args.mode}，送审稿件摘要 {digest[:12]}）")
    return 0


def verify_delivery(state: dict[str, Any], allow_blocked: bool) -> list[str]:
    problems: list[str] = []
    prd = Path(state["prd_path"])
    if not prd.is_file():
        return [f"PRD 不存在：{prd}"]
    text = prd.read_text(encoding="utf-8")
    digest = content_digest(prd)
    recorded_rounds = [entry for entry in state["rounds"] if entry["reports"]]
    if not recorded_rounds:
        problems.append("没有任何已记录的独立评审轮次")
    blocking = blocking_open_findings(state)
    if allow_blocked:
        if state["status"] != "BLOCKED" and not blocking:
            problems.append("--allow-blocked 用于阻塞交付，但台账既非 BLOCKED 也无未闭环阻塞项")
    else:
        if blocking:
            problems.append("仍有未闭环阻塞项：" + ", ".join(f"{item['id']}({item['severity']})" for item in blocking))
        if state["status"] == "BLOCKED":
            problems.append("台账已判定 BLOCKED；如需交付阻塞报告请使用 --allow-blocked")
        if not recorded_rounds:
            problems.append("独立评审未执行，不得交付")
    last = recorded_rounds[-1] if recorded_rounds else None
    if last:
        if not allow_blocked and last.get("mode", "full") != "full":
            problems.append("最后一轮是增量复核（delta），交付前必须完成一轮全量评审")
        perspectives = {item["perspective"] for item in last["reports"]}
        missing = required_perspectives(state, last) - perspectives
        if missing:
            problems.append(f"最后一轮（第 {last['round']} 轮）缺少视角：{', '.join(sorted(missing))}")
        statuses = {item["status"] for item in last["reports"]}
        if not allow_blocked and not statuses <= {"APPROVE", "APPROVE_WITH_FOLLOW_UPS"}:
            problems.append(f"最后一轮状态含 {', '.join(sorted(statuses - {'APPROVE', 'APPROVE_WITH_FOLLOW_UPS'}))}，不得按已确认交付")
        if digest != last["reports"][-1]["prd_digest"]:
            problems.append("PRD 在最后一次评审之后又被修改，但未进行下一轮复核")
    reviews_path = prd.parent / "prd-reviews.md"
    reviews_text = reviews_path.read_text(encoding="utf-8") if reviews_path.is_file() else ""
    for entry in recorded_rounds:
        for report in entry["reports"]:
            if report["review_id"] not in text:
                problems.append(f"{report['review_id']} 的回执块未追加进 PRD 的独立评审意见章节")
            if report["review_id"] not in reviews_text:
                problems.append(f"{report['review_id']} 的完整意见未归档进同目录 prd-reviews.md")
    for finding in state["findings"].values():
        if finding["status"] == "CLOSED" and finding["resolution"] == "fixed":
            later = [
                item
                for round_entry in recorded_rounds
                if round_entry["round"] > finding["raised_in_round"]
                for item in round_entry["reports"]
            ]
            if not later:
                problems.append(f"{finding['id']} 标记为 fixed 但没有提出后的复核轮次")
            elif all(item["prd_digest"] == finding.get("raised_digest") for item in later):
                problems.append(f"{finding['id']} 标记为 fixed，但复核轮次看到的仍是提出时的同一份稿件")
    if history_row_count(prd) < state["history_rows"]:
        problems.append("变更历史台账行数倒退")
    return problems


def command_verify(args: argparse.Namespace) -> int:
    state = load_state(Path(args.state))
    problems = verify_delivery(state, args.allow_blocked)
    if problems:
        print(f"INVALID: 独立评审门禁未通过（{len(problems)} 项）", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    state["status"] = "BLOCKED" if args.allow_blocked else "APPROVED"
    state["transitions"].append({"at": now(), "event": "verify", "detail": state["status"]})
    save_state(Path(args.state), state)
    print(f"VALID: 评审门禁通过（status={state['status']}，rounds={len([e for e in state['rounds'] if e['reports']])}）")
    return 0


def command_digest(args: argparse.Namespace) -> int:
    prd = Path(args.prd)
    if not prd.is_file():
        raise GateError(f"PRD 不存在或不可读：{prd}")
    print(content_digest(prd))
    return 0


def _escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _receipt_parts(payload: Any) -> tuple[str, str, int, str, list[str]]:
    """校验回执必需字段，返回 (review_id, perspective, 轮次, 回执表格 Markdown, Finding 紧凑清单)。

    只做字段与枚举检查；轮次、指纹与 Finding 闭环仍由 record 判定。
    """
    if not isinstance(payload, dict):
        raise GateError("报告负载必须是 JSON 对象")
    required = ("review_id", "review_round", "perspective", "status", "prd_digest", "prd_appendix_markdown")
    missing = [field for field in required if not str(payload.get(field, "")).strip()]
    if missing:
        raise GateError(f"报告缺少回执必需字段：{', '.join(missing)}")
    review_id = str(payload["review_id"]).strip()
    perspective = str(payload["perspective"]).strip()
    status = str(payload["status"]).strip()
    if perspective not in PERSPECTIVES:
        raise GateError(f"perspective `{perspective}` 非法（只允许 {', '.join(PERSPECTIVES)}）")
    if status not in STATUSES:
        raise GateError(f"status `{status}` 非法（只允许 {', '.join(STATUSES)}）")
    digest = re.sub(r"[^0-9a-fA-F]", "", str(payload["prd_digest"]))
    if len(digest) < 12:
        raise GateError("prd_digest 不足 12 位十六进制，无法生成可校验的稿件摘要行")
    try:
        round_number = int(payload["review_round"])
    except (TypeError, ValueError):
        raise GateError("review_round 必须是整数") from None
    if round_number < 1:
        raise GateError("review_round 必须从 1 开始")
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise GateError("findings 必须是数组（无问题时为空数组）")
    table = [
        "| 回执项 | 内容 |",
        "|--------|------|",
        f"| 稿件摘要 | {digest} |",
        f"| 状态 | {status} |",
        f"| 摘要 | {_escape_cell(payload.get('summary', ''))} |",
    ]
    compact: list[str] = []
    for item in findings:
        if not isinstance(item, dict):
            raise GateError("Finding 必须是对象")
        compact.append(
            f"- {_escape_cell(item.get('id', '?'))}（{_escape_cell(item.get('severity', '?'))} / "
            f"{_escape_cell(item.get('category', '?'))}，"
            f"blocking={str(item.get('blocking', '')).lower()}）：{_escape_cell(item.get('location', ''))}"
        )
    return review_id, perspective, round_number, "\n".join(table), compact


def render_appendix_markdown(payload: Any) -> str:
    """渲染追加进 PRD「独立评审意见」章节的紧凑回执块；完整意见另由 render-report 归档到 prd-reviews.md。"""
    review_id, perspective, round_number, table, compact = _receipt_parts(payload)
    lines = [f"### {review_id} · {perspective} · 第 {round_number} 轮", "", table]
    if compact:
        lines += ["", "**Finding 清单**", "", *compact]
    lines += ["", f"> 完整意见见同目录 `prd-reviews.md` 的 {review_id} 节。", ""]
    return "\n".join(lines)


def render_report_markdown(payload: Any) -> str:
    """渲染追加进 prd-reviews.md 的完整评审报告节（含 Finding 明细与原始意见正文）。"""
    review_id, perspective, round_number, table, _ = _receipt_parts(payload)
    appendix = str(payload["prd_appendix_markdown"]).strip()
    # prd-reviews.md 以二级标题分节；意见正文里的一/二级标题会破坏分节结构。
    if any(line.startswith(("# ", "## ")) for line in appendix.splitlines()):
        raise GateError("prd_appendix_markdown 不得包含一级或二级标题（# / ##）；小节请用三级标题、加粗文本或列表表达")
    lines = [f"## {review_id} · {perspective} · 第 {round_number} 轮", "", table]
    findings = payload.get("findings", [])
    if findings:
        lines += [
            "", "**Finding 明细**", "",
            "| ID | 严重度 | 分类 | 位置 | 证据 | 影响 | 修复动作 | 验证方式 | blocking |",
            "|----|--------|------|------|------|------|----------|----------|----------|",
        ]
        for item in findings:
            lines.append(
                "| " + " | ".join(_escape_cell(item.get(field, "")) for field in (
                    "id", "severity", "category", "location", "evidence", "impact",
                    "remediation", "verification", "blocking")) + " |"
            )
    lines += ["", appendix, ""]
    return "\n".join(lines)


def _load_report_payload(raw: str) -> Any:
    try:
        return json.loads(sys.stdin.read() if raw == "-" else Path(raw).read_text(encoding="utf-8"))
    except OSError as exc:
        raise GateError(f"无法读取报告：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"报告不是合法 JSON：{exc}") from exc


def command_render_appendix(args: argparse.Namespace) -> int:
    print(render_appendix_markdown(_load_report_payload(args.report)))
    return 0


def command_render_report(args: argparse.Namespace) -> int:
    print(render_report_markdown(_load_report_payload(args.report)))
    return 0


def command_show(args: argparse.Namespace) -> int:
    state = load_state(Path(args.state))
    summary = {
        "status": state["status"],
        "mode": state["mode"],
        "scale": state["scale"],
        "current_round": state["current_round"],
        "max_rounds": state["max_rounds"],
        "inputs": [item["source_id"] for item in state["inputs"]],
        "rounds": {
            entry["round"]: [f"{item['review_id']}:{item['status']}" for item in entry["reports"]]
            for entry in state["rounds"]
        },
        "findings": {
            identifier: f"{item['severity']}|{item['status']}|{item['resolution'] or '-'}"
            for identifier, item in sorted(state["findings"].items())
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="建立台账并冻结输入与稿件摘要")
    init.add_argument("--state", required=True)
    init.add_argument("--prd", required=True)
    init.add_argument("--mode", required=True, choices=("new", "change"))
    init.add_argument("--scale", required=True, choices=("S", "M", "L"))
    init.add_argument("--input", action="append", help="SRC-001=<路径>（可重复）")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    record = sub.add_parser("record", help="记录一份独立 Reviewer 报告")
    record.add_argument("--state", required=True)
    record.add_argument("--report", required=True, help="规范化 JSON 报告路径，或 - 读取 stdin")
    record.set_defaults(func=command_record)

    close = sub.add_parser("close", help="关闭一条或一批 Finding")
    close.add_argument("--state", required=True)
    close.add_argument("--finding", help="单条闭环的 Finding id")
    close.add_argument("--resolution", choices=RESOLUTIONS, help="单条闭环的处理结果")
    close.add_argument("--evidence", help="单条闭环的证据说明")
    close.add_argument("--q-ref", default="")
    close.add_argument("--batch", help="批量闭环 JSON 数组路径，或 - 读取 stdin；任一条失败整批不落盘")
    close.set_defaults(func=command_close)

    advance = sub.add_parser("advance", help="收齐本轮视角后推进到下一轮（full 全量 / delta 增量复核）")
    advance.add_argument("--state", required=True)
    advance.add_argument("--mode", choices=("full", "delta"), default="full")
    advance.set_defaults(func=command_advance)

    verify = sub.add_parser("verify", help="交付门禁")
    verify.add_argument("--state", required=True)
    verify.add_argument("--allow-blocked", action="store_true", help="以阻塞报告形式交付")
    verify.set_defaults(func=command_verify)

    show = sub.add_parser("show", help="输出台账摘要")
    show.add_argument("--state", required=True)
    show.set_defaults(func=command_show)

    digest = sub.add_parser("digest", help="输出稿件正文指纹（不含文末回执区），供派发封套使用")
    digest.add_argument("--prd", required=True)
    digest.set_defaults(func=command_digest)

    render = sub.add_parser("render-appendix", help="把规范化评审报告渲染为追加进 PRD 的紧凑回执块")
    render.add_argument("--report", required=True, help="规范化 JSON 报告路径，或 - 读取 stdin")
    render.set_defaults(func=command_render_appendix)

    render_full = sub.add_parser("render-report", help="把规范化评审报告渲染为追加进 prd-reviews.md 的完整意见节")
    render_full.add_argument("--report", required=True, help="规范化 JSON 报告路径，或 - 读取 stdin")
    render_full.set_defaults(func=command_render_report)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args) or 0)
    except GateError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"BLOCKED: 文件操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
