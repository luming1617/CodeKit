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

## 3.4 影响范围
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

## 12. 可观测设计
备注同步成功率、P95 同步耗时、重试次数三项指标进现有订单大盘，按服务与错误码分维度。

## 13. 告警设计
成功率低于 99% 持续 5 分钟触发 P2 告警，路由到订单值班群，Runbook 指向本方案 13.5。

## 15. 里程碑规划
| 风险 | 等级 | 缓解 |
|------|------|------|
| 下游限流 | P2 | 退避重试 |

工作量：备注同步模块 3 人日，依赖工单接口联调。

## 16. 审查闭环记录

台账由 `tech_doc_controller.py render-ledger` 从 `state.json` 生成，勿手改。
"""

EVIDENCE = (
    "证据：src/main/java/com/example/ordernote/NoteSyncService.java:128 与 "
    "db/migration/V12__note_sync.sql；覆盖度 HIGH，未发现相反证据。\n"
)

# One structural change class only: the template's M row, so the signalled chapter becomes required
# and 可观测设计 loses its 不涉及 escape hatch; 部署拓扑 only joins the floor at L.
PRD_ONE_SIGNAL = """# 产品需求说明书：客服话术查询

## 功能需求
- FR-1 对外提供查询接口，按订单号返回可用话术列表。
- FR-2 接口的错误码与鉴权沿用现有网关。

## 验收标准
- AC-1 P95 查询耗时不超过 300 毫秒。
"""

# Four change classes: the template's L row.
PRD_STRUCTURAL = """# 产品需求说明书：风控规则引擎

## 功能需求
- FR-1 新建数据表保存规则版本，字段与索引由规则管理员维护。
- FR-2 提供规则查询接口，返回当前生效版本的表达式与说明。
- FR-3 规则变更通过消息广播给结算服务，消息体包含版本号与生效时间。
- FR-4 定时任务每小时补偿一次投递失败的消息，避免下游状态漂移。

## 验收标准
- AC-1 规则发布后 1 分钟内下游可见。
"""

FILLER = "指标口径、告警阈值、责任人与复检方式在此逐条写明，不留只有结论没有做法的空话。"
CHAPTER_FILL = FILLER * 12

# TDD_BODY at M: 可观测设计 carries substance and the signalled chapter exists at all
# (部署拓扑 stays in the body although M no longer demands it, matching a doc written for L).
M_TDD_BODY = (
    TDD_BODY.replace(
        "备注同步成功率、P95 同步耗时、重试次数三项指标进现有订单大盘，按服务与错误码分维度。",
        "备注同步成功率、P95 同步耗时、重试次数三项指标进现有订单大盘，按服务与错误码分维度。\n"
        + CHAPTER_FILL,
    ).replace(
        "成功率低于 99% 持续 5 分钟触发 P2 告警，路由到订单值班群，Runbook 指向本方案 13.5。",
        "成功率低于 99% 持续 5 分钟触发 P2 告警，路由到订单值班群。\n" + CHAPTER_FILL,
    ).replace(
        "## 15. 里程碑规划",
        "## 6. 接口与契约设计\n" + CHAPTER_FILL
        + "\n\n## 14. 部署拓扑\n" + CHAPTER_FILL
        + "\n\n## 15. 里程碑规划",
    )
)

HISTORY_TAIL = """
## 变更历史（只追加，不覆盖）

### 历史版本索引

索引由 `render-ledger` 生成，只追加不手改。
"""

# Chapter numbers are deliberately sparse and uneven (1, 3.4, 7.2, 8.1, 15) because that is
# what real TDD bodies look like, and the index has to resolve a finding located at ``7.2``
# without assuming the document is numbered densely.  Chapter 15 carries filler so a small
# context window can be pushed over its read budget by one section, and 8.1 carries more table
# declarations than one lookup response is allowed to echo.
DOC_BODY = (
    """# 后端服务技术方案：索引测试

## 1. 背景

## 3.4 影响范围
| 需求 | 设计落点 |
|------|----------|
| FR-1 | 见 7.2 节 |

## 7.2 数据模型
```sql
CREATE TABLE order_note (id bigint);
ALTER TABLE work_order ADD COLUMN note text;
```
POST /api/v1/notes

### 约束说明
说明文本。

## 8.1 归档表
```sql
"""
    # 14 declared tables: more than one lookup response is allowed to echo.
    + "".join(f"CREATE TABLE archive_note_{i} (id bigint);\n" for i in range(14))
    + """```

## 15. 里程碑规划
"""
    + "风险说明。" * 400
    + """

## 16. 审查闭环记录

