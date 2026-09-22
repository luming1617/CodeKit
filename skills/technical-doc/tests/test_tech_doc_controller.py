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

## 9. 工程与模块
仓库除本目录文档外没有代码，按新建工程初始化：

```
order-note-sync/
├── src/main/java/com/example/ordernote/api/
├── src/main/java/com/example/ordernote/service/
├── src/main/java/com/example/ordernote/repository/
└── src/test/java/com/example/ordernote/
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

# The same draft with ``n`` extra body lines spliced into chapter 13, i.e. what a Refiner round
# leaves behind when it answers a Medium finding by adding prose: written where prose belongs, so
# the growth check is measured against a document that is still template-clean.
def grown_body(lines: int) -> str:
    filler = "重试窗口取 30 秒，退避上限 5 次，幂等键由订单号与版本号拼接。\n" * lines
    return TDD_BODY.replace("## 15. 里程碑规划", filler + "## 15. 里程碑规划")


# A disposition table copied into the body beside the generated ledger -- the shape
# ``_reject_handwritten_mirror`` exists to refuse, and the three tests below check that it is
# refused while the Refiner can still fix it rather than at delivery.
MIRROR_ROWS = "\n| Finding | 处置 |\n|---|---|\n| CRITIC-001 | 已补退避 |\n"

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

    def to_architect(self, state: Path, prd_changes: str | None = None) -> None:
        """INPUT -> ARCHITECT: inputs frozen, context pack persisted, architect completed."""
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

    def to_refiner(self, state: Path, prd_changes: str | None = None) -> None:
        """Drive INPUT -> REFINER with every delegated agent and artifact logged."""
        self.to_architect(state, prd_changes)
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

    def cli(self, *args: str, state: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run one subcommand the way the orchestrator does: argv, pipes, exit code."""
        argv = [sys.executable, str(MODULE_PATH), *args]
        if state is not None:
            argv += ["--state", str(state)]
        return subprocess.run(argv, capture_output=True, text=True)

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

    # ------------------------------------------------------- polish follow-ups

    def test_show_splits_findings_by_what_they_cost(self) -> None:
        """Three lists, so the orchestrator copies a verdict instead of recomputing one.

        The reference run spent two review rounds (~100 min) prose-editing findings that only
        needed a disposition; ``undisposed`` is also the replay subset after an aborted dispatch.
        """
        state = Path(controller.init_run(self.repo, self.feature, "run-100"))
        self.to_refiner(state)
        controller.record_finding(state, "CRITIC", "CRITIC-001", "High", "4.2", "缺退避", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-002", "Medium", "6.1", "缺索引", None, 0)
        controller.record_finding(state, "CRITIC", "CRITIC-003", "Medium", "7.2", "口径歧义", None, 0)
        controller.record_fix(state, "CRITIC-003", "REJECTED", "7.2", "不改，PRD 已定义口径")

        findings = controller.show(state)["findings"]
        self.assertEqual(["CRITIC-001", "CRITIC-002"], findings["undisposed"])
        self.assertEqual(["CRITIC-002"], findings["open_non_blocking_followups"])
        self.assertEqual(["CRITIC-001"], findings["undisposed_blocking"])

    def test_opening_a_round_reports_the_debts_that_need_no_prose(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-101"))
        self.write_tdd()
        self.to_review(state)
        controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "4.2", "缺退避", None, None)
        controller.record_finding(
            state, "REVIEWER", "REVIEW-1-002", "Medium", "7.2", "面板少一个维度", None, None
        )
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮报告：修订重试策略。\n")
        controller.record_review(
            state, "REQUEST_CHANGES", report["path"], self.feature / "technical-doc.md", "s-review-1"
        )
        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine-2")
        self.artifact(state, "refiner-map", "第 2 轮变更映射：REVIEW-1-001 已处置。\n")
        controller.record_fix(state, "REVIEW-1-001", "FIXED", "4.2", "补指数退避")

        result = controller.start_stage(state, "REVIEW")
        self.assertEqual(["REVIEW-1-002"], result["followups"]["open_non_blocking_followups"])
        self.assertIn("record-fix", result["followup_hint"])

    def test_the_shortcut_is_silent_while_a_blocker_is_open(self) -> None:
        """A round opened for a Blocker/High carries no disposition-only suggestion at all."""
        state = Path(controller.init_run(self.repo, self.feature, "run-102"))
        self.write_tdd()
        self.to_review(state)
        controller.record_finding(state, "REVIEWER", "REVIEW-1-001", "High", "4.2", "缺退避", None, None)
        self.record(state, "tech-reviewer", "s-review-1")
        report = self.artifact(state, "review-report", "第 1 轮报告：修订重试策略。\n")
        controller.record_review(
            state, "REQUEST_CHANGES", report["path"], self.feature / "technical-doc.md", "s-review-1"
        )
        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine-2")
        self.artifact(state, "refiner-map", "第 2 轮变更映射：待登记。\n")

        result = controller.start_stage(state, "REVIEW")
        self.assertNotIn("followups", result)
        self.assertNotIn("followup_hint", result)

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
        # a marker line is not a template placeholder, but a marker with no ledger entry
        # behind it is exactly what the delivery reconciliation refuses to deliver on
        _, result = self.gate_for(
            "run-13", TDD_BODY + "\n[需人工确认: 下游工单接口的限流阈值]\n"
        )
        self.assertIn("no-template-placeholders", result["passed"])
        self.assertIn("markers-match-confirmations", result["failures"])

    def test_a_fenced_marker_excuses_nothing(self) -> None:
        # sample text inside a code fence is invisible to the marker rules in both
        # directions, so it cannot be used to park a placeholder past the gate
        _, result = self.gate_for(
            "run-13a",
            TDD_BODY + "\n```text\n[需人工确认: 围栏内示例] 【填写】\n```\n",
        )
        self.assertIn("no-template-placeholders", result["failures"])
        self.assertIn("markers-match-confirmations", result["passed"])

    def test_data_literals_written_as_code_are_not_placeholders(self) -> None:
        # The reference run lost a delivery round here: a status enum in a DDL and the date
        # format itself are data, and the gate read them as unfilled slots.
        body = TDD_BODY.replace(
            "## 16. 审查闭环记录",
            "## 8. 数据库设计\n"
            "| 字段 | 说明 |\n|------|------|\n"
            "| prep_status | 取值 `todo` / `doing` / `done` |\n\n"
            "```sql\n"
            "CREATE TABLE prep_task (\n"
            "    prep_status TEXT NOT NULL DEFAULT 'todo',\n"
            "    task_date   TEXT NOT NULL,  -- 日期按自然日 YYYY-MM-DD\n"
            ");\n"
            "```\n\n"
            "## 16. 审查闭环记录",
        )
        _, result = self.gate_for("run-13c", body)
        self.assertIn("no-template-placeholders", result["passed"], result["failures"])

    def test_a_value_literal_left_in_prose_is_still_a_placeholder(self) -> None:
        # exemption follows the code convention, not the token: a copied template date row that
        # never got its real date stays an unfilled slot wherever it sits in the hand-written body
        _, result = self.gate_for(
            "run-13d", TDD_BODY + "\n## 人工确认\n| 确认日期 | YYYY-MM-DD |\n"
        )
        self.assertIn("no-template-placeholders", result["failures"])

    def test_english_prose_connectives_in_code_are_not_slots(self) -> None:
        """``refer to``/``similar to`` are connectives, not fill-in instructions.

        Filed as slot tokens they were unforgivable anywhere, including inside a fence, so a
        DDL or YAML comment pointing at another section was refused as an unfilled template.
        They stay placeholders where the document speaks prose.
        """
        body = TDD_BODY.replace(
            "## 16. 审查闭环记录",
            "## 8. 数据库设计\n"
            "```sql\n"
            "-- refer to section 3 for the retention rule\n"
            "CREATE TABLE prep_task (\n"
            "    prep_status TEXT NOT NULL DEFAULT 'todo',  -- similar to the card list\n"
            ");\n"
            "```\n\n## 16. 审查闭环记录",
        )
        _, result = self.gate_for("run-13e-1", body)
        self.assertIn("no-template-placeholders", result["passed"], result["failures"])

    def test_the_same_connectives_left_in_prose_are_still_placeholders(self) -> None:
        # the exemption follows the code convention, not the token
        _, result = self.gate_for(
            "run-13e-2", TDD_BODY + "\n## 人工确认\n本表口径 refer to 第 3 章。\n"
        )
        self.assertIn("no-template-placeholders", result["failures"])

    def test_text_the_ledger_echoed_from_state_is_not_a_placeholder(self) -> None:
        """render-ledger copies state.json into chapter 16, so the gate cannot fail on that copy.

        A disposition quoting the slot it removed ('把 待补充 换成…') reaches the generated block
        verbatim; blocking there would fault the controller for writing what it was told.
        """
        state = Path(controller.init_run(self.repo, self.feature, "run-13e"))
        self.write_tdd()
        self.to_refiner(state)
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "Medium", "4.2", "日期列仍是模板格式", None, 0
        )
        controller.record_fix(
            state, "CRITIC-001", "FIXED", "4.2", "把待补充与 YYYY-MM-DD 两格换成实际值"
        )
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        self.render(state)
        result = controller.gate(state, self.feature / "technical-doc.md")
        self.assertIn("no-template-placeholders", result["passed"], result["failures"])

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
        self.assertEqual(first["rendered"], ["findings", "run-metadata", "confirmations"])
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(text, tdd.read_text(encoding="utf-8"))
        # one marker pair per section, in chapter order, below the chapter 16 heading
        self.assertEqual(text.count("<!-- codekit:technical-doc:ledger:"), 6)
        chapter = text.index("## 16. 审查闭环记录")
        findings = text.index("ledger:findings:start")
        metadata = text.index("ledger:run-metadata:start")
        confirmations = text.index("ledger:confirmations:start")
        self.assertTrue(
            chapter < findings < metadata < confirmations,
            (chapter, findings, metadata, confirmations),
        )
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

    # ---------------------------------------- PR-5: 镜像检测前移，体积增幅进交付报告

    def test_a_copied_ledger_is_refused_the_moment_the_draft_is_registered(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-mirror-1"))
        self.to_architect(state)
        self.write_tdd(TDD_BODY + MIRROR_ROWS)
        with self.assertRaises(controller.ControllerError) as ctx:
            self.artifact(state, "draft")
        self.assertIn("hand-written Finding table", str(ctx.exception))
        # 产物没落盘，阶段也就停在 ARCHITECT —— 拒绝发生在写下它的那一步，不是交付当天
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "CRITIC")
        self.assertIn("draft", str(ctx.exception))
        self.write_tdd(TDD_BODY)
        self.assertEqual("draft", self.artifact(state, "draft")["kind"])

    def test_a_copied_ledger_is_refused_on_the_first_disposition(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-mirror-2"))
        self.write_tdd()
        self.to_refiner(state)
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "High", "9.3", "缺退避与压测口径", None, 0
        )
        self.write_tdd(TDD_BODY + MIRROR_ROWS)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "CRITIC-001", "FIXED", "9.3", "补 30 秒退避窗口")
        self.assertIn("hand-written Finding table", str(ctx.exception))
        # 被拒的这条处置没有进 state：半本台账留在原地才是更难查的状态
        self.assertEqual([], json.loads(state.read_text(encoding="utf-8"))["fixes"])
        self.write_tdd(TDD_BODY)
        controller.record_fix(state, "CRITIC-001", "FIXED", "9.3", "补 30 秒退避窗口")

    def test_a_traceability_row_that_cites_a_finding_is_not_a_mirror(self) -> None:
        """Chapter 16 owns the ledger; another chapter may still cite a finding id.

        The predicate used to refuse any row carrying ``CRITIC-\\d``, which blocked the
        cross-reference the SKILL asks the author to write -- and with ``record-fix`` refused,
        the only way out was editing the body, which ``review-report-matches-delivered-tdd``
        then blocks.
        """
        state = Path(controller.init_run(self.repo, self.feature, "run-mirror-2b"))
        self.write_tdd()
        self.to_refiner(state)
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "High", "9.3", "缺退避与压测口径", None, 0
        )
        self.write_tdd(
            TDD_BODY + "\n## 关键技术决策\n\n| 决策 | 依据 |\n|---|---|\n| 重试退避 | CRITIC-001 |\n"
        )
        controller.record_fix(state, "CRITIC-001", "FIXED", "9.3", "补 30 秒退避窗口")
        # the same id under the ledger's own columns is still a second copy of the table
        self.write_tdd(TDD_BODY + MIRROR_ROWS)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_fix(state, "CRITIC-001", "FIXED", "9.3", "补 30 秒退避窗口")
        self.assertIn("hand-written Finding table", str(ctx.exception))

    def test_the_batch_refuses_a_copied_ledger_once_not_once_per_item(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-mirror-3"))
        self.write_tdd()
        self.to_refiner(state)
        for finding_id, location in (("CRITIC-001", "9.3"), ("CRITIC-002", "7.2")):
            controller.record_finding(
                state, "CRITIC", finding_id, "Medium", location, "证据摘要", None, 0
            )
        self.write_tdd(TDD_BODY + MIRROR_ROWS)
        report = self.repo / "refiner-map.md"
        report.write_text(
            "@@FIX id=CRITIC-001 status=FIXED loc=9.3\n补写退避与压测口径。\n"
            "@@FIX id=CRITIC-002 status=FIXED loc=7.2\n补写索引与查询口径。\n",
            encoding="utf-8",
        )
        result = self.cli("record-fixes", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("hand-written Finding table", result.stderr)
        # 一条问题一次拒绝：两份 failed 会把人的注意力从同一个删除动作上引开
        self.assertEqual("", result.stdout)
        self.assertEqual([], json.loads(state.read_text(encoding="utf-8"))["fixes"])

    def delivered_with_growth(
        self, run_id: str, added: int, severity: str
    ) -> tuple[Path, dict[str, Any]]:
        """Deliver a TDD whose body the Refiner inflated by ``added`` lines over its draft."""
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        self.write_tdd()
        self.to_refiner(state)
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", severity, "9.3", "缺退避与压测口径", None, 0
        )
        self.write_tdd(grown_body(added))
        controller.record_fix(state, "CRITIC-001", "FIXED", "9.3", "补 30 秒退避窗口与压测口径")
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        self.render(state)
        return state, controller.gate(state, self.feature / "technical-doc.md")

    def growth_check(self, report: dict[str, Any]) -> dict[str, Any]:
        return next(item for item in report["checks"] if item["name"] == "document-growth")

    def test_growth_the_findings_did_not_pay_for_is_warned_without_blocking(self) -> None:
        _state, report = self.delivered_with_growth("run-grow-1", 8, "Medium")
        self.assertEqual(report["status"], "PASS", report["failures"])
        check = self.growth_check(report)
        self.assertTrue(check["warn"], check["detail"])
        self.assertIn("document-growth", [item["name"] for item in report["warnings"]])
        # 软检查：ok 恒为真，所以它进 passed 而不是 failures —— 长文档不是缺陷，是要人看见的账
        self.assertIn("document-growth", report["passed"])
        self.assertNotIn("document-growth", report["failures"])
        self.assertIn("none of them Blocker/High", str(check["detail"]))

    def test_a_blocking_finding_pays_for_the_same_growth(self) -> None:
        _state, report = self.delivered_with_growth("run-grow-2", 8, "High")
        self.assertEqual(report["status"], "PASS", report["failures"])
        check = self.growth_check(report)
        self.assertFalse(check["warn"])
        self.assertEqual([], report["warnings"])
        self.assertIn("pays for the growth", str(check["detail"]))

    def test_growth_with_no_finding_to_attribute_it_to_still_warns(self) -> None:
        """A round that raised nothing and still grew the body has no payer at all.

        ``all([])`` is True, which is the answer wanted here; gating on ``bool(drivers)`` let
        the clearest case of unattributed growth fall through as "nothing to attribute".
        """
        state = Path(controller.init_run(self.repo, self.feature, "run-grow-2b"))
        self.write_tdd()
        self.to_refiner(state)
        self.write_tdd(grown_body(8))
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        self.render(state)
        report = controller.gate(state, self.feature / "technical-doc.md")
        check = self.growth_check(report)
        self.assertTrue(check["warn"], check["detail"])
        self.assertIn("raised no finding at all", str(check["detail"]))
        self.assertIn("document-growth", [item["name"] for item in report["warnings"]])

    def test_growth_inside_the_threshold_is_recorded_without_a_warning(self) -> None:
        _state, report = self.delivered_with_growth("run-grow-3", 1, "Medium")
        self.assertEqual(report["status"], "PASS", report["failures"])
        check = self.growth_check(report)
        self.assertFalse(check["warn"])
        self.assertIn("threshold", str(check["detail"]))

    def test_the_reference_size_is_frozen_before_any_review_round_adds_to_it(self) -> None:
        state, report = self.delivered_with_growth("run-grow-4", 8, "Medium")
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual("draft", data["document_growth"]["baseline"])
        draft_lines = controller._body_lines(TDD_BODY)
        self.assertEqual(draft_lines, data["document_growth"]["baseline_lines"])
        # 正文涨了 8 行之后再比：参照值还是初稿那个数字，说明它只记一次、不会跟着滚
        self.assertIn(f"vs baseline {draft_lines}", str(self.growth_check(report)["detail"]))

    def test_an_update_run_measures_growth_against_the_version_it_is_rewriting(self) -> None:
        self.write_tdd(update=True)
        state = Path(controller.init_run(self.repo, self.feature, "run-grow-5"))
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual("previous-version", data["document_growth"]["baseline"])
        self.assertEqual(
            controller._body_lines(TDD_BODY + HISTORY_TAIL),
            data["document_growth"]["baseline_lines"],
        )

    def test_entering_the_critic_freezes_the_size_when_the_draft_landed_late(self) -> None:
        # Architect 先回报后落盘：write-artifact 时正文还不存在，start-stage --to CRITIC 补一次，
        # 否则这一场整份门禁报告里都不会有体积这个数字。
        state = Path(controller.init_run(self.repo, self.feature, "run-grow-6"))
        self.to_architect(state)
        self.artifact(state, "draft")
        self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["document_growth"])
        self.write_tdd()
        controller.start_stage(state, "CRITIC")
        self.assertEqual(
            "draft", json.loads(state.read_text(encoding="utf-8"))["document_growth"]["baseline"]
        )

    def test_a_run_with_no_reference_version_says_so_instead_of_reporting_zero_growth(self) -> None:
        _state, report = self.gate_for("run-grow-7")
        check = self.growth_check(report)
        self.assertFalse(check["warn"])
        self.assertIn("no reference version", str(check["detail"]))

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
        self.assertEqual(rendered["rendered"], ["findings", "run-metadata", "confirmations", "history-index"])
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

    def test_show_next_walks_the_run_without_inventing_or_omitting_a_step(self) -> None:
        """The named command is the one that then succeeds — at every stage, on a real run."""
        state = Path(controller.init_run(self.repo, self.feature, "run-119"))
        tdd = self.feature / "technical-doc.md"

        def waiting() -> dict[str, Any]:
            return controller.show(state, next_only=True)["next"]

        def expect(token: str) -> None:
            self.assertIn(token, waiting()["command"], waiting()["why"])

        expect("record-source")
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        expect("finalize-inputs")
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取")
        for stage, role, kind in (
            ("CONTEXT", "tech-context", "context-pack"),
            ("ARCHITECT", "tech-architect", "draft"),
            ("CRITIC", "tech-critic", "critic-findings"),
        ):
            expect(f"start-stage --to {stage}")
            controller.start_stage(state, stage)
            expect(f"--role {role}")
            self.record(state, role, f"s-{stage.lower()}")
            expect(f"--kind {kind}")
            self.artifact(state, kind)
        expect("record-findings")
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "Blocker", "4.2 主流程", "无退避", None, 0
        )
        expect("start-stage --to REFINER")
        controller.start_stage(state, "REFINER")
        expect("--role tech-refiner")
        self.record(state, "tech-refiner", "s-refine")
        expect("--kind refiner-map")
        self.artifact(state, "refiner-map")
        expect("record-fixes")
        controller.record_fix(state, "CRITIC-001", "FIXED", "4.2", "补指数退避")
        expect("start-stage --to REVIEW")
        controller.start_stage(state, "REVIEW")
        self.write_tdd()
        expect("--role tech-reviewer")
        self.record(state, "tech-reviewer", "s-review-1")
        expect("--kind review-report")
        report = self.artifact(state, "review-report")
        expect("record-review")
        controller.record_review(
            state, "APPROVE", report["path"], tdd, "s-review-1"
        )
        expect("start-stage --to DELIVERY")
        controller.start_stage(state, "DELIVERY")
        expect("render-ledger")
        controller.render_ledger(state, tdd)
        expect("gate")
        self.assertEqual(controller.gate(state, tdd)["status"], "PASS")
        expect("render-ledger")
        controller.render_ledger(state, tdd)
        expect("start-stage --to CLOSED")
        controller.start_stage(state, "CLOSED")
        self.assertIsNone(waiting()["command"])

    def test_cli_render_ledger_writes_the_blocks(self) -> None:
        state, tdd = self.closed_loop("run-31")
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "render-ledger", "--state", str(state),
             "--tdd-file", str(tdd)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["rendered"],
            ["findings", "run-metadata", "confirmations"],
        )
        # a NEW run has no superseded version, so the history index stays out of the document
        self.assertNotIn("ledger:history-index:start", tdd.read_text(encoding="utf-8"))

    # ---------------------------------------------------------- batch faces

    def test_record_findings_cli_registers_a_whole_report_in_one_call(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-110"))
        report = self.repo / "critic-findings.md"
        report.write_text(
            "## 阻断项\n"
            "@@FIND id=CRITIC-001 sev=Blocker loc=9.3\n"
            "会话超时与容量目标都没有定义，非功能需求无法验收。\n"
            "@@FIND id=CRITIC-002 sev=Medium must_fix=false loc=7.2,4.2\n"
            "重试次数与幂等键的口径不一致。\n",
            encoding="utf-8",
        )
        first = self.cli("record-findings", "--from-file", str(report), state=state)
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["registered"], ["CRITIC-001", "CRITIC-002"])
        # 一条 Finding 引多个章节时只登记第一条，其余回显而不是静默丢掉
        self.assertEqual(payload["multi_location"], {"CRITIC-002": "4.2"})
        stored = json.loads(state.read_text(encoding="utf-8"))["findings"]
        self.assertEqual([item["must_fix"] for item in stored], [True, False])

        # 修好文件里的一行后重跑整份报告：已登记的条目不再报错，也不会被记两次
        again = self.cli("record-findings", "--from-file", str(report), state=state)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(
            json.loads(again.stdout)["already_registered"], ["CRITIC-001", "CRITIC-002"]
        )
        self.assertEqual(len(json.loads(state.read_text(encoding="utf-8"))["findings"]), 2)

    def test_a_marked_example_inside_a_fence_is_not_a_registration(self) -> None:
        """A report that shows the format must not register the example.

        The batch face replaced the orchestrator's glue scripts, so a format sample in a fenced
        block used to become a real finding -- one that no role had raised and no evidence
        backed, which then had to be disposed of before delivery.
        """
        state = Path(controller.init_run(self.repo, self.feature, "run-110b"))
        report = self.repo / "critic-findings.md"
        report.write_text(
            "格式如下：\n\n"
            "```text\n"
            "@@FIND id=CRITIC-001 sev=High loc=9.3\n"
            "这里写证据。\n"
            "```\n\n"
            "@@FIND id=CRITIC-002 sev=Medium must_fix=false loc=7.2\n"
            "重试次数与幂等键的口径不一致。\n",
            encoding="utf-8",
        )
        result = self.cli("record-findings", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(["CRITIC-002"], json.loads(result.stdout)["registered"])
        stored = json.loads(state.read_text(encoding="utf-8"))["findings"]
        self.assertEqual(["CRITIC-002"], [item["id"] for item in stored])

    def test_record_findings_cli_keeps_the_good_items_and_names_the_bad_one(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-111"))
        report = self.repo / "critic-findings.md"
        report.write_text(
            "@@FIND id=CRITIC-001 sev=High loc=9.3\n"
            "降级策略缺失，高峰期会打穿下游。\n"
            "@@FIND id=CRITIC-002 sev=Critical loc=7.2\n"
            "严重度写错的那一条。\n",
            encoding="utf-8",
        )
        result = self.cli("record-findings", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PARTIAL")
        self.assertEqual(payload["registered"], ["CRITIC-001"])
        self.assertEqual(payload["failed"][0]["id"], "CRITIC-002")
        self.assertIn("severity", payload["failed"][0]["error"])

    def test_record_findings_cli_refuses_a_report_without_markers(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-112"))
        report = self.repo / "critic-findings.md"
        report.write_text(
            "# Critic 审查报告\n"
            "CRITIC-001：High，位于 9.3，会话超时未定义，容量目标无法验证，证据见 3.4。\n",
            encoding="utf-8",
        )
        result = self.cli("record-findings", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 2)
        self.assertIn("@@FIND", result.stderr)

    def test_record_findings_cli_fails_a_silently_diverging_replay(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-113"))
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "Medium", "7.2", "已登记的口径。", None, 0
        )
        report = self.repo / "critic-findings.md"
        report.write_text(
            "@@FIND id=CRITIC-001 sev=Blocker loc=7.2\n"
            "同一 ID 被改成了阻断级，必须让人看见这处分歧。\n",
            encoding="utf-8",
        )
        result = self.cli("record-findings", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["failed"][0]["id"], "CRITIC-001")
        self.assertIn("different", payload["failed"][0]["error"])
        self.assertEqual(
            json.loads(state.read_text(encoding="utf-8"))["findings"][0]["severity"], "Medium"
        )

    def test_record_fixes_cli_disposes_every_item_in_the_report(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-114"))
        self.write_tdd()
        self.to_refiner(state)
        for finding_id, severity, location in (
            ("CRITIC-001", "Blocker", "9.3"),
            ("CRITIC-002", "Medium", "7.2"),
        ):
            controller.record_finding(
                state, "CRITIC", finding_id, severity, location, "证据摘要", None, 0
            )
        report = self.repo / "refiner-map.md"
        report.write_text(
            "@@FIX id=CRITIC-001 status=FIXED loc=9.3\n"
            "补充会话超时 30s、容量目标与压测口径。\n"
            "@@FIX id=CRITIC-002 status=REJECTED loc=7.2\n"
            "重试只在幂等接口开启，已给出理由。\n",
            encoding="utf-8",
        )
        result = self.cli("record-fixes", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["registered"], ["CRITIC-001", "CRITIC-002"])
        summary = controller.show(state)
        self.assertEqual(summary["findings"]["undisposed"], [])
        # 处置登记完之后 dump 就是这条 Finding 的完整状态，不需要回翻 Refiner 的回报
        self.assertEqual(
            controller.dump(state, "CRITIC-002")["finding"]["disposition"]["status"], "REJECTED"
        )

    def test_record_fixes_cli_reports_the_dispositions_that_did_not_land(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-115"))
        self.write_tdd()
        self.to_refiner(state)
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "High", "9.3", "证据摘要", None, 0
        )
        controller.record_finding(
            state, "CRITIC", "CRITIC-002", "Medium", "7.2", "证据摘要", None, 0
        )
        report = self.repo / "refiner-map.md"
        report.write_text(
            "@@FIX id=CRITIC-001 status=FIXED loc=9.3\n"
            "补写了超时与降级口径。\n"
            "@@FIX id=CRITIC-009 status=FIXED loc=9.3\n"
            "这条 Finding 从来没有登记过。\n",
            encoding="utf-8",
        )
        result = self.cli("record-fixes", "--from-file", str(report), state=state)
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["registered"], ["CRITIC-001"])
        self.assertIn("no such finding", payload["failed"][0]["error"])
        # 批次没有把不存在的 ID 编成一条处置，也没有让那条没被点名的 Finding 悄悄关掉
        self.assertEqual(controller.show(state)["findings"]["undisposed"], ["CRITIC-002"])

    def test_locate_batch_cli_answers_a_cluster_in_one_call(self) -> None:
        self.write_tdd(DOC_BODY)
        state = Path(controller.init_run(self.repo, self.feature, "run-116"))
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "High", "数据模型", "证据摘要", None, 0
        )
        result = self.cli("locate", "--batch", "CRITIC-001,15,99.9", state=state)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["mode"], "batch")
        self.assertEqual([item["key"] for item in payload["sections"]], ["7.2", "15"])
        self.assertEqual(payload["unresolved"], ["99.9"])
        self.assertGreater(payload["total_chars"], 0)
        self.assertEqual(payload["budget_tokens"], 100000)
        self.assertFalse(payload["over_budget"])

    def test_dump_cli_keeps_evidence_out_until_it_is_asked_for(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-117"))
        controller.record_finding(
            state, "CRITIC", "CRITIC-001", "High", "9.3", "证据" * 40, None, 0
        )
        plain = self.cli("dump", "--finding", "CRITIC-001", state=state)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        view = json.loads(plain.stdout)["finding"]
        self.assertNotIn("evidence", view)
        self.assertEqual(view["evidence_chars"], 80)
        self.assertEqual(view["severity"], "High")
        self.assertEqual(view["disposition"], "NONE")

        asked = self.cli("dump", "--finding", "CRITIC-001", "--fields", "all", state=state)
        full = json.loads(asked.stdout)["finding"]
        self.assertEqual(len(full["evidence"]), 80)
        self.assertNotIn("evidence_chars", full)

        bad = self.cli("dump", "--finding", "CRITIC-001", "--fields", "evidance", state=state)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("unknown field", bad.stderr)

    def test_show_next_names_the_one_command_the_run_waits_on(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "run-118"))
        first = self.cli("show", "--next", state=state)
        self.assertEqual(first.returncode, 0, first.stderr)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["stage"], "INPUT")
        self.assertIn("record-source", payload["next"]["command"])
        self.assertIn(str(state), payload["next"]["command"])
        # 只回一条命令，不附带整份摘要：主控每轮都要问这个问题
        self.assertEqual(sorted(payload), ["next", "review_round", "stage"])

        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取")
        second = json.loads(self.cli("show", "--next", state=state).stdout)
        self.assertIn("start-stage --to CONTEXT", second["next"]["command"])

    # ---------------------------------------------------------- agent dispatch

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

    def open_review_round(self, run_id: str) -> Path:
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        self.write_tdd()
        self.to_review(state)
        return state

    def test_a_reviewer_dispatch_window_opens_and_closes(self) -> None:
        """The path every Reviewer metric depends on, and it used to be impossible.

        Wall time, the ``--scope`` read budget and the aborted-replay subset are all read off
        a window that is opened with ``DISPATCHED``.  Closing it collided with its own opening
        record, so none of those had a value for this role and the round could not be settled.
        """
        state = self.open_review_round("run-41a")
        controller.record_agent(
            state, "tech-reviewer", "tech-reviewer", "s-rev-1", "sha256:in", "DISPATCHED"
        )
        self.assertEqual(1, controller.show(state)["dispatches"]["open"])
        entry = controller.record_agent(
            state, "tech-reviewer", "tech-reviewer", "s-rev-1", "", "COMPLETED"
        )
        self.assertEqual("COMPLETED", entry["status"])
        self.assertIsNotNone(entry["wall_seconds"])
        summary = controller.show(state)
        self.assertEqual(0, summary["dispatches"]["open"])
        self.assertEqual(1, summary["dispatches"]["timed"])
        # the wall clock lands in the phase the role belongs to, which is what the delivery
        # report quotes when it says where the run's time went
        self.assertIn("audit", summary["minutes_by_phase"])

    def test_an_aborted_reviewer_window_frees_the_slot_for_a_replacement(self) -> None:
        """What SKILL.md promises after a dropped connection has to be true in code.

        Otherwise one interrupted dispatch takes the round's only reviewer slot forever, and
        the run's only exit is ``init`` again -- the exact replay R7 exists to remove.
        """
        state = self.open_review_round("run-41b")
        controller.record_agent(
            state, "tech-reviewer", "tech-reviewer", "s-rev-1", "sha256:in", "DISPATCHED"
        )
        aborted = controller.record_agent(
            state, "tech-reviewer", "tech-reviewer", "s-rev-1", "", "ABORTED"
        )
        self.assertEqual("ABORTED", aborted["status"])
        controller.record_agent(
            state, "tech-reviewer", "tech-reviewer", "s-rev-2", "sha256:in", "DISPATCHED"
        )
        entry = controller.record_agent(
            state, "tech-reviewer", "tech-reviewer", "s-rev-2", "", "COMPLETED"
        )
        self.assertEqual("COMPLETED", entry["status"])
        # what is still refused is a second instance in the same round
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.record_agent(
                state, "tech-reviewer", "tech-reviewer", "s-rev-3", "sha256:in", "DISPATCHED"
            )
        self.assertIn("already ran in round 1", str(ctx.exception))
        summary = controller.show(state)
        self.assertEqual(1, summary["dispatches"]["aborted"])
        self.assertEqual(0, summary["dispatches"]["open"])

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

    def test_a_build_manifest_alone_means_the_repo_is_not_greenfield(self) -> None:
        """A service written in a language the suffix list misses still ships code."""
        (self.repo / "go.mod").write_text("module example.invalid/note\n", encoding="utf-8")
        probe = self.probe("run-82b", PRD)
        self.assertFalse(probe["greenfield"], probe["code_scan"])
        self.assertEqual(probe["backend_code_files"], 0)
        self.assertEqual(probe["project_manifests"], ["go.mod"])
        self.assertIn("go.mod", probe["reported"]["basis"])

    def test_a_node_or_swift_service_counts_as_an_existing_implementation(self) -> None:
        (self.repo / "server.ts").write_text("export const handler = 1;\n", encoding="utf-8")
        (self.repo / "AppDelegate.swift").write_text("import UIKit\n", encoding="utf-8")
        probe = self.probe("run-82c", PRD)
        self.assertFalse(probe["greenfield"])
        self.assertEqual(probe["backend_code_files"], 2)

    def test_example_code_under_the_docs_tree_is_not_an_implementation(self) -> None:
        """Sample snippets travel with the PRD and TDD, and they must not decide the branch.

        A docs directory full of example code reads as a repo that already has a skeleton, which
        sends a greenfield run to the 改动模块清单 branch of a layout nobody has chosen yet.
        """
        for name in ("demo.py", "DemoService.java"):
            (self.feature / name).write_text("print('示例')\n", encoding="utf-8")
        (self.feature / "go.mod").write_text("module example.invalid/demo\n", encoding="utf-8")
        probe = self.probe("run-82e", PRD)
        self.assertTrue(probe["greenfield"], probe["code_scan"])
        self.assertEqual(probe["backend_code_files"], 0)
        self.assertEqual(probe["project_manifests"], [])
        self.assertEqual(probe["doc_excluded_code_files"], 2)
        self.assertEqual(probe["doc_excluded_manifests"], 1)

        (self.repo / "src" / "main.py").parent.mkdir(parents=True)
        (self.repo / "src" / "main.py").write_text("print('service')\n", encoding="utf-8")
        with_code = self.probe("run-82f", PRD)
        self.assertFalse(with_code["greenfield"])
        self.assertEqual(with_code["backend_code_files"], 1)
        # the excluded witnesses show up whenever they exist, so a reader sees what the
        # exclusion hid even on the branch where it did not change the verdict
        self.assertIn("文档目录另有代码 2 个、清单 1 份", probe["reported"]["basis"])
        self.assertIn("文档目录另有代码 2 个、清单 1 份", with_code["reported"]["basis"])
        self.assertFalse(with_code["greenfield"])
        self.assertEqual(with_code["doc_excluded_code_files"], 2)

    def test_a_nested_docs_directory_still_witnesses_the_service_below_it(self) -> None:
        """Only the root-level documentation tree is excluded; ``src/docs`` is generated-from-code."""
        nested = self.repo / "src" / "docs"
        nested.mkdir(parents=True)
        (nested / "client.go").write_text("package client\n", encoding="utf-8")
        probe = self.probe("run-82g", PRD)
        self.assertFalse(probe["greenfield"])
        self.assertEqual(probe["backend_code_files"], 1)

    def test_the_greenfield_verdict_reports_the_listing_it_came_from(self) -> None:
        """A classification nobody can re-derive is a declaration, so the numbers ship with it."""
        probe = self.probe("run-82d", PRD)
        self.assertTrue(probe["greenfield"])
        self.assertEqual(
            probe["reported"]["basis"], "绿地：代码文件 0 个、无工程清单、既有实现证据 0 项"
        )

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

    # ------------------------------------------------ 工程与模块: the branch the probe picks

    def structure_doc(self, chapter: str) -> str:
        """``TDD_BODY`` with its 工程与模块 chapter swapped for ``chapter``."""
        start = TDD_BODY.index("## 9. 工程与模块")
        end = TDD_BODY.index("## 12. 可观测设计")
        return TDD_BODY[:start] + chapter + TDD_BODY[end:]

    def structure_check(self, result: dict[str, Any]) -> dict[str, Any]:
        return next(
            item for item in result["checks"] if item["name"] == "project-structure-branch"
        )

    def reset_deliverable(self) -> None:
        """Drop the delivered document so the next ``gate_for`` is still a NEW run."""
        (self.feature / "technical-doc.md").unlink(missing_ok=True)

    def test_a_greenfield_run_must_decide_its_project_layout(self) -> None:
        """The template's 初始化 branch is now a delivery condition, not a suggestion."""
        deferred = "## 9. 工程与模块\n本次新建服务，工程结构在实现阶段再定。\n\n"
        _, result = self.gate_for("run-90", self.structure_doc(deferred))
        self.assertIn("project-structure-branch", result["failures"])
        detail = str(self.structure_check(result)["detail"])
        self.assertIn("没有工程结构方案", detail)
        self.assertIn("绿地：代码文件 0 个", detail)

    def test_the_templates_own_sample_tree_is_not_an_answer(self) -> None:
        sample = (
            "## 9. 工程与模块\n"
            "```\nmy-project/\n├── src/\n│   └── my_project/\n└── tests/\n```\n\n"
        )
        _, result = self.gate_for("run-91", self.structure_doc(sample))
        detail = str(self.structure_check(result)["detail"])
        self.assertIn("照抄模板示例", detail)

    def test_the_layout_cites_the_repositories_own_structure_standard(self) -> None:
        """The template points at 编程语言规范; where such a file exists the chapter must name it."""
        standard = self.repo / ".standard" / "02-stack" / "python" / "05-project-structure.md"
        standard.parent.mkdir(parents=True)
        standard.write_text("# 工程结构规范\n", encoding="utf-8")
        _, unnamed = self.gate_for("run-92", TDD_BODY)
        self.assertIn(
            "未点名所依据的工程结构规范文件", str(self.structure_check(unnamed)["detail"])
        )

        cited = TDD_BODY.replace(
            "仓库除本目录文档外没有代码，按新建工程初始化：",
            "仓库除本目录文档外没有代码，按 `.standard/02-stack/python/05-project-structure.md` 初始化：",
        )
        self.assertNotEqual(cited, TDD_BODY)
        self.reset_deliverable()
        _, passed = self.gate_for("run-93", cited)
        self.assertEqual(passed["status"], "PASS", passed["failures"])

    def test_an_existing_repo_must_name_the_modules_it_changes(self) -> None:
        (self.repo / "app.py").write_text("print('service')\n", encoding="utf-8")
        _, result = self.gate_for("run-94", TDD_BODY)
        self.assertIn("project-structure-branch", result["failures"])
        self.assertIn("模块表", str(self.structure_check(result)["detail"]))

        table = (
            "## 9. 工程与模块\n"
            "| 仓库 | 模块 | 包路径 | 职责 | 变更类型 |\n"
            "|------|------|--------|------|----------|\n"
            "| ordernote | 备注同步 | `com.example.ordernote.service` | 消费订单事件并写工单 | 新增 |\n\n"
        )
        self.reset_deliverable()
        _, passed = self.gate_for("run-95", self.structure_doc(table))
        self.assertEqual(passed["status"], "PASS", passed["failures"])
        self.assertIn("改动模块清单", str(self.structure_check(passed)["detail"]))

    def test_an_existing_repo_may_not_hand_the_chapter_back_to_initialization(self) -> None:
        (self.repo / "app.py").write_text("print('service')\n", encoding="utf-8")
        deferred = (
            "## 9. 工程与模块\n工程结构待初始化，本次不列模块。\n"
            "| 仓库 | 模块 | 包路径 | 职责 | 变更类型 |\n"
            "|------|------|--------|------|----------|\n"
            "| ordernote | 备注同步 | `com.example.ordernote.service` | 消费订单事件 | 新增 |\n\n"
        )
        _, result = self.gate_for("run-96", self.structure_doc(deferred))
        detail = str(self.structure_check(result)["detail"])
        self.assertIn("推给", detail)
        self.assertIn("既有实现：代码文件 1 个", detail)

    def test_a_missing_structure_chapter_is_reported_on_both_branches(self) -> None:
        without = self.structure_doc("")
        self.assertNotIn("工程与模块", without)
        _, greenfield = self.gate_for("run-97", without)
        self.assertIn("缺少 工程与模块 章节", str(self.structure_check(greenfield)["detail"]))

        (self.repo / "app.py").write_text("print('service')\n", encoding="utf-8")
        self.reset_deliverable()
        _, existing = self.gate_for("run-98", without)
        self.assertIn("缺少 工程与模块 章节", str(self.structure_check(existing)["detail"]))

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
        self.assertIn("| 工程结构分支 | 初始化工程结构：绿地：代码文件 0 个", text)
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


CONF_FIXTURE = """来源: ARCHITECT
阻断: true
位置: 3.4
问题: 下游工单接口的限流阈值
影响: 
选项: A 固定 50 QPS / B 令牌桶自适应
推荐: B —— 与现有网关一致
决定: 
应用: 

来源: ARCHITECT
阻断: false
位置: 13
问题: 告警值班群归属团队
影响: 
选项: 订单值班群 / 客服值班群
推荐: 订单值班群
决定: 
应用: 
"""

CONF_TWO_DECIDED = """CONF-001
来源: ARCHITECT
阻断: false
位置: 3.4
问题: 告警值班群归属团队
影响: 
选项: 订单值班群 / 客服值班群
推荐: 订单值班群
决定: 订单值班群
应用: 

CONF-002
来源: REFINER:CRITIC-007
阻断: false
位置: 7.2
问题: 归档表分区策略
影响: 
选项: 按月 / 按周
推荐: 按月分区
决定: 按周
应用: 7.2；已按周分区改写 DDL（2026-09-19T10:00:00）
"""

# A fenced sample marker and a bare one: only the bare one may be adopted on ingest.
MARKER_TDD = TDD_BODY.replace(
    "## 12. 可观测设计",
    "## 12. 可观测设计\n[需人工确认: 下游限流阈值需业务方确认]\n"
    "```text\n[需人工确认: 围栏内示例不算待办]\n```",
)


class ConfirmationLoopTests(unittest.TestCase):
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

    def write_tdd(self, body: str = TDD_BODY) -> Path:
        """Every run here is an UPDATE run, so the body carries the history chapter."""
        path = self.feature / "technical-doc.md"
        path.write_text(body + HISTORY_TAIL, encoding="utf-8")
        return path

    def write_conf(self, text: str, newline: str = "\n") -> Path:
        path = self.feature / "confirmation-log.md"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text.replace("\n", newline) if newline != "\n" else text)
        return path

    def record(self, state: Path, role: str, session: str, scope: str = "") -> None:
        controller.record_agent(state, role, role, session, "sha256:input", "COMPLETED", scope)

    def artifact(self, state: Path, kind: str, text: str = EVIDENCE) -> dict[str, Any]:
        origin = self.repo / f"tmp-{kind}.md"
        origin.write_text(text, encoding="utf-8")
        return controller.write_artifact(state, kind, origin, None, None)

    def to_architect(self, run_id: str) -> Path:
        self.write_tdd()
        state = Path(controller.init_run(self.repo, self.feature, run_id))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取", "1/0/0")
        controller.start_stage(state, "CONTEXT")
        self.record(state, "tech-context", "s-context")
        self.artifact(state, "context-pack")
        controller.start_stage(state, "ARCHITECT")
        self.record(state, "tech-architect", "s-arch")
        self.artifact(state, "draft")
        return state

    def to_refiner(self, state: Path) -> None:
        controller.start_stage(state, "CRITIC")
        self.record(state, "tech-critic", "s-critic")
        self.artifact(state, "critic-findings")
        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine")
        self.artifact(state, "refiner-map")

    def approve_round(self, state: Path, session: str = "s-review-1") -> None:
        self.record(state, "tech-reviewer", session)
        report = self.artifact(state, "review-report")
        controller.record_review(
            state, "APPROVE", report["path"], self.feature / "technical-doc.md", session
        )

    def sync(self, state: Path) -> dict[str, Any]:
        return controller.sync_confirmations(state, self.feature / "confirmation-log.md")

    def entries(self, state: Path) -> list[dict[str, Any]]:
        return json.loads(state.read_text(encoding="utf-8"))["confirmations"]

    # ------------------------------------------------------------------- sync

    def test_sync_stamps_ids_and_reports_pause_counts(self) -> None:
        state = self.to_architect("conf-1")
        self.write_conf(CONF_FIXTURE)
        counts = self.sync(state)
        self.assertEqual(counts["new_ids"], ["CONF-001", "CONF-002"])
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["open"], 2)
        self.assertEqual(counts["blocking_open"], ["CONF-001"])
        self.assertEqual(counts["settled_unapplied"], [])
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["confirmation_file"], "docs/feature/confirmation-log.md")
        self.assertEqual([item["id"] for item in data["confirmations"]], ["CONF-001", "CONF-002"])
        self.assertTrue(data["confirmations"][0]["blocking"])
        # IDs were written back as their own line, above each entry's 来源
        text = (self.feature / "confirmation-log.md").read_text(encoding="utf-8")
        self.assertIn("CONF-001\n来源: ARCHITECT", text)
        self.assertIn("CONF-002\n来源: ARCHITECT", text)
        # re-syncing is a no-op on disk and mints no new IDs
        second = self.sync(state)
        self.assertEqual(second["new_ids"], [])
        self.assertEqual(text, (self.feature / "confirmation-log.md").read_text(encoding="utf-8"))

    def test_sync_rejects_garbage_bad_enums_duplicates_and_mutations(self) -> None:
        state = self.to_architect("conf-2")
        self.write_conf(CONF_FIXTURE + "\n这行不是字段行\n")
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("not a field line", str(ctx.exception))

        self.write_conf(
            CONF_FIXTURE.replace("来源: ARCHITECT", "来源: CRITIC-001", 1)
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("来源", str(ctx.exception))

        self.write_conf(CONF_FIXTURE.replace("阻断: true", "阻断: 是", 1))
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("true or false", str(ctx.exception))

        # the same question twice cannot both be the pairing key
        self.write_conf(CONF_FIXTURE + "\n" + CONF_FIXTURE)
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("same question", str(ctx.exception))

        self.write_conf(
            "来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: "
            + "很长" * 45
            + "\n决定: \n"
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("pairing key limit", str(ctx.exception))

        # register first, then tamper: 问题 is immutable once stamped
        self.write_conf("来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 值班群归属\n决定: \n应用: \n")
        self.sync(state)
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            conf.read_text(encoding="utf-8").replace("问题: 值班群归属", "问题: 换了个措辞"),
            encoding="utf-8",
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("rewritten after registration", str(ctx.exception))

        # and an entry may not vanish either
        conf.write_text(
            "\n".join(
                line for line in conf.read_text(encoding="utf-8").splitlines()
                if line != "CONF-001"
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("append-only", str(ctx.exception))

    def test_sync_validates_finding_provenance_in_run(self) -> None:
        state = self.to_architect("conf-3")
        self.to_refiner(state)
        controller.record_finding(
            state, "CRITIC", "CRITIC-007", "High", "3.4 影响范围", "阈值缺失", None, 0
        )
        self.write_conf(
            "来源: REFINER:CRITIC-099\n阻断: false\n位置: 3.4\n问题: 未登记的出处\n决定: \n"
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("CRITIC-099", str(ctx.exception))
        # the refused sync registered nothing: replace the file and sync the valid entry
        self.write_conf(
            "来源: REFINER:CRITIC-007\n阻断: false\n位置: 3.4\n问题: 阈值取哪个数\n决定: \n"
        )
        counts = self.sync(state)
        self.assertEqual(counts["new_ids"], ["CONF-001"])

    def test_sync_stage_and_path_are_not_free_choices(self) -> None:
        self.write_conf(CONF_FIXTURE)
        state = Path(controller.init_run(self.repo, self.feature, "conf-4"))
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("stage", str(ctx.exception))
        # a confirmation file elsewhere in the repo is not this feature's ledger
        elsewhere = self.repo / "docs" / "other-confirmation-log.md"
        elsewhere.write_text(CONF_FIXTURE, encoding="utf-8")
        (self.feature / "confirmation-log.md").unlink()
        state = Path(controller.init_run(self.repo, self.feature, "conf-5"))
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取", None)
        controller.start_stage(state, "CONTEXT")
        self.record(state, "tech-context", "s-context")
        self.artifact(state, "context-pack")
        controller.start_stage(state, "ARCHITECT")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.sync_confirmations(state, elsewhere)
        self.assertIn("confirmation file", str(ctx.exception))

    # ------------------------------------------------------- decision channels

    def test_blocking_entry_gates_until_answered_and_landed(self) -> None:
        state = self.to_architect("conf-6")
        self.write_conf(CONF_FIXTURE)
        self.sync(state)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "CRITIC")
        self.assertIn("CONF-001", str(ctx.exception))
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            conf.read_text(encoding="utf-8").replace("决定: \n应用: ", "决定: 选 B，令牌桶\n应用: ", 1),
            encoding="utf-8",
        )
        counts = self.sync(state)
        self.assertEqual(counts["blocking_open"], [])
        self.assertEqual(counts["settled_unapplied"], ["CONF-001"])
        # an answered-but-never-landed blocking decision is still unfinished work
        with self.assertRaises(controller.ControllerError):
            controller.start_stage(state, "CRITIC")
        applied = controller.mark_confirmation_applied(
            state, "CONF-001", "3.4", "写入 ADR-001 采用令牌桶"
        )
        self.assertEqual(applied["settled_unapplied"], [])
        controller.start_stage(state, "CRITIC")
        text = conf.read_text(encoding="utf-8")
        self.assertIn("应用: 3.4；写入 ADR-001 采用令牌桶（", text)
        entry = next(item for item in self.entries(state) if item["id"] == "CONF-001")
        self.assertEqual(entry["status"], "ANSWERED")
        self.assertTrue(entry["applied"])

    def test_skip_to_default_is_explicit_and_writes_the_decision_line(self) -> None:
        state = self.to_architect("conf-7")
        self.write_conf(CONF_FIXTURE)
        self.sync(state)
        result = controller.skip_to_default(state, "CONF-002", "用户在会话中回复：不答但要走完")
        self.assertEqual(result["skipped_to_default"], 1)
        entry = next(item for item in self.entries(state) if item["id"] == "CONF-002")
        self.assertEqual(entry["status"], "SKIPPED-DEFAULT")
        text = (self.feature / "confirmation-log.md").read_text(encoding="utf-8")
        self.assertIn("决定: 按推荐（skip-to-default: 用户在会话中回复：不答但要走完）", text)
        # an entry that already carries a decision cannot be skipped again
        with self.assertRaises(controller.ControllerError):
            controller.skip_to_default(state, "CONF-002", "再跳一次")
        # ...and 按推荐 needs a referent: an entry without 推荐 cannot take this channel
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            text + "\n来源: ARCHITECT\n阻断: false\n位置: 15\n问题: 里程碑归属\n"
            "选项: A / B\n决定: \n应用: \n",
            encoding="utf-8",
        )
        self.sync(state)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.skip_to_default(state, "CONF-003", "跳过")
        self.assertIn("no 推荐", str(ctx.exception))

    def test_skip_to_default_before_any_sync_is_refused(self) -> None:
        state = self.to_architect("conf-8")
        self.write_conf(CONF_FIXTURE)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.skip_to_default(state, "CONF-001", "跳过")
        self.assertIn("unknown confirmation id", str(ctx.exception))

    # ------------------------------------------------------------ windows/bytes

    def test_crlf_is_parsed_and_preserved_and_gbk_is_named(self) -> None:
        state = self.to_architect("conf-9")
        self.write_conf(CONF_FIXTURE, newline="\r\n")
        self.sync(state)
        raw = (self.feature / "confirmation-log.md").read_bytes()
        self.assertTrue(raw.startswith(b"CONF-001\r\n"))
        self.assertIn(b"\r\nCONF-002\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        (self.feature / "confirmation-log.md").write_bytes(
            "来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 编码\n".encode("gbk")
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            self.sync(state)
        self.assertIn("UTF-8", str(ctx.exception))

    def test_cli_reports_blocked_and_exits_nonzero(self) -> None:
        state = self.to_architect("conf-10")
        self.write_conf(CONF_FIXTURE + "\n乱写的行\n")
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "sync-confirmations", "--state", str(state),
             "--file", str(self.feature / "confirmation-log.md")],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        # bytes, not text: the child's stderr codec is the platform's, and the message
        # names a Chinese fragment
        self.assertIn(b"BLOCKED:", result.stderr)

    # ------------------------------------------------------------------- gates

    def test_review_and_delivery_gates(self) -> None:
        state = self.to_architect("conf-11")
        self.write_conf(
            "来源: ARCHITECT\n阻断: true\n位置: 3.4\n问题: 值班群归属\n推荐: 订单群\n决定: \n"
        )
        self.sync(state)
        # an OPEN entry cannot be landed: there is no decision to bind yet
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.mark_confirmation_applied(state, "CONF-001", "3.4", "OPEN 不能被落地")
        self.assertIn("still OPEN", str(ctx.exception))
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "CRITIC")
        self.assertIn("CONF-001 [OPEN]", str(ctx.exception))
        controller.skip_to_default(state, "CONF-001", "用户在会话中回复：按推荐走")
        # skip-to-default settles the decision but does not land it -- still a blocker
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "CRITIC")
        self.assertIn("CONF-001 [SKIPPED-DEFAULT 未落地]", str(ctx.exception))
        controller.mark_confirmation_applied(state, "CONF-001", "3.4", "ADR 记录决策")
        self.to_refiner(state)
        # a non-blocking answer that never landed is refused here too, because this door still
        # has a landing channel: the Refiner dispatch is one call away
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            conf.read_text(encoding="utf-8")
            + "来源: ARCHITECT\n阻断: false\n位置: 13\n问题: 队列积压告警的触发时长\n推荐: 5 分钟\n"
            "决定: 按推荐\n应用: \n",
            encoding="utf-8",
        )
        self.sync(state)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "REVIEW")
        self.assertIn("CONF-002 never landed", str(ctx.exception))
        controller.mark_confirmation_applied(state, "CONF-002", "13", "告警阈值按 5 分钟落地")
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        # An answer that arrives after the last review door has no landing channel left in this
        # run: REFINE needs an open finding and DELIVERY is one-way, so refusing here would
        # dead-lock the run instead of fixing the document.  It is carried over and disclosed.
        conf.write_text(
            conf.read_text(encoding="utf-8")
            + "来源: ARCHITECT\n阻断: false\n位置: 7.2\n问题: 归档表是否随本期上线\n推荐: 随本期\n"
            "决定: 按推荐\n应用: \n",
            encoding="utf-8",
        )
        counts = self.sync(state)
        self.assertEqual(counts["settled_unapplied"], ["CONF-003"])
        result = controller.start_stage(state, "DELIVERY")
        self.assertEqual(result["pending_landing"], ["CONF-003"])

    def test_a_blocking_answer_still_refuses_delivery(self) -> None:
        """Carry-over is for decisions the document can ship without; 阻断 is not one of them."""
        state = self.to_architect("conf-21")
        self.to_refiner(state)
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        self.write_conf(
            "来源: ARCHITECT\n阻断: true\n位置: 3.4\n问题: 分库分表键取哪个\n推荐: 订单号\n"
            "决定: 订单号\n应用: \n"
        )
        self.sync(state)
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "DELIVERY")
        self.assertIn("blocking confirmation", str(ctx.exception))
        self.assertIn("CONF-001 [ANSWERED 未落地]", str(ctx.exception))

    def test_landing_must_stay_inside_the_dispatch_scope(self) -> None:
        state = self.to_architect("conf-12")
        self.write_conf(
            "来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 值班群归属\n推荐: 订单群\n决定: \n"
        )
        self.sync(state)
        self.to_refiner(state)
        # answered while the Refiner is on stage -- an OPEN entry does not stop an audit
        # hand-off, only a settled one that never landed does
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            conf.read_text(encoding="utf-8").replace("决定: ", "决定: 订单群", 1),
            encoding="utf-8",
        )
        self.sync(state)
        # this stage's Refiner dispatch was handed 3.4 only; 15 is outside the window
        self.record(state, "tech-refiner", "s-refine-scoped", scope="3.4")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.mark_confirmation_applied(state, "CONF-001", "15", "改到了范围外")
        self.assertIn("outside every --scope", str(ctx.exception))
        controller.mark_confirmation_applied(state, "CONF-001", "3.4", "在范围内落地")
        self.assertTrue(self.entries(state)[0]["applied"])
        # the same settled decision now lets the review door open
        controller.start_stage(state, "REVIEW")

    # ------------------------------------------------- delivery rewind (landing channel)

    def to_delivery_with_unlanded(self, run_id: str) -> Path:
        """A run that reached DELIVERY carrying one answered-but-unlanded decision."""
        state = self.to_architect(run_id)
        self.to_refiner(state)
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        self.write_conf(
            "来源: ARCHITECT\n阻断: false\n位置: 13\n问题: 归档表是否随本期上线\n推荐: 随本期\n"
            "决定: 按推荐\n应用: \n"
        )
        self.sync(state)
        result = controller.start_stage(state, "DELIVERY")
        self.assertEqual(result["pending_landing"], ["CONF-001"])
        return state

    def test_delivery_rewinds_to_the_refiner_to_land_a_settled_decision(self) -> None:
        state = self.to_delivery_with_unlanded("conf-30")
        result = controller.start_stage(state, "REFINER")
        self.assertEqual(result["stage"], "REFINER")
        # what reopened the run is the answer with no landing, not a Reviewer finding: this
        # round raised none, and the rewind does not ask for one
        self.assertEqual(result["rewind"]["confirmations"], ["CONF-001"])
        self.assertIn("review round 2", result["rewind"]["cost"])
        # the rewind is on the record: a delivery report explaining round 2 reads it off state
        rewinds = json.loads(state.read_text(encoding="utf-8"))["rewinds"]
        self.assertEqual(["CONF-001"], rewinds[0]["unlanded"])
        self.assertEqual(1, rewinds[0]["review_round"])
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["findings"], [])
        # the price is the point: the only way out of the reopened Refiner is a fresh review,
        # so the previous round's APPROVE cannot carry a body this round changed
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "DELIVERY")
        self.assertIn("illegal transition REFINER -> DELIVERY", str(ctx.exception))

    def test_the_rewind_is_not_limited_to_landing_a_decision(self) -> None:
        """The door prices the rewind; it no longer vets the reason.

        Restricting it to unlanded decisions left every other delivery-time defect without a
        legal way back: a gate failure that needs the body rewritten could neither be fixed
        (``review-report-matches-delivered-tdd``) nor left alone, so the run was replayed.
        """
        state = self.to_architect("conf-31")
        self.to_refiner(state)
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        delivered = controller.start_stage(state, "DELIVERY")
        self.assertNotIn("pending_landing", delivered)
        # nothing to land, and still legal: the price is the one thing being guarded
        result = controller.start_stage(state, "REFINER")
        self.assertEqual("REFINER", result["stage"])
        self.assertEqual([], result["rewind"]["confirmations"])
        self.assertIn("review round 2", result["rewind"]["cost"])
        # and the price is collected: the old verdict cannot carry a body this round changed
        self.record(state, "tech-refiner", "s-refine-rewind")
        self.artifact(state, "refiner-map", EVIDENCE + "按门禁失败项重写第 13 章。\n")
        self.assertEqual(2, controller.start_stage(state, "REVIEW")["review_round"])
        self.approve_round(state, "s-review-2")
        controller.start_stage(state, "DELIVERY")

        # a question nobody has answered is not a landing job either, and does not block it
        other = self.to_architect("conf-31b")
        self.to_refiner(other)
        controller.start_stage(other, "REVIEW")
        self.approve_round(other)
        self.write_conf(
            "来源: ARCHITECT\n阻断: false\n位置: 13\n问题: 归档表是否随本期上线\n推荐: 随本期\n"
            "决定: \n应用: \n"
        )
        self.sync(other)
        delivered = controller.start_stage(other, "DELIVERY")
        self.assertNotIn("pending_landing", delivered)
        controller.start_stage(other, "REFINER")

    def test_the_rewind_refuses_once_the_round_budget_is_spent(self) -> None:
        state = self.to_delivery_with_unlanded("conf-32")
        # The gate reads the budget off the state, so a spent budget is the same door as the
        # fifth round's; driving five real rounds here would only re-test the arithmetic.
        data = json.loads(state.read_text(encoding="utf-8"))
        data["max_review_rounds"] = data["review_round"]
        state.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "REFINER")
        self.assertIn("last one the review budget allows", str(ctx.exception))
        self.assertIn("CONF-001", str(ctx.exception))
        # refusing must not cost the run its legal exit: 挂账 delivery stays reachable
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["stage"], "DELIVERY")

    def test_a_rewound_run_must_land_the_answer_before_it_can_be_reviewed(self) -> None:
        state = self.to_delivery_with_unlanded("conf-33")
        controller.start_stage(state, "REFINER")
        self.record(state, "tech-refiner", "s-refine-landing")
        self.artifact(
            state, "refiner-map",
            EVIDENCE + "落地回退：CONF-001 的决定尚未写回第 13 章，本轮只登记了派发。\n",
        )
        with self.assertRaises(controller.ControllerError) as ctx:
            controller.start_stage(state, "REVIEW")
        self.assertIn("CONF-001 never landed", str(ctx.exception))

    def test_a_landed_rewind_delivers_on_its_own_fresh_verdict(self) -> None:
        state = self.to_delivery_with_unlanded("conf-34")
        controller.start_stage(state, "REFINER")
        tdd = self.feature / "technical-doc.md"
        tdd.write_text(
            tdd.read_text(encoding="utf-8").replace(
                "## 13. 告警设计\n",
                "## 13. 告警设计\n归档表随本期上线，阈值在发布后第 3 天按实际积压量复检。\n",
            ),
            encoding="utf-8",
        )
        controller.mark_confirmation_applied(state, "CONF-001", "13", "归档表随本期上线")
        self.record(state, "tech-refiner", "s-refine-landing")
        self.artifact(
            state, "refiner-map",
            EVIDENCE + "落地回退：CONF-001 的决定已写进第 13 章，未改其余设计。\n",
        )
        result = controller.start_stage(state, "REVIEW")
        self.assertEqual(result["review_round"], 2)
        self.approve_round(state, "s-review-2")
        delivered = controller.start_stage(state, "DELIVERY")
        self.assertNotIn("pending_landing", delivered)
        self.assertTrue(self.entries(state)[0]["applied"])

    def test_show_next_discloses_the_rewind_without_choosing_it(self) -> None:
        """Closing is legal with a 挂账 entry, so ``--next`` names the option, not a command."""
        state = self.to_delivery_with_unlanded("conf-35")
        nxt = controller.show(state, True)["next"]
        self.assertIn("render-ledger", nxt["command"])
        self.assertIn("CONF-001", nxt["rewind"]["confirmations"])
        self.assertIn("start-stage --to REFINER", nxt["rewind"]["command"])
        self.assertIn("review round", nxt["rewind"]["cost"])

    # ----------------------------------------------------------------- gate: B2

    def gate_check(self, state: Path, name: str) -> dict[str, Any]:
        """One named delivery check; the rest of the report is irrelevant to these tests."""
        result = controller.gate(state, self.feature / "technical-doc.md")
        return next(item for item in result["checks"] if item["name"] == name)

    def test_markers_and_entries_must_pair_in_both_directions(self) -> None:
        state = self.to_architect("conf-19")
        tdd = self.feature / "technical-doc.md"
        tdd.write_text(
            tdd.read_text(encoding="utf-8") + "\n[需人工确认: 下游限流阈值取哪个数]\n",
            encoding="utf-8",
        )
        check = self.gate_check(state, "markers-match-confirmations")
        self.assertFalse(check["ok"])
        self.assertIn("下游限流阈值取哪个数", str(check["detail"]))
        # registering the entry pairs them up, and an OPEN question may still stand in the body
        self.write_conf(
            "来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 下游限流阈值取哪个数\n决定: \n"
        )
        self.sync(state)
        self.assertTrue(self.gate_check(state, "markers-match-confirmations")["ok"])
        # the asymmetry is deliberate: an OPEN question the draft no longer raises may lose its
        # marker and stay in the ledger -- only an auditor can judge that erase
        tdd.write_text(
            tdd.read_text(encoding="utf-8").replace("[需人工确认: 下游限流阈值取哪个数]\n", ""),
            encoding="utf-8",
        )
        self.assertTrue(self.gate_check(state, "markers-match-confirmations")["ok"])
        tdd.write_text(
            tdd.read_text(encoding="utf-8") + "\n[需人工确认: 下游限流阈值取哪个数]\n",
            encoding="utf-8",
        )
        # answering is not consuming: the question is still open on the page, so its marker
        # stays legal until a dispatch says it landed
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            conf.read_text(encoding="utf-8").replace("决定: ", "决定: 令牌桶 50 QPS", 1),
            encoding="utf-8",
        )
        self.sync(state)
        self.assertTrue(self.gate_check(state, "markers-match-confirmations")["ok"])
        # once the landing is registered the document may still not ask
        controller.mark_confirmation_applied(state, "CONF-001", "3.4", "按令牌桶 50 QPS 落地")
        check = self.gate_check(state, "markers-match-confirmations")
        self.assertFalse(check["ok"])
        self.assertIn("CONF-001", str(check["detail"]))
        self.assertIn("landed", str(check["detail"]))
        # the resolved form is no longer a marker, which is what closes it
        tdd.write_text(
            tdd.read_text(encoding="utf-8").replace(
                "[需人工确认: 下游限流阈值取哪个数]",
                "[已人工确认: 下游限流阈值取哪个数 → 令牌桶 50 QPS]",
            ),
            encoding="utf-8",
        )
        self.assertTrue(self.gate_check(state, "markers-match-confirmations")["ok"])

    def test_the_confirmation_ledger_has_to_survive_to_delivery(self) -> None:
        state = self.to_architect("conf-20")
        self.write_conf(
            "来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 值班群归属\n推荐: 订单群\n决定: \n应用: \n"
        )
        self.sync(state)
        self.assertTrue(self.gate_check(state, "confirmations-file-persisted")["ok"])
        conf = self.feature / "confirmation-log.md"
        # an answer typed into the file after the last sync: state still says OPEN
        conf.write_text(
            conf.read_text(encoding="utf-8").replace("决定: ", "决定: 客服值班群", 1),
            encoding="utf-8",
        )
        check = self.gate_check(state, "confirmations-file-persisted")
        self.assertFalse(check["ok"])
        self.assertIn("CONF-001", str(check["detail"]))
        self.assertIn("决定", str(check["detail"]))
        self.sync(state)
        self.assertTrue(self.gate_check(state, "confirmations-file-persisted")["ok"])
        # rewriting the file down to an ID-less entry is not how an entry is withdrawn
        conf.write_text(
            "来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 值班群归属\n推荐: 订单群\n"
            "决定: 客服值班群\n应用: \n",
            encoding="utf-8",
        )
        check = self.gate_check(state, "confirmations-file-persisted")
        self.assertFalse(check["ok"])
        self.assertIn("CONF-ID", str(check["detail"]))
        # ...and neither is deleting the file the run registered against
        conf.unlink()
        check = self.gate_check(state, "confirmations-file-persisted")
        self.assertFalse(check["ok"])
        self.assertIn("is missing", str(check["detail"]))

    # ------------------------------------------------------------------- ledger

    def test_confirmations_ledger_is_rendered_and_idempotent(self) -> None:
        state = self.to_architect("conf-13")
        tdd = self.feature / "technical-doc.md"
        rendered = controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        self.assertIn("confirmations", rendered["rendered"])
        self.assertIn("当前没有未收口的人工确认条目。", text)
        self.assertIn("暂无人工决定。", text)
        self.write_conf(CONF_FIXTURE)
        self.sync(state)
        controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        self.assertIn("CONF-001", text)
        self.assertIn("人工确认台账", text)
        # the run-metadata echo names IDs, never raw marker text
        self.assertNotIn("[需人工确认", text)
        self.assertIn("仍需人工确认（2 项）：CONF-001 OPEN、CONF-002 OPEN", text)
        again = controller.render_ledger(state, tdd)
        self.assertFalse(again["changed"])

    def test_full_closed_loop_with_landed_decision_stays_clean(self) -> None:
        state = self.to_architect("conf-14")
        self.write_conf(
            "来源: ARCHITECT\n阻断: true\n位置: 3.4\n问题: 值班群归属\n推荐: 订单群\n决定: \n应用: \n"
        )
        self.sync(state)
        conf = self.feature / "confirmation-log.md"
        conf.write_text(
            conf.read_text(encoding="utf-8").replace("决定: \n", "决定: 订单群\n", 1),
            encoding="utf-8",
        )
        self.sync(state)
        controller.mark_confirmation_applied(state, "CONF-001", "3.4", "ADR 记录决策")
        self.to_refiner(state)
        controller.start_stage(state, "REVIEW")
        self.approve_round(state)
        controller.start_stage(state, "DELIVERY")
        controller.archive_history(state, "0.1")
        tdd = self.feature / "technical-doc.md"
        controller.render_ledger(state, tdd)
        text = tdd.read_text(encoding="utf-8")
        self.assertIn("人工决定台账", text)
        self.assertIn("ADR 记录决策", text)
        self.assertIn("CONF-001", text)
        self.assertNotIn("暂无人工决定。", text)
        result = controller.gate(state, tdd)
        self.assertEqual(result["status"], "PASS", result["failures"])
        shown = controller.show(state)
        self.assertEqual(shown["confirmations"]["applied"], 1)

    # ------------------------------------------------------------------- init

    def test_init_ingests_ledger_freezes_pending_decisions_and_adopts_bare_markers(self) -> None:
        self.write_tdd(MARKER_TDD)
        self.write_conf(CONF_TWO_DECIDED)
        state = Path(controller.init_run(self.repo, self.feature, "conf-15"))
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "UPDATE")
        by_id = {item["id"]: item for item in data["confirmations"]}
        self.assertEqual(sorted(by_id), ["CONF-001", "CONF-002", "CONF-003"])
        self.assertTrue(all(item["imported"] for item in by_id.values()))
        # CONF-002 cites a finding from the previous run's state -- imported without
        # existence validation (D12); CONF-002's own application carries over verbatim.
        self.assertEqual(by_id["CONF-002"]["status"], "ANSWERED")
        self.assertTrue(by_id["CONF-002"]["applied"])
        self.assertFalse(by_id["CONF-001"]["applied"])
        # the bare body marker became a non-blocking LEGACY-MARKER entry; the fenced one did not
        self.assertEqual(by_id["CONF-003"]["source"], "LEGACY-MARKER")
        self.assertFalse(by_id["CONF-003"]["blocking"])
        self.assertEqual(by_id["CONF-003"]["question"], "下游限流阈值需业务方确认")
        conf_text = (self.feature / "confirmation-log.md").read_text(encoding="utf-8")
        self.assertIn("问题: 下游限流阈值需业务方确认", conf_text)
        self.assertNotIn("围栏内示例", conf_text)
        # the settled-but-unlanded decision is frozen as HUMAN_DECISION input evidence
        sources = {item["source_id"]: item for item in data["sources"]}
        self.assertIn("CONFIRMATION-CONF-001", sources)
        self.assertNotIn("CONFIRMATION-CONF-002", sources)
        self.assertEqual(sources["CONFIRMATION-CONF-001"]["kind"], "HUMAN_DECISION")
        self.assertIn("订单值班群", sources["CONFIRMATION-CONF-001"]["summary"])
        self.assertEqual(
            data["confirmation_import"]["pending_landing"], ["CONF-001"]
        )
        # the carried-over decision does not stop this stage: it is the first audit hand-off
        # (CRITIC, then REVIEW) that demands every settled decision have landed by then
        controller.record_source(
            state, "SRC-1", "PRD", str(self.feature / "product-doc.md"),
            "READ", "v1", "订单备注同步需求", "HIGH",
        )
        controller.finalize_inputs(state, "PASS", "全部关键资料已读取", "1/0/0")
        controller.start_stage(state, "CONTEXT")
        self.assertEqual(controller.show(state)["confirmations"]["total"], 3)

    def test_init_without_ledger_is_a_no_op_and_a_corrupt_ledger_fails_before_mkdir(self) -> None:
        state = Path(controller.init_run(self.repo, self.feature, "conf-16"))
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertIsNone(data["confirmation_file"])
        self.assertEqual(data["confirmations"], [])
        self.write_conf(CONF_FIXTURE.replace("来源: ARCHITECT", "垃圾行", 1))
        with self.assertRaises(controller.ControllerError):
            controller.init_run(self.repo, self.feature, "conf-17")
        self.assertFalse((self.repo / ".codekit" / "technical-doc" / "conf-17").exists())

    def test_resumed_old_state_without_confirmation_keys_still_loads(self) -> None:
        state = self.to_architect("conf-18")
        data = json.loads(state.read_text(encoding="utf-8"))
        del data["confirmations"]
        del data["confirmation_file"]
        state.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        summary = controller.show(state)
        self.assertEqual(summary["confirmations"]["total"], 0)
        self.write_conf("来源: ARCHITECT\n阻断: false\n位置: 3.4\n问题: 老 run 续用\n决定: \n")
        self.assertEqual(self.sync(state)["new_ids"], ["CONF-001"])


if __name__ == "__main__":
    unittest.main()
