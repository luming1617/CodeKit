"""release-doc 门禁脚本与资产契约的回归测试。

覆盖 P0 改造的验收口径：

* P0-1 轮次、指纹、Finding 闭环与交付门禁由脚本判定，不依赖 Agent 自述；
* P0-2 草稿落盘与定向补丁的运输层表述被静态锁定，30% 重写阈值可机械判定；
* P0-3 回执与归档只能由脚本渲染，外置归档一一对应，单元格转义防撕裂；
* P0-4 固有缺口分流与 severity/blocking 对应表只有契约一个事实源；
* P0-5 章节/枚举漂移可被 --check-template-refs 检出。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import release_state  # noqa: E402
from validate_release_doc import RpdValidator, check_template_refs, fence_flags, parse_headings  # noqa: E402

SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
TEMPLATE = (SKILL_DIR / "references" / "release-doc-template.md").read_text(encoding="utf-8")
STANDARD = (SKILL_DIR / "references" / "check-standard.md").read_text(encoding="utf-8")
CONTRACT = (SKILL_DIR / "references" / "release-review-contract.md").read_text(encoding="utf-8")
AGENTS = {path.stem: path.read_text(encoding="utf-8") for path in sorted((SKILL_DIR / "agents").glob("*.md"))}

RPD_BODY = """# 后端服务发布方案

## 关键信息

| 项目 | 内容 |
|------|------|
| 需求描述 | 订单导出增加字段 |
| 风险等级 | P2 |
| 发布规模 | S |
| 发布方式 | 滚动 |
| 发布负责人 | 【填写】 |
| 是否涉及资金 | 否 |
| 是否涉及账务 | 否 |
| 是否涉及交易链路 | 否 |
| 是否涉及用户资产 | 否 |

## 变更业务

订单导出新增渠道字段，影响商家后台导出功能。

## 变更服务

| 发布顺序 | 服务名称 | 仓库地址 | 变更模块 | MR 地址 | 主要内容 | 影响范围 |
|---------:|----------|----------|----------|---------|----------|----------|
| 1 | order-service | 【填写】 | export | 【链接】 | 新增渠道字段 | 商家导出 |

## 变更产物

| 服务名称 | 产物类型 | 本次发布产物 | 构建/流水线 | 上一个已发布产物可回滚 |
|----------|----------|--------------|-------------|------------------------|
| order-service | JAR | 【填写】 | 【链接】 | 是 |

## 变更接口

### 接口检查

| 检查项 | 状态 | 证据/说明 |
|--------|------|-----------|
| HTTP 接口向后兼容 | 通过 | 新增字段可选，见 TDD 4.2 |
| Dubbo 接口向后兼容 | 不涉及 | 本次无 Dubbo 接口变更 |

## 基础设施变更

### MySQL

不涉及：本次无数据库变更（TDD 第 5 章明确）。

### Redis

不涉及：本次无缓存变更。

### MQ / Kafka

不涉及：本次无消息变更。

### Nacos

不涉及：本次无配置变更。

### XXL-JOB

不涉及：本次无任务变更。

## 依赖变更

不涉及：本次无依赖版本变更。

## 发布前检查（人工 / AI）

| 检查项 | 人工结果 | AI 结果 | 最终状态 | 证据/说明 |
|--------|----------|---------|----------|-----------|
| 回归测试 | 通过 | 无法校验 | 通过 | QA 报告 QA-2026-11 |
| 回滚产物 | 待确认 | 无法校验 | 不涉及 | 上一版 JAR 可回滚，见变更产物 |

## 发布策略

滚动发布：最大不可用实例数 0，最大新增实例数 1，间隔 30s。

## 发布步骤

| 序号 | 操作 | 执行人 | 操作结果 |
|-----:|------|--------|----------|
| 1 | 发布 order-service 首个实例 | 【填写】 | 待执行 |
| 2 | 执行技术验证 | 【填写】 | 待执行 |

## 发布后验证

### 技术验证

| 验证项 | 状态 | 结果/证据 |
|--------|------|-----------|
| 所有本次发布实例启动正常 | 待执行 | 【填写】 |
| HTTP 调用正常 | 待执行 | 【填写】 |

### 业务验证

| 验证场景 | 预期结果 | 实际结果 | 状态 | 证据 |
|----------|----------|----------|------|------|
| 订单导出正常流程 | 导出含渠道字段 | 【填写】 | 待执行 | 【填写】 |

## 发布后观察

| 指标 | 发布前基线 | 通过标准 / 回滚阈值 | 灰度后 | 全量后 | 结论 |
|------|------------|---------------------|--------|--------|------|
| 接口成功率 | 【填写】 | 【填写】 | 【填写】 | 【填写】 | 正常 / 异常 |

## 回滚方案

### 触发条件

核心接口错误率超过阈值并持续 5 分钟（阈值需人工补充，RQ-001）。

### 回滚服务

| 服务名称 | 是否回滚 | 如何回滚 | 执行人 | 通过标准 |
|----------|----------|----------|--------|----------|
| order-service | 是 | 部署上一版 JAR | 【填写】 | 实例启动正常 |

### 回滚步骤

1. 停止继续发布；2. 部署上一版产物；3. 执行核心验证。

### 数据处理

不涉及：本次无数据变更。

### 回滚验证

| 验证项 | 状态 | 证据 |
|--------|------|------|
| 应用实例已恢复到预期版本/产物 | 待执行 | 【填写】 |

## 发布结果

| 项目 | 内容 |
|------|------|
| 发布结果 | 【填写】 |
| 最终验证结论 | 【填写】 |
| 证据 | 【填写】 |
| 发布负责人确认 | 【填写】 |

## 遗留问题

| 问题 | 风险 | 临时措施 | 负责人 | 完成时间 |
|------|------|----------|--------|----------|
| RQ-001 回滚阈值待确认 | 回滚触发不可量化 | 发布前人工确认 | 需人工指定 | 发布前 |

## 变更历史（只追加，不覆盖；需求变更时必填）