台账由 `tech_doc_controller.py render-ledger` 从 `state.json` 生成，勿手改。
"""
)


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

    def to_refiner(self, state: Path, prd_changes: str | None = None) -> None:
        """Drive INPUT -> REFINER with every delegated agent and artifact logged."""
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取", prd_changes)
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

    def to_review(self, state: Path, prd_changes: str | None = None) -> None:
        """Open review round 1; dispositions belong to the Refiner stage before this."""
        self.to_refiner(state, prd_changes)
        controller.start_stage(state, "REVIEW")

    def render(self, state: Path) -> dict[str, Any]:
        """Refresh chapter 16; a failed render is a gate finding, not a test error."""
        try:
            return controller.render_ledger(state, self.feature / "technical-doc.md")
        except controller.ControllerError as exc:
            return {"error": str(exc)}

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

    def test_record_source_verifies_the_locator_on_disk(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-2b"))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_source(
                state, "SRC-1", "PRD", str(self.feature / "missing.md"),
                "READ", "v1", "需求", "HIGH",
            )
        self.assertIn("claims READ", str(ctx.exception))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_source(
                state, "SRC-2", "PRD", str(self.feature / "product-doc.md"),
                "NOT_FOUND", "v1", "需求", "HIGH",
            )
        self.assertIn("claims NOT_FOUND", str(ctx.exception))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_source(
                state, "SRC-3", "PRD", str(self.feature / "missing.md"),
                "NOT_ACCESSIBLE", "v1", "需求", "HIGH",
            )
        self.assertIn("that is NOT_FOUND", str(ctx.exception))
        entry = controller.record_source(
            state, "SRC-4", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "需求", "HIGH",
        )
        self.assertEqual(entry["resolved_kind"], "file")
        self.assertTrue(entry["content_digest"].startswith("sha256:"))
        # a source that exists but cannot be opened is present, not absent — the ledger
        # must not state the opposite of what the caller just verified
        present = controller.record_source(
            state, "SRC-5", "SYSTEM_EVIDENCE", str(self.feature / "product-doc.md"),
            "NOT_ACCESSIBLE", "v1", "存在但打不开", "LOW",
        )
        self.assertEqual(present["resolved_kind"], "inaccessible")
        absent = controller.record_source(
            state, "SRC-6", "SYSTEM_EVIDENCE", str(self.feature / "gone.md"),
            "NOT_FOUND", "v1", "确实不存在", "LOW",
        )
        self.assertEqual(absent["resolved_kind"], "absent")
        # a non-path locator (a human decision) is taken at face value
        self.assertNotIn(
            "resolved_kind",
            controller.record_source(
                state, "DEC-1", "HUMAN_DECISION", "本周评审会结论：先做同步", "READ", "v1", "决策", "HIGH"
            ),
        )

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
        self.to_refiner(state)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "Blocker", "4.2", "证据", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-002", "Medium", "6.1", "证据", None, 0)
        with self.assertRaises(controller.ControllerError):
            controller.record_fix(state, "CRITIC-001", "REJECTED", "4.2", "改法")
        with self.assertRaises(controller.ControllerError):
            controller.record_fix(state, "CRITIC-001", "PENDING_CONFIRMATION", "4.2", "改法")
        with self.assertRaises(controller.ControllerError):
            controller.record_fix(state, "CRITIC-404", "FIXED", "4.2", "改法")
        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补退避")
        controller.record_fix(state, "CRITIC-002", "REJECTED", "6.1", "不改")

    def test_dispositions_need_the_refiner_stage(self) -> None:
        """The orchestrator may not grade its own edits after a verdict is on the table."""
        state = Path(controller.init_run(self.repo, self.feature, "run-6b"))
        self.to_review(state)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "High", "4.2", "证据", None, 0)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "改法")
        self.assertIn("REFINER", str(ctx.exception))

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
            controller.record_fix(state, f"REVIEW-{round_}-001", "FIXED", "4.2", "改法")
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
        controller.record_fix(state, "REVIEW-1-001", "FIXED", "4.2", "补指数退避")
        controller.start_stage(state, "REVIEW")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["review_round"], 2)
        self.approve_round(state, "s-review-2")
        self.render(state)
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
        stays green and content checks are isolated, and the ledger is rendered last
        because every delivery requires it.
        """
        if update:
            self.write_tdd(body, update=True)
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        self.to_review(state, "2/1/0" if update else None)
        self.write_tdd(body, update)
        if update:
            controller.archive_history(state, "0.9")
        self.approve_round(state)
        self.render(state)
        return state, controller.gate(state, self.feature / "technical-doc.md")

    def test_gate_blocks_until_the_loop_is_closed(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-10"))
        self.to_refiner(state)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "Blocker", "4.2", "无退避", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-002", "Blocker", "5.6", "无幂等键", None, 0)
        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补退避")
        controller.start_stage(state, "REVIEW")
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮终审报告正文。\n")
        controller.record_review(state, "APPROVE", report["path"], self.write_tdd(), "s-review-1")

        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("blocking-findings-closed", result["failures"])
        self.assertIn("every-finding-has-a-disposition", result["failures"])

    def test_gate_passes_once_the_open_round_is_the_reviewed_round(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-10b"))
        self.to_refiner(state)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "Blocker", "4.2", "无退避", None, 0)
        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补退避")
        controller.start_stage(state, "REVIEW")
        self.write_tdd()
        self.approve_round(state)
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertEqual(result["review_round"], 1)

    def test_gate_blocks_when_the_newest_verdict_covers_an_earlier_round(self) -> None:
        """A round opened after the verdict may not borrow that verdict's independence."""
        state = Path(controller.init_run(self.repo, self.feature, "run-10c"))
        self.write_tdd()
        self.to_review(state)
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮终审报告正文。\n")
        controller.record_review(state, "APPROVE", report["path"], self.feature / "technical-doc.md", "s-review-1")
        controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "4.2", "退避策略缺失", None, None)

        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine-2")
        self.artifact(state, "refiner-map", "第 2 轮变更映射：REVIEW-1-001 已处置。\n")
        controller.record_fix(state, "REVIEW-1-001", "FIXED", "4.2", "补指数退避")
        controller.start_stage(state, "REVIEW")
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "BLOCKED", result["failures"])
        self.assertIn("final-review-covers-open-round", result["failures"])

    def test_gate_detects_edits_after_the_last_review(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-11"))
        self.to_review(state)
        self.write_tdd()
        self.approve_round(state)
        self.render(state)
        tdd = self.feature / "technical-doc.md"
        self.assertEqual(controller.gate(state, tdd)["status"], "PASS")
        # The orchestrator sneaks in a post-review edit: the verdict no longer covers it.
        tdd.write_text(
            tdd.read_text(encoding="utf-8")
            + "\n补充一段主控直接改写的设计说明，未被任何 Reviewer 看过。\n",
            encoding="utf-8",
        )
        result = controller.gate(state, tdd)
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
        self.to_review(state, "2/1/0")
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
        self.assertIn("ledger-blocks-present", json.loads(state.read_text(encoding="utf-8"))["gates"][-1]["failures"])
        self.render(state)
        controller.gate(state, self.feature / "technical-doc.md")
        controller.start_stage(state, "CLOSED")

    def test_degraded_run_can_only_deliver_partially(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-17"))
        self.to_review(state)
        self.write_tdd()
        self.approve_round(state)
        controller.degrade(state, "tech-reviewer", "宿主不支持具名子代理，已降级为串行终审")
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PARTIAL_DEGRADED")
        self.assertTrue(result["degradations"])
        self.assertEqual(result["failures"], [])
        controller.start_stage(state, "DELIVERY")
        controller.start_stage(state, "CLOSED")

    # ---------------------------------------------------------- generated ledger

    def closed_loop(self, run_id: str, update: bool = False) -> tuple[Path, Path]:
        """One Refiner round with a closed Blocker, an approval, and the delivered document."""
        if update:
            self.write_tdd(update=True)
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        self.to_refiner(state, "2/1/0" if update else None)
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "Blocker", "4.2 主流程", "无退避", None, 0
        )
        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补指数退避")
        controller.start_stage(state, "REVIEW")
        self.write_tdd(update=update)
        if update:
            controller.archive_history(state, "0.9")
        self.approve_round(state)
        return state, self.feature / "technical-doc.md"

    def test_render_ledger_is_idempotent_and_gate_clean(self) -> None:
        state, tdd = self.closed_loop("run-20")
        first = controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        second = controller.render_ledger(state, tdd)
        self.assertEqual(first["rendered"], ["findings", "run-metadata"])
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(text, tdd.read_text(encoding="utf-8"))
        # one marker pair per section, in chapter order, below the chapter 16 heading
        self.assertEqual(text.count("<!-- codekit:technical-doc:ledger:"), 4)
        chapter = text.index("## 16. 审查闭环记录")
        findings = text.index("ledger:findings:start")
        metadata = text.index("ledger:run-metadata:start")
        self.assertTrue(chapter < findings < metadata, (chapter, findings, metadata))
        self.assertEqual(first["handwritten_mirror"], [])
        result = controller.gate(state, tdd)
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertIn("ledger-blocks-present", result["passed"])
        self.assertIn("ledger-matches-state", result["passed"])
        self.assertIn("CRITIC-001", tdd.read_text(encoding="utf-8"))

    def test_refreshing_the_ledger_keeps_the_review_binding(self) -> None:
        state, tdd = self.closed_loop("run-21")
        recorded = json.loads(state.read_text(encoding="utf-8"))["reviews"][-1]["tdd_digest"]
        rendered = controller.render_ledger(state, tdd)
        self.assertTrue(rendered["changed"])
        self.assertEqual(
            json.loads(state.read_text(encoding="utf-8"))["reviews"][-1]["tdd_digest"], recorded
        )
        result = controller.gate(state, tdd)
        self.assertIn("review-report-matches-delivered-tdd", result["passed"], result["failures"])

    def test_windows_line_endings_do_not_break_the_binding_or_the_ledger(self) -> None:
        state, tdd = self.closed_loop("run-22")
        controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        tdd.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
        result = controller.gate(state, tdd)
        self.assertIn("review-report-matches-delivered-tdd", result["passed"], result["failures"])
        self.assertIn("ledger-matches-state", result["passed"], result["failures"])

    def test_gate_blocks_until_the_ledger_covers_every_finding(self) -> None:
        state, tdd = self.closed_loop("run-23")
        controller.render_ledger(state, tdd)
        self.assertEqual(controller.gate(state, tdd)["status"], "PASS")
        # Round 1 raises a new finding; chapter 16 now under-reports the review loop.
        controller.record_finding(
            state, "REVIEWER", "REVIEW-1-001", "High", "5.6 幂等", "无幂等键", None, None
        )
        controller.start_stage(state, "REFINER")
        result = controller.gate(state, tdd)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("ledger-matches-state", result["failures"])
        self.assertIn("every-finding-has-a-disposition", result["failures"])

    def test_gate_blocks_when_a_generated_block_is_edited_by_hand(self) -> None:
        state, tdd = self.closed_loop("run-24")
        controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        tdd.write_text(text.replace("补指数退避", "已手工改写"), encoding="utf-8")
        result = controller.gate(state, tdd)
        self.assertIn("ledger-matches-state", result["failures"])
        detail = next(item for item in result["checks"] if item["name"] == "ledger-matches-state")
        self.assertIn("findings", str(detail["detail"]))

    def test_hand_written_mirror_table_is_reported_and_blocks_delivery(self) -> None:
        state, tdd = self.closed_loop("run-28")
        controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        tdd.write_text(
            text + "\n| Finding | 处置 |\n|---|---|\n| REVIEW-1-002 | 手工维护的第二份台账 |\n",
            encoding="utf-8",
        )
        second = controller.render_ledger(state, tdd)
        self.assertTrue(second["handwritten_mirror"])
        self.assertIn("REVIEW-1-002", str(second["handwritten_mirror"]))
        result = controller.gate(state, tdd)
        self.assertIn("ledger-matches-state", result["failures"])

    def test_ledger_markers_must_stay_outside_code_fences(self) -> None:
        state, tdd = self.closed_loop("run-25")
        text = tdd.read_text(encoding="utf-8")
        tdd.write_text(
            text
            + "\n```text\n<!-- codekit:technical-doc:ledger:findings:start -->\n示例\n```\n",
            encoding="utf-8",
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.render_ledger(state, tdd)
        self.assertIn("inside a code fence", str(ctx.exception))

    def test_update_run_needs_the_prd_change_ledger(self) -> None:
        self.write_tdd(update=True)
        state = Path(controller.init_run(self.repo, self.feature, "run-26"))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v2", "订单备注同步需求（变更）", "HIGH",
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.finalize_inputs(state, "PASS", "已读取变更")
        self.assertIn("--prd-changes", str(ctx.exception))
        with self.assertRaises(controller.ControllerError):
            controller.finalize_inputs(state, "PASS", "已读取变更", "3/1")

        state, tdd = self.closed_loop("run-27", update=True)
        rendered = controller.render_ledger(state, tdd)
        self.assertEqual(rendered["rendered"], ["findings", "run-metadata", "history-index"])
        text = tdd.read_text(encoding="utf-8")
        self.assertIn("| 1 | 0.9 |", text)
        self.assertIn("2/1/0", text)
        result = controller.gate(state, tdd)
        self.assertEqual(result["status"], "PASS", result["failures"])

    def test_history_index_accumulates_across_update_runs(self) -> None:
        """The second UPDATE run may not erase the rows the first run's render wrote."""
        first, tdd = self.closed_loop("run-h1", update=True)
        controller.render_ledger(first, tdd)
        second = Path(controller.init_run(self.repo, self.feature, "run-h2"))
        self.to_refiner(second, "3/1/0")
        controller.start_stage(second, "REVIEW")
        controller.archive_history(second, "1.0")
        self.approve_round(second, "s-review-h2")
        controller.render_ledger(second, tdd)
        # Re-rendering merges the rows it just wrote back against the same state: no duplicates.
        again = controller.render_ledger(second, tdd)
        self.assertFalse(again["changed"])
        text = tdd.read_text(encoding="utf-8")
        self.assertIn("| 1 | 0.9 |", text)
        self.assertIn("| 2 | 1.0 |", text)
        self.assertEqual(text.count("| 1.0 |"), 1)
        self.assertEqual(text.count("| 0.9 |"), 1)
        result = controller.gate(second, tdd)
        self.assertEqual(result["status"], "PASS", result["failures"])

    def test_render_ledger_refuses_once_the_run_is_closed(self) -> None:
        state, tdd = self.closed_loop("run-30")
        controller.render_ledger(state, tdd)
        controller.gate(state, tdd)
        controller.start_stage(state, "DELIVERY")
        controller.start_stage(state, "CLOSED")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.render_ledger(state, tdd)
        self.assertIn("closed", str(ctx.exception).lower())

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

    def test_cli_render_ledger_writes_the_blocks(self) -> None:
        state, tdd = self.closed_loop("run-31")
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "render-ledger", "--state", str(state),
             "--tdd-file", str(tdd)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["rendered"], ["findings", "run-metadata"])
        # a NEW run has no superseded version, so the history index stays out of the document
        self.assertNotIn("ledger:history-index:start", tdd.read_text(encoding="utf-8"))

    # --------------------------------------------------------- agent dispatch

    def test_dispatch_window_records_wall_time_by_phase(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-40"))
        controller.record_agent(
            state, "tech-architect", "tech-architect", "s-arch-1", "sha256:in", "DISPATCHED"
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-architect", "tech-architect", "s-arch-1", "sha256:in", "DISPATCHED"
            )
        self.assertIn("already dispatched", str(ctx.exception))
        entry = controller.record_agent(
            state, "tech-architect", "tech-architect", "s-arch-1", "", "COMPLETED"
        )
        self.assertEqual(entry["status"], "COMPLETED")
        self.assertIsNotNone(entry["wall_seconds"])
        self.assertEqual(entry["input_digest"], "sha256:in")
        summary = controller.show(state)
        self.assertEqual(
            summary["dispatches"],
            {"total": 1, "timed": 1, "aborted": 0, "open": 0, "scoped": 0, "over_budget": []},
        )
        self.assertIn("write", summary["minutes_by_phase"])
        # no dispatch declared a scope and no index was built: the run stays as it was before W3
        self.assertIsNone(summary["doc_index"])
        self.assertEqual(summary["context_window"], controller.DEFAULT_CONTEXT_WINDOW)

    def test_open_dispatch_is_visible_and_only_the_owner_may_close_it(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-41"))
        controller.record_agent(
            state, "tech-critic", "tech-critic", "s-critic-1", "sha256:in", "DISPATCHED"
        )
        self.assertEqual(controller.show(state)["dispatches"]["open"], 1)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-refiner", "tech-refiner", "s-critic-1", "", "COMPLETED"
            )
        self.assertIn("dispatched as", str(ctx.exception))
        entry = controller.record_agent(
            state, "tech-critic", "tech-critic", "s-critic-1", "", "ABORTED"
        )
        self.assertEqual(entry["status"], "ABORTED")
        summary = controller.show(state)
        self.assertEqual(summary["dispatches"]["aborted"], 1)
        self.assertEqual(summary["dispatches"]["open"], 0)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-critic", "tech-critic", "s-critic-1", "", "COMPLETED"
            )
        self.assertIn("already recorded", str(ctx.exception))

    # ------------------------------------- document index and per-section budget

    def cached_index(self, state: Path) -> dict[str, Any]:
        return json.loads((state.parent / "doc-index.json").read_text(encoding="utf-8"))

    def test_index_keys_sections_and_collects_symbols(self) -> None:
        tdd = self.write_tdd(DOC_BODY)
        state = Path(controller.init_run(self.repo, self.feature, "run-50"))
        controller.record_finding(state, "CRITIC", "CRITIC-001", "High", "7.2", "证据", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-002", "Medium", "未编号的段落", "证据", None, 0)
        summary = controller.doc_index(state, tdd)
        self.assertEqual(summary["status"], "OK")
        index = self.cached_index(state)
        by_key = {item["key"]: item for item in index["sections"]}
        self.assertIn("title:约束说明", by_key)
        self.assertEqual(by_key["title:约束说明"]["parent"], "7.2")
        self.assertEqual(
            by_key["7.2"]["symbols"], ["order_note", "work_order", "POST /api/v1/notes"]
        )
        self.assertEqual(by_key["3.4"]["refs"], ["7.2"])
        self.assertEqual(by_key["7.2"]["findings"], ["CRITIC-001"])
        self.assertEqual(index["unlocated_findings"], ["CRITIC-002"])
        self.assertEqual(summary["unlocated_total"], 1)
        # The ranges are the product: they must address the real lines of the real file, and a
        # level-2 section owns the sub-headings nested under it.
        lines = tdd.read_text(encoding="utf-8").splitlines()
        section = by_key["7.2"]
        self.assertTrue(lines[section["start"] - 1].startswith("## 7.2 数据模型"))
        self.assertTrue(lines[section["end"]].startswith("## 8.1 归档表"))
        self.assertLess(by_key["title:约束说明"]["start"], section["end"])
        self.assertTrue(lines[by_key["15"]["end"]].startswith("## 16"))
        # Deterministic: rebuilding the same bytes yields the same sections.
        controller.doc_index(state, tdd)
        self.assertEqual(self.cached_index(state)["sections"], index["sections"])

    def test_index_refuses_an_unclosed_fence(self) -> None:
        tdd = self.write_tdd(DOC_BODY + "```sql\nCREATE TABLE dangling (id bigint);\n")
        state = Path(controller.init_run(self.repo, self.feature, "run-51"))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.doc_index(state, tdd)
        self.assertIn("unclosed code fence", str(ctx.exception))

    def test_locate_returns_the_ranges_a_finding_needs(self) -> None:
        tdd = self.write_tdd(DOC_BODY)
        state = Path(controller.init_run(self.repo, self.feature, "run-52", 1000))
        controller.record_finding(state, "CRITIC", "CRITIC-001", "High", "§3.4", "证据", None, 0)
        controller.doc_index(state, tdd)
        # A finding stated in 3.4 is consumed by 7.2, so both are handed over together.
        out = controller.locate(state, tdd, finding_id="CRITIC-001")
        self.assertEqual(out["status"], "OK")
        self.assertEqual(out["resolved_key"], "3.4")
        self.assertEqual([item["key"] for item in out["sections"]], ["3.4", "7.2"])
        self.assertEqual(out["budget_tokens"], 500)
        self.assertFalse(out["over_budget"])
        self.assertTrue(out["cached"])
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.locate(state, tdd, finding_id="CRITIC-404")
        self.assertIn("unknown finding id", str(ctx.exception))
        # Chapter 15 alone is bigger than this run's whole budget.
        self.assertTrue(controller.locate(state, tdd, section="15")["over_budget"])
        # The echo is bounded: 8.1 declares 14 tables and the full list stays in the index file,
        # because a lookup that prints more than the grep it replaces costs the wave its point.
        many = controller.locate(state, tdd, section="8.1")
        self.assertEqual(many["symbols_total"], 14)
        self.assertEqual(len(many["symbols"]), controller.LOCATE_LIST_LIMIT)
        stored = next(i for i in self.cached_index(state)["sections"] if i["key"] == "8.1")
        self.assertEqual(len(stored["symbols"]), 14)

    def test_locate_says_so_instead_of_guessing(self) -> None:
        self.write_tdd(DOC_BODY)
        state = Path(controller.init_run(self.repo, self.feature, "run-53"))
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "Medium", "未编号的段落", "证据", None, 0
        )
        out = controller.locate(state, None, finding_id="CRITIC-001")
        self.assertEqual(out["status"], "NOT_LOCATED")
        self.assertEqual(out["fallback"], controller.LOCATE_FALLBACK)
        self.assertEqual(out["sections"], [])
        # locate builds the index itself when the run has none, and --tdd-file defaults to
        # the path init froze, so a dispatch needs one short command to get its line ranges.
        self.assertTrue((state.parent / controller.DOC_INDEX_FILE).is_file())
        for command, expected in (
            (["index"], "OK"),
            (["locate", "--finding", "CRITIC-001"], "NOT_LOCATED"),
        ):
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), *command, "--state", str(state)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], expected)

    def test_scope_budget_is_measured_from_the_index(self) -> None:
        # CJK and ASCII are priced apart: one flat divisor underestimates a prose section and
        # overestimates a DDL section, and both errors let a too-large scope pass as safe.
        self.assertEqual(controller._estimate_tokens("风" * 100), 120)
        self.assertEqual(controller._estimate_tokens("a" * 100), 25)
        tdd = self.write_tdd(DOC_BODY)
        state = Path(controller.init_run(self.repo, self.feature, "run-54", 1000))
        entry = controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-1", "sha256:in", "DISPATCHED",
            "7.2,3.4 7.2",
        )
        budget = entry["budget"]
        self.assertEqual(budget["sections"], ["7.2", "3.4"])
        index = self.cached_index(state)
        by_key = {item["key"]: item for item in index["sections"]}
        self.assertEqual(
            budget["read_chars"], by_key["7.2"]["chars"] + by_key["3.4"]["chars"]
        )
        self.assertFalse(budget["over_budget"])
        wide = controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-2", "sha256:in", "DISPATCHED", "15"
        )
        self.assertTrue(wide["budget"]["over_budget"])
        self.assertEqual(
            wide["budget"]["ranges"],
            [{"key": "15", "start": by_key["15"]["start"], "end": by_key["15"]["end"]}],
        )
        summary = controller.show(state)
        self.assertEqual(summary["dispatches"]["scoped"], 2)
        self.assertEqual(len(summary["dispatches"]["over_budget"]), 1)
        self.assertEqual(summary["doc_index"]["sections"], index["section_count"])
        # An unknown key is an error, not a section silently dropped from the injection.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-refiner", "tech-refiner", "s-refine-3", "sha256:in", "DISPATCHED", "9.9"
            )
        self.assertIn("unknown section key", str(ctx.exception))

    def test_scope_needs_the_document_and_opens_with_the_dispatch(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-55"))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-architect", "tech-architect", "s-arch-1", "sha256:in", "DISPATCHED", "7.2"
            )
        self.assertIn("needs the TDD", str(ctx.exception))
        self.write_tdd(DOC_BODY)
        controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-1", "sha256:in", "DISPATCHED", "7.2"
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-refiner", "tech-refiner", "s-refine-1", "", "COMPLETED", "7.2,15"
            )
        self.assertIn("belongs on the call that opens", str(ctx.exception))
        closed = controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-1", "", "COMPLETED"
        )
        self.assertEqual(closed["budget"]["sections"], ["7.2"])

    def test_a_scoped_refiner_may_only_dispose_of_what_it_was_handed(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-56"))
        self.write_tdd(DOC_BODY)
        self.to_refiner(state)
        controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-2", "sha256:in", "COMPLETED", "7.2"
        )
        controller.record_finding(state, "CRITIC", "CRITIC-001", "Medium", "7.2", "证据", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-002", "Medium", "15", "证据", None, 0)
        controller.record_fix(state, "CRITIC-001", "FIXED", "7.2", "补唯一索引")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "CRITIC-002", "FIXED", "15.1", "补退避")
        self.assertIn("outside every --scope", str(ctx.exception))
        # Splitting the round into a second scoped dispatch widens what may be disposed of: the
        # union covers 7.2 and 15, so the fix belongs to whichever dispatch was handed it.
        controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-3", "sha256:in", "COMPLETED", "15"
        )
        controller.record_fix(state, "CRITIC-002", "FIXED", "15.1", "补退避")
        # A location no heading resolves to stays disposable: the guard must not invent a scope.
        controller.record_finding(
            state, "CRITIC", "CRITIC-003", "Medium", "未编号的段落", "证据", None, 0
        )
        controller.record_fix(state, "CRITIC-003", "REJECTED", "未编号的段落", "不改")

    def test_a_scope_window_is_the_round_it_was_declared_in(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-58"))
        self.write_tdd(DOC_BODY)
        self.to_refiner(state)
        controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-1", "sha256:in", "COMPLETED", "7.2"
        )
        controller.start_stage(state, "REVIEW")
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮报告：范围外仍有改动。\n")
        controller.record_review(
            state, "REQUEST_CHANGES", report["path"], self.feature / "technical-doc.md", "s-review-1"
        )
        controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "7.2", "证据", None, None)
        controller.start_stage(state, "REFINER")
        controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-2", "sha256:in", "COMPLETED", "15"
        )
        self.artifact(state, "refiner-map", "第 2 轮变更映射：只注入了 15 章。\n")
        # Round 1 was handed 7.2; that does not license editing it two rounds later without
        # re-reading it, which is how a fix lands on text the role no longer has.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "REVIEW-1-001", "FIXED", "7.2", "沿用上一轮的认知")
        self.assertIn("outside every --scope", str(ctx.exception))
        # The bypass this closes: naming an in-scope section while the finding itself sits
        # outside is still a disposition written without ever reading the original text.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "REVIEW-1-001", "FIXED", "15.2", "范围内顺手改")
        self.assertIn("finding location", str(ctx.exception))
        # Widening the round with a scoped dispatch for 7.2 makes the same fix legal.
        controller.record_agent(
            state, "tech-refiner", "tech-refiner", "s-refine-3", "sha256:in", "COMPLETED", "7.2"
        )
        controller.record_fix(state, "REVIEW-1-001", "FIXED", "15.2", "本轮范围内改")

    def test_render_ledger_leaves_the_index_fresh(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-57"))
        tdd = self.write_tdd(DOC_BODY)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "High", "7.2", "证据", None, 0)
        controller.doc_index(state, tdd)
        report = controller.render_ledger(state, tdd)
        self.assertEqual(report["doc_index"]["unlocated_total"], 0)
        index = self.cached_index(state)
        self.assertEqual(index["reason"], "render-ledger")
        lines = tdd.read_text(encoding="utf-8").splitlines()
        chapter = next(item for item in index["sections"] if item["key"] == "16")
        self.assertTrue(lines[chapter["start"] - 1].startswith("## 16"))
        body = "\n".join(lines[chapter["start"] : chapter["end"]])
        self.assertIn("ledger:findings:start", body)
        # cached=True proves the index on disk already matched the bytes the render wrote.
        self.assertTrue(controller.locate(state, tdd, finding_id="CRITIC-001")["cached"])

    # --------------------------------------- severity-gated stop and incremental final review

    def quiet_round(self, state: Path, round_: int, next_scope: str | None = None) -> dict[str, Any]:
        """Approve with one Low follow-up, dispose of it, then open the next round.

        This is the shape rounds 2-5 took in the reference run: no Blocker/High, and a round that
        raised nothing at all could not have continued the loop in the first place.
        """
        self.record(state, "tech-reviewer", f"s-review-{round_}")
        report = self.artifact(state, "review-report", f"第 {round_} 轮独立终审报告：无阻塞项。\n")
        controller.record_review(
            state, "APPROVE_WITH_FOLLOW_UPS", report["path"],
            self.feature / "technical-doc.md", f"s-review-{round_}",
        )
        controller.record_finding(
            state, "REVIEWER", f"REVIEW-{round_}-001", "Low", "12", "大盘少一个分维度面板", None, None
        )
        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", f"s-refine-{round_}")
        self.artifact(state, "refiner-map", f"第 {round_} 轮处置映射已记录。\n")
        controller.record_fix(
            state, f"REVIEW-{round_}-001", "FIXED", "12", "补分维度面板")
        return controller.start_stage(state, "REVIEW", next_scope)

    def after_quiet_rounds(
        self, run_id: str, rounds: int = 2, policy: str = "severity"
    ) -> tuple[Path, dict[str, Any]]:
        """Drive the run to the round opened after ``rounds`` quiet verdicts."""
        state = Path(controller.init_run(self.repo, self.feature, run_id, None, policy))
        self.write_tdd()
        self.to_review(state)
        opened: dict[str, Any] = {}
        for round_ in range(1, rounds + 1):
            opened = self.quiet_round(state, round_)
        return state, opened

    def test_two_quiet_rounds_scope_the_next_review(self) -> None:
        state, opened = self.after_quiet_rounds("run-70")
        self.assertEqual(opened["review_round"], 3)
        self.assertEqual(opened["review_scope"], "incremental")
        # The Low from round 2 sits in chapter 12, so that is the section the reviewer is handed.
        self.assertIn("12", opened["sections"])
        self.assertFalse(opened["over_budget"])
        pending = json.loads(state.read_text(encoding="utf-8"))["pending_review"]
        self.assertEqual(pending["baseline_round"], 2)
        self.assertTrue(pending["severity_stop"]["forced"])

    def test_a_scoped_round_may_not_be_asked_before_the_stop(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-71"))
        self.write_tdd()
        self.to_review(state)
        with self.assertRaises(controller.ControllerError) as ctx:
            self.quiet_round(state, 1, "incremental")
        self.assertIn("may not be scoped yet", str(ctx.exception))

    def test_a_quiet_run_may_not_insist_on_a_full_round(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-71b"))
        self.write_tdd()
        self.to_review(state)
        self.quiet_round(state, 1)
        with self.assertRaises(controller.ControllerError) as ctx:
            self.quiet_round(state, 2, "full")
        self.assertIn("omit --review-scope", str(ctx.exception))
        self.assertIn("--review-stop-policy fixed", str(ctx.exception))

    def test_an_open_must_fix_keeps_the_review_full(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-72"))
        self.write_tdd()
        self.to_review(state)
        for round_ in (1, 2):
            self.record(state, "tech-reviewer", f"s-review-{round_}")
            report = self.artifact(state, "review-report", f"第 {round_} 轮报告：高危未闭环。\n")
            controller.record_review(
                state, "REQUEST_CHANGES", report["path"],
                self.feature / "technical-doc.md", f"s-review-{round_}",
            )
            controller.record_finding(
                state, "REVIEWER", f"REVIEW-{round_}-001", "High", "13", "无告警规则", None, None
            )
            controller.start_stage(state, "REFINER")
            self.record(state, "tech-refiner", f"s-refine-{round_}")
            self.artifact(state, "refiner-map", f"第 {round_} 轮处置映射已记录。\n")
            opened = controller.start_stage(state, "REVIEW")
        self.assertEqual(opened["review_round"], 3)
        self.assertEqual(opened["review_scope"], "full")
        self.assertNotIn("sections", opened)
        stop = json.loads(state.read_text(encoding="utf-8"))["pending_review"]["severity_stop"]
        self.assertFalse(stop["quiet"])
        self.assertEqual(stop["open_must_fix"], ["REVIEW-1-001", "REVIEW-2-001"])

    def test_a_fixed_policy_never_scopes_a_round(self) -> None:
        state, opened = self.after_quiet_rounds("run-73", policy="fixed")
        self.assertEqual(opened["review_scope"], "full")
        stop = json.loads(state.read_text(encoding="utf-8"))["pending_review"]["severity_stop"]
        self.assertTrue(stop["quiet"])
        self.assertFalse(stop["forced"])

    def test_a_verdict_may_not_relabel_its_round(self) -> None:
        state, _ = self.after_quiet_rounds("run-74")
        self.record(state, "tech-reviewer", "s-review-3")
        report = self.artifact(state, "review-report", "第 3 轮增量终审报告。\n")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_review(
                state, "APPROVE", report["path"], self.feature / "technical-doc.md",
                "s-review-3", "full",
            )
        self.assertIn("re-label", str(ctx.exception))

    def test_a_scoped_round_that_saw_every_change_still_delivers(self) -> None:
        state, _ = self.after_quiet_rounds("run-75")
        self.approve_round(state, "s-review-3")
        tdd = self.feature / "technical-doc.md"
        self.render(state)
        result = controller.gate(state, tdd)
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertIn("incremental-review-covers-changes", result["passed"])

    def test_gate_proves_a_scoped_round_saw_every_change(self) -> None:
        state, opened = self.after_quiet_rounds("run-76")
        # An edit outside the handed sections, landed before the verdict is bound: the
        # whole-document digest matches, so only the per-section diff can show the gap.
        self.write_tdd(
            TDD_BODY.replace(
                "工作量：备注同步模块 3 人日，依赖工单接口联调。",
                "工作量：备注同步模块 8 人日，其中数据迁移 5 人日，依赖工单接口联调。",
            )
        )
        self.assertIn("12", opened["sections"])
        self.approve_round(state, "s-review-3")
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertIn("incremental-review-covers-changes", result["failures"])
        detail = next(item for item in result["checks"] if item["name"] == "incremental-review-covers-changes")
        self.assertIn("15", str(detail["detail"]))

    def test_a_deleted_top_level_chapter_is_not_silently_covered(self) -> None:
        """A section nobody can re-read any more still has to appear in some later scope."""
        state, opened = self.after_quiet_rounds("run-79")
        tdd = self.feature / "technical-doc.md"
        text = tdd.read_text(encoding="utf-8")
        start = text.index("## 15. 里程碑规划")
        end = text.index("## 16. 审查闭环记录")
        tdd.write_text(text[:start] + text[end:], encoding="utf-8")
        self.assertIn("12", opened["sections"])
        self.approve_round(state, "s-review-3")
        self.render(state)
        result = controller.gate(state, tdd)
        self.assertIn("incremental-review-covers-changes", result["failures"])
        detail = next(item for item in result["checks"] if item["name"] == "incremental-review-covers-changes")
        self.assertIn("15", str(detail["detail"]))

    def test_a_final_round_followup_closes_without_an_extra_refiner_round(self) -> None:
        """The deadlock shape: round 5 approves with Low follow-ups and nowhere left to refine."""
        state, _ = self.after_quiet_rounds("run-78", 4)
        self.record(state, "tech-reviewer", "s-review-5")
        report = self.artifact(state, "review-report", "第 5 轮增量终审报告：无阻塞项，两个 follow-up。\n")
        controller.record_review(
            state, "APPROVE_WITH_FOLLOW_UPS", report["path"],
            self.feature / "technical-doc.md", "s-review-5",
        )
        controller.record_finding(
            state, "REVIEWER", "REVIEW-5-001", "Low", "12", "大盘少一个分维度面板", None, None
        )
        # FIXED claims a text change: after a verdict that still needs its own round.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "REVIEW-5-001", "FIXED", "12", "补面板")
        self.assertIn("Refiner round", str(ctx.exception))
        # Delivery is refused while the ledger is unfinished — and it says which finding.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "DELIVERY")
        self.assertIn("REVIEW-5-001", str(ctx.exception))
        # Older rounds keep the Refiner-only rule; the exception is scoped to this round.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "REVIEW-1-001", "REJECTED", "12", "重新论证")
        self.assertIn("reviewer round 1", str(ctx.exception))
        # A disposition must answer the section the finding was raised against.
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "REVIEW-5-001", "REJECTED", "13", "不改")
        self.assertIn("must answer that finding", str(ctx.exception))
        controller.record_fix(state, "REVIEW-5-001", "REJECTED", "12", "维持现状")
        controller.start_stage(state, "DELIVERY")
        # The window stays open from DELIVERY too, so a late non-blocking finding cannot strand it.
        controller.record_finding(
            state, "REVIEWER", "REVIEW-5-002", "Low", "13", "告警阈值未路由", None, None
        )
        controller.record_fix(
            state, "REVIEW-5-002", "PENDING_CONFIRMATION", "13", "保留待确认")
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PASS", result["failures"])
        controller.start_stage(state, "CLOSED")

    # ------------------------------------------------------------------- scale and greenfield

    def write_prd(self, text: str) -> Path:
        path = self.feature / "product-doc.md"
        path.write_text(text, encoding="utf-8")
        return path

    def probe(self, run_id: str, prd: str) -> dict[str, Any]:
        """Run the input gate for ``prd`` and return the scale probe it recorded."""
        self.write_prd(prd)
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "需求已读取", "HIGH",
        )
        out = controller.finalize_inputs(state, "PASS", "全部关键资料已读取")
        data = json.loads(state.read_text(encoding="utf-8"))
        return {**data["scale"], "reported": out["scale"]}

    def test_scale_probe_grades_prd_signals(self) -> None:
        quiet = self.probe("run-80", PRD)
        self.assertEqual(quiet["scale"], "S")
        self.assertEqual(quiet["signals"], {})

        structural = self.probe("run-81", PRD_STRUCTURAL)
        self.assertEqual(structural["scale"], "L")
        self.assertEqual(
            sorted(structural["signals"]), ["api", "data", "message", "scheduled"]
        )

    def test_scale_probe_marks_a_greenfield_repo_from_the_files_it_found(self) -> None:
        probe = self.probe("run-82", PRD)
        self.assertTrue(probe["greenfield"], probe["code_scan"])
        self.assertEqual(probe["backend_code_files"], 0)

        (self.repo / "app.py").write_text("print('service')\n", encoding="utf-8")
        with_code = self.probe("run-83", PRD)
        self.assertFalse(with_code["greenfield"])
        self.assertEqual(with_code["backend_code_files"], 1)

    def test_scale_probe_refuses_a_prd_that_drifted_after_init(self) -> None:
        """Designing against bytes the run never froze is exactly what the probe exists to stop."""
        state = Path(controller.init_run(self.repo, self.feature, "run-83b"))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "需求已读取", "HIGH",
        )
        self.write_prd(PRD + "\n## 功能需求\n运营希望导出接口增加可配置的限流阈值。\n")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.finalize_inputs(state, "PASS", "全部关键资料已读取")
        self.assertIn("changed after init", str(ctx.exception))

    def test_scale_gate_demands_the_chapters_the_probe_flagged(self) -> None:
        self.write_prd(PRD_STRUCTURAL)
        _, result = self.gate_for("run-84")
        self.assertIn("scale-chapters", result["failures"])
        detail = str(next(item for item in result["checks"] if item["name"] == "scale-chapters"))
        for chapter in ("数据库设计", "接口与契约设计", "消息设计", "定时任务设计", "部署拓扑"):
            self.assertIn(chapter, detail)

    def test_scale_gate_accepts_a_document_that_meets_its_scale(self) -> None:
        self.write_prd(PRD_ONE_SIGNAL)
        _, result = self.gate_for("run-85", M_TDD_BODY)
        self.assertEqual(result["status"], "PASS", result["failures"])
        detail = next(item for item in result["checks"] if item["name"] == "scale-chapters")
        self.assertIn("scale=M", str(detail["detail"]))

    def test_not_applicable_escape_is_not_substance_at_M(self) -> None:
        self.write_prd(PRD_ONE_SIGNAL)
        escaped = M_TDD_BODY.replace(
            "备注同步成功率、P95 同步耗时、重试次数三项指标进现有订单大盘，按服务与错误码分维度。\n" + CHAPTER_FILL,
            "不涉及，本服务沿用基础大盘即可。",
        )
        self.assertNotEqual(escaped, M_TDD_BODY)
        _, result = self.gate_for("run-86", escaped)
        self.assertIn("scale-chapters", result["failures"])
        detail = str(next(item for item in result["checks"] if item["name"] == "scale-chapters"))
        self.assertIn("不涉及", detail)
        self.assertIn("可观测设计", detail)

    def test_a_reasoned_not_applicable_keeps_a_conditional_chapter_legal_at_M(self) -> None:
        """The probe flags a class; the template still lets a 条件必填 chapter answer 不涉及."""
        self.write_prd(PRD_ONE_SIGNAL)
        reasoned = M_TDD_BODY.replace(
            "## 6. 接口与契约设计\n" + CHAPTER_FILL,
            "## 6. 接口与契约设计\n本方案不新增对外接口，原因与替代路径如下。\n" + CHAPTER_FILL,
        )
        self.assertNotEqual(reasoned, M_TDD_BODY)
        _, result = self.gate_for("run-86b", reasoned)
        self.assertEqual(result["status"], "PASS", result["failures"])

    def test_a_bare_not_applicable_in_a_conditional_chapter_still_blocks(self) -> None:
        """An escape without an argument is the silence the gate exists to catch."""
        self.write_prd(PRD_ONE_SIGNAL)
        bare = M_TDD_BODY.replace(
            "## 6. 接口与契约设计\n" + CHAPTER_FILL,
            "## 6. 接口与契约设计\n不涉及。\n",
        )
        _, result = self.gate_for("run-86c", bare)
        self.assertIn("scale-chapters", result["failures"])
        detail = str(next(item for item in result["checks"] if item["name"] == "scale-chapters"))
        self.assertIn("没给原因", detail)

    def test_record_only_enforcement_reports_without_blocking(self) -> None:
        self.write_prd(PRD_STRUCTURAL)
        state = Path(controller.init_run(
            self.repo, self.feature, "run-87", None, "severity", "record-only"
        ))
        self.to_review(state)
        self.write_tdd()
        self.approve_round(state)
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertEqual(result["status"], "PASS", result["failures"])
        self.assertIn("探针仅记录", str(result["checks"]))

    def test_run_metadata_ledger_reports_the_scale_decision(self) -> None:
        self.write_prd(PRD_STRUCTURAL)
        state, _ = self.gate_for("run-88")
        text = (self.feature / "technical-doc.md").read_text(encoding="utf-8")
        self.assertIn("| 规模判定 | L 级", text)
        self.assertIn("绿地仓库", text)
        self.assertIn("r1 full", text)

    def test_cli_carries_the_rollback_switches(self) -> None:
        """Both W4 rollback flags have to be reachable from the command the orchestrator runs."""
        self.write_prd(PRD_STRUCTURAL)
        state = self.repo / ".codekit" / "technical-doc" / "run-89" / "state.json"
        out = subprocess.run(
            [sys.executable, str(MODULE_PATH), "init", "--repo-root", str(self.repo),
             "--feature-directory", str(self.feature), "--run-id", "run-89",
             "--review-stop-policy", "fixed", "--scale-enforcement", "record-only"],
            capture_output=True, text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), [f"STATE={state}"])
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["review_stop_policy"], "fixed")
        self.assertEqual(data["scale_enforcement"], "record-only")

        done = subprocess.run(
            [sys.executable, str(MODULE_PATH), "show", "--state", str(state)],
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads(done.stdout)
        self.assertEqual(summary["review_stop"], {"policy": "fixed", "forced": False, "quiet": False,
                                                 "quiet_rounds": [], "open_must_fix": []})
        self.assertIsNone(summary["scale"])


if __name__ == "__main__":
    unittest.main()
