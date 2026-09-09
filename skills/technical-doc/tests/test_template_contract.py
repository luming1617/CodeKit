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
        history = TEMPLATE.index("## 变更历史")
        self.assertGreater(history, TEMPLATE.index("# 评审检查与结论"))
        self.assertGreater(history, TEMPLATE.index("# 14. 部署拓扑"))
        self.assertGreater(history, TEMPLATE.index("# 16. 审查闭环记录"))

    def test_template_no_longer_embeds_previous_version_in_a_fence(self) -> None:
        self.assertNotIn("```markdown", TEMPLATE)
        self.assertIn("历史版本索引", TEMPLATE)
        self.assertIn("archive-history", TEMPLATE)

    def test_java_specific_chapters_have_an_adaptation_rule(self) -> None:
        self.assertIn("## 技术栈适配规则", TEMPLATE)
        self.assertRegex(TEMPLATE, r"5\. Java 详细设计")

    def test_mandatory_chapters_survive_the_S_tailoring_rule(self) -> None:
        tailoring = TEMPLATE.split("## 方案裁剪规则", 1)[1].split("##", 1)[0]
        for anchor in ("ADR", "风险", "工作量", "审查闭环"):
            self.assertIn(anchor, tailoring)


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


if __name__ == "__main__":
    unittest.main()
