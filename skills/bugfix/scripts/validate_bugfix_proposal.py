#!/usr/bin/env python3
"""校验后端 Bugfix 提案（proposal.md）的结构与门禁语义。

必需章节、字段标签、影响面维度、"必需"列和占位符清单全部从
`assets/bugfix-proposal-template.md` 派生：模板是唯一事实源，改模板不需要再改本脚本。
只有具备门禁语义的规则保留在代码里（三态判定、审批口令绑定、E2/E3 证据等级、
允许修改文件必须是仓库相对路径），并且当模板不再提供这些锚点标签时会立即报错，
而不是静默放过。

退出码：0 合规；1 结构或内容不合规；2 输入、模板或用法错误。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = SKILL_DIR / "assets" / "bugfix-proposal-template.md"

# 模板中用于划分「诊断区（任何判定都必须填）」与「方案区（仅 CONFIRMED_BUG 强制）」的锚点。
PROPOSAL_MARKER = "bugfix:proposal-required-start"

# 这些标签承载审批绑定与写入范围语义；模板若重命名它们，校验器必须显式失败。
ANCHOR_LABELS = ("方案 ID", "方案版本", "状态", "允许修改文件", "审批状态", "审批口令")

STATUS_ENUM = ("CONFIRMED_BUG", "NOT_A_BUG", "INSUFFICIENT_EVIDENCE")
STATUS_PATTERN = "|".join(STATUS_ENUM)
PROPOSAL_ID = re.compile(r"BF-\d{8}-[A-Za-z0-9][A-Za-z0-9-]*")
TODO_PATTERN = re.compile(r"\bTODO\b|\bTBD\b|待补充|待确认|待填")
EVIDENCE_LEVEL = re.compile(r"\bE([0-3])\b")
# 字段与标题的行内空白一律用 [ \t]，避免 \s 跨行吞掉下一行导致字段错位。
HEADING = re.compile(r"^##[ \t]+(?P<number>\d+)\.[ \t]+(?P<title>[^#\n]*?)[ \t]*$", flags=re.MULTILINE)
FIELD_LINE = re.compile(
    r"^[ \t]*-[ \t]+\*\*(?P<label>[^*]+)\*\*[：:][ \t]*(?P<value>.*?)[ \t]*$", flags=re.MULTILINE
)
TABLE_ROW = re.compile(r"^[ \t]*\|.*\|[ \t]*$")
TABLE_SEPARATOR = re.compile(r"^[ \t]*\|[ \t|:-]+\|[ \t]*$")
PLACEHOLDER = re.compile(r"<[^<>\n]+>")
MANDATORY_HEADER = re.compile(r"必需|必填|must", flags=re.IGNORECASE)
AFFIRMATIVE = {"yes", "y", "是", "必须", "必需"}
DENY_LISTS = re.compile(r"(?:^|[\s、，,])(?:全部|所有|按需|不限|任意|自行决定)")
REPORTED_PLACEHOLDERS = 5


class ValidationError(Exception):
    """输入、模板或用法层面的不可恢复错误，对应退出码 2。"""


def _reconfigure_streams() -> None:
    """Windows 上重定向到管道时 stdout/stderr 跟随 ANSI 代码页，中文错误清单会变乱码；
    这份错误清单正是主 Agent 逐条修正提案的唯一依据，因此强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover
            pass


@dataclass(frozen=True)
class Section:
    number: str
    title: str
    body: str
    start: int


@dataclass(frozen=True)
class SectionSpec:
    number: str
    title: str
    labels: tuple[str, ...]
    required_rows: tuple[str, ...]
    mandatory_column: str | None = None
    label_values: dict[str, str] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class TemplateSchema:
    sections: tuple[SectionSpec, ...]
    proposal_start: int
    status_label: str
    placeholders: tuple[str, ...]

    @property
    def all_labels(self) -> set[str]:
        return {label for spec in self.sections for label in spec.labels}


def split_sections(text: str) -> list[Section]:
    matches = list(HEADING.finditer(text))
    sections: list[Section] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            Section(
                number=match.group("number"),
                title=match.group("title").strip(),
                body=text[match.end():end],
                start=match.start(),
            )
        )
    return sections


