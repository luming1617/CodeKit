#!/usr/bin/env python3
"""release-doc 独立评审轮次与 Finding 闭环台账。

主 Agent 用它推进状态，而不是凭记忆或子代理的一句话宣布“已审查 / 已修复”：
轮次上限、稿件摘要一致性、阻塞项闭环与评审回执入档都由本脚本判定。
评审流程的唯一事实源是 references/release-review-contract.md，本脚本是其机械实现。

用法：
    release_state.py init   --state release-review.json --rpd release-doc.md --mode new --input SRC-001=technical-doc.md
    release_state.py record --state release-review.json --report round1.json
    release_state.py close  --state release-review.json --finding RPR-001 --resolution fixed --evidence "已按 SRC-001 修正发布步骤"
    release_state.py advance --state release-review.json [--mode full|delta]
    release_state.py verify  --state release-review.json [--allow-blocked]
    release_state.py show    --state release-review.json
    release_state.py digest  --rpd release-doc.md
    release_state.py render-appendix --report round1.json
    release_state.py render-report   --report round1.json
    release_state.py close   --state release-review.json --batch closures.json
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

CONTRACT_VERSION = "release-review-v2"
SCHEMA_VERSION = "1.0"
MAX_ROUNDS = 5
STATUSES = ("APPROVE", "APPROVE_WITH_FOLLOW_UPS", "REQUEST_CHANGES", "BLOCKED")
SEVERITIES = ("Blocker", "High", "Medium", "Low")
BLOCKING_SEVERITIES = {"Blocker", "High"}
# P1-1 门停放宽：只有 blocking（Blocker/High）触发评审轮；Medium/Low 走非阻塞跟进。
PERSPECTIVES = ("COMPREHENSIVE", "RECHECK")
CATEGORIES = ("TEMPLATE", "EVIDENCE", "ORDER", "VERIFICATION", "OBSERVATION", "ROLLBACK", "RISK")
SCOPE = "release_plan_only"
REVIEW_ID_RE = re.compile(r"RPD-R\d+")
REPORT_FIELDS = (
    "review_id", "contract_version", "review_round", "perspective", "scale", "rpd_digest",
    "status", "scope", "summary", "findings", "appendix_markdown",
)
FINDING_FIELDS = (
    "id", "severity", "category", "location", "evidence",
    "impact", "remediation", "verification",
)
RESOLUTIONS = ("fixed", "deferred", "rejected")
REVIEWS_FILENAME = "release-reviews.md"
# 定向补丁优先：改动超过正文行数 30% 才允许整篇重写（与 product-doc 阈值一致）。
FULL_REWRITE_THRESHOLD = 0.30
# P1-2 机械补丁白名单：以 validate_release_doc.py 的错误码为唯一来源。
# STRUCT002 = 章节顺序（脚本确定性重排，内容逐字节不变）；
# STATUS003 = 状态枚举空白变形（仅允许去空白归一化，不得改换结论）。
# 其余错误码的修复需要事实或判断，一律走 Refiner + 独立评审。
MECHANICAL_PATCH_CODES = ("STRUCT002", "STATUS003")
# P1-3 规模裁剪：探针机械判定最低严格度，声明只能上调不能下调；
# 命中任一高风险信号强制 L 级全矩阵，章节只折叠不删除，交付轮仍全量。
SCALES = ("S", "M", "L")
SCALE_ORDER = {"S": 0, "M": 1, "L": 2}
RISK_FIELDS = ("是否涉及资金", "是否涉及账务", "是否涉及交易链路", "是否涉及用户资产")
INFRA_SECTIONS = ("MySQL", "Redis", "MQ / Kafka", "Nacos", "XXL-JOB")
HIGH_RISK_SQL = re.compile(
    r"\b(?:DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE\s+TABLE|TRUNCATE\s+(?!TABLE\b)\S+|ALTER\s+TABLE\s+\S+\s+DROP)\b",
    re.IGNORECASE,
)
HIGH_RISK_TEXT = ("不可逆数据迁移", "不可逆迁移", "删除表", "删除字段", "无标准回滚路径", "无标准回滚能力")
NEGATION_WORDS = ("不涉及", "无需", "不存在", "没有")
CHAOS_MODES = (
    "超时", "部分成功", "重复执行", "依赖不可用", "版本不兼容", "配置未生效",
    "消息积压", "数据异常", "流量放大", "监控失效", "回滚失败",
)
ALWAYS_CHAOS_MODES = (
    "超时", "部分成功", "重复执行", "依赖不可用", "版本不兼容", "流量放大", "监控失效", "回滚失败",
)

from validate_release_doc import (  # noqa: E402
    APPENDIX_TITLE_RE,
    CONCRETE_STATUSES,
    REQUIRED_SECTIONS,
    RpdValidator,
    build_sections,
    duplicate_sections,
    fence_flags,
    is_receipt_heading,
    parse_headings,
    parse_tables,
)


class GateError(RuntimeError):
    """状态推进或报告校验失败；调用方必须以非 0 退出码停止交付。"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 文末回执区是评审完成后的追加产物，不得计入“被评审稿件”的指纹；
# 否则“把评审意见归档”本身会被当成一次修订，既让轮次门禁形同虚设，
# 又让交付校验永远报“评审后又改过稿”。


def without_appendix(lines: list[str]) -> list[str]:
    """只剔除章节结构定位到的文末回执节。

    与 parse_headings 同一标题判定（# 后必须有空格）且排除围栏：正文中部的
    伪标题（含无空格变形）与历史快照里的旧回执标题都不参与剔除，
    不得在正文中制造指纹盲区；伪标题本身由验证器 REVIEW005 拒收。
    回执节之后若出现非回执块标题（REVIEW006 缺陷），其后的章节仍计入指纹，
    不得借追加非强制章节藏匿未经评审的发布口径。
    """
    headings = parse_headings(lines, fence_flags(lines))
    appendix = next(
        (heading for heading in reversed(headings) if APPENDIX_TITLE_RE.match(f"{'#' * heading.level} {heading.text}")),
        None,
    )
    if appendix is None:
        return lines
    end = len(lines)
    for heading in headings:
        if heading.line > appendix.line and not is_receipt_heading(heading):
            end = heading.line
            break
    return lines[: appendix.line] + lines[end:]


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


