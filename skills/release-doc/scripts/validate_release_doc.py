#!/usr/bin/env python3
"""release-doc 产物的确定性结构门禁。

不依赖子代理或主 Agent 的自述：直接解析生成的 ``release-doc.md``，校验模板章节、
检查表状态枚举与证据、发布结果预写、示例占位符残留、变更历史与独立评审回执。

与 product-doc 的关键差异：``【填写】``/``【链接】`` 是发布方案合法的缺口表达，
不判残留；只有模板示例占位符（【服务 1】等）与未定稿的状态枚举才拒收。

用法（``<PYTHON>`` 按宿主解析：Windows 用 ``py -3``，macOS/Linux 用 ``python3``）：
    <PYTHON> validate_release_doc.py <release-doc.md> [--draft]
    <PYTHON> validate_release_doc.py --check-template-refs --refs-target SKILL.md --refs-target references/check-standard.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

# 模板强制章节（按输出顺序）；发布前检查按前缀匹配，兼容全角/半角括号差异。
REQUIRED_SECTIONS = (
    "关键信息", "变更业务", "变更服务", "变更产物", "变更接口",
    "基础设施变更", "依赖变更", "发布前检查", "发布策略", "发布步骤",
    "发布后验证", "发布后观察", "回滚方案", "发布结果", "遗留问题",
    "变更历史", "独立评审意见",
)
# 条件章节也必须保留：无变更写“不涉及”，不得删除。
REQUIRED_SUBSECTIONS = {
    "基础设施变更": ("MySQL", "Redis", "MQ / Kafka", "Nacos", "XXL-JOB"),
    "发布后验证": ("技术验证", "业务验证"),
    "回滚方案": ("触发条件", "回滚服务", "回滚步骤", "数据处理", "回滚验证"),
}
# 模板示例占位符：必须替换为真实内容或“需人工补充”，不得原样留在产物里。
EXAMPLE_PLACEHOLDERS = (
    "【服务 1】", "【服务 2】", "【服务名称】", "【阈值】", "【时间】",
    "【正常流程】", "【异常流程】", "【重复请求/幂等场景】", "【其他场景】",
    "【核心业务指标】", "【本次变更业务指标】",
)
# 合法缺口表达：允许保留，等待人工补充；不得出现在定稿状态列。
GAP_PLACEHOLDER_RE = re.compile(r"^【[^】]*】$")
CONCRETE_STATUSES = ("通过", "不涉及", "待执行")
# 稳定错误码：release_state.py 的机械补丁白名单以本文件错误码为唯一来源。
# 只有语义中性的缺陷（STRUCT002 章节顺序、STATUS003 状态枚举空白变形）可直修；
# 其余错误码的修复需要事实或判断，一律走 Refiner + 独立评审。
ERROR_CODE_RE = re.compile(r"^\[([A-Z]+\d+)\]")
REVIEW_STATUSES = ("APPROVE", "APPROVE_WITH_FOLLOW_UPS", "REQUEST_CHANGES", "BLOCKED")
REVIEW_ID_RE = re.compile(r"RPD-R\d+\b")
DIGEST_RE = re.compile(r"\b[0-9a-fA-F]{12,64}\b")
# 与 release_state.py 保持一致的契约枚举；漂移检查以契约文件为事实源。
CONTRACT_VERSION = "release-review-v2"
SEVERITIES = ("Blocker", "High", "Medium", "Low")
PERSPECTIVES = ("COMPREHENSIVE", "RECHECK")
CATEGORIES = ("TEMPLATE", "EVIDENCE", "ORDER", "VERIFICATION", "OBSERVATION", "ROLLBACK", "RISK")


class Heading(NamedTuple):
    level: int
    text: str
    line: int


class Section(NamedTuple):
    text: str
    level: int
    start: int
    end: int


class Table(NamedTuple):
    header: list[str]
    rows: list[list[str]]
    header_line: int
    row_lines: list[int]


def fence_flags(lines: list[str]) -> list[bool]:
    """标记每行是否位于代码围栏内；围栏内的标题、表格与措辞不参与校验。

    按 CommonMark 配对：开启围栏记录标记字符与长度，闭合必须同字符、长度不小于
    开启长度且缩进不超过 3 空格；混用 ```/~~~ 或深缩进行不得翻转围栏状态，
    否则可借伪围栏把高风险文本藏进“围栏内”或让真实检查表跳过校验。
    """
    flags = [False] * len(lines)
    fence_char = ""
    fence_len = 0
    for index, raw in enumerate(lines):
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.lstrip(" ")
        if indent <= 3 and (stripped.startswith("```") or stripped.startswith("~~~")):
            marker = stripped[:3]
            run = len(stripped) - len(stripped.lstrip(marker[0]))
            if not fence_char:
                # CommonMark：反引号围栏的信息串不得含反引号，否则是行内代码而非围栏；
                # 不得借 ```text `x` 这类伪围栏把检查表藏进“围栏内”跳过校验。
                if marker[0] == "`" and "`" in stripped[run:]:
                    flags[index] = False
                    continue
                fence_char, fence_len = marker[0], run
                flags[index] = False
                continue
            if marker[0] == fence_char and run >= fence_len and not stripped[run:].strip():
                fence_char, fence_len = "", 0
                flags[index] = False
                continue
        flags[index] = bool(fence_char)
    return flags