def iter_tables(body: str) -> list[tuple[list[str], list[list[str]]]]:
    """按 Markdown 表格解析正文，返回 (表头单元格, 数据行) 列表。"""
    lines = body.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    for index, line in enumerate(lines):
        if not TABLE_SEPARATOR.match(line):
            continue
        if index == 0 or not TABLE_ROW.match(lines[index - 1]):
            continue
        header = _cells(lines[index - 1])
        rows: list[list[str]] = []
        for row in lines[index + 1:]:
            if not TABLE_ROW.match(row):
                break
            rows.append(_cells(row))
        tables.append((header, rows))
    return tables


def _cells(row: str) -> list[str]:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def template_sections(text: str) -> tuple[tuple[SectionSpec, ...], int]:
    """从模板派生章节规格；``proposal_start`` 是方案区第一个章节的编号。"""
    marker = text.find(f"<!-- {PROPOSAL_MARKER}")
    if marker < 0:
        raise ValidationError(
            f"模板缺少分区锚点注释 `<!-- {PROPOSAL_MARKER} -->`，无法判断哪些章节只对 "
            "CONFIRMED_BUG 强制校验。请保留模板中的该注释。"
        )
    specs: list[SectionSpec] = []
    proposal_start = 0
    for section in split_sections(text):
        if section.start > marker and not proposal_start:
            proposal_start = int(section.number)
        fields_list = list(FIELD_LINE.finditer(section.body))
        labels = tuple(dict.fromkeys(match.group("label").strip() for match in fields_list))
        rows: list[str] = []
        mandatory_column: str | None = None
        for header, table_rows in iter_tables(section.body):
            for cell in header:
                if MANDATORY_HEADER.search(_bare(cell)):
                    mandatory_column = _bare(cell)
                    break
            for row in table_rows:
                if not row:
                    continue
                key = _bare(row[0])
                # 首列本身是占位符的行（例如「计划修改」表）由作者自行决定条数，不作强制要求。
                if key and not PLACEHOLDER.search(row[0]):
                    rows.append(key)
        specs.append(
            SectionSpec(
                number=section.number,
                title=section.title,
                labels=labels,
                required_rows=tuple(dict.fromkeys(rows)),
                mandatory_column=mandatory_column,
                label_values={
                    match.group("label").strip(): match.group("value").strip()
                    for match in fields_list
                },
            )
        )
    if not proposal_start:
        raise ValidationError(f"模板锚点 `<!-- {PROPOSAL_MARKER} -->` 之后没有 `## N.` 章节。")
    return tuple(specs), proposal_start


def load_schema(template: Path) -> TemplateSchema:
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"无法读取提案模板 {template}: {exc}") from exc
    specs, proposal_start = template_sections(text)
    schema = TemplateSchema(
        sections=specs,
        proposal_start=proposal_start,
        status_label=_status_label(specs),
        placeholders=template_placeholders(text),
    )
    missing = [label for label in ANCHOR_LABELS if label not in schema.all_labels]
    if missing:
        raise ValidationError(
            f"模板 {template} 不再提供校验器依赖的锚点标签：{', '.join(missing)}"
        )
    return schema


def _status_label(specs: tuple[SectionSpec, ...]) -> str:
    if not specs or not specs[0].labels:
        raise ValidationError("模板第一个章节没有 `- **标签**：值` 字段，无法定位问题判定。")
    return specs[0].labels[0]


def template_placeholders(text: str) -> tuple[str, ...]:
    """收集模板里的占位符字面量；HTML 注释（含分区锚点）不算占位符。"""
    body = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return tuple(dict.fromkeys(PLACEHOLDER.findall(body)))


def _bare(value: str) -> str:
    return value.strip().strip("`").strip()


def is_template_placeholder(value: str, schema: TemplateSchema) -> bool:
    return any(literal in value for literal in schema.placeholders)


def has_meaningful_value(value: str, schema: TemplateSchema) -> bool:
    bare = value.strip()
    return bool(bare) and not TODO_PATTERN.search(bare) and not is_template_placeholder(bare, schema)


def find_section(sections: list[Section], spec: SectionSpec) -> Section | None:
    for section in sections:
        if section.number == spec.number and section.title == spec.title:
            return section
    for section in sections:
        if section.number == spec.number:
            return section
    return None


def section_fields(body: str) -> dict[str, list[str]]:
    fields_map: dict[str, list[str]] = {}
    for match in FIELD_LINE.finditer(body):
        fields_map.setdefault(match.group("label").strip(), []).append(match.group("value").strip())
    return fields_map


