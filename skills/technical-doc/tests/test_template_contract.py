"""Contract tests that keep the technical-doc assets from drifting apart.

The design review this suite follows up on found four silent-drift classes: the
template had no home for chapters the Skill gates demand, the quality standard
claimed a different consumer, a private PlantUML host was hard-coded into the
workflow, and the role prompts existed twice (inline and installed). Each class is
now asserted here so it fails loudly instead of resurfacing as a runtime surprise.
"""

from __future__ import annotations

import argparse
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
MANUAL = (SKILL_DIR / "references" / "controller-contract.md").read_text(encoding="utf-8")
AGENTS = {path.stem: path.read_text(encoding="utf-8") for path in sorted((SKILL_DIR / "agents").glob("*.md"))}
ROLE_LINE = re.compile(r"^\|\s*\d\s*\|\s*`(tech-[a-z]+)`", re.MULTILINE)
CONTROLLER_SRC = MODULE_PATH.read_text(encoding="utf-8")


def _pattern_terms(pattern: str) -> list[str]:
    """Bare literals of an alternation regex, so prose can be checked against a scanner."""
    body = pattern.removeprefix("(?:").removesuffix(")")
    return [term for term in (raw.replace(r"\b", "").strip() for raw in body.split("|")) if term]


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

    def test_the_template_ships_no_copyable_value_literal_in_prose(self) -> None:
        """A date format written as prose travels into every draft and then fails the gate.

        Value-shaped tokens (``todo``, ``YYYY-MM-DD``) read as data only where the document
        renders them as code, so the skeleton must not hand the Architect a row the delivery
        check will bounce back.
        """
        lines = TEMPLATE.splitlines()
        code_lines = controller._fence_line_mask(lines)
        offenders = [
            f"line {index + 1}: {line.strip()[:70]}"
            for index, line in enumerate(lines)
            if controller.PLACEHOLDER_VALUES.search(line)
            and controller._is_unfilled_slot(line, index in code_lines)
        ]
        self.assertEqual([], offenders)

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


class PolishRoundContractTests(unittest.TestCase):
    """The two rules that decide whether a round is worth burning must live in one place."""

    def test_the_disposition_shortcut_names_the_fields_the_controller_returns(self) -> None:
        """The prose promises a copyable list; a renamed field makes every report rely on memory."""
        for name in ("undisposed", "open_non_blocking_followups", "undisposed_blocking"):
            with self.subTest(name=name):
                self.assertIn(name, SKILL_MD)
                self.assertIn(f'"{name}"', CONTROLLER_SRC)

    def test_the_shortcut_stays_bound_to_non_blocking_findings(self) -> None:
        self.assertIn("`must_fix` 与 `Blocker`/`High` 不在豁免范围内", SKILL_MD)
        self.assertIn("只有用户明确要求", SKILL_MD)
        self.assertIn("触及正确性、安全、数据一致性", SKILL_MD)

    def test_parallel_refiners_and_the_replay_subset_are_stated(self) -> None:
        self.assertIn("并行派发多个 Refiner 实例", SKILL_MD)
        self.assertIn("按**实例**分别声明", SKILL_MD)
        self.assertIn("不做整轮回放", SKILL_MD)
        self.assertIn("findings.undisposed", SKILL_MD)

    def test_placeholder_classes_are_listed_the_way_the_gate_scans_them(self) -> None:
        """The gate sentence enumerates both token classes; a new literal has to show up there."""
        for token in _pattern_terms(controller.PLACEHOLDER_SLOTS.pattern):
            with self.subTest(token=token):
                self.assertIn(token, SKILL_MD)
        for token in _pattern_terms(controller.PLACEHOLDER_VALUES.pattern):
            with self.subTest(token=token):
                self.assertIn(token, SKILL_MD)
        self.assertIn("槽位字面量", SKILL_MD)
        self.assertIn("取值字面量", SKILL_MD)


