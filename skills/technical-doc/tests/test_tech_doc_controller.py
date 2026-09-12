from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "skills" / "technical-doc" / "scripts" / "tech_doc_controller.py"
SPEC = importlib.util.spec_from_file_location("tech_doc_controller", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load tech_doc_controller from {MODULE_PATH}")
controller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controller)

PRD = """# 产品需求说明书：订单备注同步

## 功能需求
- FR-1 用户在订单详情页保存备注后，3 秒内同步到客服工单系统。
- FR-2 同步失败时保留用户输入并给出可重试提示。

## 验收标准
- AC-1 P95 同步耗时不超过 800 毫秒。
- AC-2 重复提交同一备注不产生重复工单。
"""

TDD_BODY = """# 后端服务技术方案：订单备注同步

## 变更记录
| 版本 | 日期 | 变更类型 |
|------|------|----------|
| 0.1 | 2026-09-09 | 新增 |

## 3.4 需求到设计追踪
| 需求 | 设计落点 |
|------|----------|
| FR-1 | 4.1 主流程 |

## 3.5 关键技术决策记录（ADR）
| 编号 | 背景 | 决策 | 备选方案 |
|------|------|------|----------|
| ADR-001 | 同步通道选型 | 复用 Kafka | 同步 RPC |

```mermaid
sequenceDiagram
    order-->>kafka: 备注变更
```

## 15. 风险登记与工作量评估
| 风险 | 等级 | 缓解 |
|------|------|------|
| 下游限流 | P2 | 退避重试 |

工作量：备注同步模块 3 人日，依赖工单接口联调。

## 16. 审查闭环记录
| Finding | 处置 | 验证 |
|---------|------|------|
| CRITIC-001 | 已修复 | 重试退避单测 |

review_round：1，最终评审状态：APPROVE
"""

EVIDENCE = (
    "证据：src/main/java/com/example/ordernote/NoteSyncService.java:128 与 "
    "db/migration/V12__note_sync.sql；覆盖度 HIGH，未发现相反证据。\n"
)

HISTORY_TAIL = """

## 变更历史（只追加，不覆盖）

### 历史版本索引
| 序号 | 版本 | 归档文件 |
|------|------|----------|
| 1 | 0.9 | .codekit/technical-doc/run/history/01-0.9.md |
"""


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


class TechDocControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "CodeKit Test")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "base")
        self.feature = self.repo / "docs" / "feature"
        self.feature.mkdir(parents=True)
        (self.feature / "product-doc.md").write_text(PRD, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    # ---------------------------------------------------------------- helpers

    def write_tdd(self, body: str = TDD_BODY, update: bool = False) -> Path:
        text = body + (HISTORY_TAIL if update else "")
        path = self.feature / "technical-doc.md"
        path.write_text(text, encoding="utf-8")
        return path

    def record(self, state: Path, role: str, session: str) -> None:
        controller.record_agent(state, role, role, session, "sha256:input", "COMPLETED")

    def artifact(self, state: Path, kind: str, text: str = "") -> dict[str, Any]:
        origin = self.repo / f"tmp-{kind}.md"
        origin.write_text(
            f"{text}\n{EVIDENCE}" if text else EVIDENCE, encoding="utf-8"
        )
        return controller.write_artifact(state, kind, origin, None, None)

    def to_review(self, state: Path) -> None:
        """Drive INPUT -> REVIEW round 1 with every delegated agent logged."""
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取")
        controller.start_stage(state, "CONTEXT")
        self.record(state, "tech-context", "s-context")
        self.artifact(state, "context-pack")
        controller.start_stage(state, "ARCHITECT")
        self.record(state, "tech-architect", "s-arch")
        self.artifact(state, "draft")
        controller.start_stage(state, "CRITIC")
        self.record(state, "tech-critic", "s-critic")
        self.artifact(state, "critic-findings")
        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine")
        self.artifact(state, "refiner-map")
        controller.start_stage(state, "REVIEW")

    def approve_round(self, state: Path, session: str = "s-review-1") -> None:
        self.record(state, "tech-reviewer", session)
        report = self.artifact(state, "review-report")
        controller.record_review(
            state, "APPROVE", report["path"], self.feature / "technical-doc.md", session
        )

    # ------------------------------------------------------------------- init

    def test_init_freezes_prd_and_detects_update_mode(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-1"))
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "NEW")
        self.assertEqual(data["stage"], "INPUT")
        self.assertTrue(data["materials"]["product_requirements"]["content_digest"].startswith("sha256:"))

        self.write_tdd()
        state = Path(controller.init_run(self.repo, self.feature, "run-2"))
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "UPDATE")
        self.assertIn("previous_technical_design", data["materials"])

    def test_init_rejects_placeholder_and_empty_prd(self) -> None:
        (self.feature / "product-doc.md").write_text(
            "# 产品需求说明书：订单备注同步\n"
            "## 功能需求\n"
            "【填写】\n【填写】\n【链接】\nYYYY-MM-DD\n【填写】\n【填写】\n",
            encoding="utf-8",
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.init_run(self.repo, self.feature, "run-x")
        self.assertIn("placeholder", str(ctx.exception))

        (self.feature / "product-doc.md").write_text("# 标题\n", encoding="utf-8")
        with self.assertRaises(controller.ControllerError):
            controller.init_run(self.repo, self.feature, "run-y")

    def test_init_rejects_missing_prd_and_duplicate_run(self) -> None:
        (self.feature / "product-doc.md").unlink()
        with self.assertRaises(controller.ControllerError):
            controller.init_run(self.repo, self.feature, "run-a")
        (self.feature / "product-doc.md").write_text(PRD, encoding="utf-8")
        controller.init_run(self.repo, self.feature, "run-a")
        with self.assertRaises(controller.ControllerError):
            controller.init_run(self.repo, self.feature, "run-a")

    # ------------------------------------------------------------ input gate

    def test_input_status_rules(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-2"))
        with self.assertRaises(controller.ControllerError):
            controller.finalize_inputs(state, "PASS", "无来源")

        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "NOT_ACCESSIBLE", "v1", "无法读取", "LOW",
        )
        with self.assertRaises(controller.ControllerError):
            controller.finalize_inputs(state, "PARTIAL", "PRD 读不到")
        controller.finalize_inputs(state, "BLOCKED", "PRD 不可读，停止全部子 Agent")
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "CONTEXT")

    def test_stage_order_is_enforced(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-3"))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "ok")
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "ARCHITECT")
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "CRITIC")
        controller.start_stage(state, "CONTEXT")
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "ARCHITECT")  # no artifact yet
        self.record(state, "tech-context", "s-context")
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "ARCHITECT")  # artifact still missing
        self.artifact(state, "context-pack")
        controller.start_stage(state, "ARCHITECT")

    def test_artifact_requires_its_own_agent_and_role_match(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-4"))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "ok")
        controller.start_stage(state, "CONTEXT")
        with self.assertRaises(controller.ControllerError):
            self.artifact(state, "context-pack")
        self.record(state, "tech-context", "s-context")
        self.artifact(state, "context-pack")
        origin = self.repo / "tmp-bad.md"
        origin.write_text("内容足够长的一段说明文字，用于通过实质性检查。\n", encoding="utf-8")
        with self.assertRaises(controller.ControllerError):
            controller.write_artifact(state, "draft", origin, "tech-context", None)

    # -------------------------------------------------------------- findings

    def test_finding_id_discipline(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-5"))
        self.to_review(state)
        for bad in ("CRITIC-1", "FINDING-001", "REVIEW-9-001"):
            with self.assertRaises(controller.ControllerError, msg=bad):
                controller.record_finding(
                    state, "CRITIC", bad, "Blocker", "4.2", "缺少退避", None, None
                )
        with self.assertRaises(controller.ControllerError):
            controller.record_finding(state, "REVIEWER", "REVIEW-9-001", "High", "4.2", "轮次不符", None, None)
        controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "4.2", "缺少退避", None, None)
        with self.assertRaises(controller.ControllerError):
            controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "4.2", "重复", None, None)
        entry = next(item for item in json.loads(state.read_text(encoding="utf-8"))["findings"])
        self.assertTrue(entry["must_fix"])

    def test_blocking_findings_cannot_be_rejected_or_left_pending(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-6"))
        controller.record_finding(state, "CRITIC", "CRITIC-001", "Blocker", "4.2", "证据", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-002", "Medium", "6.1", "证据", None, 0)
        with self.assertRaises(controller.ControllerError):
            controller.record_fix(state, "CRITIC-001", "REJECTED", "4.2", "改法", "验证")
        with self.assertRaises(controller.ControllerError):
            controller.record_fix(state, "CRITIC-001", "PENDING_CONFIRMATION", "4.2", "改法", "验证")
        with self.assertRaises(controller.ControllerError):
            controller.record_fix(state, "CRITIC-404", "FIXED", "4.2", "改法", "验证")
        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补退避", "单测")
        controller.record_fix(state, "CRITIC-002", "REJECTED", "6.1", "不改", "接口契约证据")

    # ---------------------------------------------------------- review rounds

    def test_review_round_cap_is_five(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-7"))
        self.write_tdd()
        self.to_review(state)
        for round_ in range(1, controller.MAX_REVIEW_ROUNDS + 1):
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["review_round"], round_)
            self.record(state, "tech-reviewer", f"s-review-{round_}")
            with self.assertRaises(controller.ControllerError):
                self.record(state, "tech-reviewer", f"extra-in-{round_}")  # one fresh reviewer per round
            report = self.artifact(state, "review-report", f"第 {round_} 轮独立终审报告：仍存在待关闭项。\n")
            controller.record_review(
                state, "REQUEST_CHANGES", report["path"], self.feature / "technical-doc.md",
                f"s-review-{round_}",
            )
            controller.record_finding(
                state, "REVIEWER", f"REVIEW-{round_}-001", "High", "4.2", "证据", None, None
            )
            controller.start_stage(state, "REFINER")
            self.record(state, "tech-refiner", f"s-refine-{round_}")
            self.artifact(state, "refiner-map", f"第 {round_} 轮变更映射已记录。\n")
            controller.record_fix(state, f"REVIEW-{round_}-001", "FIXED", "4.2", "改法", "验证")
            if round_ < controller.MAX_REVIEW_ROUNDS:
                controller.start_stage(state, "REVIEW")
            else:
                with self.assertRaises(controller.ControllerError) as ctx:
                    controller.start_stage(state, "REVIEW")
                self.assertIn("cap", str(ctx.exception).lower())
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["review_round"], 5)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("final-review-approved", result["failures"])

    def test_round_two_needs_a_refiner_map_for_that_round(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-8"))
        self.write_tdd()
        self.to_review(state)
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮报告：需要修订重试策略。\n")
        controller.record_review(state, "REQUEST_CHANGES", report["path"], self.write_tdd(), "s-review-1")
        controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "4.2", "缺少退避", None, None)

        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine-2")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "REVIEW")
        self.assertIn("refiner-map", str(ctx.exception))

        self.artifact(state, "refiner-map", "第 2 轮变更映射：CRITIC 与 REVIEW-1-001 已处置。\n")
        controller.record_fix(state, "REVIEW-1-001", "FIXED", "4.2", "补指数退避", "压测脚本")
        controller.start_stage(state, "REVIEW")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["review_round"], 2)
        self.approve_round(state, "s-review-2")
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertEqual(result["review_round"], 2)

    def test_reviewer_verdict_needs_a_completed_reviewer(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-9"))
        self.to_review(state)
        report = self.repo / "review-report-raw.md"
        report.write_text("报告内容足够长以通过实质性检查。\n", encoding="utf-8")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_review(state, "APPROVE", report, self.write_tdd(), "s-review-x")
        self.assertIn("tech-reviewer", str(ctx.exception))

    # ------------------------------------------------------------------ gate

    def gate_for(
        self, run_id: str, body: str = TDD_BODY, update: bool = False
    ) -> tuple[Path, dict[str, Any]]:
        """Deliver one document and return its gate report.

        The document is written *before* the verdict so the snapshot-consistency check
        stays green and content checks are isolated.
        """
        if update:
            self.write_tdd(body, update=True)
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        self.to_review(state)
        self.write_tdd(body, update)
        if update:
            controller.archive_history(state, "0.9")
        self.approve_round(state)
        return state, controller.gate(state, self.feature / "technical-doc.md")

    def test_gate_blocks_until_the_loop_is_closed(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-10"))
        self.to_review(state)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "Blocker", "4.2", "无退避", None, 0)
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮终审报告正文。\n")
        controller.record_review(state, "APPROVE", report["path"], self.write_tdd(), "s-review-1")

        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocking-findings-closed", result["failures"])
        self.assertIn("every-finding-has-a-disposition", result["failures"])

        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补退避", "单测")
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertEqual(result["review_round"], 1)

    def test_gate_detects_edits_after_the_last_review(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-11"))
        self.to_review(state)
        self.write_tdd()
        self.approve_round(state)
        self.assertEqual(controller.gate(state, self.feature / "technical-doc.md")["status"], "PASS")
        # The orchestrator sneaks in a post-review edit: the verdict no longer covers it.
        self.write_tdd(TDD_BODY + "\n补充一段主控直接改写的设计说明，未被任何 Reviewer 看过。\n")
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("review-report-matches-delivered-tdd", result["failures"])

    def test_gate_rejects_unfilled_template_placeholders(self) -> None:
        _, result = self.gate_for("run-12a", TDD_BODY + "\n## 容量测算\n此处后续补充。\n")
        self.assertIn("no-template-placeholders", result["failures"])

    def test_gate_rejects_missing_required_anchors(self) -> None:
        _, result = self.gate_for("run-12b", TDD_BODY.replace("审查闭环记录", "其它章节"))
        self.assertIn("template-anchors", result["failures"])

    def test_gate_rejects_legacy_repo_agent_writeback(self) -> None:
        _, result = self.gate_for(
            "run-12c", TDD_BODY + "\n参见 AGENTS.md 中的 technical-doc-context:start 区块。\n"
        )
        self.assertIn("no-repo-agent-config-writeback", result["failures"])

    def test_gate_accepts_a_complete_new_requirement(self) -> None:
        _, result = self.gate_for("run-12d")
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertIn("review-report-matches-delivered-tdd", result["passed"])

    def test_human_confirmation_lines_are_allowed(self) -> None:
        _, result = self.gate_for(
            "run-13", TDD_BODY + "\n[需人工确认: 下游工单接口的限流阈值]\n"
        )
        self.assertIn("no-template-placeholders", result["passed"])

    def test_gate_flags_unbalanced_fences(self) -> None:
        _, result = self.gate_for("run-13b", TDD_BODY + "\n```mermaid\nsequenceDiagram\n")
        self.assertIn("diagram-fences", result["failures"])

    def test_nested_markdown_fence_is_refused_by_gate_and_archive(self) -> None:
        state, result = self.gate_for("run-14", TDD_BODY + "\n```markdown\n旧方案正文\n```\n")
        self.assertIn("diagram-fences", result["failures"])
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.archive_history(state, "0.9")
        self.assertIn("markdown fence", str(ctx.exception))

    def test_update_mode_delivers_only_after_the_previous_version_is_archived(self) -> None:
        state, result = self.gate_for("run-15", update=True)
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertEqual(len(result["checks"]), len(result["passed"]) + len(result["failures"]))
        archived = json.loads(state.read_text(encoding="utf-8"))["history"]
        self.assertEqual(len(archived), 1)
        self.assertTrue(Path(archived[0]["path"]).is_file())
        self.assertNotIn("```markdown", Path(archived[0]["path"]).read_text(encoding="utf-8"))

    def test_update_mode_blocks_without_history(self) -> None:
        self.write_tdd(update=True)
        state = Path(controller.init_run(self.repo, self.feature, "run-15b"))
        self.to_review(state)
        self.approve_round(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertIn("previous-tdd-archived", result["failures"])

    def test_closed_stage_requires_a_passed_gate(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-16"))
        self.to_review(state)
        self.write_tdd()
        self.approve_round(state)
        controller.start_stage(state, "DELIVERY")
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "CLOSED")
        controller.gate(state, self.feature / "technical-doc.md")
        controller.start_stage(state, "CLOSED")

    def test_degraded_run_can_only_deliver_partially(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-17"))
        self.to_review(state)
        self.write_tdd()
        self.approve_round(state)
        controller.degrade(state, "tech-reviewer", "宿主不支持具名子代理，已降级为串行终审")
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PARTIAL_DEGRADED")
        self.assertTrue(result["degradations"])
        self.assertEqual(result["failures"], [])
        controller.start_stage(state, "DELIVERY")
        controller.start_stage(state, "CLOSED")

    def test_cli_reports_blocked_on_gate_failure(self) -> None:
        controller.init_run(self.repo, self.feature, "run-18")
        state = self.repo / ".codekit" / "technical-doc" / "run-18" / "state.json"
        self.write_tdd()
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "gate", "--state", str(state),
             "--tdd-file", str(self.feature / "technical-doc.md")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "BLOCKED")
        self.assertIn("independent-review-ran", payload["failures"])

        bad = subprocess.run(
            [sys.executable, str(MODULE_PATH), "start-stage", "--state", str(state), "--to", "CONTEXT"],
            capture_output=True, text=True,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("BLOCKED:", bad.stderr)


if __name__ == "__main__":
    unittest.main()