# 回执章节标题的唯一判定：与 parse_headings 一致要求 # 后有空格；
# release_state.without_appendix 按章节结构定位同一标题，两处不得各用一套正则。
APPENDIX_TITLE_RE = re.compile(r"^#{1,6} 独立评审意见")


def parse_headings(lines: list[str], in_fence: list[bool]) -> list[Heading]:
    headings: list[Heading] = []
    for index, raw in enumerate(lines):
        if in_fence[index] or not raw.startswith("#"):
            continue
        level = len(raw) - len(raw.lstrip("#"))
        if not 1 <= level <= 6 or raw[level : level + 1] != " ":
            continue
        headings.append(Heading(level, raw[level:].strip(), index))
    return headings


def build_sections(headings: list[Heading], total: int) -> list[Section]:
    sections: list[Section] = []
    for position, heading in enumerate(headings):
        end = total
        for candidate in headings[position + 1 :]:
            if candidate.level <= heading.level:
                end = candidate.line
                break
        sections.append(Section(heading.text, heading.level, heading.line, end))
    return sections


def duplicate_sections(sections: list[Section], name: str) -> list[Section]:
    """与首个匹配章节同级且在其范围之外的重复章节；合法子节（如 ### 变更业务补充）不算重复。"""
    matches = [section for section in sections if section.text.startswith(name)]
    if not matches:
        return []
    first = matches[0]
    return [section for section in matches[1:] if section.level == first.level and section.start >= first.end]


def is_receipt_heading(heading: Heading) -> bool:
    """回执块标题：三级且以 review_id 开头；回执节之后只允许这类标题。"""
    return heading.level == 3 and bool(REVIEW_ID_RE.match(heading.text))


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def is_separator(cells: Iterable[str]) -> bool:
    values = [cell for cell in cells if cell]
    return bool(values) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in values)


def parse_tables(lines: list[str], in_fence: list[bool], section: Section | None) -> list[Table]:
    if section is None:
        return []
    tables: list[Table] = []
    index = section.start + 1
    while index < section.end:
        if in_fence[index] or not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        header = split_cells(lines[index])
        cursor = index + 1
        if cursor < section.end and not in_fence[cursor] and is_separator(split_cells(lines[cursor])):
            cursor += 1
        rows: list[list[str]] = []
        row_lines: list[int] = []
        while cursor < section.end and not in_fence[cursor] and lines[cursor].lstrip().startswith("|"):
            cells = split_cells(lines[cursor])
            if not is_separator(cells):
                rows.append(cells)
                row_lines.append(cursor)
            cursor += 1
        tables.append(Table(header, rows, index, row_lines))
        index = max(cursor, index + 1)
    return tables


def is_gap_only(value: str) -> bool:
    """空值或纯占位符都视为“没有内容”。"""
    stripped = value.strip()
    return stripped in {"", "-", "—"} or bool(GAP_PLACEHOLDER_RE.fullmatch(stripped))