无历史版本
"""


def errors_of(text: str, draft: bool = False) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "release-doc.md"
        path.write_text(text, encoding="utf-8")
        return RpdValidator(path, draft=draft).validate()


def joined(errors: list[str]) -> str:
    return "\n".join(errors)


def appendix_block(review_id: str = "RPD-R1", status: str = "APPROVE", digest: str = "a" * 64) -> str:
    payload = {
        "review_id": review_id,
        "review_round": int(review_id.split("R")[-1]),
        "perspective": "COMPREHENSIVE",
        "status": status,
        "rpd_digest": digest,
        "summary": "覆盖结构、证据与回滚维度",
        "findings": [],
        "appendix_markdown": "**结论**：未发现问题。",
    }
    return release_state.render_appendix_markdown(payload)


class AssetContractTests(unittest.TestCase):
    """跨文件一致性：契约是唯一事实源，运输层表述被静态锁定。"""

    def test_contract_is_declared_single_source(self) -> None:
        self.assertIn("唯一状态与严重度事实源", CONTRACT)
        self.assertIn("release-review-contract.md", SKILL_MD)
        self.assertIn("唯一事实源", STANDARD)

    def test_status_and_severity_enums_live_in_contract(self) -> None:
        for token in (*release_state.STATUSES, *release_state.SEVERITIES, *release_state.CATEGORIES):
            self.assertIn(token, CONTRACT)
        self.assertIn(release_state.CONTRACT_VERSION, CONTRACT)

    def test_gap_routing_stated_in_contract_and_standard(self) -> None:
        self.assertIn("资料固有缺口分流", CONTRACT)
        self.assertIn("资料固有缺口分流", STANDARD)
        self.assertIn("Medium", CONTRACT)

    def test_writer_transport_is_draft_file_not_session_fulltext(self) -> None:
        body = AGENTS["release-writer"]
        self.assertIn("release-doc.draft.md", body)
        self.assertIn("不返回初稿全文", body)
        self.assertIn("release-doc.draft.md", SKILL_MD)

    def test_refiner_transport_is_targeted_patches(self) -> None:
        body = AGENTS["release-refiner"]
        self.assertIn("定向补丁", body)
        self.assertIn("不返回修订版全文", body)
        self.assertIn("30%", body)
        self.assertIn("release_state.py diff", SKILL_MD)

    def test_reviewer_outputs_contract_json_without_writing_files(self) -> None:
        body = AGENTS["release-reviewer"]
        self.assertIn("appendix_markdown", body)
        self.assertIn("release-review-v2", body)
        self.assertIn("tools: Read, Grep, Glob, Bash", body)
        frontmatter = body.split("---")[1]
        self.assertNotIn("Write", frontmatter)
        self.assertNotIn("Edit", frontmatter)

    def test_prompt_does_not_restate_role_body(self) -> None:
        sys.path.insert(0, str(SKILL_DIR.parents[1] / "scripts"))
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "validate_skill_layout", SKILL_DIR.parents[1] / "scripts" / "validate-skill-layout.py"
        )
        layout = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(layout)
        problems: list[str] = []
        layout.check_role_body_dupes(SKILL_DIR, problems)
        self.assertEqual(problems, [], joined(problems))

    def test_external_archive_and_ledger_are_named_in_skill(self) -> None:
        for token in ("release-reviews.md", "release-review.json", "render-appendix", "render-report",
                      "close --batch", "RQ", "<PYTHON>"):
            self.assertIn(token, SKILL_MD)

    def test_p1_delta_and_gate_stop_are_stated_consistently(self) -> None:
        for token in ("--mode delta", "RECHECK", "verified_findings", "COMPREHENSIVE"):
            self.assertIn(token, SKILL_MD)
            self.assertIn(token, CONTRACT)
        self.assertIn("verified_findings", AGENTS["release-reviewer"])
        self.assertIn("Medium`/`Low` 不触发开轮", CONTRACT)
        self.assertIn("仅由 `Blocker`/`High` 触发", STANDARD)

    def test_p1_mechanical_patch_whitelist_is_stated_consistently(self) -> None:
        for token in ("STRUCT002", "STATUS003", "mechanical_patches", "patch"):
            self.assertIn(token, SKILL_MD)
            self.assertIn(token, CONTRACT)
        self.assertIn("机械补丁白名单", CONTRACT)
        self.assertIn("机械缺陷与语义缺陷的分流", STANDARD)
        self.assertEqual(release_state.MECHANICAL_PATCH_CODES, ("STRUCT002", "STATUS003"))

    def test_p1_scale_tailoring_is_stated_consistently(self) -> None:
        for token in ("--scale", "探针", "强制 L", "只折叠不删除", "chaos"):
            self.assertIn(token, SKILL_MD)
        self.assertIn("scale", CONTRACT)
        self.assertIn("发布规模", TEMPLATE)
        self.assertIn("S 级", STANDARD)
        self.assertIn("剪枝", AGENTS["release-critic"])

    def test_review_fix_gates_are_stated_consistently(self) -> None:
        # 审查修复后的新门禁必须在契约/SKILL 有对应表述，prompt 不得要求手工算指纹。
        for token in ("REVIEW005", "STRUCT004", "先闭环后记录", "复查消费"):
            self.assertIn(token, CONTRACT)
        self.assertIn("rpd_digest", AGENTS["release-critic"])
        self.assertIn("不得手工计算 sha256", (SKILL_DIR / "references" / "release-reviewer-prompt.md").read_text(encoding="utf-8"))

    def test_round2_fix_gates_are_stated_consistently(self) -> None:
        # 第二轮复审修复的门禁与模板形态必须被静态锁定，防止再次漂移。
        for token in ("REVIEW006", "KEY003", "HIST002", "闭环前", "first-wins"):
            self.assertIn(token, CONTRACT)
        for token in ("REVIEW006", "KEY003", "HIST002", "闭环前"):
            self.assertIn(token, SKILL_MD)
        self.assertIn("````markdown", TEMPLATE)  # 快照围栏必须能嵌套内层 ``` 围栏

    def test_template_has_appendix_section(self) -> None:
        self.assertIn("# 独立评审意见", TEMPLATE)
        self.assertIn("release-reviews.md", TEMPLATE)

    def test_shipped_assets_pass_drift_gate(self) -> None:
        targets = [
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "references" / "check-standard.md",
            SKILL_DIR / "references" / "release-review-contract.md",
        ]
        errors = check_template_refs(targets, SKILL_DIR / "references" / "release-doc-template.md")
        self.assertEqual(errors, [], joined(errors))

    def test_drift_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.md"
            target.write_text("只有关键信息和发布步骤两章。\n", encoding="utf-8")
            errors = check_template_refs([target], SKILL_DIR / "references" / "release-doc-template.md")
        self.assertGreater(len(errors), 10, joined(errors))

    def test_contract_enum_drift_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "release-review-contract.md"
            target.write_text(CONTRACT.replace("OBSERVATION", "METRICS"), encoding="utf-8")
            errors = check_template_refs([target], SKILL_DIR / "references" / "release-doc-template.md")
        self.assertTrue(any("OBSERVATION" in item for item in errors), joined(errors))


class ValidatorTests(unittest.TestCase):
    def test_valid_rpd_passes(self) -> None:
        text = RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block()
        self.assertEqual(errors_of(text), [], joined(errors_of(text)))

    def test_missing_section_is_rejected(self) -> None:
        text = RPD_BODY.replace("## 回滚方案", "## 回滚安排") + "\n# 独立评审意见\n\n" + appendix_block()
        errors = errors_of(text)
        self.assertTrue(any("回滚方案" in item for item in errors), joined(errors))

    def test_section_order_is_enforced(self) -> None:
        text = RPD_BODY.replace("## 发布策略", "## 发布步骤").replace("## 发布步骤\n\n| 序号", "## 发布策略\n\n| 序号", 1)
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("顺序" in item for item in errors), joined(errors))

    def test_infra_subsections_cannot_be_deleted(self) -> None:
        text = RPD_BODY.replace("### XXL-JOB\n\n不涉及：本次无任务变更。\n", "")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("XXL-JOB" in item for item in errors), joined(errors))

    def test_example_placeholder_is_rejected_but_gap_marker_is_allowed(self) -> None:
        text = RPD_BODY.replace("| order-service |", "| 【服务 1】 |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("【服务 1】" in item for item in errors), joined(errors))
        # 【填写】是合法缺口表达，不得被判残留
        self.assertFalse(any("【填写】" in item and "占位符未替换" in item for item in errors), joined(errors))

    def test_pass_without_evidence_is_rejected(self) -> None:
        text = RPD_BODY.replace("| HTTP 接口向后兼容 | 通过 | 新增字段可选，见 TDD 4.2 |",
                                "| HTTP 接口向后兼容 | 通过 | 【填写】 |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("证据为空" in item for item in errors), joined(errors))

    def test_not_applicable_without_reason_is_rejected(self) -> None:
        text = RPD_BODY.replace("| Dubbo 接口向后兼容 | 不涉及 | 本次无 Dubbo 接口变更 |",
                                "| Dubbo 接口向后兼容 | 不涉及 |  |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("不适用原因为空" in item for item in errors), joined(errors))

    def test_unfinalized_status_rejected_in_final_but_allowed_in_draft(self) -> None:
        text = RPD_BODY.replace("| HTTP 接口向后兼容 | 通过 | 新增字段可选，见 TDD 4.2 |",
                                "| HTTP 接口向后兼容 | 【通过/不涉及】 | 【填写】 |")
        body = text + "\n# 独立评审意见\n\n" + appendix_block()
        self.assertTrue(any("状态未定稿" in item for item in errors_of(body)), joined(errors_of(body)))
        draft_errors = errors_of(text, draft=True)
        self.assertFalse(any("状态未定稿" in item for item in draft_errors), joined(draft_errors))

    def test_illegal_status_value_is_rejected(self) -> None:
        text = RPD_BODY.replace("| HTTP 接口向后兼容 | 通过 |", "| HTTP 接口向后兼容 | 正常 |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("状态值非法" in item for item in errors), joined(errors))

    def test_premature_success_is_rejected(self) -> None:
        text = RPD_BODY.replace("| 发布结果 | 【填写】 |", "| 发布结果 | 成功 |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("发布结果" in item for item in errors), joined(errors))

    def test_success_requires_passed_final_verification(self) -> None:
        text = (RPD_BODY.replace("| 发布结果 | 【填写】 |", "| 发布结果 | 成功 |")
                .replace("| 最终验证结论 | 【填写】 |", "| 最终验证结论 | 未通过 |")
                .replace("| 证据 | 【填写】 |", "| 证据 | 发布记录 R-1 |")
                .replace("| 发布负责人确认 | 【填写】 |", "| 发布负责人确认 | 张三 |"))
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("最终验证结论不是“通过”" in item for item in errors), joined(errors))

    def test_empty_change_history_is_rejected(self) -> None:
        text = RPD_BODY.replace("无历史版本", "（待补充）")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("变更历史" in item for item in errors), joined(errors))

    def test_missing_review_block_is_rejected(self) -> None:
        errors = errors_of(RPD_BODY + "\n# 独立评审意见\n")
        self.assertTrue(any("没有任何评审块" in item for item in errors), joined(errors))

    def test_review_block_status_enum_is_enforced(self) -> None:
        block = appendix_block().replace("| 状态 | APPROVE |", "| 状态 | 通过 |")
        errors = errors_of(RPD_BODY + "\n# 独立评审意见\n\n" + block)
        self.assertTrue(any("评审状态非法" in item for item in errors), joined(errors))

    def test_missing_risk_field_is_rejected(self) -> None:
        text = RPD_BODY.replace("| 是否涉及资金 | 否 |\n", "")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[KEY001]" in item and "资金" in item for item in errors), joined(errors))

    def test_unresolved_risk_field_rejected_in_final_but_allowed_in_draft(self) -> None:
        text = RPD_BODY.replace("| 是否涉及资金 | 否 |", "| 是否涉及资金 | 是 / 否 |")
        body = text + "\n# 独立评审意见\n\n" + appendix_block()
        self.assertTrue(any("[KEY002]" in item for item in errors_of(body)), joined(errors_of(body)))
        self.assertFalse(any("[KEY002]" in item for item in errors_of(text, draft=True)))

    def test_missing_or_unselected_scale_is_rejected(self) -> None:
        text = RPD_BODY.replace("| 发布规模 | S |", "| 发布规模 | S / M / L |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[SCALE001]" in item for item in errors), joined(errors))
        text2 = RPD_BODY.replace("| 发布规模 | S |\n", "")
        errors2 = errors_of(text2 + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[SCALE001]" in item for item in errors2), joined(errors2))

    def test_collapsed_subsection_requires_reason(self) -> None:
        text = RPD_BODY.replace("不涉及：本次无缓存变更。", "不涉及")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[COLLAPSE001]" in item for item in errors), joined(errors))

    def test_collapsed_subsection_with_reason_passes(self) -> None:
        text = RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block()
        self.assertEqual(errors_of(text), [], joined(errors_of(text)))

    def test_rendered_appendix_passes_structure_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rpd = Path(tmp) / "release-doc.md"
            rpd.write_text(RPD_BODY, encoding="utf-8")
            payload = {
                "review_id": "RPD-R1",
                "contract_version": release_state.CONTRACT_VERSION,
                "review_round": 1,
                "perspective": "COMPREHENSIVE",
                "rpd_digest": release_state.content_digest(rpd),
                "status": "APPROVE",
                "scope": "release_plan_only",
                "summary": "覆盖结构、证据与回滚维度，未发现问题",
                "findings": [],
                "appendix_markdown": "**结论**：未发现阻塞问题。",
            }
            block = release_state.render_appendix_markdown(payload)
            rpd.write_text(RPD_BODY + "\n# 独立评审意见\n\n" + block, encoding="utf-8")
            self.assertEqual(RpdValidator(rpd).validate(), [])

    def test_duplicate_required_section_is_rejected(self) -> None:
        # 审查发现#8：重复强制章节不得静默，first-match 校验会互相遮蔽。
        text = RPD_BODY + "\n## 关键信息\n\n| 项目 | 内容 |\n|------|------|\n| 是否涉及资金 | 是 |\n"
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[STRUCT004]" in item for item in errors), joined(errors))

    def test_fake_appendix_heading_in_body_is_rejected(self) -> None:
        # 审查发现#1：无空格变形与正文中部的正规回执标题都必须报 REVIEW005。
        for variant in ("#独立评审意见", "# 独立评审意见"):
            text = RPD_BODY.replace("## 发布策略", variant + "\n\n## 发布策略")
            errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
            self.assertTrue(any("[REVIEW005]" in item for item in errors), joined(errors))

    def test_fence_pairing_follows_marker_char_and_length(self) -> None:
        # 审查发现#4：混用标记不得闭合围栏；长围栏内的短标记是内容。
        lines = ["```sql", "~~~", "DROP TABLE t;", "```", "正常正文"]
        self.assertEqual(fence_flags(lines), [False, True, True, False, False])
        lines = ["````markdown", "```sql", "SELECT 1;", "```", "````", "正常正文"]
        self.assertEqual(fence_flags(lines), [False, True, True, True, False, False])
        lines = ["    ```", "无标准回滚路径", "    ```"]
        self.assertEqual(fence_flags(lines), [False, False, False])  # 4 空格缩进是缩进代码块，不是围栏

    def test_backtick_info_string_does_not_open_fence(self) -> None:
        # 复审 N6：CommonMark 中反引号围栏信息串含反引号时不是围栏，不得藏检查表。
        lines = ["```text `x`", "| 检查项 | 状态 |", "正常正文"]
        self.assertEqual(fence_flags(lines), [False, False, False])

    def test_non_receipt_chapter_after_appendix_is_rejected(self) -> None:
        # 复审 N1：回执节必须是最后一章，其后追加非强制章节报 REVIEW006。
        text = RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block() + "\n## 补充说明\n\n发布步骤改为跳过灰度直接全量。\n"
        errors = errors_of(text)
        self.assertTrue(any("[REVIEW006]" in item for item in errors), joined(errors))

    def test_duplicate_risk_row_is_rejected(self) -> None:
        # 复审 N3：重复风险行报 KEY003，不得用后行覆盖前行降级风险。
        text = RPD_BODY.replace("| 是否涉及资金 | 否 |", "| 是否涉及资金 | 是 |\n| 是否涉及资金（复核） | 否 |")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[KEY003]" in item for item in errors), joined(errors))

    def test_history_snapshot_fence_requires_four_markers(self) -> None:
        # 复审 N7：模板同款 ```markdown 快照无法嵌套内层围栏，HIST002 拒收。
        text = RPD_BODY.replace("无历史版本", "```markdown\n旧版正文\n```")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertTrue(any("[HIST002]" in item for item in errors), joined(errors))
        text2 = RPD_BODY.replace("无历史版本", "````markdown\n```sql\nSELECT 1;\n```\n````")
        self.assertFalse(any("[HIST002]" in item for item in errors_of(text2 + "\n# 独立评审意见\n\n" + appendix_block())))

    def test_legitimate_subsection_is_not_duplicate(self) -> None:
        # 复审 N8：合法子节（### 变更业务补充）不得被误报 STRUCT004。
        text = RPD_BODY.replace("## 变更服务", "### 变更业务补充\n\n补充说明。\n\n## 变更服务")
        errors = errors_of(text + "\n# 独立评审意见\n\n" + appendix_block())
        self.assertFalse(any("[STRUCT004]" in item for item in errors), joined(errors))


class ReviewStateCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "release-review.json"
        self.rpd = self.root / "release-doc.md"
        self.rpd.write_text(RPD_BODY, encoding="utf-8")

    def cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["release_state.py", *argv]
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = release_state.main()
        except SystemExit as exc:
            code = int(exc.code or 0)
        finally:
            sys.argv = old_argv
        return int(code or 0), buffer.getvalue()

    def init(self, scale: str = "S") -> None:
        source = self.root / "SRC-001.md"
        source.write_text("# TDD 正文\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", scale, "--input", f"SRC-001={source}")
        self.assertEqual(code, 0, out)

    def chaos(self, scenarios: list[str] | None = None, scale: str | None = None,
              findings: list[dict] | None = None, digest: str | None = None) -> tuple[int, str]:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        payload = {
            "scale": scale or state["scale"],
            "rpd_digest": digest if digest is not None else release_state.content_digest(self.rpd),
            "scenarios_checked": scenarios if scenarios is not None else list(release_state.required_chaos_modes(state)),
            "findings": findings if findings is not None else [],
        }
        path = self.root / "chaos.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.cli("chaos", "--state", str(self.state), "--report", str(path))

    def report(self, review_id: str, status: str, findings: list[dict], **extra) -> Path:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        payload = {
            "review_id": review_id,
            "contract_version": release_state.CONTRACT_VERSION,
            "review_round": state["current_round"],
            "perspective": "COMPREHENSIVE",
            "scale": state["scale"],
            "rpd_digest": release_state.content_digest(self.rpd),
            "status": status,
            "scope": "release_plan_only",
            "summary": "覆盖结构、证据与回滚维度",
            "findings": findings,
            "appendix_markdown": f"{review_id} 的评审意见正文",
        }
        payload.update(extra)
        path = self.root / f"{review_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def finding(identifier: str = "RPR-001", severity: str = "High", **extra) -> dict:
        item = {
            "id": identifier,
            "severity": severity,
            "category": "ROLLBACK",
            "location": "回滚方案/回滚服务",
            "evidence": "TDD 未提供回滚产物来源",
            "impact": "回滚不可执行",
            "remediation": "补充上一版产物来源",
            "blocking": severity in release_state.BLOCKING_SEVERITIES,
            "verification": "复核回滚章节",
        }
        item.update(extra)
        return item

    def append_appendix(self, review_id: str, status: str = "APPROVE", perspective: str = "COMPREHENSIVE") -> None:
        payload = {
            "review_id": review_id,
            "review_round": int(review_id.split("R")[-1]),
            "perspective": perspective,
            "status": status,
            "rpd_digest": release_state.content_digest(self.rpd),
            "summary": "覆盖结构、证据与回滚维度",
            "findings": [],
            "appendix_markdown": "意见正文",
        }
        block = release_state.render_appendix_markdown(payload)
        text = self.rpd.read_text(encoding="utf-8")
        # 与脚本同一判定：只认围栏外的正规回执标题；快照里的旧标题不算已有回执区。
        lines = text.splitlines()
        headings = parse_headings(lines, fence_flags(lines))
        if not any(item.level == 1 and item.text.startswith("独立评审意见") for item in headings):
            text += "\n# 独立评审意见\n"
        self.rpd.write_text(text + "\n" + block, encoding="utf-8")

    def archive(self, review_id: str, status: str = "APPROVE", perspective: str = "COMPREHENSIVE") -> None:
        payload = {
            "review_id": review_id,
            "review_round": int(review_id.split("R")[-1]),
            "perspective": perspective,
            "status": status,
            "rpd_digest": release_state.content_digest(self.rpd),
            "summary": "覆盖结构、证据与回滚维度",
            "findings": [],
            "appendix_markdown": "意见正文",
        }
        section = release_state.render_report_markdown(payload)
        path = self.root / "release-reviews.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else "# 发布方案评审归档\n"
        path.write_text(existing + "\n" + section, encoding="utf-8")

    def edit_body(self, note: str) -> None:
        text = self.rpd.read_text(encoding="utf-8")
        self.rpd.write_text(text.replace("订单导出新增渠道字段", f"订单导出新增渠道字段（{note}）"), encoding="utf-8")

    # --- init / digest ------------------------------------------------------

    def test_init_requires_registered_inputs(self) -> None:
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd), "--mode", "new", "--scale", "S")
        self.assertEqual(code, 1)
        self.assertIn("必须至少登记一份输入资料", out)

    def test_init_rejects_missing_input_file(self) -> None:
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={self.root / 'nope.md'}")
        self.assertEqual(code, 1)
        self.assertIn("输入资料不存在", out)

    def test_init_rejects_existing_state_without_force(self) -> None:
        self.init()
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={self.root / 'SRC-001.md'}")
        self.assertEqual(code, 1)
        self.assertIn("--force", out)

    def test_digest_excludes_appendix(self) -> None:
        before = release_state.content_digest(self.rpd)
        self.append_appendix("RPD-R1")
        self.assertEqual(release_state.content_digest(self.rpd), before)
        code, out = self.cli("digest", "--rpd", str(self.rpd))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), before)

    # --- record -------------------------------------------------------------

    def test_record_rejects_wrong_round_and_review_id(self) -> None:
        self.init()
        path = self.report("RPD-R2", "APPROVE", [])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("与当前轮次不符", out)

    def test_record_rejects_digest_mismatch(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [], rpd_digest="b" * 64)
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("摘要不一致", out)

    def test_record_rejects_second_report_in_same_round(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("已记录评审报告", out)

    def test_record_rejects_blocker_marked_non_blocking(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding(severity="Blocker", blocking=False)])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("不得标记 blocking=false", out)

    def test_record_rejects_approve_with_blocking_finding(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [self.finding()])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("不得给出 APPROVE", out)

    def test_record_rejects_request_changes_without_findings(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("至少一条", out)

    def test_medium_only_request_changes_is_rejected_after_p1(self) -> None:
        # P1-1 门停放宽：Medium/Low 不再触发评审轮，只能走非阻塞跟进。
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding(severity="Medium", blocking=False)])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("Medium/Low 走非阻塞跟进", out)

    def test_low_only_request_changes_is_rejected(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding(severity="Low", blocking=False)])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("blocking finding", out)

    def test_high_cannot_be_disguised_as_non_blocking(self) -> None:
        # 对抗用例：把 High 标成 blocking=false 试图绕过门停，必须被拒。
        self.init()
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS", [self.finding(severity="High", blocking=False)])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("不得标记 blocking=false", out)

    def test_record_rejects_blocked_without_reason(self) -> None:
        self.init()
        path = self.report("RPD-R1", "BLOCKED", [])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("blocked_reason", out)

    def test_record_rejects_wrong_scope(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [], scope="code_review")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("scope", out)

    def test_record_rejects_malformed_open_question(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS",
                           [self.finding(severity="Medium", blocking=False)],
                           open_questions=[{"id": "Q-1", "question": "阈值", "impact": "放行", "owner": "需人工指定"}])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("RQ-001", out)

    # --- close --------------------------------------------------------------

    def test_fixed_requires_advance_first(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")
        self.assertEqual(code, 1)
        self.assertIn("必须先 advance", out)
        self.edit_body("补充回滚产物")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")
        self.assertEqual(code, 0, out)

    def test_deferred_requires_registered_rq(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS", [self.finding(severity="Medium", blocking=False)])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "deferred", "--evidence", "阈值待发布负责人确认", "--q-ref", "RQ-999")
        self.assertEqual(code, 1)
        self.assertIn("未出现在 RPD", out)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "deferred", "--evidence", "阈值待发布负责人确认", "--q-ref", "RQ-001")
        self.assertEqual(code, 0, out)

    def test_deferred_accepts_rq_registered_in_ledger(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS",
                           [self.finding(severity="Medium", blocking=False)],
                           open_questions=[{"id": "RQ-002", "question": "观察阈值", "impact": "放行判断", "owner": "需人工指定"}])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "deferred", "--evidence", "阈值待发布负责人确认", "--q-ref", "RQ-002")
        self.assertEqual(code, 0, out)

    def test_blocker_cannot_be_rejected(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding(severity="Blocker")])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "rejected", "--evidence", "认为该问题不影响交付，理由充分且已复核两次")
        self.assertEqual(code, 1)
        self.assertIn("不得判定为不采纳", out)

    def test_batch_close_is_atomic_and_equivalent_to_single(self) -> None:
        self.init()
        findings = [self.finding(identifier="RPR-001", severity="Medium", blocking=False),
                    self.finding(identifier="RPR-002", severity="Low", blocking=False)]
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS", findings)
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        closures = [
            {"finding": "RPR-001", "resolution": "deferred", "evidence": "阈值待发布负责人确认", "q_ref": "RQ-001"},
            {"finding": "RPR-002", "resolution": "rejected", "evidence": "该措辞已被回滚步骤第 2 条替代，不构成方案缺陷"},
        ]
        batch = self.root / "closures.json"
        batch.write_text(json.dumps(closures, ensure_ascii=False), encoding="utf-8")
        code, out = self.cli("close", "--state", str(self.state), "--batch", str(batch))
        self.assertEqual(code, 0, out)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["findings"]["RPR-001"]["resolution"], "deferred")
        self.assertEqual(state["findings"]["RPR-002"]["resolution"], "rejected")

    def test_batch_failure_leaves_no_partial_closure(self) -> None:
        self.init()
        findings = [self.finding(identifier="RPR-001", severity="Medium", blocking=False),
                    self.finding(identifier="RPR-002", severity="Low", blocking=False)]
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS", findings)
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        closures = [
            {"finding": "RPR-001", "resolution": "deferred", "evidence": "阈值待发布负责人确认", "q_ref": "RQ-001"},
            {"finding": "RPR-404", "resolution": "rejected", "evidence": "不存在的编号，应当整批失败不落盘"},
        ]
        batch = self.root / "closures.json"
        batch.write_text(json.dumps(closures, ensure_ascii=False), encoding="utf-8")
        code, _ = self.cli("close", "--state", str(self.state), "--batch", str(batch))
        self.assertEqual(code, 1)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["findings"]["RPR-001"]["status"], "OPEN")

    def test_batch_and_single_params_cannot_mix(self) -> None:
        self.init()
        batch = self.root / "closures.json"
        batch.write_text("[]", encoding="utf-8")
        code, out = self.cli("close", "--state", str(self.state), "--batch", str(batch),
                             "--finding", "RPR-001", "--resolution", "fixed", "--evidence", "混用参数")
        self.assertEqual(code, 1)
        self.assertIn("不得同时使用", out)

    # --- advance / verify ---------------------------------------------------

    def test_advance_requires_edited_body_when_blocking_open(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("稿件正文与送审时完全一致", out)
        self.edit_body("补充回滚产物")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)

    def test_advance_rejects_history_regression(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        text = self.rpd.read_text(encoding="utf-8").replace("无历史版本\n", "")
        self.rpd.write_text(text, encoding="utf-8")
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("历史不得删除", out)

    def test_round_limit_forces_blocked_delivery(self) -> None:
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        for round_number in range(1, 6):
            path = self.report(f"RPD-R{round_number}", "REQUEST_CHANGES",
                               [self.finding(identifier=f"RPR-{round_number:03d}")])
            code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
            self.assertEqual(code, 0, out)
            self.append_appendix(f"RPD-R{round_number}", "REQUEST_CHANGES")
            self.archive(f"RPD-R{round_number}", "REQUEST_CHANGES")
            if round_number < 5:
                self.edit_body(f"第 {round_number} 轮修订")
            if round_number == 4:
                self.assertEqual(self.chaos()[0], 0)  # 进入末轮前重跑 chaos（BLOCKED 后不得再记录）
            code, out = self.cli("advance", "--state", str(self.state))
            self.assertEqual(code, 0 if round_number < 5 else 1, out)
        self.assertIn("已达 5 轮上限", out)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("仍有未闭环阻塞项", out)
        code, out = self.cli("verify", "--state", str(self.state), "--allow-blocked")
        self.assertEqual(code, 0, out)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "BLOCKED")

    def test_clean_two_round_delivery_is_valid(self) -> None:
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        first = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(first))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.archive("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        self.assertEqual(self.chaos()[0], 0)  # 改稿后旧覆盖度失效，重跑 chaos
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("RPD-R2")
        self.archive("RPD-R2")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)

    def test_verify_rejects_unarchived_review_id(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("release-reviews.md", out)

    def test_verify_rejects_edit_after_last_review(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.edit_body("未经复核的正文改动")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("又被修改", out)

    def test_verify_rejects_fixed_without_later_round(self) -> None:
        self.init()
        first = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(first))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.archive("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")[0], 0)
        # 第 2 轮没有派发 Reviewer 就尝试交付：fixed 缺少复核轮次必须被拒。
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)


class DiffAndRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_small_patch_does_not_allow_full_rewrite(self) -> None:
        before = self.root / "before.md"
        after = self.root / "after.md"
        before.write_text(RPD_BODY, encoding="utf-8")
        after.write_text(RPD_BODY.replace("间隔 30s", "间隔 60s"), encoding="utf-8")
        buffer = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["release_state.py", "diff", "--before", str(before), "--after", str(after)]
        try:
            with contextlib.redirect_stdout(buffer):
                code = release_state.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0)
        result = json.loads(buffer.getvalue())
        self.assertFalse(result["full_rewrite_allowed"])
        self.assertLess(result["change_ratio"], 0.30)

    def test_large_rewrite_is_detected(self) -> None:
        before = self.root / "before.md"
        after = self.root / "after.md"
        before.write_text(RPD_BODY, encoding="utf-8")
        after.write_text("# 后端服务发布方案\n\n" + "\n".join(f"重写第 {i} 行" for i in range(200)), encoding="utf-8")
        buffer = io.StringIO()
        old_argv = sys.argv
        sys.argv = ["release_state.py", "diff", "--before", str(before), "--after", str(after)]
        try:
            with contextlib.redirect_stdout(buffer):
                code = release_state.main()
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["full_rewrite_allowed"])

    def test_render_appendix_is_compact_and_points_to_archive(self) -> None:
        payload = {
            "review_id": "RPD-R1", "review_round": 1, "perspective": "COMPREHENSIVE", "status": "APPROVE",
            "rpd_digest": "c" * 64, "summary": "未发现问题", "findings": [],
            "appendix_markdown": "评审意见正文",
        }
        out = release_state.render_appendix_markdown(payload)
        self.assertIn("release-reviews.md", out)
        self.assertNotIn("评审意见正文", out)

    def test_render_report_rejects_top_level_headings(self) -> None:
        payload = {
            "review_id": "RPD-R1", "review_round": 1, "perspective": "COMPREHENSIVE", "status": "APPROVE",
            "rpd_digest": "c" * 64, "summary": "未发现问题", "findings": [],
            "appendix_markdown": "# 伪一级标题\n正文",
        }
        with self.assertRaises(release_state.GateError):
            release_state.render_report_markdown(payload)

    def test_render_cells_escape_pipes_and_newlines(self) -> None:
        # 对抗用例：summary/location 含竖线与换线时不得撕裂回执表或注入伪表格行。
        payload = {
            "review_id": "RPD-R1", "review_round": 1, "perspective": "COMPREHENSIVE", "status": "REQUEST_CHANGES",
            "rpd_digest": "c" * 64, "summary": "摘要|注入\n| 伪行 | 内容 |",
            "findings": [{
                "id": "RPR-001", "severity": "High", "category": "ROLLBACK",
                "location": "回滚方案|注入\n### 伪评审块", "evidence": "e", "impact": "i",
                "remediation": "r", "verification": "v", "blocking": True,
            }],
            "appendix_markdown": "意见正文",
        }
        for renderer in (release_state.render_appendix_markdown, release_state.render_report_markdown):
            out = renderer(payload)
            for line in out.splitlines():
                if line.startswith("|"):
                    self.assertNotIn("\n", line)
            # 换行被压平后不得在行首形成伪评审块/伪标题（parse_review_blocks 以行首 ### 为界）。
            self.assertFalse(any(line.startswith("### 伪评审块") for line in out.splitlines()), out)
            self.assertIn("\\|", out)


class DeltaRecheckGateTests(unittest.TestCase):
    """P1-1 放宽的对抗用例：每项放宽都有“变形输入 + 试图绕过”的拒收路径。"""

    # 复用台账 CLI 助手，但不继承其用例（避免重复执行）。
    setUp = ReviewStateCliTests.setUp
    cli = ReviewStateCliTests.cli
    init = ReviewStateCliTests.init
    chaos = ReviewStateCliTests.chaos
    report = ReviewStateCliTests.report
    finding = staticmethod(ReviewStateCliTests.finding)
    append_appendix = ReviewStateCliTests.append_appendix
    archive = ReviewStateCliTests.archive
    edit_body = ReviewStateCliTests.edit_body

    def start_blocking_round(self) -> None:
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.archive("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")

    def test_delta_recheck_flow_then_full_delivery(self) -> None:
        self.start_blocking_round()
        code, out = self.cli("advance", "--state", str(self.state), "--mode", "delta")
        self.assertEqual(code, 0, out)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-001"])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(second))
        self.assertEqual(code, 0, out)
        self.append_appendix("RPD-R2", "APPROVE", "RECHECK")
        self.archive("RPD-R2", "APPROVE", "RECHECK")
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")
        self.assertEqual(code, 0, out)
        # delta 不得作为交付轮：必须补一轮全量。
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("最后一轮是增量复核", out)
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 0, out)
        third = self.report("RPD-R3", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(third))[0], 0)
        self.append_appendix("RPD-R3")
        self.archive("RPD-R3")
        self.assertEqual(self.chaos()[0], 0)  # 交付稿已变，覆盖度必须针对当前稿
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)

    def test_recheck_approve_cannot_be_empty_confirmation(self) -> None:
        # 对抗用例①：台账仍有未闭环项时 RECHECK 空确认 APPROVE 必须被拒。
        self.start_blocking_round()
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=[])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(second))
        self.assertEqual(code, 1)
        self.assertIn("不得空确认", out)

    def test_recheck_verified_id_must_be_open_in_ledger(self) -> None:
        self.start_blocking_round()
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-404"])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(second))
        self.assertEqual(code, 1)
        self.assertIn("必须是台账中未闭环的 Finding", out)

    def test_verified_findings_rejected_in_full_report(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [], verified_findings=["RPR-001"])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("只允许出现在 RECHECK", out)

    def test_delta_round_rejects_comprehensive_perspective(self) -> None:
        self.start_blocking_round()
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="COMPREHENSIVE")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(second))
        self.assertEqual(code, 1)
        self.assertIn("只接受视角 RECHECK", out)

    def test_full_round_rejects_recheck_perspective(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [], perspective="RECHECK")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("只接受视角 COMPREHENSIVE", out)

    def test_missing_or_malformed_perspective_is_rejected_not_skipped(self) -> None:
        # 对抗用例④：封套变形（perspective 缺失/非法）必须解析失败即报错，不得静默跳过。
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["perspective"]
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("缺少契约字段", out)
        path = self.report("RPD-R1", "APPROVE", [], perspective="FULL")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("perspective", out)

    def test_consecutive_delta_is_rejected(self) -> None:
        # 对抗用例②：delta 之后不得再接 delta。
        self.start_blocking_round()
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-001"])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")[0], 0)
        self.edit_body("第二轮修订")
        code, out = self.cli("advance", "--state", str(self.state), "--mode", "delta")
        self.assertEqual(code, 1)
        self.assertIn("不得连续进行", out)

    def test_delta_cannot_open_the_final_round(self) -> None:
        # 对抗用例②：第 4 轮后不得用 delta 开启第 5 轮（最终轮必须全量）。
        self.init()
        for round_number in range(1, 5):
            path = self.report(f"RPD-R{round_number}", "REQUEST_CHANGES",
                               [self.finding(identifier=f"RPR-{round_number:03d}")])
            self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
            self.append_appendix(f"RPD-R{round_number}", "REQUEST_CHANGES")
            self.edit_body(f"第 {round_number} 轮修订")
            if round_number < 4:
                self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        code, out = self.cli("advance", "--state", str(self.state), "--mode", "delta")
        self.assertEqual(code, 1)
        self.assertIn("最终轮必须是全量评审", out)

    def test_delta_requires_open_findings(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("advance", "--state", str(self.state), "--mode", "delta")
        self.assertEqual(code, 1)
        self.assertIn("没有未闭环 Finding", out)

    def test_fixed_close_requires_recheck_verification(self) -> None:
        # 对抗用例③：存在 RECHECK 报告时，未被其 verified_findings 确认的 Finding 不得 close fixed。
        self.init()
        findings = [self.finding(identifier="RPR-001"), self.finding(identifier="RPR-002")]
        path = self.report("RPD-R1", "REQUEST_CHANGES", findings)
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-001"])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-002",
                             "--resolution", "fixed", "--evidence", "自称已修复但未经 RECHECK 确认")
        self.assertEqual(code, 1)
        self.assertIn("verified_findings", out)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")
        self.assertEqual(code, 0, out)

    def test_recheck_new_blocking_forces_full_next_round(self) -> None:
        self.start_blocking_round()
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "REQUEST_CHANGES", [self.finding(identifier="RPR-002")],
                             perspective="RECHECK", verified_findings=["RPR-001"])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.edit_body("修复新阻塞问题")
        code, out = self.cli("advance", "--state", str(self.state), "--mode", "delta")
        self.assertEqual(code, 1)
        self.assertIn("不得连续进行", out)
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)

    def test_registered_gap_followups_deliver_in_one_round(self) -> None:
        # P1-1 的核心收益：已登记固有缺口记 Medium 非阻塞，单轮即可带跟进交付，不再空转。
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS",
                           [self.finding(severity="Medium", blocking=False, category="EVIDENCE",
                                         location="关键信息/发布负责人", evidence="资料未提供负责人，已登记【填写】",
                                         remediation="发布前人工指派")])
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 0, out)
        self.append_appendix("RPD-R1", "APPROVE_WITH_FOLLOW_UPS")
        self.archive("RPD-R1", "APPROVE_WITH_FOLLOW_UPS")
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "deferred", "--evidence", "负责人待人工指派，已登记 RQ-001", "--q-ref", "RQ-001")
        self.assertEqual(code, 0, out)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)


class MechanicalPatchTests(unittest.TestCase):
    """P1-2 放宽的对抗用例：白名单由错误码界定，语义夹带与未留痕修改都有拒收路径。"""

    setUp = ReviewStateCliTests.setUp
    cli = ReviewStateCliTests.cli
    init = ReviewStateCliTests.init
    chaos = ReviewStateCliTests.chaos
    report = ReviewStateCliTests.report
    finding = staticmethod(ReviewStateCliTests.finding)
    append_appendix = ReviewStateCliTests.append_appendix
    archive = ReviewStateCliTests.archive
    edit_body = ReviewStateCliTests.edit_body

    def flagged_status_line(self, cell: str = "通 过") -> int:
        text = self.rpd.read_text(encoding="utf-8").replace("| HTTP 接口向后兼容 | 通过 |", f"| HTTP 接口向后兼容 | {cell} |")
        self.rpd.write_text(text, encoding="utf-8")
        errors = RpdValidator(self.rpd).validate()
        match = next(re.search(r"\bL(\d+)\b", item) for item in errors if item.startswith("[STATUS003]"))
        return int(match.group(1))

    def test_errors_carry_stable_codes(self) -> None:
        self.rpd.write_text(RPD_BODY.replace("| HTTP 接口向后兼容 | 通过 |", "| HTTP 接口向后兼容 | 通 过 |"), encoding="utf-8")
        errors = RpdValidator(self.rpd).validate()
        self.assertTrue(any(item.startswith("[STATUS003]") for item in errors), joined(errors))
        self.assertTrue(all(re.match(r"^\[[A-Z]+\d+\]", item) for item in errors), joined(errors))

    def test_status_normalization_fixes_records_and_clears_error(self) -> None:
        line = self.flagged_status_line()
        self.append_appendix("RPD-R1")
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                             "--line", str(line), "--old", "通 过", "--new", "通过")
        self.assertEqual(code, 0, out)
        self.assertEqual(RpdValidator(self.rpd).validate(), [])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        entry = state["mechanical_patches"][0]
        self.assertEqual(entry["code"], "STATUS003")
        self.assertEqual(entry["line"], line)
        self.assertEqual((entry["old"], entry["new"]), ("通 过", "通过"))
        self.assertNotEqual(entry["before_digest"], entry["after_digest"])
        code, out = self.cli("show", "--state", str(self.state))
        self.assertIn("STATUS003", json.loads(out)["mechanical_patches"][0])

    def test_semantic_smuggling_through_normalization_is_rejected(self) -> None:
        # 对抗用例①：借归一化之名改换结论（通 过 → 不涉及）必须被拒。
        line = self.flagged_status_line()
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                             "--line", str(line), "--old", "通 过", "--new", "不涉及")
        self.assertEqual(code, 1)
        self.assertIn("只允许空白归一化", out)
        self.assertIn("通 过", self.rpd.read_text(encoding="utf-8"))

    def test_conclusion_swap_is_rejected(self) -> None:
        # 对抗用例①：`正常` → `通过` 不是空白变形，是结论改换。
        line = self.flagged_status_line(cell="正常")
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                             "--line", str(line), "--old", "正常", "--new", "通过")
        self.assertEqual(code, 1)
        self.assertIn("语义修改", out)

    def test_unflagged_line_is_rejected(self) -> None:
        # 对抗用例③：门禁未点名的行不得借通道修改。
        self.flagged_status_line()
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                             "--line", "1", "--old", "x", "--new", "通过")
        self.assertEqual(code, 1)
        self.assertIn("未被门禁报出", out)

    def test_non_whitelist_code_is_rejected(self) -> None:
        # 对抗用例③：白名单外错误码（PLACE001 等）不得直修。
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "PLACE001")
        self.assertNotEqual(code, 0)
        self.assertIn("invalid choice", out)

    def test_ambiguous_anchor_is_rejected(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| HTTP 接口向后兼容 | 通过 | 新增字段可选，见 TDD 4.2 |",
            "| HTTP 接口向后兼容 | 通 过 | 通 过 |",
        )
        self.rpd.write_text(text, encoding="utf-8")
        errors = RpdValidator(self.rpd).validate()
        line = int(re.search(r"\bL(\d+)\b", next(item for item in errors if item.startswith("[STATUS003]"))).group(1))
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                             "--line", str(line), "--old", "通 过", "--new", "通过")
        self.assertEqual(code, 1)
        self.assertIn("不唯一", out)

    def test_identical_patch_is_rejected(self) -> None:
        line = self.flagged_status_line()
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                             "--line", str(line), "--old", "通过", "--new", "通过")
        self.assertEqual(code, 1)

    def test_status_patch_requires_line_old_new(self) -> None:
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STATUS003")
        self.assertEqual(code, 1)
        self.assertIn("--line/--old/--new", out)

    def scramble_sections(self) -> None:
        text = self.rpd.read_text(encoding="utf-8")
        i, j = text.index("## 发布策略"), text.index("## 发布步骤")
        k = text.index("## 发布后验证")
        self.rpd.write_text(text[:i] + text[j:k] + text[i:j] + text[k:], encoding="utf-8")

    def test_struct002_reorder_preserves_content_and_fixes_order(self) -> None:
        self.rpd.write_text(RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block(), encoding="utf-8")
        self.scramble_sections()
        before_lines = sorted(self.rpd.read_text(encoding="utf-8").splitlines())
        self.assertTrue(any("[STRUCT002]" in item for item in RpdValidator(self.rpd).validate()))
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STRUCT002")
        self.assertEqual(code, 0, out)
        errors = RpdValidator(self.rpd).validate()
        self.assertFalse(any("[STRUCT002]" in item for item in errors), joined(errors))
        self.assertEqual(sorted(self.rpd.read_text(encoding="utf-8").splitlines()), before_lines)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["mechanical_patches"][0]["code"], "STRUCT002")

    def test_struct002_without_flag_is_rejected(self) -> None:
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STRUCT002")
        self.assertEqual(code, 1)
        self.assertIn("未报出 STRUCT002", out)

    def test_struct002_with_missing_section_is_rejected(self) -> None:
        # 对抗用例：缺章时重排会丢失内容，必须拒收而不是静默搬运。
        self.rpd.write_text(RPD_BODY.replace("## 回滚方案", "## 回滚安排"), encoding="utf-8")
        self.scramble_sections()
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STRUCT002")
        self.assertEqual(code, 1)
        self.assertIn("缺少章节", out)

    def test_patch_after_review_forces_a_full_round(self) -> None:
        # 交付轮仍全量：评审后的机械补丁使指纹过期，必须补一轮全量评审才能交付。
        self.rpd.write_text(RPD_BODY.replace("| HTTP 接口向后兼容 | 通过 |", "| HTTP 接口向后兼容 | 通 过 |"), encoding="utf-8")
        errors = RpdValidator(self.rpd).validate()
        line = int(re.search(r"\bL(\d+)\b", next(item for item in errors if item.startswith("[STATUS003]"))).group(1))
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.assertEqual(self.cli("patch", "--state", str(self.state), "--code", "STATUS003",
                                  "--line", str(line), "--old", "通 过", "--new", "通过")[0], 0)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("又被修改", out)
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        self.assertEqual(self.chaos()[0], 0)  # 补丁改稿后重跑 chaos
        second = self.report("RPD-R2", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("RPD-R2")
        self.archive("RPD-R2")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)

    def test_unrecorded_hand_edit_fails_verify(self) -> None:
        # 对抗用例②：绕过 patch 通道手改正文（未留痕）必须被交付门禁指纹绑定拦下。
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.edit_body("绕过台账的手改")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("又被修改", out)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state.get("mechanical_patches", []), [])


class ScaleTailoringTests(unittest.TestCase):
    """P1-3 放宽的对抗用例：高风险强制 L、只折叠不删除、Chaos 剪枝不得自行缩小、规模联动。"""

    setUp = ReviewStateCliTests.setUp
    cli = ReviewStateCliTests.cli
    init = ReviewStateCliTests.init
    chaos = ReviewStateCliTests.chaos
    report = ReviewStateCliTests.report
    finding = staticmethod(ReviewStateCliTests.finding)
    append_appendix = ReviewStateCliTests.append_appendix
    archive = ReviewStateCliTests.archive
    edit_body = ReviewStateCliTests.edit_body

    def set_risk(self, field: str, value: str) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(f"| {field} | 否 |", f"| {field} | {value} |")
        self.rpd.write_text(text, encoding="utf-8")

    def set_scale_row(self, value: str) -> None:
        text = self.rpd.read_text(encoding="utf-8")
        text = re.sub(r"\| 发布规模 \| [SML] \|", f"| 发布规模 | {value} |", text)
        self.rpd.write_text(text, encoding="utf-8")

    def test_high_risk_field_forces_l(self) -> None:
        # 对抗用例①：资金=是 时声明 S/M 必须被拒，强制 L。
        self.set_risk("是否涉及资金", "是")
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        for scale in ("S", "M"):
            code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                                 "--mode", "new", "--scale", scale, "--input", f"SRC-001={source}")
            self.assertEqual(code, 1)
            self.assertIn("强制 L 级", out)
        self.set_scale_row("L")
        self.init(scale="L")

    def test_destructive_sql_forces_l(self) -> None:
        # 对抗用例①：MySQL 围栏里的 DROP TABLE 是高风险信号，不得判 S。
        text = self.rpd.read_text(encoding="utf-8").replace(
            "不涉及：本次无数据库变更（TDD 第 5 章明确）。",
            "```sql\nDROP TABLE legacy_orders;\n```",
        )
        self.rpd.write_text(text, encoding="utf-8")
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={source}")
        self.assertEqual(code, 1)
        self.assertIn("高风险 SQL", out)

    def test_template_guidance_and_negation_are_not_signals(self) -> None:
        # 模板提示语（引语）与否定句不得误判为高风险信号。
        text = self.rpd.read_text(encoding="utf-8").replace(
            "## 变更业务",
            "> 如果涉及用户资金、不可逆数据迁移、删除表/字段，应改用完整版发布模板。\n\n## 变更业务",
        ).replace("不涉及：本次无缓存变更。", "本次不涉及不可逆数据迁移，无需删除表。")
        self.rpd.write_text(text, encoding="utf-8")
        self.init(scale="S")

    def test_unresolved_risk_field_cannot_be_s(self) -> None:
        self.set_risk("是否涉及账务", "【填写】")
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={source}")
        self.assertEqual(code, 1)
        self.assertIn("未决风险字段", out)

    def test_multi_service_cannot_be_s(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| 1 | order-service | 【填写】 | export | 【链接】 | 新增渠道字段 | 商家导出 |",
            "| 1 | order-service | 【填写】 | export | 【链接】 | 新增渠道字段 | 商家导出 |\n"
            "| 2 | pay-service | 【填写】 | core | 【链接】 | 适配 | 支付 |",
        )
        self.rpd.write_text(text, encoding="utf-8")
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={source}")
        self.assertEqual(code, 1)
        self.assertIn("服务数=2", out)

    def test_upward_declaration_is_allowed(self) -> None:
        self.set_scale_row("M")
        self.init(scale="M")  # 探针 S，声明 M 是安全方向
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["scale"], "M")
        self.assertEqual(state["scale_basis"]["probe_scale"], "S")
        self.assertEqual(release_state.required_chaos_modes(state), release_state.CHAOS_MODES)

    def test_rpd_scale_row_must_match_ledger(self) -> None:
        # 对抗用例④：RPD 声明规模与台账不一致，init 与 verify 都拒。
        self.rpd.write_text(self.rpd.read_text(encoding="utf-8").replace("| 发布规模 | S |", "| 发布规模 | M |"), encoding="utf-8")
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={source}")
        self.assertEqual(code, 1)
        self.assertIn("不一致", out)
        self.init(scale="M")
        self.rpd.write_text(self.rpd.read_text(encoding="utf-8").replace("| 发布规模 | M |", "| 发布规模 | S |"), encoding="utf-8")
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("发布规模", out)

    def test_post_init_risk_escalation_fails_verify(self) -> None:
        # 对抗用例①变体：init 后正文新增高风险信号，verify 重探针必须拦下。
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.set_risk("是否涉及交易链路", "是")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("台账规模 S 不足", out)

    def test_chaos_cannot_shrink_required_modes(self) -> None:
        # 对抗用例③：S 级剪枝后仍必查的模式被 Critic 省掉，必须拒收。
        self.init()
        required = release_state.required_chaos_modes(json.loads(self.state.read_text(encoding="utf-8")))
        self.assertNotIn("消息积压", required)  # 无 MQ → 剪枝合法
        code, out = self.chaos(scenarios=[m for m in required if m != "回滚失败"])
        self.assertEqual(code, 1)
        self.assertIn("回滚失败", out)
        self.assertEqual(self.chaos()[0], 0)

    def test_chaos_scale_must_match_ledger(self) -> None:
        self.init()
        code, out = self.chaos(scale="L")
        self.assertEqual(code, 1)
        self.assertIn("不一致", out)

    def test_m_level_requires_full_matrix(self) -> None:
        self.set_scale_row("M")
        self.init(scale="M")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(release_state.required_chaos_modes(state), release_state.CHAOS_MODES)
        code, out = self.chaos(scenarios=list(release_state.ALWAYS_CHAOS_MODES))
        self.assertEqual(code, 1)
        self.assertIn("消息积压", out)

    def test_s_with_applicable_mq_requires_backlog_mode(self) -> None:
        # 剪枝范围由探针推导：MQ 适用时 S 级也必须查消息积压。
        text = self.rpd.read_text(encoding="utf-8").replace(
            "### MQ / Kafka\n\n不涉及：本次无消息变更。",
            "### MQ / Kafka\n\n新增 Consumer：topic=order-export，group=export-cg。",
        )
        self.rpd.write_text(text, encoding="utf-8")
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={source}")
        self.assertEqual(code, 1)  # 基础设施适用 → 至少 M
        self.assertIn("适用基础设施", out)
        self.set_scale_row("M")
        self.init(scale="M")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertIn("消息积压", release_state.required_chaos_modes(state))

    def test_verify_requires_chaos_record(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("Chaos 故障审查未入台账", out)

    def test_report_scale_must_match_ledger(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [], scale="L")
        code, out = self.cli("record", "--state", str(self.state), "--report", str(path))
        self.assertEqual(code, 1)
        self.assertIn("scale", out)

    def test_s_delivery_end_to_end(self) -> None:
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)


class ReviewFixAdversarialTests(unittest.TestCase):
    """代码审查发现 #1-#9 的对抗用例：每条已证实的绕过路径都有变形输入 + 拒收断言。"""

    setUp = ReviewStateCliTests.setUp
    cli = ReviewStateCliTests.cli
    init = ReviewStateCliTests.init
    chaos = ReviewStateCliTests.chaos
    report = ReviewStateCliTests.report
    finding = staticmethod(ReviewStateCliTests.finding)
    append_appendix = ReviewStateCliTests.append_appendix
    archive = ReviewStateCliTests.archive
    edit_body = ReviewStateCliTests.edit_body

    def init_expect_reject(self, scale: str) -> str:
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", scale, "--input", f"SRC-001={source}")
        self.assertEqual(code, 1, out)
        return out

    def replace_mysql(self, body: str) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace("不涉及：本次无数据库变更（TDD 第 5 章明确）。", body)
        self.rpd.write_text(text, encoding="utf-8")

    def snapshot_history(self, snapshot: str) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace("无历史版本", "上一版有效 RPD 快照：\n\n" + snapshot)
        self.rpd.write_text(text, encoding="utf-8")

    # --- #1 指纹盲区 ---------------------------------------------------------

    def test_fake_appendix_heading_creates_no_digest_blind_spot(self) -> None:
        self.rpd.write_text(RPD_BODY.replace("## 发布策略", "#独立评审意见\n\n## 发布策略"), encoding="utf-8")
        self.assertTrue(any("[REVIEW005]" in item for item in RpdValidator(self.rpd).validate()))
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        # 评审后修改伪标题之后的正文：指纹必须变化，verify 必须拒收。
        text = self.rpd.read_text(encoding="utf-8").replace("间隔 30s", "间隔 5s，直接全量发布")
        self.rpd.write_text(text, encoding="utf-8")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("又被修改", out)

    def test_history_snapshot_appendix_heading_is_not_stripped(self) -> None:
        # change 模式历史围栏里的上一版回执标题是天然盲区候选：不得参与剔除。
        self.snapshot_history("````markdown\n" + RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block() + "\n````")
        before = release_state.content_digest(self.rpd)
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.assertEqual(release_state.content_digest(self.rpd), before)  # 只追加回执不改指纹
        text = self.rpd.read_text(encoding="utf-8").replace("间隔 30s", "间隔 5s")
        self.rpd.write_text(text, encoding="utf-8")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("又被修改", out)

    # --- #2 fixed 时序绕过 ----------------------------------------------------

    def test_close_before_record_in_delta_round_is_rejected(self) -> None:
        self.init()
        findings = [self.finding(identifier="RPR-001"), self.finding(identifier="RPR-002")]
        path = self.report("RPD-R1", "REQUEST_CHANGES", findings)
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-002",
                             "--resolution", "fixed", "--evidence", "抢在 RECHECK 记录前自称修复")
        self.assertEqual(code, 1)
        self.assertIn("不得先闭环后记录", out)

    def test_pre_close_all_then_empty_recheck_cannot_deliver(self) -> None:
        self.init()
        findings = [self.finding(identifier="RPR-001"), self.finding(identifier="RPR-002")]
        path = self.report("RPD-R1", "REQUEST_CHANGES", findings)
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.archive("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-001", "RPR-002"])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("RPD-R2", "APPROVE", "RECHECK")
        self.archive("RPD-R2", "APPROVE", "RECHECK")
        for identifier in ("RPR-001", "RPR-002"):
            self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", identifier,
                                      "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")[0], 0)
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        third = self.report("RPD-R3", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(third))[0], 0)
        self.append_appendix("RPD-R3")
        self.archive("RPD-R3")
        self.assertEqual(self.chaos()[0], 0)
        self.assertEqual(self.cli("verify", "--state", str(self.state))[0], 0)
        # 对照绕过：篡改台账模拟“先闭环后记录”得逞的残留状态，verify 消费复查必须拦下。
        self.setUp()
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", findings)
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.archive("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("补充回滚产物来源")
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        second = self.report("RPD-R2", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-001"])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("RPD-R2", "APPROVE", "RECHECK")
        self.archive("RPD-R2", "APPROVE", "RECHECK")
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 补充回滚产物来源")[0], 0)
        ledger = json.loads(self.state.read_text(encoding="utf-8"))
        ledger["findings"]["RPR-002"].update(status="CLOSED", resolution="fixed", closed_in_round=2,
                                             closure_evidence="自称已修复但未经 RECHECK 确认")
        self.state.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
        self.edit_body("第三轮前的定向修订")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        third = self.report("RPD-R3", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(third))[0], 0)
        self.append_appendix("RPD-R3")
        self.archive("RPD-R3")
        self.assertEqual(self.chaos()[0], 0)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("未被任何后续增量复核的 verified_findings 确认", out)

    # --- #3 探针信号绕过 ------------------------------------------------------

    def test_negation_word_cannot_swallow_same_line_signals(self) -> None:
        self.replace_mysql("不涉及停机，但本次包含不可逆数据迁移，无标准回滚路径。")
        out = self.init_expect_reject("S")
        self.assertIn("强制 L 级", out)
        self.assertIn("不可逆数据迁移", out)

    def test_sql_outside_fence_forces_l(self) -> None:
        self.replace_mysql("本次执行 DROP TABLE legacy_orders 清理数据。")
        out = self.init_expect_reject("M")
        self.assertIn("高风险 SQL", out)

    def test_annotated_yes_risk_field_forces_l(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace("| 是否涉及资金 | 否 |", "| 是否涉及资金 | 是（涉及订单金额） |")
        self.rpd.write_text(text, encoding="utf-8")
        out = self.init_expect_reject("M")
        self.assertIn("强制 L 级", out)

    # --- #4 围栏状态机绕过 ----------------------------------------------------

    def test_mixed_fence_marker_cannot_hide_sql(self) -> None:
        self.replace_mysql("```sql\n~~~\nDROP TABLE legacy_orders;\n```")
        out = self.init_expect_reject("M")
        self.assertIn("高风险 SQL", out)

    def test_indented_fake_fence_cannot_hide_text_signal(self) -> None:
        self.replace_mysql("    ```\n无标准回滚路径\n    ```")
        out = self.init_expect_reject("M")
        self.assertIn("无标准回滚路径", out)

    # --- #5 历史快照截断 ------------------------------------------------------

    def test_history_snapshot_deletion_is_detected(self) -> None:
        self.snapshot_history("````markdown\n" + RPD_BODY + "\n````")
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        text = self.rpd.read_text(encoding="utf-8").replace("| HTTP 接口向后兼容 | 通过 | 新增字段可选，见 TDD 4.2 |\n", "")
        self.rpd.write_text(text, encoding="utf-8")
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("历史不得删除", out)

    # --- #6 Chaos 指纹与闭环 --------------------------------------------------

    def test_chaos_digest_mismatch_is_rejected(self) -> None:
        self.init()
        code, out = self.chaos(digest="b" * 64)
        self.assertEqual(code, 1)
        self.assertIn("摘要不一致", out)

    def test_chaos_coverage_expires_after_body_edit(self) -> None:
        self.init()
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.edit_body("评审后的正文改动")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        second = self.report("RPD-R2", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("RPD-R2")
        self.archive("RPD-R2")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("重跑 chaos", out)
        self.assertEqual(self.chaos()[0], 0)
        self.assertEqual(self.cli("verify", "--state", str(self.state))[0], 0)

    def test_chaos_blocking_finding_gates_delivery(self) -> None:
        self.init()
        code, out = self.chaos(findings=[self.finding(identifier="RPR-090")])
        self.assertEqual(code, 0, out)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("RPR-090", out)
        # Chaos 先于评审：修复后闭环，再由全量评审覆盖新稿，单轮即可交付。
        self.edit_body("按 Chaos 风险补全预案")
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-090",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 补全回滚预案")[0], 0)
        self.assertEqual(self.chaos()[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        self.assertEqual(self.cli("verify", "--state", str(self.state))[0], 0)
        # 对照：未改稿就闭环（复核轮看到的仍是提出时稿件）必须被 verify 拒收。
        self.setUp()
        self.init()
        self.assertEqual(self.chaos(findings=[self.finding(identifier="RPR-090")])[0], 0)
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-090",
                                  "--resolution", "fixed", "--evidence", "未改稿就自称补全预案")[0], 0)
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("同一份稿件", out)

    def test_chaos_finding_id_cannot_collide_with_ledger(self) -> None:
        self.init()
        path = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding()])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.chaos(findings=[self.finding(identifier="RPR-001")])
        self.assertEqual(code, 1)
        self.assertIn("编号冲突", out)

    # --- #7 RQ 前缀碰撞 -------------------------------------------------------

    def test_deferred_rq_prefix_collision_is_rejected(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS", [self.finding(severity="Medium", blocking=False)])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        for ref in ("RQ-0", "RQ-00"):
            code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                 "--resolution", "deferred", "--evidence", "阈值待发布负责人确认", "--q-ref", ref)
            self.assertEqual(code, 1)
            self.assertIn("未出现在 RPD", out)
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                  "--resolution", "deferred", "--evidence", "阈值待发布负责人确认", "--q-ref", "RQ-001")[0], 0)

    def test_deferred_rq_in_history_snapshot_does_not_count(self) -> None:
        self.snapshot_history("````markdown\nRQ-777 旧版待确认项\n````")
        self.init()
        path = self.report("RPD-R1", "APPROVE_WITH_FOLLOW_UPS", [self.finding(severity="Medium", blocking=False)])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        code, out = self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                             "--resolution", "deferred", "--evidence", "旧版编号不得冒充登记", "--q-ref", "RQ-777")
        self.assertEqual(code, 1)
        self.assertIn("未出现在 RPD", out)

    # --- #8 重复章节与重排 ----------------------------------------------------

    def test_struct002_reorder_rejects_duplicate_sections(self) -> None:
        text = RPD_BODY + "\n## 关键信息\n\n| 项目 | 内容 |\n|------|------|\n| 是否涉及资金 | 是 |\n"
        i, j = text.index("## 发布策略"), text.index("## 发布步骤")
        k = text.index("## 发布后验证")
        text = text[:i] + text[j:k] + text[i:j] + text[k:]
        self.rpd.write_text(text + "\n# 独立评审意见\n\n" + appendix_block(), encoding="utf-8")
        errors = RpdValidator(self.rpd).validate()
        self.assertTrue(any("[STRUCT004]" in item for item in errors), joined(errors))
        self.assertTrue(any("[STRUCT002]" in item for item in errors), joined(errors))
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STRUCT002")
        self.assertEqual(code, 1)
        self.assertIn("重复出现", out)

    # --- #9 CRLF 守恒 ---------------------------------------------------------

    def test_struct002_preserves_crlf_line_endings(self) -> None:
        text = (RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block()).replace("\n", "\r\n")
        i, j = text.index("## 发布策略"), text.index("## 发布步骤")
        k = text.index("## 发布后验证")
        self.rpd.write_text(text[:i] + text[j:k] + text[i:j] + text[k:], encoding="utf-8", newline="")
        self.assertTrue(any("[STRUCT002]" in item for item in RpdValidator(self.rpd).validate()))
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STRUCT002")
        self.assertEqual(code, 0, out)
        patched = self.rpd.read_bytes().decode("utf-8")
        self.assertNotIn("\n", patched.replace("\r\n", ""))  # 不得有裸 LF，行尾逐字节保留
        self.assertEqual(RpdValidator(self.rpd).validate(), [])