def probe_scale(rpd: Path) -> dict[str, Any]:
    """P1-3 规模探针：从 RPD 结构化字段与 SQL/文本信号机械判定最低严格度。

    高风险信号（资金/账务/交易/用户资产=是、不可逆迁移/删表删字段 SQL、无标准回滚）→ L；
    多服务/适用基础设施/风险字段未决/停机降级 → 至少 M；单服务+无基础设施+风险全否 → S。
    引语（模板提示语）与否定句不算信号；风险字段未决不得判 S（fail-closed）。
    """
    text = rpd.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_fence = fence_flags(lines)
    headings = parse_headings(lines, in_fence)
    sections = build_sections(headings, len(lines))

    def section_of(name: str) -> Any:
        return next((item for item in sections if item.text.startswith(name)), None)

    risk_values: dict[str, str] = {}
    extra_flags: dict[str, str] = {}
    risk_row_counts: dict[str, int] = {}
    key = section_of("关键信息")
    if key is not None:
        for table in parse_tables(lines, in_fence, key):
            # 与验证器同口径：只认项目/内容表且 first-wins，异表头诱饵表不参与判定。
            if table.header[:2] != ["项目", "内容"]:
                continue
            for row in table.rows:
                if len(row) < 2:
                    continue
                for field in RISK_FIELDS:
                    if field in row[0]:
                        risk_row_counts[field] = risk_row_counts.get(field, 0) + 1
                        if field not in risk_values:
                            risk_values[field] = row[1].strip()
                for field in ("是否需要停机", "是否需要业务降级"):
                    if field in row[0] and field not in extra_flags:
                        extra_flags[field] = row[1].strip()
    services = 0
    changed = section_of("变更服务")
    if changed is not None:
        for table in parse_tables(lines, in_fence, changed):
            if any("服务名称" in cell for cell in table.header):
                # 取最大行数（fail-closed）：前置空诱饵表不得把多服务降为单服务。
                services = max(services, len(table.rows))
    infra_applicable: list[str] = []
    for name in INFRA_SECTIONS:
        section = section_of(name)
        if section is None:
            infra_applicable.append(name)  # 缺小节时无法证明不涉及，fail-closed
            continue
        body_lines = lines[section.start + 1 : section.end]
        has_table = bool(parse_tables(lines, in_fence, section))
        has_fence = any(raw.lstrip().startswith(("```", "~~~")) for raw in body_lines)
        body = "\n".join(body_lines)
        # 保留检查表/SQL 围栏视为适用（S 级的合法形态是折叠为“不涉及+原因”的 prose）。
        if has_table or has_fence or "不涉及" not in body:
            infra_applicable.append(name)
    signals: list[str] = []
    for field in RISK_FIELDS:
        # 带注解的肯定回答（如“是（涉及订单金额）”）同样计信号，不得降为“未决”。
        if str(risk_values.get(field, "")).startswith("是"):
            signals.append(f"关键信息/{field}=是")
    # 围栏内 SQL 去注释后整体扫描：注释替代空白（DROP/**/TABLE）与跨行书写
    # 都是真实可执行的 SQL，逐行扫描会漏检。
    fence_text = "\n".join(raw for number, raw in enumerate(lines) if in_fence[number])
    fence_text = re.sub(r"/\*.*?\*/", " ", fence_text, flags=re.S)
    fence_text = re.sub(r"(?:--|#)[^\n]*", " ", fence_text)
    for match in HIGH_RISK_SQL.finditer(fence_text):
        signals.append(f"围栏内高风险 SQL（{' '.join(match.group(0).split())[:40]}）")
    for number, raw in enumerate(lines):
        if in_fence[number]:
            continue
        if HIGH_RISK_SQL.search(raw):
            # prose/表格里的 DROP TABLE 同样是删表声明。
            signals.append(f"L{number + 1}: 高风险 SQL（{raw.strip()[:40]}）")
        stripped = raw.lstrip()
        if stripped.startswith(">"):
            continue  # 模板提示语是引语，不是本次发布的声明
        # 否定豁免只作用于短语所在子句：同行其余子句的真实信号照常计入，
        # 不得用“不涉及停机，但包含不可逆迁移”或括号定语混排吞掉强制 L 信号。
        for clause in re.split(r"[，。；;,.!?！？（）():：]|但(?:是)?|然而|不过", raw):
            if any(word in clause for word in NEGATION_WORDS):
                continue
            for phrase in HIGH_RISK_TEXT:
                if phrase in clause:
                    signals.append(f"L{number + 1}: {phrase}")
    unresolved = [
        field for field in RISK_FIELDS
        if risk_row_counts.get(field, 0) > 1  # 重复风险行无法机械判定真伪，fail-closed 按未决
        or (not str(risk_values.get(field, "")).startswith("是") and risk_values.get(field) != "否")
    ]
    if signals:
        minimum = "L"
    elif (
        services > 1
        or infra_applicable
        or unresolved
        or extra_flags.get("是否需要停机", "").startswith("是")
        or extra_flags.get("是否需要业务降级", "").startswith("是")
    ):
        minimum = "M"
    else:
        minimum = "S"
    return {
        "probe_scale": minimum,
        "services": services,
        "infra_applicable": infra_applicable,
        "high_risk_signals": signals,
        "unresolved_risk_fields": unresolved,
        "risk_values": risk_values,
    }


def required_chaos_modes(state: dict[str, Any]) -> tuple[str, ...]:
    """Chaos 必查故障模式：M/L 全矩阵；S 按探针结论剪枝，剪枝范围不得自行扩大或缩小。"""
    if state.get("scale") in {"M", "L"}:
        return CHAOS_MODES
    applicable = set(state.get("scale_basis", {}).get("infra_applicable", []))
    modes = list(ALWAYS_CHAOS_MODES)
    if "MQ / Kafka" in applicable:
        modes.append("消息积压")
    if applicable & {"MySQL", "Redis"}:
        modes.append("数据异常")
    if "Nacos" in applicable:
        modes.append("配置未生效")
    return tuple(modes)


