#!/usr/bin/env python3
"""product-doc 产物的确定性结构门禁。

不依赖子代理或主 Agent 的自述：直接解析生成的 ``product-doc.md``，校验模板结构、
编号追踪、9.3 来源台账覆盖、占位符与模糊表述残留、变更历史与独立评审回执的一致性。

用法：
    python3 validate_prd.py <product-doc.md>
    python3 validate_prd.py --check-template-refs --refs-target SKILL.md --refs-target references/check-standard.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

ID_PREFIXES = (
    "PRD", "OBJ", "KPI", "SC", "FR", "BR", "AF", "ST", "UI", "F",
    "EVT", "EXP", "AC", "DEP", "RISK", "Q", "DEC", "SRC", "PR",
)
ID_RE = re.compile(r"\b(" + "|".join(ID_PREFIXES) + r")-(\d+)\b")
# 一行中的“定义位”：第一个内容恰好是一个编号的单元格（兼容编号不在首列的 8.2/9.2 表）。
PURE_ID_CELL = re.compile(r"^(" + "|".join(ID_PREFIXES) + r")-(\d+)$")
# 9.3 台账必须覆盖的前缀；SRC 只在台账定义，PR 属于评审产物，F 随父功能记录。
LEDGER_PREFIXES = {
    prefix for prefix in ID_PREFIXES if prefix not in {"SRC", "PR", "F"}
}
# 只有定义在正文清单里的前缀才参与“悬挂引用”判定。
DECLARABLE_PREFIXES = LEDGER_PREFIXES | {"F"}
ALLOWED_LABELS = {"修改", "新增", "删除", "已删除", "无", "不涉及", "可选", "待补充"}
PLACEHOLDER_RE = re.compile(r"【([^】]*)】")
VAGUE_TERMS = (
    "体验良好", "高性能", "性能较好", "尽快", "尽量兼容", "明显提升",
    "更好的体验", "大量用户", "响应较快", "基本满足",
)
REVIEW_STATUSES = ("APPROVE", "APPROVE_WITH_FOLLOW_UPS", "REQUEST_CHANGES", "BLOCKED")
PERSPECTIVES = ("COMPLETENESS", "NON_FUNCTIONAL", "COMPREHENSIVE")
REVIEW_ID_RE = re.compile(r"PR-R\d+-[A-Z]{2,3}\b")
DIGEST_RE = re.compile(r"\b[0-9a-fA-F]{12,64}\b")

ALWAYS_REQUIRED_HEADINGS = (
    r"^1\. 文档信息", r"^1\.1 基本信息", r"^1\.2 变更记录",
    r"^2\. 背景与目标", r"^2\.1 问题与机会", r"^2\.2 目标与衡量指标",
    r"^2\.3 范围与非目标", r"^2\.4 前提与依赖",
    r"^3\. 用户与使用场景", r"^3\.1 角色与场景", r"^3\.2 角色与操作权限",
    r"^4\. 功能需求", r"^4\.1 功能清单", r"^4\.2 业务流程", r"^4\.3 功能详细说明", r"^4\.4 页面与交互",
    r"^5\. 非功能要求",
    r"^6\. 数据埋点与监控", r"^6\.1 事件定义", r"^6\.2 指标与漏斗", r"^6\.3 看板、告警与数据质量",
    r"^7\. 验收标准", r"^7\.1 验收清单", r"^7\.2 验收与上线确认",
    r"^8\. 发布、依赖与风险", r"^8\.1 发布与放量", r"^8\.2 依赖与风险",
    r"^9\. 附录", r"^9\.1 术语与参考文档", r"^9\.2 决策与待决问题", r"^9\.3 来源与追踪台账",
    r"^变更历史", r"^PRD 自检与确认", r"^独立评审意见",
)
CHANGE_ONLY_HEADINGS = (r"^本轮变更摘要",)
# 台账与回执区只做“引用”，不做编号定义与表内唯一性检查（9.2 是 Q/DEC 的定义区）。
REFERENCE_HEADING = re.compile(
    r"^9\.3|^变更历史|^独立评审意见|^PRD 自检与确认|^1\.2 变更记录"
)


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
    """标记每行是否位于代码围栏内；围栏内的标题、表格与措辞不参与校验。"""
    flags = [False] * len(lines)
    active = False
    for index, raw in enumerate(lines):
        stripped = raw.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            active = not active
            flags[index] = False
            continue
        flags[index] = active
    return flags


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


def ids_in(text: str) -> list[str]:
    return [f"{match.group(1)}-{match.group(2)}" for match in ID_RE.finditer(text)]


class PrdValidator:
    """对生成的 PRD 做机械校验，返回全部缺陷而非首个失败。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []
        self.lines = path.read_text(encoding="utf-8").splitlines()
        self.in_fence = fence_flags(self.lines)
        self.headings = parse_headings(self.lines, self.in_fence)
        self.sections = build_sections(self.headings, len(self.lines))
        self._owner_cache: dict[int, Section | None] = {}

    # --- 基础设施 -----------------------------------------------------------

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def find_section(self, pattern: str) -> Section | None:
        compiled = re.compile(pattern)
        for section in self.sections:
            if compiled.match(section.text):
                return section
        return None

    def tables_of(self, pattern: str) -> list[Table]:
        return parse_tables(self.lines, self.in_fence, self.find_section(pattern))

    def owner_of(self, number: int) -> Section | None:
        """返回包含该行的最内层章节。"""
        if number in self._owner_cache:
            return self._owner_cache[number]
        best: Section | None = None
        for section in self.sections:
            if section.start <= number < section.end and (best is None or section.start >= best.start):
                best = section
        self._owner_cache[number] = best
        return best

    def at(self, number: int) -> str:
        return f"L{number + 1}"

    # --- 1. 结构 ------------------------------------------------------------

    def is_change_mode(self) -> bool:
        section = self.find_section(r"^变更历史")
        if section is None:
            return False
        text = "\n".join(self.lines[section.start : section.end])
        return "无历史版本" not in text

    def check_structure(self) -> None:
        required = list(ALWAYS_REQUIRED_HEADINGS)
        if self.is_change_mode():
            required += list(CHANGE_ONLY_HEADINGS)
        found: list[int] = []
        for pattern in required:
            section = self.find_section(pattern)
            if section is None:
                self.error("结构", f"缺少模板要求的章节 `/{pattern}/`")
                continue
            found.append(section.start)
        if found != sorted(found):
            self.error("结构", "章节顺序与 product-doc-template.md 不一致")
        for heading in self.headings:
            if re.match(r"^\d+\.\s", heading.text) and heading.level != 1:
                self.error(self.at(heading.line), f"章级标题 `{heading.text}` 必须使用一级标题")

    # --- 2. 占位符与模糊表述 ------------------------------------------------

    def check_residue(self) -> None:
        for number, raw in enumerate(self.lines):
            for match in PLACEHOLDER_RE.finditer(raw):
                label = match.group(1).strip()
                if label not in ALLOWED_LABELS:
                    self.error(self.at(number), f"模板占位符未替换：`【{label[:24]}】`")
            if self.in_fence[number]:
                continue
            for term in VAGUE_TERMS:
                if term in raw:
                    self.error(self.at(number), f"不可验证的模糊表述：`{term}`")

    # --- 3. 编号追踪与来源台账 ----------------------------------------------

    def defining_ids(self, number: int) -> list[str]:
        """标题行与表内的“编号列”是定义位；台账与回执区只定义 SRC。"""
        raw = self.lines[number]
        owner = self.owner_of(number)
        if raw.startswith("#"):
            return ids_in(raw)
        if not raw.lstrip().startswith("|"):
            return []
        identifiers = [cell for cell in split_cells(raw) if PURE_ID_CELL.match(cell)]
        if owner is not None and REFERENCE_HEADING.match(owner.text):
            return [item for item in identifiers if item.startswith("SRC-")]
        return identifiers[:1]

    def check_traceability(self) -> None:
        declared: dict[str, int] = {}
        referenced: set[str] = set()
        for number in range(len(self.lines)):
            if self.in_fence[number]:
                continue
            referenced.update(ids_in(self.lines[number]))
            for identifier in self.defining_ids(number):
                declared.setdefault(identifier, number + 1)
        dangling = sorted(
            item
            for item in referenced - set(declared)
            if item.split("-")[0] in DECLARABLE_PREFIXES
        )
        if dangling:
            self.error("追踪", f"编号被引用但从未在正文定义：{', '.join(dangling)}")
        self.check_table_duplicates()
        self.check_ledger_coverage(declared)
        self.check_deleted_ids()
        self.check_p0_acceptance()

    def check_table_duplicates(self) -> None:
        """表内编号唯一性按“表格所属章节”判定，而不是按遍历到的章节。

        台账与回执区可能被上级章节包含（如 9.3 位于 9. 附录之内），因此必须先归属再过滤，
        否则会把台账里的 SRC 编号误判成重复定义。
        """
        inspected: set[int] = set()
        for section in self.sections:
            for table in parse_tables(self.lines, self.in_fence, section):
                if table.header_line in inspected:
                    continue
                inspected.add(table.header_line)
                owner = self.owner_of(table.header_line)
                if owner is None or REFERENCE_HEADING.match(owner.text):
                    continue
                seen: dict[str, int] = {}
                for line in table.row_lines:
                    for identifier in self.defining_ids(line):
                        if identifier in seen:
                            self.error(
                                self.at(line),
                                f"编号 `{identifier}` 在同一张表内重复定义（首见于 L{seen[identifier]}）",
                            )
                        seen.setdefault(identifier, line + 1)

    def check_ledger_coverage(self, declared: dict[str, int]) -> None:
        ledger = self.find_section(r"^9\.3")
        if ledger is None:
            return
        covered = set(ids_in("\n".join(self.lines[ledger.start : ledger.end])))
        missing = sorted(
            identifier
            for identifier in declared
            if identifier.split("-")[0] in LEDGER_PREFIXES and identifier not in covered
        )
        if missing:
            self.error(
                "来源",
                f"以下编号在 9.3 来源与追踪台账中没有资料支撑：{', '.join(missing[:15])}"
                + ("…" if len(missing) > 15 else ""),
            )
        ghosts = sorted(
            identifier
            for identifier in covered
            if identifier.split("-")[0] in LEDGER_PREFIXES and identifier not in declared
        )
        if ghosts:
            self.error("来源", f"9.3 台账引用了正文中不存在的编号：{', '.join(ghosts[:15])}")

    def check_deleted_ids(self) -> None:
        """【删除】台账里声明已移除的编号，不得还留在 4.1 功能清单的当前有效条目中。"""
        section = self.find_section(r"^本轮变更摘要")
        if section is None:
            return
        removed: set[str] = set()
        for number in range(section.start, section.end):
            if "【删除】" in self.lines[number]:
                removed.update(ids_in(self.lines[number]))
        if not removed:
            return
        for table in self.tables_of(r"^4\.1 功能清单"):
            for row in table.rows:
                if not row:
                    continue
                hits = sorted(removed & set(ids_in(row[0])))
                for identifier in hits:
                    self.error(
                        "4.1",
                        f"已删除编号 `{identifier}` 仍作为 4.1 功能清单的当前有效条目",
                    )

    def check_p0_acceptance(self) -> None:
        for table in self.tables_of(r"^4\.1 功能清单"):
            joined = "".join(table.header)
            if "优先级" not in joined or "关联验收" not in joined:
                self.error("4.1", "功能清单表头缺少“优先级”或“关联验收”列（不得删列）")
                continue
            priority = next(index for index, cell in enumerate(table.header) if "优先级" in cell)
            acceptance = next(index for index, cell in enumerate(table.header) if "关联验收" in cell)
            for offset, row in enumerate(table.rows):
                if len(row) <= max(priority, acceptance):
                    self.error("4.1", f"功能清单第 {offset + 1} 行列数不足：{row}")
                    continue
                if not row[priority].startswith("P0"):
                    continue
                if not any(item.startswith("AC-") for item in ids_in(row[acceptance])):
                    identifier = row[0] if row else f"第 {offset + 1} 行"
                    self.error("4.1", f"P0 功能 `{identifier}` 的“关联验收”未引用任何 AC 编号")

    def check_open_questions(self) -> None:
        for table in self.tables_of(r"^9\.2 决策与待决问题"):
            header_join = "".join(table.header)
            if "负责人" not in header_join or "当前状态" not in header_join:
                self.error("9.2", "决策与待决问题表头缺少“负责人”或“当前状态”列")
                continue
            columns = {
                name: next(index for index, cell in enumerate(table.header) if name in cell)
                for name in ("编号", "负责人", "截止日期", "当前状态")
            }
            for offset, row in enumerate(table.rows):
                if not row or not re.match(r"Q-\d+", row[columns["编号"]] if len(row) > columns["编号"] else ""):
                    continue
                for name in ("负责人", "截止日期", "当前状态"):
                    value = row[columns[name]] if len(row) > columns[name] else ""
                    if value.strip() in {"", "-", "—"}:
                        self.error("9.2", f"`{row[columns['编号']]}` 的“{name}”为空，缺口必须可指派")
                if len(row) > columns["截止日期"]:
                    deadline = row[columns["截止日期"]]
                    if not re.search(r"\d{4}-\d{2}-\d{2}", deadline):
                        self.error("9.2", f"`{row[columns['编号']]}` 的截止日期不是具体日期：`{deadline}`")

    # --- 4. 变更历史 --------------------------------------------------------

    def check_change_history(self) -> None:
        section = self.find_section(r"^变更历史")
        if section is None:
            return
        if any(self.lines[number].lstrip().startswith(("```", "~~~")) for number in range(section.start, section.end)):
            self.error(
                "变更历史",
                "检测到内嵌代码围栏：上一版正文快照必须写入同目录 history/ 文件，本表只登记路径与内容摘要",
            )
        seen_keys: set[tuple[str, str]] = set()
        data_rows = 0
        for table in parse_tables(self.lines, self.in_fence, section):
            for row in table.rows:
                data_rows += 1
                if len(row) < 7:
                    self.error("变更历史", f"台账行缺少内容摘要/快照路径/确认人等必需列：{row}")
                    continue
                key = (row[0], row[2])
                if key in seen_keys:
                    self.error("变更历史", f"版本 `{row[0]}` 与内容摘要 `{row[2]}` 组合重复追加（重跑未判重）")
                seen_keys.add(key)
                if not DIGEST_RE.search(row[2]):
                    self.error("变更历史", f"版本 `{row[0]}` 的内容摘要不是 sha256 片段：`{row[2]}`")
                if "无历史版本" in row[5]:
                    continue
                if not re.search(r"history/[^\s`]*\.md", row[5]):
                    self.error("变更历史", f"版本 `{row[0]}` 的快照路径未指向 history/ 下的独立文件：`{row[5]}`")
        if data_rows == 0:
            self.error("变更历史", "变更历史台账为空；新需求也必须写一行“无历史版本”")

    # --- 5. 独立评审回执与结论一致性 ----------------------------------------

    def parse_review_blocks(self) -> list[dict[str, str]]:
        section = self.find_section(r"^独立评审意见")
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

    def check_review_appendix(self) -> list[str]:
        blocks = self.parse_review_blocks()
        if not blocks:
            self.error("评审", "独立评审意见章节没有任何评审块；至少需要 1 轮独立 Reviewer 结果")
            return []
        perspectives: set[str] = set()
        statuses: list[str] = []
        for block in blocks:
            where = f"L{block.get('line', '?')}"
            heading = block.get("heading", "")
            tokens = [token.strip() for token in re.split(r"[·|/]", heading)]
            found = [token for token in tokens if token in PERSPECTIVES]
            if found:
                perspectives.add(found[0])
            else:
                self.error(where, f"评审块标题缺少 perspective：`{heading}`")
            if not any(REVIEW_ID_RE.search(token) for token in tokens):
                self.error(where, f"评审块标题缺少 review_id（形如 PR-R1-CO）：`{heading}`")
            if not DIGEST_RE.search(block.get("稿件摘要", "")):
                self.error(where, "评审块缺少 12 位以上的稿件摘要，无法证明评审对象是当前稿")
            status = block.get("状态", "").strip()
            if status not in REVIEW_STATUSES:
                self.error(where, f"评审状态非法：`{status or '空'}`（只允许 {', '.join(REVIEW_STATUSES)}）")
            else:
                statuses.append(status)
        self.check_review_routing(perspectives)
        self.check_signoff(statuses)
        return statuses

    def declared_scale(self) -> str | None:
        for table in self.tables_of(r"^1\.1 基本信息"):
            for row in table.rows:
                if len(row) >= 2 and "需求规模" in row[0]:
                    letters = set(re.findall(r"\b([SML])\b", row[1].split("/")[0]))
                    if len(letters) == 1:
                        return letters.pop()
                    self.error("1.1", f"需求规模未选定，无法路由独立评审：`{row[1]}`")
                    return None
        self.error("1.1", "基本信息表中找不到“需求规模 / 优先级”行")
        return None

    def check_review_routing(self, perspectives: set[str]) -> None:
        scale = self.declared_scale()
        if scale is None:
            return
        required = {"COMPREHENSIVE"} if scale == "S" else {"COMPLETENESS", "NON_FUNCTIONAL"}
        missing = sorted(required - perspectives)
        if missing:
            self.error("评审", f"{scale} 级需求的独立评审缺少视角：{', '.join(missing)}")

    def check_signoff(self, statuses: list[str]) -> None:
        conclusion = ""
        blockers = ""
        failures = 0
        for table in self.tables_of(r"^PRD 自检与确认"):
            joined = "".join(table.header)
            if "检查项" in joined and "结论" in joined:
                index = table.header.index(next(cell for cell in table.header if "结论" in cell))
                for row in table.rows:
                    if len(row) > index and "不通过" in row[index]:
                        failures += 1
            elif table.header == ["项目", "内容"]:
                cells = {row[0]: row[1] for row in table.rows if len(row) >= 2}
                conclusion = cells.get("结论", "")
                blockers = cells.get("阻塞项与遗留项", "")
        if "已确认" not in conclusion or "有条件确认" in conclusion:
            return
        if failures:
            self.error("自检", f"存在 {failures} 项自检不通过，结论却写“已确认”")
        if blockers.strip() not in {"无", "无。", ""}:
            self.error("自检", f"结论为“已确认”但阻塞项非空：{blockers.strip()[:40]}")
        if statuses and statuses[-1] not in {"APPROVE", "APPROVE_WITH_FOLLOW_UPS"}:
            self.error("自检", f"最后一轮独立评审状态为 `{statuses[-1]}`，结论不得写“已确认”")

    # --- 汇总 ---------------------------------------------------------------

    def validate(self) -> list[str]:
        if not any(line.startswith("# ") for line in self.lines):
            return ["文档为空或没有一级标题"]
        self.check_structure()
        self.check_residue()
        self.check_traceability()
        self.check_open_questions()
        self.check_change_history()
        self.check_review_appendix()
        return self.errors