class RpdValidator:
    """对生成的 RPD 做机械校验，返回全部缺陷而非首个失败。"""

    def __init__(self, path: Path, draft: bool = False) -> None:
        self.path = path
        self.draft = draft
        self.errors: list[str] = []
        self.lines = path.read_text(encoding="utf-8").splitlines()
        self.in_fence = fence_flags(self.lines)
        self.headings = parse_headings(self.lines, self.in_fence)
        self.sections = build_sections(self.headings, len(self.lines))

    def error(self, where: str, message: str, code: str) -> None:
        self.errors.append(f"[{code}] {where}: {message}")

    def at(self, number: int) -> str:
        return f"L{number + 1}"

    def find_section(self, name: str) -> Section | None:
        for section in self.sections:
            if section.text.startswith(name):
                return section
        return None

    def tables_of(self, name: str) -> list[Table]:
        return parse_tables(self.lines, self.in_fence, self.find_section(name))

    # --- 1. 章节结构 --------------------------------------------------------

    def check_structure(self) -> None:
        found: list[int] = []
        for name in REQUIRED_SECTIONS:
            matches = [section for section in self.sections if section.text.startswith(name)]
            if not matches:
                self.error("结构", f"缺少模板要求的章节 `{name}`（无变更也必须保留并写不涉及）", "STRUCT001")
                continue
            if len(matches) > 1:
                duplicates = duplicate_sections(self.sections, name)
                if duplicates:
                    self.error("结构", f"章节 `{name}` 出现 {len(duplicates) + 1} 次；重复章节会让 first-match 校验互相遮蔽", "STRUCT004")
            found.append(matches[0].start)
        if found != sorted(found):
            self.error("结构", "章节顺序与 release-doc-template.md 不一致", "STRUCT002")
        for parent, children in REQUIRED_SUBSECTIONS.items():
            parent_section = self.find_section(parent)
            if parent_section is None:
                continue
            inner = [
                heading for heading in self.headings
                if parent_section.start < heading.line < parent_section.end
            ]
            for child in children:
                if not any(heading.text.startswith(child) for heading in inner):
                    self.error("结构", f"`{parent}` 缺少条件小节 `{child}`（不涉及时保留小节并写明原因）", "STRUCT003")

    # --- 2. 示例占位符 ------------------------------------------------------

    def check_example_placeholders(self) -> None:
        for number, raw in enumerate(self.lines):
            if self.in_fence[number]:
                continue
            for placeholder in EXAMPLE_PLACEHOLDERS:
                if placeholder in raw:
                    self.error(self.at(number), f"模板示例占位符未替换：`{placeholder}`（改为真实内容或需人工补充）", "PLACE001")

    # --- 3. 检查表状态与证据 ------------------------------------------------

    def check_status_tables(self) -> None:
        inspected: set[int] = set()
        for section in self.sections:
            for table in parse_tables(self.lines, self.in_fence, section):
                if table.header_line in inspected:
                    continue
                inspected.add(table.header_line)
                joined = "".join(table.header)
                if "状态" not in joined or ("检查项" not in joined and "验证项" not in joined):
                    continue
                status_index = next(i for i, cell in enumerate(table.header) if "状态" in cell)
                # 证据列优先认“证据”；发布前检查表的“人工结果/AI 结果”不是证据列。
                evidence_index = next(
                    (i for i, cell in enumerate(table.header) if "证据" in cell), None
                )
                if evidence_index is None:
                    evidence_index = next(
                        (i for i, cell in enumerate(table.header) if "结果" in cell), None
                    )
                for offset, row in enumerate(table.rows):
                    where = f"{section.text} L{table.row_lines[offset] + 1}"
                    if len(row) <= status_index:
                        self.error(where, f"检查表第 {offset + 1} 行列数不足：{row}", "STATUS001")
                        continue
                    status = row[status_index].strip()
                    if status.startswith("【"):
                        if not self.draft:
                            self.error(where, f"状态未定稿：`{status}`（定稿只允许 {', '.join(CONCRETE_STATUSES)}）", "STATUS002")
                        continue
                    if status not in CONCRETE_STATUSES:
                        self.error(where, f"状态值非法：`{status}`（只允许 {', '.join(CONCRETE_STATUSES)}）", "STATUS003")
                        continue
                    if status == "待执行":
                        continue
                    if evidence_index is None or len(row) <= evidence_index:
                        self.error(where, f"状态为 `{status}` 但缺少证据/说明列内容", "STATUS004")
                        continue
                    evidence = row[evidence_index]
                    if is_gap_only(evidence):
                        label = "证据" if status == "通过" else "不适用原因"
                        self.error(where, f"状态为 `{status}` 但{label}为空或仍是占位符", "STATUS004")

    # --- 3b. 关键信息高风险字段与发布规模 ------------------------------------

    def declared_scale(self) -> str | None:
        """关键信息表的发布规模行；未选定（如 S / M / L）或缺行时返回 None。"""
        for table in self.tables_of("关键信息"):
            for row in table.rows:
                if len(row) >= 2 and "发布规模" in row[0]:
                    value = row[1].strip()
                    return value if re.fullmatch(r"[SML]", value) else None
        return None

    def check_key_information(self) -> None:
        cells: dict[str, str] = {}
        risk_row_counts: dict[str, int] = {}
        for table in self.tables_of("关键信息"):
            if table.header[:2] != ["项目", "内容"]:
                continue
            if not cells:
                cells = {row[0]: row[1] for row in table.rows if len(row) >= 2}  # first-wins，与探针同口径
            for row in table.rows:
                if len(row) >= 2:
                    for field in ("是否涉及资金", "是否涉及账务", "是否涉及交易链路", "是否涉及用户资产"):
                        if field in row[0]:
                            risk_row_counts[field] = risk_row_counts.get(field, 0) + 1
        if not cells:
            self.error("关键信息", "缺少项目/内容表", "KEY001")
            return
        for field in ("是否涉及资金", "是否涉及账务", "是否涉及交易链路", "是否涉及用户资产"):
            if risk_row_counts.get(field, 0) > 1:
                self.error("关键信息", f"高风险字段 `{field}` 出现 {risk_row_counts[field]} 行；重复行/诱饵表会遮蔽真实风险", "KEY003")
            value = next((v for k, v in cells.items() if field in k), None)
            if value is None:
                self.error("关键信息", f"缺少高风险字段行 `{field}`", "KEY001")
            elif not self.draft and value.strip() not in {"是", "否"}:
                self.error("关键信息", f"`{field}` 未决：`{value}`（定稿只允许 是/否）", "KEY002")
        scale_value = next((v for k, v in cells.items() if "发布规模" in k), None)
        if scale_value is None:
            self.error("关键信息", "缺少发布规模行（S/M/L）", "SCALE001")
        elif not self.draft and not re.fullmatch(r"[SML]", scale_value.strip()):
            self.error("关键信息", f"发布规模未选定：`{scale_value}`（只允许 S/M/L 单选）", "SCALE001")

    # --- 3c. 折叠小节必须带原因（只折叠不删除） ------------------------------

    def check_collapsed_subsections(self) -> None:
        names = list(REQUIRED_SUBSECTIONS["基础设施变更"]) + ["依赖变更"]
        for name in names:
            section = self.find_section(name)
            if section is None:
                continue  # 缺章由 check_structure 报 STRUCT003
            has_table = bool(parse_tables(self.lines, self.in_fence, section))
            has_fence = any(
                self.lines[number].lstrip().startswith(("```", "~~~"))
                for number in range(section.start + 1, section.end)
            )
            if has_table or has_fence:
                continue
            reason_ok = False
            for number in range(section.start + 1, section.end):
                raw = self.lines[number]
                if "不涉及" in raw:
                    remainder = raw.replace("不涉及", "").strip(" ：:。.-—>\t")
                    if remainder:
                        reason_ok = True
            if not reason_ok:
                self.error(name, "折叠小节必须写 不涉及 + 原因（只折叠不删除，不得只写不涉及）", "COLLAPSE001")

    # --- 4. 发布结果不得预写 ------------------------------------------------

    def check_release_result(self) -> None:
        cells: dict[str, str] = {}
        for table in self.tables_of("发布结果"):
            if table.header[:2] == ["项目", "内容"]:
                cells = {row[0]: row[1] for row in table.rows if len(row) >= 2}
        if not cells:
            return
        result = cells.get("发布结果", "").strip()
        if result not in {"成功", "回滚", "部分回滚"}:
            return
        if is_gap_only(cells.get("证据", "")) or is_gap_only(cells.get("发布负责人确认", "")):
            self.error("发布结果", f"发布结果已写 `{result}` 但证据或发布负责人确认仍是占位符", "RESULT001")
        if result == "成功" and cells.get("最终验证结论", "").strip() != "通过":
            self.error("发布结果", "发布结果为“成功”但最终验证结论不是“通过”", "RESULT002")

    # --- 5. 变更历史 --------------------------------------------------------

    def check_change_history(self) -> None:
        section = self.find_section("变更历史")
        if section is None:
            return
        text = "\n".join(self.lines[section.start : section.end])
        if "无历史版本" not in text and "```" not in text:
            self.error("变更历史", "必须登记“无历史版本”或上一版有效 RPD 快照；不得留空", "HIST001")
        # 快照围栏必须至少 4 个标记字符：上一版 RPD 自身含 ``` 围栏，
        # 同长度围栏无法嵌套，内层围栏会闭合外层导致标题泄漏、历史计数被截断。
        fence_char = ""
        fence_len = 0
        for number in range(section.start + 1, section.end):
            raw = self.lines[number]
            indent = len(raw) - len(raw.lstrip(" "))
            stripped = raw.lstrip(" ")
            if indent > 3 or not (stripped.startswith("```") or stripped.startswith("~~~")):
                continue
            marker = stripped[:3]
            run = len(stripped) - len(stripped.lstrip(marker[0]))
            if not fence_char:
                if run < 4:
                    self.error(self.at(number), "历史快照围栏必须至少 4 个标记字符（如 ````markdown），否则内层 ``` 围栏会闭合外层", "HIST002")
                fence_char, fence_len = marker[0], run
            elif marker[0] == fence_char and run >= fence_len and not stripped[run:].strip():
                fence_char, fence_len = "", 0

    # --- 6. 独立评审回执 ----------------------------------------------------

    def parse_review_blocks(self) -> list[dict[str, str]]:
        section = self.find_section("独立评审意见")
        if section is None:
            return []
        blocks: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        for number in range(section.start + 1, section.end):
            raw = self.lines[number]
            if raw.startswith("### "):
                if current:
                    blocks.append(current)
                current = {"heading": raw[4:].strip(), "line": str(number + 1)}
                continue
            if current is None or not raw.lstrip().startswith("|"):
                continue
            cells = split_cells(raw)
            if len(cells) >= 2 and cells[0] not in current:
                current[cells[0]] = cells[1]
        if current:
            blocks.append(current)
        return blocks

    def check_review_appendix(self) -> None:
        blocks = self.parse_review_blocks()
        if not blocks:
            self.error("评审", "独立评审意见章节没有任何评审块；至少需要 1 轮独立 Reviewer 结果（初稿阶段本报错属预期）", "REVIEW001")
            return
        for block in blocks:
            where = f"L{block.get('line', '?')}"
            heading = block.get("heading", "")
            if not REVIEW_ID_RE.search(heading):
                self.error(where, f"评审块标题缺少 review_id（形如 RPD-R1）：`{heading}`", "REVIEW002")
            if not DIGEST_RE.search(block.get("稿件摘要", "")):
                self.error(where, "评审块缺少 12 位以上的稿件摘要，无法证明评审对象是当前稿", "REVIEW003")
            status = block.get("状态", "").strip()
            if status not in REVIEW_STATUSES:
                self.error(where, f"评审状态非法：`{status or '空'}`（只允许 {', '.join(REVIEW_STATUSES)}）", "REVIEW004")

    # --- 6b. 回执标题只能出现在文末 ------------------------------------------

    def check_appendix_position(self) -> None:
        """正文中部不得出现回执标题（含无空格变形）；否则指纹剔除会被伪标题欺骗。"""
        real = self.find_section("独立评审意见")
        for heading in self.headings:
            if APPENDIX_TITLE_RE.match(f"{'#' * heading.level} {heading.text}") and (
                real is None or heading.line != real.start
            ):
                self.error(self.at(heading.line), "「独立评审意见」标题只能出现在文末一次；正文中部的回执标题会污染指纹剔除", "REVIEW005")
        for number, raw in enumerate(self.lines):
            if self.in_fence[number] or parse_headings([raw], [False]):
                continue
            if re.match(r"^#{1,6}独立评审意见", raw):
                self.error(self.at(number), "疑似回执标题缺少 # 后的空格；不得以变形标题在正文中制造指纹盲区", "REVIEW005")
        # 回执节必须是文档最后一章：其后只允许回执块标题，不得追加非强制章节
        # 藏匿未经评审的发布口径（那部分正文会被指纹剔除成为盲区）。
        if real is not None:
            for heading in self.headings:
                if heading.line > real.start and not is_receipt_heading(heading):
                    self.error(self.at(heading.line), "回执节之后不得再出现章节标题；独立评审意见必须是文档最后一章", "REVIEW006")

    # --- 汇总 ---------------------------------------------------------------

    def validate(self) -> list[str]:
        if not any(line.startswith("# ") for line in self.lines):
            return ["[EMPTY001] 文档为空或没有一级标题"]
        self.check_structure()
        self.check_example_placeholders()
        self.check_status_tables()
        self.check_key_information()
        self.check_collapsed_subsections()
        self.check_release_result()
        self.check_change_history()
        self.check_review_appendix()
        self.check_appendix_position()
        return self.errors