def history_line_count(rpd: Path) -> int:
    """统计变更历史节的行数（含围栏快照），用于检测历史被删改（倒退）。

    起止边界用围栏感知的标题解析：快照围栏内的上一版标题不得截断计数，
    否则删掉整段快照而行数不变，历史门禁对 change 模式形同虚设。
    """
    try:
        lines = rpd.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError(f"无法读取 {rpd}: {exc}") from exc
    headings = parse_headings(lines, fence_flags(lines))
    sections = build_sections(headings, len(lines))
    section = next((item for item in sections if item.text.startswith("变更历史")), None)
    if section is None:
        return 0
    # fail-closed：节末仍在围栏内说明快照围栏未配对（标题泄漏截断了边界），
    # 此时行数不可信，不得拿被截断的计数放行历史删改。
    if fence_flags(lines)[section.end - 1]:
        raise GateError("变更历史快照围栏未闭合或标题泄漏；历史计数不可信，请先修复快照围栏（至少 4 个标记字符）")
    return section.end - section.start


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


def validate_finding(raw: Any, review_round: int, seen_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GateError(f"第 {review_round} 轮 finding 必须是对象：{raw!r}")
    missing = [field for field in FINDING_FIELDS if not str(raw.get(field, "")).strip()]
    if missing:
        raise GateError(f"Finding {raw.get('id', '?')} 缺少非空字段：{', '.join(missing)}")
    identifier = str(raw["id"]).strip()
    if not re.fullmatch(r"RPR-\d{2,4}", identifier):
        raise GateError(f"Finding id 必须形如 RPR-001：{identifier}")
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


def validate_open_questions(payload: Any) -> list[dict[str, str]]:
    questions = payload.get("open_questions", [])
    if not isinstance(questions, list):
        raise GateError("open_questions 必须是数组（无待确认项时为空数组）")
    result: list[dict[str, str]] = []
    for item in questions:
        if not isinstance(item, dict):
            raise GateError("open_questions 的每一项必须是对象")
        identifier = str(item.get("id", "")).strip()
        if not re.fullmatch(r"RQ-\d+", identifier):
            raise GateError(f"open_questions 的 id 必须形如 RQ-001：{identifier}")
        for field in ("question", "impact", "owner"):
            if not str(item.get(field, "")).strip():
                raise GateError(f"{identifier} 的 {field} 不得为空（owner 未知时写需人工指定）")
        result.append({field: str(item[field]).strip() for field in ("id", "question", "impact", "owner")})
    return result


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
    if payload.get("scope") != SCOPE:
        raise GateError(f"scope 必须为 {SCOPE}；本评审只覆盖发布方案，不得夹带代码审查结论")
    if payload.get("scale") != state.get("scale"):
        raise GateError(f"报告 scale={payload.get('scale')} 与台账规模 {state.get('scale')} 不一致")
    perspective = payload["perspective"]
    if perspective not in PERSPECTIVES:
        raise GateError(f"perspective `{perspective}` 非法（只允许 {', '.join(PERSPECTIVES)}）")
    current_entry = round_entry_of(state, state["current_round"])
    if current_entry.get("mode", "full") == "delta":
        if perspective != "RECHECK":
            raise GateError(f"增量复核轮只接受视角 RECHECK，不接受 {perspective}")
    elif perspective != "COMPREHENSIVE":
        raise GateError(f"全量评审轮只接受视角 COMPREHENSIVE，不接受 {perspective}")
    status = payload["status"]
    if status not in STATUSES:
        raise GateError(f"status `{status}` 非法（只允许 {', '.join(STATUSES)}）")
    review_id = str(payload["review_id"]).strip()
    if not REVIEW_ID_RE.fullmatch(review_id):
        raise GateError(f"review_id 必须形如 RPD-R<轮次>：{review_id}")
    if review_id != f"RPD-R{state['current_round']}":
        raise GateError(f"review_id {review_id} 与当前轮次不符；每轮只有一个全新独立 Reviewer")
    rpd = Path(state["rpd_path"])
    actual = content_digest(rpd)
    if not digest_matches(str(payload["rpd_digest"]), actual):
        raise GateError(
            f"报告 rpd_digest 与当前稿件摘要不一致（评审对象已失效）：{payload['rpd_digest']} != {actual[:12]}"
        )
    existing = round_reports(state, state["current_round"])
    if existing:
        raise GateError(f"第 {state['current_round']} 轮已记录评审报告；每轮只接受一个独立 Reviewer 的结论")
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
        raise GateError("status=REQUEST_CHANGES 要求至少一条 blocking finding；Medium/Low 走非阻塞跟进，不得据此开轮")
    if status in {"APPROVE", "APPROVE_WITH_FOLLOW_UPS"} and blocking:
        raise GateError("存在 blocking finding 时不得给出 APPROVE/APPROVE_WITH_FOLLOW_UPS")
    if status == "APPROVE" and (follow_ups or (still_open and perspective != "RECHECK")):
        raise GateError(
            "status=APPROVE 要求本次无 Finding 且台账中无未闭环阻塞项；未闭环："
            + ", ".join(item["id"] for item in still_open + follow_ups)
        )
    if status == "APPROVE_WITH_FOLLOW_UPS" and not follow_ups and not open_findings(state):
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
    if not str(payload.get("appendix_markdown", "")).strip():
        raise GateError("appendix_markdown 为空；评审意见必须可原样追加到 release-reviews.md")
    questions = validate_open_questions(payload)
    report = {
        "review_id": review_id,
        "perspective": perspective,
        "status": status,
        "rpd_digest": actual,
        "summary": str(payload["summary"]),
        "blocked_reason": str(payload.get("blocked_reason", "")),
        "appendix_digest": hashlib.sha256(str(payload["appendix_markdown"]).encode("utf-8")).hexdigest(),
        "verified_findings": [str(item) for item in verified],
        "recorded_at": now(),
    }
    state.setdefault("open_questions", {})
    for question in questions:
        state["open_questions"][question["id"]] = question
    return report, findings


def command_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    rpd = Path(args.rpd).resolve()
    if not rpd.is_file():
        raise GateError(f"RPD 不存在或不可读：{rpd}")
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
        raise GateError("必须至少登记一份输入资料（--input SRC-001=<路径>）；PRD 与 TDD 必须登记")
    basis = probe_scale(rpd)
    if SCALE_ORDER[args.scale] < SCALE_ORDER[basis["probe_scale"]]:
        raise GateError(
            f"声明规模 {args.scale} 低于探针判定的最低严格度 {basis['probe_scale']}；"
            f"依据：高风险信号={basis['high_risk_signals'] or '无'}，"
            f"适用基础设施={basis['infra_applicable'] or '无'}，服务数={basis['services']}，"
            f"未决风险字段={basis['unresolved_risk_fields'] or '无'}；"
            "命中高风险信号强制 L 级，规模只能上调不能下调"
        )
    declared = RpdValidator(rpd).declared_scale()
    if declared is not None and declared != args.scale:
        raise GateError(f"RPD 关键信息的发布规模 {declared} 与 --scale {args.scale} 不一致")
    digest = content_digest(rpd)
    state = {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "max_rounds": MAX_ROUNDS,
        "current_round": 1,
        "status": "IN_PROGRESS",
        "mode": args.mode,
        "scale": args.scale,
        "scale_basis": basis,
        "rpd_path": str(rpd),
        "rpd_digest": digest,
        "history_lines": history_line_count(rpd),
        "inputs": inputs,
        "rounds": [{"round": 1, "opened_at": now(), "rpd_digest": digest, "reports": []}],
        "findings": {},
        "open_questions": {},
        "transitions": [{"at": now(), "event": "init", "detail": f"mode={args.mode} scale={args.scale} probe={basis['probe_scale']}"}],
    }
    save_state(state_path, state)
    print(f"INIT: 台账已建立 {state_path}（round=1，scale={args.scale}，probe={basis['probe_scale']}，inputs={len(inputs)}）")
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
        finding["raised_digest"] = report["rpd_digest"]
        existing = state["findings"].get(finding["id"])
        if existing and existing["status"] == "OPEN":
            # 同一个 Finding 在多个轮次里反复出现且未闭环时，刷新证据但保留最早出现轮次。
            finding["first_seen_round"] = min(existing["first_seen_round"], finding["first_seen_round"])
        state["findings"][finding["id"]] = {**finding, "status": "OPEN", "resolution": None, "closed_in_round": None,
                                            "closure_evidence": "", "q_ref": ""}
    state["transitions"].append({"at": now(), "event": "record", "detail": f"{report['review_id']} -> {report['status']}"})
    if report["status"] == "BLOCKED":
        state["status"] = "BLOCKED"
    save_state(state_path, state)
    print(f"RECORDED: {report['review_id']} status={report['status']}（可 close/advance）")
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
        current_entry = round_entry_of(state, state["current_round"])
        if current_entry.get("mode", "full") == "delta":
            # delta 轮内闭环必须先有本轮 RECHECK 确认；不得抢在报告记录前自称修复。
            confirmed = any(
                identifier in report.get("verified_findings", [])
                for report in current_entry["reports"]
                if report.get("perspective") == "RECHECK"
            )
            if not confirmed:
                raise GateError(
                    f"增量复核轮内以 fixed 关闭 {identifier}，必须先记录本轮 RECHECK 报告"
                    "且其 verified_findings 已确认该项；不得先闭环后记录"
                )
        recheck_reports = [
            report
            for round_entry in state["rounds"]
            if round_entry["round"] > finding["raised_in_round"]
            for report in round_entry["reports"]
            if report.get("perspective") == "RECHECK"
        ]
        if recheck_reports and not any(
            identifier in report.get("verified_findings", []) for report in recheck_reports
        ):
            raise GateError(
                f"Finding {identifier} 未出现在任何后续 RECHECK 报告的 verified_findings 中，"
                "不得以 fixed 关闭；请先由增量复核确认修复"
            )
    if resolution == "deferred":
        if not re.fullmatch(r"RQ-\d+", q_ref or ""):
            raise GateError("deferred 必须通过 --q-ref 指向已登记的 RQ 待人工确认编号")
        # 词边界匹配且只在遗留问题节（围栏外）查找：前缀碰撞（RQ-0 对 RQ-001）
        # 与历史快照里的旧编号都不得蒙混过关。
        rpd_lines = Path(state["rpd_path"]).read_text(encoding="utf-8").splitlines()
        rpd_fence = fence_flags(rpd_lines)
        rpd_sections = build_sections(parse_headings(rpd_lines, rpd_fence), len(rpd_lines))
        legacy = next((item for item in rpd_sections if item.text.startswith("遗留问题")), None)
        legacy_text = "\n".join(
            raw for number, raw in enumerate(rpd_lines)
            if legacy is not None and legacy.start <= number < legacy.end and not rpd_fence[number]
        ) if legacy is not None else ""
        known = set(state.get("open_questions", {}))
        if not re.search(rf"\b{re.escape(q_ref)}\b", legacy_text) and q_ref not in known:
            raise GateError(f"{q_ref} 未出现在 RPD 遗留问题节的待人工确认项或评审台账中，不得以“待确认”关闭")
    if resolution == "rejected":
        if finding["severity"] in BLOCKING_SEVERITIES:
            raise GateError(f"{finding['severity']} 级 Finding 不得判定为不采纳；只能修复或转 RQ 待确认")
        if len(evidence) < 20:
            raise GateError("不采纳必须给出更长理由（≥20 字符），说明为何不构成发布方案缺陷")
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


def command_chaos(args: argparse.Namespace) -> int:
    """P1-3 Chaos 覆盖门禁：必查故障模式由台账规模与探针结论推导，Critic 不得自行缩小。"""
    state_path = Path(args.state)
    state = load_state(state_path)
    if state["status"] != "IN_PROGRESS":
        raise GateError(f"台账状态为 {state['status']}，不得记录 Chaos 审查")
    try:
        payload = json.loads(sys.stdin.read() if args.report == "-" else Path(args.report).read_text(encoding="utf-8"))
    except OSError as exc:
        raise GateError(f"无法读取 Chaos 报告：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise GateError(f"Chaos 报告不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise GateError("Chaos 报告负载必须是 JSON 对象")
    if payload.get("scale") != state.get("scale"):
        raise GateError(f"Chaos 报告 scale={payload.get('scale')} 与台账规模 {state.get('scale')} 不一致")
    # 覆盖度必须绑定稿件指纹：改稿后旧覆盖度失效，不得拿早期审查冒充交付稿的审查。
    chaos_digest = content_digest(Path(state["rpd_path"]))
    if not digest_matches(str(payload.get("rpd_digest", "")), chaos_digest):
        raise GateError("Chaos 报告 rpd_digest 与当前稿件摘要不一致；覆盖度必须针对当前稿，改稿后请重跑 chaos")
    scenarios = payload.get("scenarios_checked")
    if not isinstance(scenarios, list) or not all(isinstance(item, str) and item.strip() for item in scenarios):
        raise GateError("scenarios_checked 必须是非空字符串数组（列出已检查的故障模式）")
    required = required_chaos_modes(state)
    missing = [mode for mode in required if mode not in scenarios]
    if missing:
        raise GateError(
            f"Chaos 审查缺少必查故障模式：{', '.join(missing)}；"
            f"{state.get('scale')} 级必查范围由台账推导，Critic 不得自行缩小"
        )
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        raise GateError("findings 必须是数组")
    # Chaos 风险与评审 Finding 走同一闭环台账：Blocker/High 未闭环同样禁止交付。
    seen_ids: set[str] = set()
    chaos_findings = [validate_finding(item, state["current_round"], seen_ids) for item in findings]
    for finding in chaos_findings:
        if finding["id"] in state["findings"]:
            raise GateError(f"Chaos Finding {finding['id']} 与台账已有 Finding 编号冲突；请使用新编号")
    state["chaos"] = {
        "scale": state["scale"],
        "scenarios_checked": [item.strip() for item in scenarios],
        "required_modes": list(required),
        "findings_count": len(findings),
        "rpd_digest": chaos_digest,
        "recorded_at": now(),
    }
    for finding in chaos_findings:
        finding["raised_digest"] = chaos_digest
        finding["source"] = "chaos"
        # Chaos 先于本轮评审：登记为上一轮提出（评审前=0），fixed 闭环仍须有
        # 后续看到新稿件的全量评审轮；不得因登记轮次靠前而豁免复核。
        finding["raised_in_round"] = state["current_round"] - 1
        finding["first_seen_round"] = max(1, state["current_round"] - 1)
        state["findings"][finding["id"]] = {**finding, "status": "OPEN", "resolution": None, "closed_in_round": None,
                                            "closure_evidence": "", "q_ref": ""}
    state["transitions"].append({"at": now(), "event": "chaos", "detail": f"modes={len(scenarios)} findings={len(findings)}"})
    save_state(state_path, state)
    print(f"CHAOS: 覆盖门禁通过（必查 {len(required)} 模式，实查 {len(scenarios)}，风险 {len(findings)} 条）")
    return 0


def command_advance(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    if state["status"] != "IN_PROGRESS":
        raise GateError(f"台账状态为 {state['status']}，不得推进轮次")
    number = state["current_round"]
    entry = round_reports(state, number)
    if not entry:
        raise GateError(f"第 {number} 轮没有评审记录")
    current = round_entry_of(state, number)
    if args.mode == "delta":
        if current.get("mode", "full") != "full":
            raise GateError("增量复核不得连续进行：上一轮已是 delta，本轮修复后必须回到全量评审")
        if not open_findings(state):
            raise GateError("没有未闭环 Finding，无需增量复核；请直接 verify 或推进全量轮")
        if number + 1 >= state["max_rounds"]:
            raise GateError("最终轮必须是全量评审，不得以增量复核轮作为最后一轮")
    rpd = Path(state["rpd_path"])
    digest = content_digest(rpd)
    lines = history_line_count(rpd)
    if lines < state["history_lines"]:
        raise GateError(f"变更历史从 {state['history_lines']} 行减少到 {lines} 行：历史不得删除、覆盖或重排")
    state["history_lines"] = lines
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
    reviewed = {item["rpd_digest"] for item in entry}
    if blocking_open_findings(state) and digest in reviewed:
        raise GateError(
            "稿件正文与送审时完全一致，但仍有未闭环的阻塞项；必须先按 Finding 修改正文再推进"
        )
    state["current_round"] = number + 1
    state["rounds"].append({"round": number + 1, "mode": args.mode, "opened_at": now(), "rpd_digest": digest, "reports": []})
    state["transitions"].append({"at": now(), "event": "advance", "detail": f"{number} -> {number + 1} ({args.mode})"})
    save_state(state_path, state)
    print(f"ADVANCE: 进入第 {number + 1} 轮（{args.mode}，送审稿件摘要 {digest[:12]}）")
    return 0


def verify_delivery(state: dict[str, Any], allow_blocked: bool) -> list[str]:
    problems: list[str] = []
    rpd = Path(state["rpd_path"])
    if not rpd.is_file():
        return [f"RPD 不存在：{rpd}"]
    text = rpd.read_text(encoding="utf-8")
    digest = content_digest(rpd)
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
        statuses = {item["status"] for item in last["reports"]}
        if not allow_blocked and not statuses <= {"APPROVE", "APPROVE_WITH_FOLLOW_UPS"}:
            problems.append(f"最后一轮状态含 {', '.join(sorted(statuses - {'APPROVE', 'APPROVE_WITH_FOLLOW_UPS'}))}，不得按可放行交付")
        if digest != last["reports"][-1]["rpd_digest"]:
            problems.append("RPD 在最后一次评审之后又被修改，但未进行下一轮复核")
    reviews_path = rpd.parent / REVIEWS_FILENAME
    reviews_text = reviews_path.read_text(encoding="utf-8") if reviews_path.is_file() else ""
    for entry in recorded_rounds:
        for report in entry["reports"]:
            if report["review_id"] not in text:
                problems.append(f"{report['review_id']} 的回执块未追加进 RPD 的独立评审意见章节")
            if report["review_id"] not in reviews_text:
                problems.append(f"{report['review_id']} 的完整意见未归档进同目录 {REVIEWS_FILENAME}")
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
            elif all(item["rpd_digest"] == finding.get("raised_digest") for item in later):
                problems.append(f"{finding['id']} 标记为 fixed，但复核轮次看到的仍是提出时的同一份稿件")
            # 交付前复查消费：提出后、闭环前存在 delta 轮的 fixed，必须被某轮 RECHECK
            # 的 verified_findings 确认；堵死“先闭环后记录”与 pre-close 清空后空确认的时序绕过。
            # 上界取 closed_in_round：闭环之后为其他 Finding 开的 delta 轮不得追溯要求已 CLOSED 项。
            delta_rounds = [
                round_entry for round_entry in recorded_rounds
                if finding["raised_in_round"] < round_entry["round"] <= finding["closed_in_round"]
                and round_entry.get("mode", "full") == "delta"
            ]
            if delta_rounds and not any(
                finding["id"] in report.get("verified_findings", [])
                for round_entry in delta_rounds
                for report in round_entry["reports"]
                if report.get("perspective") == "RECHECK"
            ):
                problems.append(f"{finding['id']} 标记为 fixed，但未被任何后续增量复核的 verified_findings 确认")
    if history_line_count(rpd) < state["history_lines"]:
        problems.append("变更历史行数倒退")
    # 回执节必须是最后一章（与验证器 REVIEW006 同口径）：其后追加的章节不得游离于评审之外。
    delivery_headings = parse_headings(text.splitlines(), fence_flags(text.splitlines()))
    delivery_appendix = next(
        (item for item in reversed(delivery_headings)
         if APPENDIX_TITLE_RE.match(f"{'#' * item.level} {item.text}")),
        None,
    )
    if delivery_appendix is not None and any(
        item.line > delivery_appendix.line and not is_receipt_heading(item) for item in delivery_headings
    ):
        problems.append("回执节之后出现非回执章节；独立评审意见必须是文档最后一章")
    # P1-3 规模联动：Chaos 覆盖必须入台账；RPD 声明规模与台账一致；正文风险信号不得超出台账规模。
    chaos = state.get("chaos")
    if not chaos:
        problems.append("Chaos 故障审查未入台账（chaos 命令）；不得交付")
    elif chaos.get("scale") != state.get("scale"):
        problems.append("Chaos 台账规模与评审台账不一致")
    elif chaos.get("rpd_digest") != digest:
        problems.append("Chaos 覆盖度对应的稿件与交付稿不一致；正文变更后必须重跑 chaos")
    declared = RpdValidator(rpd).declared_scale()
    if declared != state.get("scale"):
        problems.append(f"RPD 关键信息的发布规模（{declared or '未选定'}）与台账规模 {state.get('scale')} 不一致")
    basis = probe_scale(rpd)
    if SCALE_ORDER[basis["probe_scale"]] > SCALE_ORDER[state.get("scale", "L")]:
        problems.append(
            f"正文高风险信号要求最低 {basis['probe_scale']} 级，台账规模 {state.get('scale')} 不足："
            + "; ".join(basis["high_risk_signals"][:5])
        )
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
    rpd = Path(args.rpd)
    if not rpd.is_file():
        raise GateError(f"RPD 不存在或不可读：{rpd}")
    print(content_digest(rpd))
    return 0


def command_diff(args: argparse.Namespace) -> int:
    """比较修订前后正文（不含回执区）的行级改动比例，判定是否允许整篇重写。"""
    before_path, after_path = Path(args.before), Path(args.after)
    for path in (before_path, after_path):
        if not path.is_file():
            raise GateError(f"对比文件不存在或不可读：{path}")
    before = without_appendix(before_path.read_text(encoding="utf-8").splitlines())
    after = without_appendix(after_path.read_text(encoding="utf-8").splitlines())
    added = removed = 0
    for line in difflib.unified_diff(before, after, n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    base = max(len(before), 1)
    ratio = (added + removed) / base
    print(json.dumps(
        {
            "before_lines": len(before),
            "after_lines": len(after),
            "added": added,
            "removed": removed,
            "change_ratio": round(ratio, 4),
            "full_rewrite_allowed": ratio > FULL_REWRITE_THRESHOLD,
        },
        ensure_ascii=False,
    ))
    return 0


def _escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _receipt_parts(payload: Any) -> tuple[str, str, int, str, list[str]]:
    """校验回执必需字段，返回 (review_id, perspective, 轮次, 回执表格 Markdown, Finding 紧凑清单)。

    只做字段与枚举检查；轮次、指纹与 Finding 闭环仍由 record 判定。
    """
    if not isinstance(payload, dict):
        raise GateError("报告负载必须是 JSON 对象")
    required = ("review_id", "review_round", "perspective", "status", "rpd_digest", "appendix_markdown")
    missing = [field for field in required if not str(payload.get(field, "")).strip()]
    if missing:
        raise GateError(f"报告缺少回执必需字段：{', '.join(missing)}")
    review_id = str(payload["review_id"]).strip()
    perspective = str(payload["perspective"]).strip()
    status = str(payload["status"]).strip()
    if not REVIEW_ID_RE.fullmatch(review_id):
        raise GateError(f"review_id 必须形如 RPD-R<轮次>：{review_id}")
    if perspective not in PERSPECTIVES:
        raise GateError(f"perspective `{perspective}` 非法（只允许 {', '.join(PERSPECTIVES)}）")
    if status not in STATUSES:
        raise GateError(f"status `{status}` 非法（只允许 {', '.join(STATUSES)}）")
    digest = re.sub(r"[^0-9a-fA-F]", "", str(payload["rpd_digest"]))
    if len(digest) < 12:
        raise GateError("rpd_digest 不足 12 位十六进制，无法生成可校验的稿件摘要行")
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
    """渲染追加进 RPD「独立评审意见」章节的紧凑回执块；完整意见另由 render-report 归档到 release-reviews.md。"""
    review_id, perspective, round_number, table, compact = _receipt_parts(payload)
    lines = [f"### {review_id} · {perspective} · 第 {round_number} 轮", "", table]
    if compact:
        lines += ["", "**Finding 清单**", "", *compact]
    lines += ["", f"> 完整意见见同目录 `release-reviews.md` 的 {review_id} 节。", ""]
    return "\n".join(lines)


def render_report_markdown(payload: Any) -> str:
    """渲染追加进 release-reviews.md 的完整评审报告节（含 Finding 明细与原始意见正文）。"""
    review_id, perspective, round_number, table, _ = _receipt_parts(payload)
    appendix = str(payload["appendix_markdown"]).strip()
    # release-reviews.md 以二级标题分节；意见正文里的一/二级标题会破坏分节结构。
    if any(line.startswith(("# ", "## ")) for line in appendix.splitlines()):
        raise GateError("appendix_markdown 不得包含一级或二级标题（# / ##）；小节请用三级标题、加粗文本或列表表达")
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
        "scale": state.get("scale"),
        "probe_scale": state.get("scale_basis", {}).get("probe_scale"),
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
        "open_questions": sorted(state.get("open_questions", {})),
        "chaos": state.get("chaos"),
        "mechanical_patches": [
            f"{item['code']}|{item.get('line') or '-'}|{item['at']}"
            for item in state.get("mechanical_patches", [])
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _error_signature(error: str) -> str:
    """去掉行号后的错误签名：章节重排会移动行号，但不得新增/改变缺陷本身。"""
    return re.sub(r"\bL\d+\b", "L#", error)


def _reorder_sections(text: str) -> str:
    """STRUCT002 的确定性修复：把强制章节块按模板顺序重排，块内容逐字节不变。

    主控不提供任何新文本，因此不可能借重排夹带语义修改；前言与嵌套小节随所属块移动。
    用 splitlines(keepends=True) 保留行尾原始字节：CRLF 属于块内容，不得全文 LF 化。
    """
    lines = text.splitlines(keepends=True)
    headings = parse_headings(lines, fence_flags(lines))
    sections = build_sections(headings, len(lines))
    found: list[tuple[int, Any]] = []
    used: set[int] = set()
    for name in REQUIRED_SECTIONS:
        section = next(
            (item for item in sections if item.text.startswith(name) and item.start not in used), None
        )
        if section is None:
            raise GateError(f"缺少章节 {name}，无法重排；缺章修复需要内容，走 Refiner")
        duplicates = [
            item for item in duplicate_sections(sections, name) if item.start not in used
        ]
        if duplicates:
            raise GateError(f"章节 {name} 重复出现；重排无法判定保留哪一份，先去重（STRUCT004）再打补丁")
        used.add(section.start)
        found.append((REQUIRED_SECTIONS.index(name), section))
    # 按物理位置切块：后一块的起点截断前一块，避免嵌套章节（变更历史在遗留问题内）被重复搬运。
    found.sort(key=lambda pair: pair[1].start)
    bounds = [item.start for _, item in found[1:]] + [len(lines)]
    blocks: dict[int, list[str]] = {}
    for (index, section), end in zip(found, bounds):
        blocks[index] = lines[section.start : end]
    first_start = found[0][1].start
    reordered = lines[:first_start] + [line for index in sorted(blocks) for line in blocks[index]]
    if sorted(reordered) != sorted(lines):
        raise GateError("重排前后行集合不一致；拒绝可能丢失内容的重排")
    return "".join(reordered)


def command_patch(args: argparse.Namespace) -> int:
    """P1-2 机械补丁通道：白名单错误码 + 语义中性谓词 + 台账留痕。

    例外由代码而非信任界定：门禁必须实际报出该错误码，补丁只能做该码对应的
    语义中性变换，且不得引入新缺陷；白名单外的一切修改保持原价：Refiner + 新一轮评审。
    补丁后若发生在最后一轮评审之后，verify 的指纹绑定会强制补一轮全量评审才能交付。
    """
    state_path = Path(args.state)
    state = load_state(state_path)
    if state["status"] != "IN_PROGRESS":
        raise GateError(f"台账状态为 {state['status']}，不得再打机械补丁")
    code = args.code
    if code not in MECHANICAL_PATCH_CODES:
        raise GateError(
            f"错误码 {code} 不在机械补丁白名单（{', '.join(MECHANICAL_PATCH_CODES)}）；"
            "其余缺陷的修复需要事实或判断，走 Refiner + 独立评审"
        )
    rpd = Path(state["rpd_path"])
    # 按原始字节读入：read_text 的通用换行会把 CRLF 转成 LF，违反逐字节守恒承诺。
    text = rpd.read_bytes().decode("utf-8")
    before_digest = content_digest(rpd)
    errors_before = RpdValidator(rpd).validate()
    if code == "STRUCT002":
        if not any(item.startswith("[STRUCT002]") for item in errors_before):
            raise GateError("门禁未报出 STRUCT002；机械补丁只修实际报出的缺陷，不得预修或借机改稿")
        new_text = _reorder_sections(text)
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "release-doc.md"
            probe.write_bytes(new_text.encode("utf-8"))
            errors_after = RpdValidator(probe).validate()
        if any(item.startswith("[STRUCT002]") for item in errors_after):
            raise GateError("重排后章节顺序仍不合规")
        signatures_before = {_error_signature(item) for item in errors_before}
        if not {_error_signature(item) for item in errors_after} <= signatures_before:
            raise GateError("重排引入了新缺陷；拒绝落盘")
        line_no = None
        old_value = new_value = ""
    else:
        if not (args.line and args.old is not None and args.new is not None):
            raise GateError("STATUS003 补丁需要 --line/--old/--new")
        flagged = set()
        for item in errors_before:
            if item.startswith("[STATUS003]"):
                match = re.search(r"\bL(\d+)\b", item)
                if match:
                    flagged.add(int(match.group(1)))
        if args.line not in flagged:
            raise GateError(f"L{args.line} 未被门禁报出 STATUS003；不得修改未被点名的行")
        old, new = args.old, args.new
        if old == new:
            raise GateError("补丁前后相同，无意义")
        if re.sub(r"\s+", "", old) != new or new not in CONCRETE_STATUSES:
            raise GateError(
                "STATUS003 只允许空白归一化（如 `通 过` → `通过`）；"
                "改换结论（如 `正常` → `通过`）是语义修改，走 Refiner + 独立评审"
            )
        lines = text.splitlines(keepends=True)
        if not 1 <= args.line <= len(lines):
            raise GateError(f"行号越界：L{args.line}")
        cells = lines[args.line - 1].split("|")
        hits = [index for index, cell in enumerate(cells) if cell.strip() == old]
        if len(hits) != 1:
            raise GateError(f"L{args.line} 中 `{old}` 锚点不存在或不唯一；拒绝模糊补丁")
        cell = cells[hits[0]]
        lead = cell[: len(cell) - len(cell.lstrip())]
        trail = cell[len(cell.rstrip()) :]
        cells[hits[0]] = lead + new + trail
        lines[args.line - 1] = "|".join(cells)
        new_text = "".join(lines)
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "release-doc.md"
            probe.write_bytes(new_text.encode("utf-8"))
            errors_after = RpdValidator(probe).validate()
        if not set(errors_after) <= set(errors_before):
            raise GateError("补丁引入了新缺陷；拒绝落盘")
        line_no, old_value, new_value = args.line, old, new
    # 按字节落盘：write_text 的 newline=None 在 Windows 上会把 \n 翻译为 \r\n，
    # 已含 CRLF 的文本会变成 \r\r\n，破坏逐字节守恒承诺。
    rpd.write_bytes(new_text.encode("utf-8"))
    after_digest = content_digest(rpd)
    entry = {
        "code": code,
        "line": line_no,
        "old": old_value,
        "new": new_value,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "at": now(),
    }
    state.setdefault("mechanical_patches", []).append(entry)
    state["transitions"].append({"at": now(), "event": "patch", "detail": f"{code} L{line_no or '-'}"})
    save_state(state_path, state)
    print(f"PATCHED: {code}（L{line_no or '-'}）已落盘并留痕；交付前仍须全量独立评审覆盖当前稿")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="建立台账并冻结输入与稿件摘要（含规模探针）")
    init.add_argument("--state", required=True)
    init.add_argument("--rpd", required=True)
    init.add_argument("--mode", required=True, choices=("new", "change"))
    init.add_argument("--scale", required=True, choices=SCALES, help="声明规模；不得低于探针判定的最低严格度")
    init.add_argument("--input", action="append", help="SRC-001=<路径>（可重复）")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    record = sub.add_parser("record", help="记录一份独立 Reviewer 报告")
    record.add_argument("--state", required=True)
    record.add_argument("--report", required=True, help="规范化 JSON 报告路径，或 - 读取 stdin")
    record.set_defaults(func=command_record)

    chaos = sub.add_parser("chaos", help="记录 Chaos 审查覆盖度（必查模式由台账规模推导）")
    chaos.add_argument("--state", required=True)
    chaos.add_argument("--report", required=True, help="Chaos JSON 报告路径，或 - 读取 stdin")
    chaos.set_defaults(func=command_chaos)

    close = sub.add_parser("close", help="关闭一条或一批 Finding")
    close.add_argument("--state", required=True)
    close.add_argument("--finding", help="单条闭环的 Finding id")
    close.add_argument("--resolution", choices=RESOLUTIONS, help="单条闭环的处理结果")
    close.add_argument("--evidence", help="单条闭环的证据说明")
    close.add_argument("--q-ref", default="")
    close.add_argument("--batch", help="批量闭环 JSON 数组路径，或 - 读取 stdin；任一条失败整批不落盘")
    close.set_defaults(func=command_close)

    advance = sub.add_parser("advance", help="本轮评审与定向修改完成后推进到下一轮（full 全量 / delta 增量复核）")
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

    patch = sub.add_parser("patch", help="机械补丁白名单：主控直修纯机械缺陷并留痕（STRUCT002/STATUS003）")
    patch.add_argument("--state", required=True)
    patch.add_argument("--code", required=True, choices=MECHANICAL_PATCH_CODES)
    patch.add_argument("--line", type=int, help="STATUS003 的目标行号")
    patch.add_argument("--old", help="STATUS003 的原状态单元格文本")
    patch.add_argument("--new", help="STATUS003 的归一化后文本")
    patch.set_defaults(func=command_patch)

    digest = sub.add_parser("digest", help="输出稿件正文指纹（不含文末回执区），供派发封套使用")
    digest.add_argument("--rpd", required=True)
    digest.set_defaults(func=command_digest)

    diff = sub.add_parser("diff", help="比较修订前后正文行级改动比例，判定是否允许整篇重写")
    diff.add_argument("--before", required=True, help="修订前快照路径")
    diff.add_argument("--after", required=True, help="修订后稿件路径")
    diff.set_defaults(func=command_diff)

    render = sub.add_parser("render-appendix", help="把规范化评审报告渲染为追加进 RPD 的紧凑回执块")
    render.add_argument("--report", required=True, help="规范化 JSON 报告路径，或 - 读取 stdin")
    render.set_defaults(func=command_render_appendix)

    render_full = sub.add_parser("render-report", help="把规范化评审报告渲染为追加进 release-reviews.md 的完整意见节")
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