def template_section_ids(template: Path) -> set[str]:
    lines = template.read_text(encoding="utf-8").splitlines()
    in_fence = fence_flags(lines)
    ids: set[str] = set()
    for heading in parse_headings(lines, in_fence):
        for match in re.finditer(r"(?<![\d.])(\d+\.\d+)(?![\d.])", heading.text):
            ids.add(match.group(1))
    return ids


def check_template_refs(targets: list[Path], template: Path) -> list[str]:
    """防止 SKILL.md 或 check-standard.md 引用模板里已不存在的章节号。"""
    valid = template_section_ids(template)
    errors: list[str] = []
    pattern = re.compile(r"(?<![\dA-Za-z.])([1-9]\.[1-9])(?![\dA-Za-z.%])")
    for target in targets:
        lines = target.read_text(encoding="utf-8").splitlines()
        in_fence = fence_flags(lines)
        for number, raw in enumerate(lines):
            if in_fence[number]:
                continue
            for match in pattern.finditer(raw):
                if match.group(1) not in valid:
                    errors.append(f"{target.name}:{number + 1}: 引用了模板不存在的小节 {match.group(1)}")
    return errors


def main() -> int:
    default_template = Path(__file__).resolve().parents[1] / "references" / "product-doc-template.md"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="待校验的 product-doc.md")
    parser.add_argument("--check-template-refs", action="store_true", help="改为执行章节引用漂移检查")
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
            print(f"INVALID: {len(errors)} 处章节引用漂移", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
            return 1
        print(f"VALID: {len(targets)} 个文件的章节引用与模板一致")
        return 0

    if not args.target:
        parser.error("缺少待校验的 product-doc.md 路径")
    path = Path(args.target)
    try:
        errors = PrdValidator(path).validate()
    except OSError as exc:
        print(f"INVALID: 无法读取 {path}：{exc}", file=sys.stderr)
        return 2
    if errors:
        print(f"INVALID: {path} 有 {len(errors)} 处结构、追踪或回执缺陷", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"VALID: {path} 通过模板结构、编号追踪、来源台账、变更历史与评审回执校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