class ControllerManualTests(unittest.TestCase):
    """PR-3/PR-4 moved command knowledge into one thin page; the page must stay complete."""

    def test_every_subcommand_has_a_line_in_the_manual(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        controller._fixer_commands(sub)
        names = sorted(sub.choices)
        self.assertIn("record-findings", names)
        self.assertIn("dump", names)
        for name in names:
            with self.subTest(command=name):
                self.assertIn(name, MANUAL)

    def test_the_manual_is_the_command_source_and_the_template_is_not(self) -> None:
        self.assertIn("references/controller-contract.md", SKILL_MD)
        self.assertIn("不要在 run 中途回读", SKILL_MD)
        self.assertIn("不要在 run 中途回读", MANUAL)
        # 主控不再读整份模板：章节齐整性靠 gate 的锚点清单
        self.assertNotIn("主控与 Architect 读取完整模板", SKILL_MD)
        self.assertIn("只有 Architect 读取完整模板", SKILL_MD)
        self.assertIn("template-anchors", SKILL_MD)

    def test_the_batch_faces_are_named_where_the_orchestrator_reads(self) -> None:
        for token in (
            "record-findings", "record-fixes", "--from-file", "--batch", "show --next",
            "evidence_chars",
        ):
            with self.subTest(token=token):
                self.assertIn(token, SKILL_MD)
        self.assertIn("不推进任何东西", SKILL_MD)

    def test_the_report_markers_are_taught_to_every_writer(self) -> None:
        for role, marker, face in (
            ("tech-critic", "@@FIND", "record-findings"),
            ("tech-reviewer", "@@FIND", "record-findings"),
            ("tech-refiner", "@@FIX", "record-fixes"),
        ):
            with self.subTest(role=role):
                self.assertIn(marker, AGENTS[role])
                self.assertIn(face, AGENTS[role])
                self.assertIn(marker, MANUAL)

    def test_the_marker_fields_the_manual_lists_are_the_parser_s(self) -> None:
        documented = set(controller.MARKED_FIELDS["FIND"]) | set(controller.MARKED_FIELDS["FIX"])
        for kind, fields in controller.MARKED_OPTIONAL.items():
            documented |= set(fields)
        for field in sorted(documented):
            with self.subTest(field=field):
                self.assertIn(f"{field}=", MANUAL)


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

    def test_the_structure_branch_is_one_rule_shared_by_five_assets(self) -> None:
        """工程与模块 has two branches; probe, gate, template, SKILL and the architect say the same one.

        The chapter used to be prose only: nothing decided which branch applied, so the rule
        could live in the template while the gate checked something else, or vice versa.
        """
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertEqual(controller.STRUCTURE_CHAPTER, "工程与模块")
        self.assertIn('"project-structure-branch"', source)
        for asset in (TEMPLATE, SKILL_MD, STANDARD, AGENTS["tech-architect"]):
            self.assertIn(controller.STRUCTURE_CHAPTER, asset)
            self.assertIn("project-structure-branch", asset)
        # the template's pointer has to resolve to a real规范路径, not to "编程语言规范"
        self.assertIn(".standard/", TEMPLATE)
        self.assertIn("05-project-structure.md", TEMPLATE)
        self.assertIn("05-project-structure.md", SKILL_MD)
        for token in controller.STRUCTURE_SAMPLE_TOKENS:
            self.assertIn(token, TEMPLATE)
        self.assertIn("待初始化", TEMPLATE)
        self.assertIn("待初始化", controller.STRUCTURE_DEFERRALS)

    def test_the_module_table_columns_match_the_template_header(self) -> None:
        """The gate demands five columns; the template is the only place that lists them."""
        header = next(
            line for line in TEMPLATE.splitlines()
            if line.strip().startswith("|")
            and all(column in line for column in controller.STRUCTURE_TABLE_COLUMNS)
        )
        for column in controller.STRUCTURE_TABLE_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, header)

    def test_the_greenfield_witnesses_are_the_ones_the_prose_names(self) -> None:
        """A probe that counts more than the prose admits is a silent reclassification."""
        self.assertTrue({".js", ".ts", ".mjs", ".cjs"} <= controller.BACKEND_CODE_SUFFIXES)
        self.assertFalse({".jsx", ".tsx"} & controller.BACKEND_CODE_SUFFIXES)
        self.assertIn("go.mod", controller.PROJECT_MANIFESTS)
        self.assertIn("pyproject.toml", controller.PROJECT_MANIFESTS)
        # a dependency list in an otherwise empty repo is a plan, not an implementation
        self.assertNotIn("requirements.txt", controller.PROJECT_MANIFESTS)
        for asset in (SKILL_MD, AGENTS["tech-context"]):
            self.assertIn("工程清单", asset)
        # the docs exclusion is part of the same measurement, so every reader of the verdict
        # has to be able to see that example code under docs/ was set aside on purpose
        self.assertEqual({"doc", "docs"}, controller.DOC_DIR_NAMES)
        for asset in (SKILL_MD, TEMPLATE, AGENTS["tech-context"]):
            self.assertIn("`doc/`、`docs/`", asset)
        self.assertIn("工程结构分支", SKILL_MD)
        self.assertIn("工程结构分支", MODULE_PATH.read_text(encoding="utf-8"))

    def test_the_confirmation_loop_is_enforced_and_the_template_stays_innocent(self) -> None:
        """Both delivery checks are controller code now, and the template must not mint findings.

        The marker format is instruction text in SKILL.md and the role prompts; pasted into
        the template it would reach every drafted TDD verbatim, and an unregistered marker is
        exactly what ``markers-match-confirmations`` blocks delivery on.
        """
        source = MODULE_PATH.read_text(encoding="utf-8")
        for name in ("markers-match-confirmations", "confirmations-file-persisted"):
            self.assertIn(f'"{name}"', source)
            self.assertIn(name, SKILL_MD)
        self.assertNotIn("[需人工确认", TEMPLATE)
        self.assertIn("confirmation-log.md", TEMPLATE)
        self.assertIn("confirmation-log.md", SKILL_MD)

    def test_the_landing_channel_rule_is_stated_where_each_reader_needs_it(self) -> None:
        """One rule about where a settled decision may still land, read from three assets.

        The controller enforces it at the audit doors and lets a review-vintage answer carry;
        if only the code says so, the orchestrator keeps chasing a decision with no channel
        left and the Reviewer keeps marking such items 阻断.  The judgement half belongs to the
        auditors' standard, not to a gate that cannot see it.
        """
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("pending_landing", source)
        self.assertIn("pending_landing", SKILL_MD)
        self.assertIn("人工决定与 marker", STANDARD)
        self.assertIn("人工决定与 marker", AGENTS["tech-reviewer"])
        self.assertIn("阻断", AGENTS["tech-reviewer"])
        # the template must not promise more than markers-match-confirmations actually blocks
        self.assertNotIn("悄悄抹掉", TEMPLATE)

    def test_the_delivery_rewind_is_the_only_way_back_and_says_what_it_costs(self) -> None:
        """`DELIVERY -> REFINER` is one edge with one purpose; both prose surfaces must say so.

        The edge is the difference between "an answer that arrives after the final review is
        unshippable" and "it costs one more review round".  If the code, the orchestrator's
        manual and the auditor's standard stop agreeing on the price, the rewind either gets
        used as a general re-edit channel or nobody dares to take it.
        """
        self.assertEqual({"REFINER", "CLOSED"}, controller.STAGE_TRANSITIONS["DELIVERY"])
        self.assertTrue(callable(getattr(controller, "_require_landing_only_rewind", None)))
        self.assertIn("落地回退", SKILL_MD)
        self.assertIn("落地回退", MANUAL)
        self.assertIn("landing_rewind", MANUAL)
        # the two refusals: nothing to land, and no budget left to pay for the fresh review
        self.assertIn("no entry is waiting to be landed", CONTROLLER_SRC)
        self.assertIn("last one the review budget allows", CONTROLLER_SRC)
        self.assertIn("没有待落地条目", MANUAL)
        self.assertIn("轮次预算已耗尽", MANUAL)
        self.assertIn("落地回退", STANDARD)