def check_section(spec: SectionSpec, body: str, schema: TemplateSchema, check_values: bool) -> list[str]:
    """校验单个章节的字段与表格；``check_values`` 为假时只做存在性检查（草稿模式）。"""
    errors: list[str] = []
    fields_map = section_fields(body)
    for label in spec.labels:
        values = fields_map.get(label, [])
        if not values:
            errors.append(
                f"缺少字段：{label}（章节 ## {spec.number}. {spec.title}，"
                f"须写成列表项：- **{label}**：实际内容）"
            )
        elif check_values and not any(has_meaningful_value(value, schema) for value in values):
            errors.append(
                f"字段没有有效内容：{label}（章节 ## {spec.number}. {spec.title}，"
                "不得保留模板占位符或写 TODO/待补充）"
            )

    tables = iter_tables(body)
    flat_rows = {(_bare(row[0]) if row else ""): row for header, rows in tables for row in rows}
    for key in spec.required_rows:
        row = flat_rows.get(key)
        if row is None:
            errors.append(f"影响面缺少维度行：{key}（须保留模板中的维度名原文）")
        elif check_values and not all(
            has_meaningful_value(cell, schema) for cell in row[1:]
        ):
            errors.append(f"影响面维度行未填写完整：{key}")

    if spec.mandatory_column:
        errors.extend(check_mandatory_column(spec, tables, schema, check_values))
    return errors


def check_mandatory_column(
    spec: SectionSpec, tables: list[tuple[list[str], list[list[str]]]], schema: TemplateSchema,
    check_values: bool,
) -> list[str]:
    """验证计划表必须至少有一行把「必需」列填成肯定值。"""
    errors: list[str] = []
    for header, rows in tables:
        bare_header = [_bare(cell) for cell in header]
        if spec.mandatory_column not in bare_header:
            continue
        index = bare_header.index(spec.mandatory_column)
        satisfied = any(
            len(row) > index and _bare(row[index]).lower() in AFFIRMATIVE for row in rows
        )
        if satisfied:
            return []
        if check_values:
            errors.append(
                f"验证计划至少需要一项『{spec.mandatory_column}=Yes』的检查"
                f"（章节 ## {spec.number}. {spec.title} 的表格内，可写 Yes/是）"
            )
        return errors
    errors.append(
        f"章节 ## {spec.number}. {spec.title} 的表格缺少『{spec.mandatory_column}』列"
    )
    return errors


def get_status(text: str, schema: TemplateSchema) -> str | None:
    pattern = re.compile(
        rf"\*\*{re.escape(schema.status_label)}\*\*\s*[：:]\s*`?\s*({STATUS_PATTERN})"
    )
    match = pattern.search(text)
    return match.group(1) if match else None


def diagnosis_text(sections: list[Section]) -> str:
    return "\n".join(section.body for section in sections)


def diagnosis_region(sections: list[Section], schema: TemplateSchema) -> list[Section]:
    return [section for section in sections if int(section.number) < schema.proposal_start]


