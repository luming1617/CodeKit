"""Contract tests that keep the technical-doc assets from drifting apart.

The design review this suite follows up on found four silent-drift classes: the
template had no home for chapters the Skill gates demand, the quality standard
claimed a different consumer, a private PlantUML host was hard-coded into the
workflow, and the role prompts existed twice (inline and installed). Each class is
now asserted here so it fails loudly instead of resurfacing as a runtime surprise.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_DIR / "scripts" / "tech_doc_controller.py"
SPEC = importlib.util.spec_from_file_location("tech_doc_controller_for_contract", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
TEMPLATE = (SKILL_DIR / "references" / "technical-doc-template.md").read_text(encoding="utf-8")
STANDARD = (SKILL_DIR / "references" / "check-standard.md").read_text(encoding="utf-8")
AGENTS = {path.stem: path.read_text(encoding="utf-8") for path in sorted((SKILL_DIR / "agents").glob("*.md"))}
ROLE_LINE = re.compile(r"^\|\s*\d\s*\|\s*`(tech-[a-z]+)`", re.MULTILINE)


class TemplateContractTests(unittest.TestCase):
    def test_every_gated_anchor_has_a_template_home(self) -> None:
        """A gate that demands a chapter needs somewhere in the template to put it."""
        for anchor in (*controller.REQUIRED_ANCHORS, *controller.UPDATE_ONLY_ANCHORS):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, TEMPLATE)
                self.assertIn(anchor, SKILL_MD)
                self.assertIn(anchor, STANDARD)

    def test_change_history_is_the_last_chapter(self) -> None:
        """SKILL.md appends history 'at the end of the document'; the template must agree."""
        history = TEMPLATE.index("# 变更历史")
        self.assertGreater(history, TEMPLATE.index("# 部署拓扑"))
        self.assertGreater(history, TEMPLATE.index("# 审查闭环记录"))
        self.assertLess(history, TEMPLATE.index("# 人工评审与结论"))

    def test_template_no_longer_embeds_previous_version_in_a_fence(self) -> None:
        self.assertNotIn("```markdown", TEMPLATE)
        self.assertIn("历史版本索引", TEMPLATE)
        self.assertIn("archive-history", TEMPLATE)

    def test_stack_adaptation_lives_in_the_overview_not_in_java_chapters(self) -> None:
        """The template dropped its Java-shaped chapters; the stack decision is a 方案概述 section."""
        self.assertIn("## 技术栈", TEMPLATE)
        self.assertNotIn("Java 详细设计", TEMPLATE)

    def test_mandatory_chapters_survive_the_S_tailoring_rule(self) -> None:
        tailoring = TEMPLATE.split("## 方案裁剪规则", 1)[1].split("##", 1)[0]
        for anchor in ("ADR", "风险", "工作量", "里程碑规划", "审查闭环"):
            self.assertIn(anchor, tailoring)


class LedgerContractTests(unittest.TestCase):
    """The 审查闭环记录/变更历史 ledgers are generated, so the template, anchors, and prose must agree."""

    def test_template_ships_the_managed_ledger_skeleton(self) -> None:
        blocks = controller._ledger_blocks(TEMPLATE.splitlines())
        self.assertEqual(set(blocks), set(controller.LEDGER_ORDER))
        for section in controller.LEDGER_ORDER:
            with self.subTest(section=section):
                start, end = blocks[section]
                body = TEMPLATE.splitlines()[start + 1 : end]
                self.assertTrue(body, section)

    def test_template_no_longer_offers_a_hand_written_finding_table(self) -> None:
        self.assertNotIn("Finding ID", TEMPLATE)
        self.assertIsNone(re.search(r"CRITIC-\d{3}", TEMPLATE), "template ships a copied finding row")

    def test_ledger_anchors_match_template_headings(self) -> None:
        lines = TEMPLATE.splitlines()
        for section in controller.LEDGER_ORDER:
            with self.subTest(section=section):
                self.assertIsNotNone(controller._anchor_index(lines, controller.LEDGER_ANCHOR[section]))

    def test_skill_and_agents_point_at_the_render_step(self) -> None:
        self.assertIn("render-ledger", SKILL_MD)
        self.assertIn("--prd-changes", SKILL_MD)
        for name in ("ledger-blocks-present", "ledger-matches-state"):
            self.assertIn(name, SKILL_MD)
        for role in ("tech-architect", "tech-refiner", "tech-reviewer"):
            with self.subTest(role=role):
                self.assertIn("render-ledger", AGENTS[role])


class DocIndexContractTests(unittest.TestCase):
    """Section injection only pays off if the prompts and the controller say the same thing."""

    def test_skill_points_at_the_index_commands_and_scope_flag(self) -> None:
        self.assertIn("读取预算与按节注入", SKILL_MD)
        self.assertIn("`index` / `locate`", SKILL_MD)
        self.assertIn("--scope", SKILL_MD)
        for name in ("NOT_LOCATED", "over_budget"):
            with self.subTest(name=name):
                self.assertIn(name, SKILL_MD)

    def test_prose_budget_numbers_match_the_controller(self) -> None:
        """A window or share changed in code but not in the prose silently mis-sizes every dispatch."""
        self.assertIn(f"默认 {controller.DEFAULT_CONTEXT_WINDOW}", SKILL_MD)
        self.assertIn(f"窗口 × {controller.BUDGET_SHARE}", SKILL_MD)

    def test_location_is_a_real_section_number_for_every_writer(self) -> None:
        for role in ("tech-critic", "tech-reviewer"):
            with self.subTest(role=role):
                self.assertIn("真实章节号", AGENTS[role])
        self.assertIn("locate", AGENTS["tech-architect"])

    def test_refiner_reads_by_section_but_keeps_the_full_read(self) -> None:
        """The wave ships a locator, not a wall: widening the read must stay allowed and declared."""
        self.assertIn("locate", AGENTS["tech-refiner"])
        self.assertIn("自行扩大", AGENTS["tech-refiner"])
        self.assertIn("本轮 `--scope` 之外的章节", AGENTS["tech-refiner"])
        self.assertIn("允许扩大读取", SKILL_MD)
        self.assertIn("不是硬门禁", SKILL_MD)
        # the fallback the code hands back and the fallback the prose promises must be one sentence
        phrase = "声明多读了哪些章节"
        self.assertIn(phrase, controller.LOCATE_FALLBACK)
        self.assertIn(phrase, SKILL_MD)
        self.assertIn(phrase, AGENTS["tech-refiner"])

    def test_full_review_is_not_narrowed_by_the_index(self) -> None:
        """A locator is a page-turn saver; only the severity gate may shrink a review round."""
        self.assertIn("不缩小终审范围", AGENTS["tech-reviewer"])
        self.assertIn("Critic 做全量审查", SKILL_MD)
        self.assertIn("不缩小审查范围", SKILL_MD)
        self.assertIn("增量终审轮例外", SKILL_MD)


class StandardContractTests(unittest.TestCase):
    def test_standard_serves_this_skill_not_code_review(self) -> None:
        self.assertIn("technical-doc", STANDARD)
        self.assertNotIn("用于 Code Review 阶段", STANDARD)
        self.assertNotIn("/codekit:code-review` 审查方案质量", STANDARD)

    def test_standard_drops_out_of_scope_and_unmeasurable_checks(self) -> None:
        self.assertNotIn("前端技术方案", STANDARD)
        self.assertNotIn("60%", STANDARD)
        self.assertNotIn("10%", STANDARD)

    def test_standard_defers_mechanical_checks_to_the_gate(self) -> None:
        self.assertIn("tech_doc_controller.py gate", STANDARD)


class SkillDocumentTests(unittest.TestCase):
    def test_no_hard_coded_environment(self) -> None:
        self.assertIsNone(re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", SKILL_MD), "SKILL.md embeds an IP literal")
        self.assertNotIn("172.19.100.170", SKILL_MD)
        self.assertNotIn("PlantUML", SKILL_MD)

    def test_context_pack_never_writes_back_into_repo_agent_config(self) -> None:
        self.assertNotIn("technical-doc-context", SKILL_MD)
        self.assertNotIn("将摘要安全地写入仓库上下文文件", SKILL_MD)
        self.assertIn("不得读取后当作既有约定使用", SKILL_MD)

    def test_role_prompts_have_a_single_source(self) -> None:
        self.assertNotIn("#### 角色提示词", SKILL_MD)
        self.assertIn("唯一规范源", SKILL_MD)

    def test_every_dispatched_role_exists_as_an_agent_file(self) -> None:
        roles = [match.group(1) for match in ROLE_LINE.finditer(SKILL_MD)]
        self.assertEqual(len(roles), 5, roles)
        for role in roles:
            self.assertIn(role, AGENTS)
            self.assertIn(f"name: {role}", AGENTS[role])

    def test_round_semantics_are_stated_once_and_match_the_controller(self) -> None:
        self.assertIn("1～5", SKILL_MD)
        self.assertNotIn("review_round < 5", SKILL_MD)
        self.assertNotIn("review_round ≤ 5", SKILL_MD)
        self.assertEqual(controller.MAX_REVIEW_ROUNDS, 5)
        self.assertIn("已执行的独立终审次数", SKILL_MD)

    def test_finding_id_conventions_match_the_controller(self) -> None:
        self.assertIn("CRITIC-<NNN>", SKILL_MD)
        self.assertIn("REVIEW-<round>-<NNN>", SKILL_MD)
        for sample in ("CRITIC-001", "CRITIC-012"):
            self.assertTrue(controller.FINDING_ID["CRITIC"].fullmatch(sample))
        for sample in ("REVIEW-1-001", "REVIEW-5-010"):
            self.assertTrue(controller.FINDING_ID["REVIEWER"].fullmatch(sample))
        self.assertIsNone(controller.FINDING_ID["CRITIC"].fullmatch("CRITIC-1"))
        self.assertIsNone(controller.FINDING_ID["REVIEWER"].fullmatch("CRITIC-001"))

    def test_skill_declares_its_tool_boundary(self) -> None:
        frontmatter = SKILL_MD.split("---")[1]
        self.assertIn("allowed-tools:", frontmatter)
        self.assertIn("argument-hint:", frontmatter)

    def test_shared_conventions_are_referenced_not_recopied(self) -> None:
        self.assertIn("_shared/prompts/subagent-mandate.md", SKILL_MD)
        self.assertIn("_shared/prompts/path-convention.md", SKILL_MD)
        self.assertNotIn("@agent-", SKILL_MD)
        self.assertNotIn("- **Codex：**", SKILL_MD)

    def test_every_referenced_resource_exists(self) -> None:
        for name in re.findall(r"references/([A-Za-z0-9_.-]+\.md)", SKILL_MD):
            self.assertTrue((SKILL_DIR / "references" / name).is_file(), name)
        for name in re.findall(r"agents/([a-z-]+\.md)", SKILL_MD):
            self.assertTrue((SKILL_DIR / "agents" / name).is_file(), name)


class AgentContractTests(unittest.TestCase):
    def test_read_only_roles_declare_no_write_tools(self) -> None:
        for role in ("tech-context", "tech-critic", "tech-reviewer"):
            tools = re.search(r"^tools: (.+)$", AGENTS[role], re.MULTILINE).group(1)
            with self.subTest(role=role):
                self.assertNotIn("Write", tools)
                self.assertNotIn("Edit", tools)

    def test_writable_roles_do_not_claim_read_only(self) -> None:
        for role in ("tech-architect", "tech-refiner"):
            with self.subTest(role=role):
                self.assertIn("Write", AGENTS[role].splitlines()[4])

    def test_context_loader_forbids_repo_config_writeback(self) -> None:
        self.assertIn("不得写入仓库根目录的 `AGENTS.md`", AGENTS["tech-context"])

    def test_reviewer_binds_its_verdict_to_a_snapshot(self) -> None:
        self.assertIn("record-review", AGENTS["tech-reviewer"])
        self.assertIn("字节快照", AGENTS["tech-reviewer"])

    def test_refiner_handles_reviewer_findings_too(self) -> None:
        self.assertIn("REVIEW-<round>", AGENTS["tech-refiner"])


class ReviewScopeContractTests(unittest.TestCase):
    """M4 changed what a review round is, so every asset must use the same two words."""

    def test_skill_names_the_scope_flags_and_checks(self) -> None:
        self.assertIn("严重度门停与增量终审", SKILL_MD)
        for name in ("--review-scope", "--review-stop-policy", "incremental-review-covers-changes"):
            with self.subTest(name=name):
                self.assertIn(name, SKILL_MD)
        self.assertIn("review_stop", SKILL_MD)
        for name in ("incremental-review-covers-changes",):
            self.assertIn(name, STANDARD)

    def test_streak_length_matches_between_code_and_prose(self) -> None:
        """'连续两轮' is the constant; a changed constant with old prose lies about the gate."""
        self.assertEqual(controller.SEVERITY_STOP_STREAK, 2)
        self.assertIn("连续两轮", SKILL_MD)
        self.assertIn("连续两轮", STANDARD)

    def test_rollback_switches_are_documented(self) -> None:
        self.assertEqual(controller.REVIEW_STOP_POLICIES, ("severity", "fixed"))
        self.assertIn("--review-stop-policy fixed", SKILL_MD)
        self.assertEqual(controller.SCALE_ENFORCEMENTS, ("enforce", "record-only"))
        self.assertIn("--scale-enforcement record-only", SKILL_MD)

    def test_reviewer_may_not_pass_off_an_incremental_verdict_as_full(self) -> None:
        self.assertIn("incremental", AGENTS["tech-reviewer"])
        self.assertIn("不得把增量结论", AGENTS["tech-reviewer"])
        self.assertIn("不得把增量结论", SKILL_MD)
        self.assertIn("不得把增量结论", STANDARD)
        for scope in controller.REVIEW_SCOPES:
            self.assertIn(f"`{scope}`", AGENTS["tech-reviewer"])

    def test_standard_defers_scope_and_scale_mechanics_to_the_gate(self) -> None:
        self.assertIn("审查范围与规模的判据", STANDARD)
        self.assertIn("scale-chapters", STANDARD)
        self.assertIn("范围由控制器给出", STANDARD)


class ScaleContractTests(unittest.TestCase):
    """M8's whole claim is one table; the probe, the gate, the template and the prompts."""

    def test_every_scale_chapter_has_a_template_home_and_an_architect(self) -> None:
        chapters = (
            *controller.ALWAYS_CHAPTERS,
            controller.DEPLOYMENT_CHAPTER,
            *set(controller.SIGNAL_CHAPTERS.values()),
        )
        for chapter in chapters:
            with self.subTest(chapter=chapter):
                self.assertIn(chapter, TEMPLATE)
                self.assertIn(chapter, AGENTS["tech-architect"])

    def test_signal_classes_are_named_once(self) -> None:
        for signal, chapter in controller.SIGNAL_CHAPTERS.items():
            with self.subTest(signal=signal):
                self.assertIn(chapter, AGENTS["tech-architect"])
        # every chapter the gate can demand comes from a class the probe actually greps
        self.assertTrue(set(controller.SIGNAL_CHAPTERS) <= set(controller.SCALE_SIGNALS))
        self.assertIn("数据 / 接口 / 消息 / 定时 / 配置", TEMPLATE)

    def test_skill_quotes_the_template_m_row(self) -> None:
        """The M row is the authority on the floor; quoting it keeps the gate from drifting."""
        self.assertIn("完整监控大盘与告警", TEMPLATE)
        self.assertIn("完整监控大盘与告警", SKILL_MD)
        self.assertIn("scale-chapters", SKILL_MD)

    def test_scale_grading_thresholds_match_the_prose(self) -> None:
        self.assertEqual(controller.SCALE_L_MIN_SIGNALS, 4)
        self.assertEqual(controller.SCALES, ("S", "M", "L"))
        self.assertIn("≥4 是 `L`", SKILL_MD)
        self.assertIn("≥4 类为 `L`", TEMPLATE)

    def test_greenfield_rules_the_context_loader_not_the_evidence_bar(self) -> None:
        self.assertIn("greenfield", AGENTS["tech-context"])
        self.assertIn("明确跳过", AGENTS["tech-context"])
        self.assertIn("跳过的是检索动作，不是那一节内容", AGENTS["tech-context"])
        self.assertIn("跳过的是检索动作，不是那一节内容", SKILL_MD)
        self.assertIn("未发现证据", AGENTS["tech-context"])

    def test_critic_severity_is_calibrated_by_scale(self) -> None:
        self.assertIn("scale", AGENTS["tech-critic"])
        self.assertIn("校准严重度", AGENTS["tech-critic"])
        self.assertIn("校准严重度", SKILL_MD)

    def test_the_two_tier_floor_is_stated_everywhere(self) -> None:
        """The 条件必填 escape survived the replay: 不涉及 is a decision there, padding in the scale-required chapters."""
        self.assertNotEqual(set(controller.SIGNAL_CHAPTERS.values()), set(controller.ALWAYS_CHAPTERS))
        for asset in (TEMPLATE, SKILL_MD, STANDARD, AGENTS["tech-architect"], AGENTS["tech-critic"]):
            self.assertIn("条件必填", asset)


if __name__ == "__main__":
    unittest.main()