class GrowthAndMirrorContractTests(unittest.TestCase):
    """PR-5: the two per-round costs nobody used to pay attention to get a stated owner.

    A hand-copied ledger was only refused at delivery, so the orchestrator strip-edited it by
    hand every run; body growth was not measured at all, so +15% of reading per round showed up
    nowhere. Both rules are controller code now, and the prose that tells the orchestrator when
    it will be refused has to name the same moment the code refuses at.
    """

    def test_the_mirror_refusal_is_one_predicate_called_at_three_moments(self) -> None:
        self.assertTrue(callable(getattr(controller, "_reject_stray_ledger_rows", None)))
        self.assertIn("hand-written Finding table", CONTROLLER_SRC)
        # early refusal: the two registrations that write into the TDD, and the disposition that
        # claims the body was edited
        self.assertEqual(
            3,
            CONTROLLER_SRC.count("_reject_stray_ledger_rows(state)"),
            "the early mirror check is expected at write-artifact, record-fix and record-fixes",
        )
        for asset in (SKILL_MD, MANUAL):
            self.assertIn("手抄的 Finding 表", asset)
        self.assertIn("对整批只判一次", SKILL_MD)
        self.assertIn("对整批只判一次", MANUAL)
        # the role prompt must promise the early refusal, not only the delivery-time one
        self.assertIn("前移", AGENTS["tech-refiner"])

    def test_growth_is_a_soft_check_that_the_delivery_report_must_quote(self) -> None:
        self.assertTrue(callable(getattr(controller, "_document_growth_check", None)))
        self.assertIn("document-growth", SKILL_MD)
        self.assertIn("document-growth", MANUAL)
        self.assertIn('"document-growth"', CONTROLLER_SRC)
        self.assertEqual(8.0, controller.GROWTH_WARN_PERCENT)
        self.assertIn("GROWTH_WARN_PERCENT", MANUAL)
        self.assertIn("8%", SKILL_MD)
        # ``ok`` is always True, so the only teeth the check has is being quoted in the report
        self.assertIn("软检查", SKILL_MD)
        self.assertIn("软检查", MANUAL)
        self.assertIn("强制照抄", SKILL_MD)
        self.assertIn('"warnings"', CONTROLLER_SRC)
        self.assertIn("warnings", SKILL_MD)
        self.assertIn("warnings", MANUAL)

    def test_the_reference_size_is_frozen_before_any_round_adds_to_it(self) -> None:
        self.assertIn('"document_growth"', CONTROLLER_SRC)
        self.assertTrue(callable(getattr(controller, "_freeze_growth_baseline", None)))
        for asset in (SKILL_MD, MANUAL):
            self.assertIn("体积参照", asset)
            self.assertIn("冻结参照", asset)

    def test_the_orchestrator_is_told_not_to_read_whole_documents(self) -> None:
        """PR-5c is a rule about the controller's own output surfaces, so both pages state it."""
        self.assertIn("主控侧不整读原文", SKILL_MD)
        self.assertIn("主控侧不整读原文", MANUAL)
        for token in ("locate", "dump", "render-ledger"):
            with self.subTest(token=token):
                self.assertIn(f"`{token}`", SKILL_MD)
        # the field measurement the rule is justified by must be the same number on both pages
        self.assertIn("58K", SKILL_MD)
        self.assertIn("58K", MANUAL)


if __name__ == "__main__":
    unittest.main()