class Round2FixAdversarialTests(unittest.TestCase):
    """第二轮复审 N1-N9 的对抗用例：每条残留绕过与修复引入的新问题都有变形输入 + 拒收/放行断言。"""

    setUp = ReviewStateCliTests.setUp
    cli = ReviewStateCliTests.cli
    init = ReviewStateCliTests.init
    chaos = ReviewStateCliTests.chaos
    report = ReviewStateCliTests.report
    finding = staticmethod(ReviewStateCliTests.finding)
    append_appendix = ReviewStateCliTests.append_appendix
    archive = ReviewStateCliTests.archive
    edit_body = ReviewStateCliTests.edit_body

    def init_expect_reject(self, scale: str) -> str:
        source = self.root / "SRC-001.md"
        source.write_text("# TDD\n", encoding="utf-8")
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", scale, "--input", f"SRC-001={source}")
        self.assertEqual(code, 1, out)
        return out

    def replace_mysql(self, body: str) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace("不涉及：本次无数据库变更（TDD 第 5 章明确）。", body)
        self.rpd.write_text(text, encoding="utf-8")

    # --- N1 回执节后追加章节 --------------------------------------------------

    def test_appended_chapter_after_appendix_stays_in_digest_and_fails_verify(self) -> None:
        self.init()
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        self.append_appendix("RPD-R1")
        self.archive("RPD-R1")
        text = self.rpd.read_text(encoding="utf-8") + "\n## 补充说明\n\n发布步骤改为跳过灰度直接全量。\n"
        self.rpd.write_text(text, encoding="utf-8")
        self.assertTrue(any("[REVIEW006]" in item for item in RpdValidator(self.rpd).validate()))
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("回执节之后出现非回执章节", out)
        # 追加章节仍计入指纹：改其正文同样被“评审后又改稿”拦下。
        before = release_state.content_digest(self.rpd)
        self.rpd.write_text(self.rpd.read_text(encoding="utf-8").replace("跳过灰度直接全量", "改为分批全量"), encoding="utf-8")
        self.assertNotEqual(release_state.content_digest(self.rpd), before)

    # --- N2 闭环后 delta 轮不追溯（合法流程不得死锁） --------------------------

    def test_full_round_close_then_later_delta_does_not_deadlock(self) -> None:
        self.init()
        first = self.report("RPD-R1", "REQUEST_CHANGES", [self.finding(identifier="RPR-001")])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(first))[0], 0)
        self.append_appendix("RPD-R1", "REQUEST_CHANGES")
        self.archive("RPD-R1", "REQUEST_CHANGES")
        self.edit_body("修复第一个问题")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        second = self.report("RPD-R2", "REQUEST_CHANGES", [self.finding(identifier="RPR-002")])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(second))[0], 0)
        self.append_appendix("RPD-R2", "REQUEST_CHANGES")
        self.archive("RPD-R2", "REQUEST_CHANGES")
        # full 轮合法闭环 RPR-001（当时无任何 RECHECK 报告）。
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-001",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 修复第一个问题")[0], 0)
        self.edit_body("修复第二个问题")
        self.assertEqual(self.cli("advance", "--state", str(self.state), "--mode", "delta")[0], 0)
        third = self.report("RPD-R3", "APPROVE", [], perspective="RECHECK", verified_findings=["RPR-002"])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(third))[0], 0)
        self.append_appendix("RPD-R3", "APPROVE", "RECHECK")
        self.archive("RPD-R3", "APPROVE", "RECHECK")
        self.assertEqual(self.cli("close", "--state", str(self.state), "--finding", "RPR-002",
                                  "--resolution", "fixed", "--evidence", "已按 SRC-001 修复第二个问题")[0], 0)
        self.edit_body("交付前定向修订")
        self.assertEqual(self.cli("advance", "--state", str(self.state))[0], 0)
        fourth = self.report("RPD-R4", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(fourth))[0], 0)
        self.append_appendix("RPD-R4")
        self.archive("RPD-R4")
        self.assertEqual(self.chaos()[0], 0)
        code, out = self.cli("verify", "--state", str(self.state))
        self.assertEqual(code, 0, out)  # 闭环后的 delta 轮不得追溯要求已 CLOSED 的 RPR-001

    # --- N3 探针 first-wins 与诱饵表 ------------------------------------------

    def test_duplicate_risk_row_cannot_downgrade_scale(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| 是否涉及资金 | 否 |", "| 是否涉及资金 | 是 |\n| 是否涉及资金（复核） | 否 |")
        self.rpd.write_text(text, encoding="utf-8")
        out = self.init_expect_reject("S")
        self.assertIn("强制 L 级", out)  # first-wins 读到“是”，后行诱饵不得覆盖
        self.assertTrue(any("[KEY003]" in item for item in RpdValidator(self.rpd).validate()))

    def test_duplicate_risk_row_with_false_first_is_fail_closed(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| 是否涉及资金 | 否 |", "| 是否涉及资金 | 否 |\n| 是否涉及资金（复核） | 是 |")
        self.rpd.write_text(text, encoding="utf-8")
        out = self.init_expect_reject("S")
        self.assertIn("未决风险字段", out)  # 重复行无法判真伪，按未决 fail-closed

    def test_decoy_table_with_other_header_is_ignored(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| 是否涉及资金 | 否 |", "| 是否涉及资金 | 是 |")
        text = text.replace("## 变更业务", "| 字段 | 答案 |\n|------|------|\n| 是否涉及资金 | 否 |\n\n## 变更业务")
        self.rpd.write_text(text, encoding="utf-8")
        out = self.init_expect_reject("S")
        self.assertIn("强制 L 级", out)  # 异表头诱饵表不参与判定

    def test_empty_service_decoy_table_cannot_downgrade(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| 发布顺序 | 服务名称 | 仓库地址 | 变更模块 | MR 地址 | 主要内容 | 影响范围 |",
            "| 服务名称 | 备注 |\n|----------|------|\n\n| 发布顺序 | 服务名称 | 仓库地址 | 变更模块 | MR 地址 | 主要内容 | 影响范围 |",
        ).replace(
            "| 1 | order-service | 【填写】 | export | 【链接】 | 新增渠道字段 | 商家导出 |",
            "| 1 | order-service | 【填写】 | export | 【链接】 | 新增渠道字段 | 商家导出 |\n"
            "| 2 | pay-service | 【填写】 | core | 【链接】 | 适配 | 支付 |",
        )
        self.rpd.write_text(text, encoding="utf-8")
        out = self.init_expect_reject("S")
        self.assertIn("服务数=2", out)  # 取最大行数，前置空诱饵表不得降级

    # --- N4 SQL 变形 ----------------------------------------------------------

    def test_sql_comment_and_multiline_variants_force_l(self) -> None:
        for body in ("```sql\nDROP/**/TABLE legacy_orders;\n```",
                     "```sql\nTRUNCATE legacy_orders;\n```",
                     "```sql\nDROP\nTABLE legacy_orders;\n```"):
            self.setUp()
            self.replace_mysql(body)
            out = self.init_expect_reject("M")
            self.assertIn("高风险 SQL", out)

    # --- N5 括号定语否定 ------------------------------------------------------

    def test_parenthesized_signal_cannot_be_swallowed_by_negation(self) -> None:
        self.replace_mysql("无需回滚（不可逆迁移）。")
        out = self.init_expect_reject("S")
        self.assertIn("不可逆迁移", out)

    # --- N6 信息串含反引号的伪围栏 --------------------------------------------

    def test_backtick_info_string_fence_cannot_hide_status_table(self) -> None:
        text = self.rpd.read_text(encoding="utf-8").replace(
            "| 检查项 | 状态 | 证据/说明 |\n|--------|------|-----------|\n| HTTP 接口向后兼容 | 通过 | 新增字段可选，见 TDD 4.2 |",
            "```text `x`\n| 检查项 | 状态 | 证据/说明 |\n|--------|------|-----------|\n| HTTP 接口向后兼容 | 通过 | 【填写】 |",
        )
        self.rpd.write_text(text, encoding="utf-8")
        errors = RpdValidator(self.rpd).validate()
        self.assertTrue(any("[STATUS004]" in item for item in errors), joined(errors))

    # --- N7 模板形态快照与历史计数 --------------------------------------------

    def test_template_form_snapshot_with_inner_fences_counts_correctly(self) -> None:
        snapshot = "````markdown\n" + RPD_BODY.replace(
            "不涉及：本次无数据库变更（TDD 第 5 章明确）。",
            "```sql\nDROP TABLE legacy_orders;\n```",
        ) + "\n````"
        self.rpd.write_text(self.rpd.read_text(encoding="utf-8").replace("无历史版本", snapshot), encoding="utf-8")
        self.rpd.write_text(self.rpd.read_text(encoding="utf-8").replace("| 发布规模 | S |", "| 发布规模 | L |"), encoding="utf-8")
        self.init(scale="L")  # 快照内 DROP TABLE 被围栏整体扫描命中，强制 L
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertGreater(state["history_lines"], len(RPD_BODY.splitlines()))
        path = self.report("RPD-R1", "APPROVE", [])
        self.assertEqual(self.cli("record", "--state", str(self.state), "--report", str(path))[0], 0)
        # 只删快照内的行（DROP TABLE 仅存在于快照）：历史计数必须下降并被拦下。
        text = self.rpd.read_text(encoding="utf-8").replace("DROP TABLE legacy_orders;\n", "")
        self.rpd.write_text(text, encoding="utf-8")
        code, out = self.cli("advance", "--state", str(self.state))
        self.assertEqual(code, 1)
        self.assertIn("历史不得删除", out)

    def test_unclosed_history_fence_is_fail_closed(self) -> None:
        self.rpd.write_text(self.rpd.read_text(encoding="utf-8").replace("无历史版本", "````markdown\n旧版正文"), encoding="utf-8")
        with self.assertRaises(release_state.GateError):
            release_state.history_line_count(self.rpd)
        code, out = self.cli("init", "--state", str(self.state), "--rpd", str(self.rpd),
                             "--mode", "new", "--scale", "S", "--input", f"SRC-001={self.root / 'SRC-001.md'}")
        self.assertEqual(code, 1)

    # --- N9 混合行尾逐字节守恒 ------------------------------------------------

    def test_struct002_preserves_mixed_line_endings(self) -> None:
        text = RPD_BODY + "\n# 独立评审意见\n\n" + appendix_block()
        lines = text.split("\n")
        mixed = "\r\n".join(lines[:20]) + "\n" + "\n".join(lines[20:])
        i, j = mixed.index("## 发布策略"), mixed.index("## 发布步骤")
        k = mixed.index("## 发布后验证")
        self.rpd.write_bytes((mixed[:i] + mixed[j:k] + mixed[i:j] + mixed[k:]).encode("utf-8"))
        self.assertTrue(any("[STRUCT002]" in item for item in RpdValidator(self.rpd).validate()))
        self.init()
        code, out = self.cli("patch", "--state", str(self.state), "--code", "STRUCT002")
        self.assertEqual(code, 0, out)
        patched = self.rpd.read_bytes().decode("utf-8")
        self.assertIn("\r\n", patched)  # CRLF 段保留
        self.assertIn("\n", patched.replace("\r\n", ""))  # LF 段也保留，不得全文统一化
        self.assertEqual(RpdValidator(self.rpd).validate(), [])


if __name__ == "__main__":
    unittest.main()