def check_confirmed_semantics(
    text: str, sections: list[Section], schema: TemplateSchema, errors: list[str]
) -> None:
    """CONFIRMED_BUG 的审批绑定、证据等级与写入范围不变量。"""
    labels: dict[str, list[str]] = {}
    for section in sections:
        for label, values in section_fields(section.body).items():
            labels.setdefault(label, []).extend(values)

    ids = [_bare(value) for value in labels.get("方案 ID", [])]
    for proposal_id in ids:
        if not PROPOSAL_ID.fullmatch(proposal_id):
            errors.append(f"方案 ID 必须符合 BF-YYYYMMDD-short-name：{proposal_id}")
    versions = [_bare(value) for value in labels.get("方案版本", [])]
    for version in versions:
        if not re.fullmatch(r"\d+", version):
            errors.append(f"方案版本必须是整数：{version}")
    for label in ("状态", "审批状态"):
        for value in labels.get(label, []):
            if _bare(value) != "WAITING_APPROVAL":
                errors.append(f"{label} 必须为 WAITING_APPROVAL：{value}")

    if ids and versions and PROPOSAL_ID.fullmatch(ids[0]) and re.fullmatch(r"\d+", versions[0]):
        phrase = re.compile(rf"批准执行\s+{re.escape(ids[0])}\s+v{re.escape(versions[0])}")
        if not phrase.search(text):
            errors.append(
                f"审批口令必须明确绑定方案 ID 和版本：批准执行 {ids[0]} v{versions[0]}"
            )

    levels = set(EVIDENCE_LEVEL.findall(diagnosis_text(diagnosis_region(sections, schema))))
    if levels and not levels & {"2", "3"}:
        errors.append(
            f"CONFIRMED_BUG 的判定章节证据等级不足：只出现 E{'/E'.join(sorted(levels))}，"
            "根因要求 E2 或 E3（见 references/contracts.md 第 2 节）"
        )

    for value in labels.get("允许修改文件", []):
        if not has_meaningful_value(value, schema):
            continue
        if DENY_LISTS.search(value):
            errors.append(
                f"允许修改文件必须是明确的仓库相对路径清单，不得使用开放表述：{value}"
            )
        for token in re.split(r"[,，;；\s]+", value.strip("`").strip()):
            if not token or token.startswith("<"):
                continue
            if token.startswith("/") or token.startswith("./") or ".." in token.split("/"):
                errors.append(f"允许修改文件必须是仓库相对路径：{token}")


def validate(path: Path, template: Path, allow_placeholders: bool) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError(f"无法读取提案 {path}: {exc}") from exc
    schema = load_schema(template)

    errors: list[str] = []
    sections = split_sections(text)
    for spec in schema.sections:
        if not any(s.number == spec.number and s.title == spec.title for s in sections):
            errors.append(f"缺少必需章节：## {spec.number}. {spec.title}")

    status = get_status(text, schema)
    if status is None:
        errors.append(f"缺少有效的问题判定：- **{schema.status_label}**：{' | '.join(STATUS_ENUM)}")
        check_values = not allow_placeholders
        for spec in schema.sections:
            section = find_section(sections, spec)
            if section is not None:
                errors.extend(check_section(spec, section.body, schema, check_values))
    else:
        confirmed = status == "CONFIRMED_BUG"
        for spec in schema.sections:
            in_proposal = int(spec.number) >= schema.proposal_start
            if in_proposal and not confirmed:
                continue
            section = find_section(sections, spec)
            if section is None:
                continue  # 缺失章节已在上面报告
            errors.extend(
                check_section(spec, section.body, schema, confirmed and not allow_placeholders)
            )
        if confirmed:
            if not allow_placeholders:
                check_confirmed_semantics(text, sections, schema, errors)
        elif not re.search(r"不(?:执行|进入|实施|创建).{0,12}修复", text):
            errors.append(f"{status} 必须明确说明不执行修复")

    if not allow_placeholders:
        leftovers = [literal for literal in schema.placeholders if literal in text]
        for literal in leftovers[:REPORTED_PLACEHOLDERS]:
            errors.append(f"存在未替换的模板占位符：{literal}")
        if len(leftovers) > REPORTED_PLACEHOLDERS:
            errors.append(f"另有 {len(leftovers) - REPORTED_PLACEHOLDERS} 处模板占位符未替换")
        generic = TODO_PATTERN.search(text)
        if generic:
            errors.append(f"存在未替换占位符：{generic.group(0)}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proposal", type=Path)
    parser.add_argument(
        "--template", type=Path, default=DEFAULT_TEMPLATE,
        help=f"提案模板，用于派生必需章节与字段（默认 {DEFAULT_TEMPLATE}）",
    )
    parser.add_argument(
        "--allow-placeholders", action="store_true",
        help="草稿模式：只校验结构存在性，不校验内容是否已替换占位符",
    )
    args = parser.parse_args(argv)

    if not args.proposal.is_file():
        print(f"ERROR: file not found: {args.proposal}", file=sys.stderr)
        return 2
    if not args.template.is_file():
        print(f"ERROR: template not found: {args.template}", file=sys.stderr)
        return 2

    try:
        errors = validate(args.proposal, args.template, args.allow_placeholders)
    except ValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    mode = "draft" if args.allow_placeholders else "complete"
    print(f"OK[{mode}]: bugfix proposal structure is valid: {args.proposal}")
    return 0


if __name__ == "__main__":
    _reconfigure_streams()
    raise SystemExit(main())