def check_template_refs(targets: list[Path], template: Path) -> list[str]:
    """防止下游表述与模板/契约漂移。

    - SKILL.md 与 check-standard.md 必须完整列出模板强制章节名；
    - release-review-contract.md 必须包含脚本使用的全部状态/严重度/分类枚举与契约版本；
    - 模板自身必须包含全部强制章节标题。
    """
    errors: list[str] = []
    template_lines = template.read_text(encoding="utf-8").splitlines()
    template_headings = [heading.text for heading in parse_headings(template_lines, fence_flags(template_lines))]
    for name in REQUIRED_SECTIONS:
        if not any(text.startswith(name) for text in template_headings):
            errors.append(f"{template.name}: 模板缺少强制章节标题 {name}")
    for target in targets:
        text = target.read_text(encoding="utf-8")
        if target.name == "release-review-contract.md":
            for token in (CONTRACT_VERSION, *REVIEW_STATUSES, *SEVERITIES, *PERSPECTIVES, *CATEGORIES):
                if token not in text:
                    errors.append(f"{target.name}: 契约缺少脚本枚举 {token}（唯一事实源漂移）")
            continue
        for name in REQUIRED_SECTIONS:
            if name not in text:
                errors.append(f"{target.name}: 未列出模板强制章节 {name}")
    return errors


def main() -> int:
    default_template = Path(__file__).resolve().parents[1] / "references" / "release-doc-template.md"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="待校验的 release-doc.md")
    parser.add_argument("--draft", action="store_true", help="初稿模式：允许检查表状态保留【通过/不涉及】等待定稿占位")
    parser.add_argument("--check-template-refs", action="store_true", help="改为执行章节/枚举引用漂移检查")
    parser.add_argument("--refs-target", action="append", default=[], help="漂移检查目标文件（可重复）")
    parser.add_argument("--template", default=str(default_template))
    args = parser.parse_args()

    if args.check_template_refs:
        targets = [Path(item) for item in args.refs_target]
        if not targets and args.target:
            targets = [Path(args.target)]
        if not targets:
            print("INVALID: --check-template-refs 需要至少一个 --refs-target", file=sys.stderr)
            return 2
        try:
            errors = check_template_refs(targets, Path(args.template))
        except OSError as exc:
            print(f"INVALID: 无法读取文件：{exc}", file=sys.stderr)
            return 2
        if errors:
            print(f"INVALID: {len(errors)} 处章节/枚举引用漂移", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"VALID: {len(targets)} 个文件的章节与枚举引用与模板/契约一致")
        return 0

    if not args.target:
        parser.error("缺少待校验的 release-doc.md 路径")
    path = Path(args.target)
    try:
        errors = RpdValidator(path, draft=args.draft).validate()
    except OSError as exc:
        print(f"INVALID: 无法读取 {path}：{exc}", file=sys.stderr)
        return 2
    if errors:
        print(f"INVALID: {path} 有 {len(errors)} 处结构、状态或回执缺陷", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"VALID: {path} 通过模板结构、状态证据、发布结果、变更历史与评审回执校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())