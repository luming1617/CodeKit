"""P1-1 DELTA 增量复核轮接线的回归与对抗测试。

对抗用例清单（方案合入前提，逐项覆盖"变形输入 + 试图绕过"）：
1. 首轮 DELTA；2. 连续 DELTA；3. FULL APPROVE 后接 DELTA；4. DELTA 报告改标 FULL 登记；
5. repaired_findings 为空/含非阻塞 id/含未知 id；6. 上轮报告 digest 不符；
7. DELTA 最新轮直接交付（delivery_eligible=false）；8. 第 4 次冻结；
9. LITE run 声明 DELTA；10. DELTA handoff 缺 delta_recheck 块（缺 --previous-report/--repaired-findings）。
另覆盖：check-repair-scope 的范围内/越界/承重字段变化三分支、结构校验非 0 拒登记、verdict 改标。

运行：python3 -m unittest discover -s skills/code-fix/tests -p 'test_*.py'
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

# 复用同目录既有夹具与合规提案映射，避免第二份口径漂移。
from test_code_fix_skill_scripts import CliFixture, filled_proposal, state


def make_report(verdict: str, mode: str = "FULL", blocking_ids: tuple[str, ...] = ()) -> dict:
    """最小下游规范化报告形状：record-review 只消费 verdict/review_context/findings。"""
    return {
        "schema_version": "1.0",
        "review_context": {"review_mode": mode, "documents_status": "available"},
        "verdict": verdict,
        "findings": [
            {"id": fid, "severity": "high", "blocking": True, "location": "src/pay/order.py:1"}
            for fid in blocking_ids
        ] + [
            {"id": "F-900", "severity": "low", "blocking": False, "location": "src/pay/order.py:9"}
        ],
    }


class DeltaFixture(CliFixture):
    """FULL 模式（有有效文档）的最短合规路径，停在首轮 freeze-review 之后。"""

    def setUp(self):
        super().setUp()
        # GitRepoFixture 的 docs/tdd.md 不是有效文档名；放一份 technical-doc.md 供 preflight 冻结。
        doc = self.root / "docs" / "technical-doc.md"
        doc.write_text("# 技术方案\n\n扣款必须幂等，重复请求不得产生二次扣款；对账 Job 只读不改。\n" * 6,
                       encoding="utf-8")
        # 验证脚本在 preflight 前就存在，属于预先存在的工作树内容，不计入本次修复范围。
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        code, payload, err = self.cli("preflight", "--root", str(self.root), "--run-id", "20260909-delta",
                                      "--feature-directory", "docs")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["documents_status"], "AVAILABLE")
        self.manifest = self.root / ".codekit" / "code-fix" / "20260909-delta" / "state.json"
        self.run_dir = self.manifest.parent
        self.proposal = self.run_dir / "proposal.md"
        self.proposal.write_text(filled_proposal(), encoding="utf-8")
        self.approval = self.run_dir / "approval.txt"
        self.approval.write_text("批准执行 BF-20260909-order-retry v1，按当前绑定快照和允许修改文件实施。\n",
                                 encoding="utf-8")
        self.assertEqual(self.cli("record-proposal", "--manifest", str(self.manifest), "--proposal", str(self.proposal),
                                  "--proposal-id", "BF-20260909-order-retry", "--version", "1",
                                  "--review-mode", "FULL")[0], 0)
        self.assertEqual(self.cli("verify-approval", "--manifest", str(self.manifest),
                                  "--approval-file", str(self.approval), "--proposal", str(self.proposal))[0], 0)
        _, contract_payload, _ = self.cli("freeze-contract", "--manifest", str(self.manifest))
        self.contract = Path(contract_payload["contract"])
        (self.root / "src" / "pay").mkdir()
        (self.root / "src" / "pay" / "order.py").write_text("def charge():\n    return 1  # fixed\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_pay.py").write_text("def test_charge():\n    assert True\n", encoding="utf-8")
        self.rewrite_plan([
            {"name": "回归", "proves": "根因已消除", "required": True, "command": [sys.executable, "ok.py"]},
        ])
        self.assertEqual(self.cli("run-plan", "--manifest", str(self.manifest))[0], 0)
        self.assertEqual(self.cli("freeze-review", "--manifest", str(self.manifest))[0], 0)

    def rewrite_plan(self, checks: list[dict]) -> None:
        plan_path = self.run_dir / "validation-plan.json"
        plan = self.read(plan_path)
        plan["checks"] = checks
        # 模拟合法的新版合同：同步合同冻结的验证计划并重算合同自摘要。
        contract_path = self.run_dir / "implementation-contract.json"
        contract = self.read(contract_path)
        contract["validation_commands"] = checks
        contract["contract_digest"] = state.canonical_digest(
            {key: value for key, value in contract.items() if key != "contract_digest"}
        )
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        plan["contract_digest"] = contract["contract_digest"]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def write_report(self, name: str, verdict: str, mode: str = "FULL",
                     blocking_ids: tuple[str, ...] = ()) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(make_report(verdict, mode, blocking_ids), ensure_ascii=False), encoding="utf-8")
        return path

    def record(self, report: Path, verdict: str, mode: str = "FULL", validator_exit: int = 0) -> tuple[int, dict, str]:
        return self.cli("record-review", "--manifest", str(self.manifest), "--report", str(report),
                        "--verdict", verdict, "--review-mode", mode,
                        "--validator-exit-code", str(validator_exit))

    def write_repaired(self, items: list[dict], name: str = "repaired.json") -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return path

    def freeze_delta(self, previous: Path, repaired: Path, *extra: str) -> tuple[int, dict, str]:
        return self.cli("freeze-review", "--manifest", str(self.manifest), "--review-mode", "DELTA",
                        "--previous-report", str(previous), "--repaired-findings", str(repaired), *extra)

    def rerun_validation(self) -> None:
        self.assertEqual(self.cli("run-plan", "--manifest", str(self.manifest))[0], 0)


class RoundPredicates(DeltaFixture):
    def test_happy_path_full_delta_full_and_delivery_eligibility(self):
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        code, payload, _ = self.record(report, "REQUEST_CHANGES")
        self.assertEqual(code, 0)
        self.assertFalse(payload["delivery_eligible"])
        # 范围内修复 → DELTA 冻结 → DELTA 登记（封顶 APPROVE_WITH_FOLLOW_UPS）
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "按幂等键去重"},
        ])
        self.rerun_validation()
        code, payload, err = self.freeze_delta(report, repaired)
        self.assertEqual(code, 0, err)
        handoff = self.read(Path(payload["review_handoff"]))
        self.assertEqual(handoff["review_mode"], "DELTA")
        self.assertIn("delta_recheck", handoff)
        self.assertNotIn("validation_evidence", handoff, "DELTA 轮不得携带可复用证据")
        self.assertEqual(payload["freeze_index"], 2)
        delta_report = self.write_report("r2.json", "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        code, payload, _ = self.record(delta_report, "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        self.assertEqual(code, 0)
        self.assertFalse(payload["delivery_eligible"], "DELTA 最新轮不得直接交付")
        # 最终 FULL 确认轮
        self.rerun_validation()
        code, payload, _ = self.cli("freeze-review", "--manifest", str(self.manifest))
        self.assertEqual(code, 0)
        self.assertEqual(payload["freeze_index"], 3)
        final_report = self.write_report("r3.json", "APPROVE")
        code, payload, _ = self.record(final_report, "APPROVE")
        self.assertEqual(code, 0)
        self.assertTrue(payload["delivery_eligible"])
        rounds = self.read(self.manifest)["review_rounds"]
        self.assertEqual([r["review_mode"] for r in rounds], ["FULL", "DELTA", "FULL"])

    def test_first_round_delta_rejected(self):
        report = self.write_report("r1.json", "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        with self.assertRaises(state.StateError) as caught:
            self.record(report, "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        self.assertIn("首轮", str(caught.exception))

    def test_consecutive_delta_rejected(self):
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])
        self.rerun_validation()
        self.assertEqual(self.freeze_delta(report, repaired)[0], 0)
        delta_report = self.write_report("r2.json", "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        self.assertEqual(self.record(delta_report, "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")[0], 0)
        # 第二次 DELTA：无论冻结还是登记都必须拒绝
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(report, repaired)
        self.assertIn("不得连续", str(caught.exception))
        another = self.write_report("r3.json", "REQUEST_CHANGES", mode="DELTA", blocking_ids=("F-001",))
        with self.assertRaises(state.StateError) as caught:
            self.record(another, "REQUEST_CHANGES", mode="DELTA")
        self.assertIn("不得连续", str(caught.exception))

    def test_delta_after_full_approve_rejected(self):
        report = self.write_report("r1.json", "APPROVE")
        self.assertEqual(self.record(report, "APPROVE")[0], 0)
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(report, repaired)
        self.assertIn("REQUEST_CHANGES", str(caught.exception))
        delta_report = self.write_report("r2.json", "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        with self.assertRaises(state.StateError) as caught:
            self.record(delta_report, "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        self.assertIn("REQUEST_CHANGES", str(caught.exception))

    def test_delta_report_relabeled_full_rejected(self):
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])
        self.rerun_validation()
        self.assertEqual(self.freeze_delta(report, repaired)[0], 0)
        # 报告本体是 DELTA，登记时改标 FULL 试图绕过交付门禁；先补 DELTA 冻结使配对检查
        # 不先于模式交叉校验报错，确保命中的是防改标谓词。
        delta_report = self.write_report("r2.json", "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        with self.assertRaises(state.StateError) as caught:
            self.record(delta_report, "APPROVE_WITH_FOLLOW_UPS", mode="FULL")
        self.assertIn("改标", str(caught.exception))

    def test_delta_cannot_register_approve(self):
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])
        self.rerun_validation()
        self.assertEqual(self.freeze_delta(report, repaired)[0], 0)
        delta_report = self.write_report("r2.json", "APPROVE", mode="DELTA")
        with self.assertRaises(state.StateError) as caught:
            self.record(delta_report, "APPROVE", mode="DELTA")
        self.assertIn("APPROVE", str(caught.exception))

    def test_verdict_relabeled_rejected(self):
        report = self.write_report("r1.json", "REQUEST_CHANGES")
        with self.assertRaises(state.StateError) as caught:
            self.record(report, "APPROVE")
        self.assertIn("不得改写下游结论", str(caught.exception))

    def test_nonzero_validator_exit_rejected(self):
        report = self.write_report("r1.json", "APPROVE")
        with self.assertRaises(state.StateError) as caught:
            self.record(report, "APPROVE", validator_exit=1)
        self.assertIn("结构拒绝", str(caught.exception))

    def test_record_before_freeze_rejected(self):
        self.assertEqual(self.cli("start", "--root", str(self.root), "--run-id", "no-freeze")[0], 0)
        manifest = self.root / ".codekit" / "code-fix" / "no-freeze" / "state.json"
        report = self.write_report("r1.json", "APPROVE")
        with self.assertRaises(state.StateError):
            self.cli("record-review", "--manifest", str(manifest), "--report", str(report),
                     "--verdict", "APPROVE")

    def test_fourth_freeze_rejected_budget(self):
        # FULL(REQUEST_CHANGES) → DELTA → FULL(REQUEST_CHANGES) 用尽 3 次预算
        r1 = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(r1, "REQUEST_CHANGES")[0], 0)
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])
        self.rerun_validation()
        self.assertEqual(self.freeze_delta(r1, repaired)[0], 0)
        r2 = self.write_report("r2.json", "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")
        self.assertEqual(self.record(r2, "APPROVE_WITH_FOLLOW_UPS", mode="DELTA")[0], 0)
        self.rerun_validation()
        self.assertEqual(self.cli("freeze-review", "--manifest", str(self.manifest))[0], 0)
        r3 = self.write_report("r3.json", "REQUEST_CHANGES", blocking_ids=("F-002",))
        self.assertEqual(self.record(r3, "REQUEST_CHANGES")[0], 0)
        with self.assertRaises(state.StateError) as caught:
            self.cli("freeze-review", "--manifest", str(self.manifest))
        self.assertIn("预算已用尽", str(caught.exception))


class DeltaHandoffAdmission(DeltaFixture):
    def repaired_ok(self) -> Path:
        return self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "按幂等键去重"},
        ])

    def previous_rc(self) -> Path:
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        return report

    def test_empty_repaired_findings_rejected(self):
        previous = self.previous_rc()
        empty = self.write_repaired([], name="empty.json")
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(previous, empty)
        self.assertIn("非空", str(caught.exception))

    def test_non_blocking_finding_id_rejected(self):
        previous = self.previous_rc()
        repaired = self.write_repaired([
            {"finding_id": "F-900", "locations": ["src/pay/order.py:9"], "remediation": "修非阻塞项"},
        ])
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(previous, repaired)
        self.assertIn("阻塞 Finding 集合", str(caught.exception))

    def test_unknown_finding_id_rejected(self):
        previous = self.previous_rc()
        repaired = self.write_repaired([
            {"finding_id": "F-404", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(previous, repaired)
        self.assertIn("阻塞 Finding 集合", str(caught.exception))

    def test_duplicate_finding_id_rejected(self):
        previous = self.previous_rc()
        item = {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"}
        repaired = self.write_repaired([item, dict(item)])
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(previous, repaired)
        self.assertIn("重复", str(caught.exception))

    def test_previous_report_digest_mismatch_rejected(self):
        previous = self.previous_rc()
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(previous, self.repaired_ok(), "--previous-report-digest", "0" * 64)
        self.assertIn("不符", str(caught.exception))

    def test_previous_report_must_be_full_request_changes(self):
        # 报告本体是 APPROVE：即使台账被手工篡改成 REQUEST_CHANGES 也过不了报告核实
        previous = self.write_report("r1.json", "APPROVE")
        data = self.read(self.manifest)
        data["review_rounds"] = [{"verdict": "REQUEST_CHANGES", "review_mode": "FULL"}]
        self.manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(previous, self.repaired_ok())
        self.assertIn("REQUEST_CHANGES", str(caught.exception))

    def test_missing_previous_report_or_repaired_rejected(self):
        self.previous_rc()
        self.rerun_validation()
        with self.assertRaises(state.StateError):
            self.cli("freeze-review", "--manifest", str(self.manifest), "--review-mode", "DELTA",
                     "--repaired-findings", str(self.repaired_ok()))
        with self.assertRaises(state.StateError):
            self.cli("freeze-review", "--manifest", str(self.manifest), "--review-mode", "DELTA",
                     "--previous-report", str(self.run_dir / "r1.json"))

    def test_lite_run_cannot_declare_delta(self):
        # 另开一个无文档 LITE run，走到 freeze-review 后声明 DELTA 必须拒绝
        code, _, _ = self.cli("start", "--root", str(self.root), "--run-id", "lite-run")
        self.assertEqual(code, 0)
        manifest = self.root / ".codekit" / "code-fix" / "lite-run" / "state.json"
        run_dir = manifest.parent
        proposal = run_dir / "proposal.md"
        proposal.write_text(filled_proposal(), encoding="utf-8")
        approval = run_dir / "approval.txt"
        approval.write_text("批准执行 BF-20260909-order-retry v1\n", encoding="utf-8")
        self.assertEqual(self.cli("record-proposal", "--manifest", str(manifest), "--proposal", str(proposal),
                                  "--proposal-id", "BF-20260909-order-retry", "--version", "1",
                                  "--review-mode", "LITE")[0], 0)
        self.assertEqual(self.cli("verify-approval", "--manifest", str(manifest),
                                  "--approval-file", str(approval), "--proposal", str(proposal))[0], 0)
        self.assertEqual(self.cli("freeze-contract", "--manifest", str(manifest))[0], 0)
        plan_path = run_dir / "validation-plan.json"
        plan = self.read(plan_path)
        checks = [{"name": "回归", "proves": "a", "required": True, "command": [sys.executable, "ok.py"]}]
        plan["checks"] = checks
        # 模拟合法新版合同：同步合同冻结的验证计划并重算自摘要。
        contract_path = run_dir / "implementation-contract.json"
        contract = self.read(contract_path)
        contract["validation_commands"] = checks
        contract["contract_digest"] = state.canonical_digest(
            {key: value for key, value in contract.items() if key != "contract_digest"}
        )
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        plan["contract_digest"] = contract["contract_digest"]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.cli("run-plan", "--manifest", str(manifest))[0], 0)
        self.assertEqual(self.cli("freeze-review", "--manifest", str(manifest))[0], 0)
        report = run_dir / "r1.json"
        report.write_text(json.dumps(make_report("REQUEST_CHANGES", blocking_ids=("F-001",))), encoding="utf-8")
        self.assertEqual(self.cli("record-review", "--manifest", str(manifest), "--report", str(report),
                                  "--verdict", "REQUEST_CHANGES")[0], 0)
        repaired = run_dir / "repaired.json"
        repaired.write_text(json.dumps([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ]), encoding="utf-8")
        with self.assertRaises(state.StateError) as caught:
            self.cli("freeze-review", "--manifest", str(manifest), "--review-mode", "DELTA",
                     "--previous-report", str(report), "--repaired-findings", str(repaired))
        self.assertIn("FULL 模式", str(caught.exception))


class FreezePairingAdversarial(DeltaFixture):
    """M2/M3/L3 对抗用例：换报告、轮次与冻结配对、DELTA 内联范围谓词。"""

    def previous_rc(self) -> Path:
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        return report

    def repaired_ok(self) -> Path:
        return self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ])

    def test_swapped_previous_report_rejected(self):
        # M2：另造一份 FULL/REQUEST_CHANGES 报告缩小阻塞集合，不得掉包台账登记的那一份
        self.previous_rc()
        fake = self.write_report("fake.json", "REQUEST_CHANGES", blocking_ids=("F-777",))
        fake_repaired = self.write_repaired([
            {"finding_id": "F-777", "locations": ["src/pay/order.py:1"], "remediation": "修复"},
        ], name="fake-repaired.json")
        self.rerun_validation()
        with self.assertRaises(state.StateError) as caught:
            self.freeze_delta(fake, fake_repaired)
        self.assertIn("台账登记的报告摘要不符", str(caught.exception))

    def test_full_record_on_delta_freeze_rejected(self):
        # M3：不做第三次冻结，直接把 FULL 确认轮登记在 DELTA 冻结上
        report = self.previous_rc()
        self.rerun_validation()
        self.assertEqual(self.freeze_delta(report, self.repaired_ok())[0], 0)
        full_report = self.write_report("r2.json", "APPROVE")
        with self.assertRaises(state.StateError) as caught:
            self.record(full_report, "APPROVE", mode="FULL")
        self.assertIn("模式", str(caught.exception))

    def test_second_record_on_same_freeze_rejected(self):
        # M3：同一冻结上补登第二轮必须拒绝（每轮消耗新冻结）
        report = self.write_report("r1.json", "REQUEST_CHANGES")
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        another = self.write_report("r2.json", "APPROVE")
        with self.assertRaises(state.StateError) as caught:
            self.record(another, "APPROVE")
        self.assertIn("新冻结", str(caught.exception))

    def test_delta_freeze_inlines_repair_scope(self):
        # L3：跳过 check-repair-scope 直接冻结 DELTA，越界修复仍被内联谓词拦下
        self.previous_rc()
        (self.root / "src" / "sneaky.py").write_text("escape\n", encoding="utf-8")
        self.rerun_validation()
        code, payload, _ = self.freeze_delta(self.run_dir / "r1.json", self.repaired_ok())
        self.assertEqual(code, 1)
        self.assertEqual(payload["repair_scope"]["scope"]["out_of_scope"], ["src/sneaky.py"])

    def test_delta_freeze_inlines_gate_change(self):
        # L3：承重字段变化（扩允许范围）同样被内联谓词拦下
        self.previous_rc()
        self.proposal.write_text(
            filled_proposal().replace("src/pay/order.py, tests/test_pay.py",
                                      "src/pay/order.py, tests/test_pay.py, src/pay/extra.py"),
            encoding="utf-8")
        self.rerun_validation()
        code, payload, _ = self.freeze_delta(self.run_dir / "r1.json", self.repaired_ok())
        self.assertEqual(code, 1)
        self.assertFalse(payload["repair_scope"]["proposal_gate_match"])


class CrossSkillDeltaHandoff(DeltaFixture):
    def test_delta_handoff_passes_downstream_freeze(self):
        """真产物验收：DELTA handoff 交给 code-review --handoff 比对器不得 BLOCKED。"""
        report = self.write_report("r1.json", "REQUEST_CHANGES", blocking_ids=("F-001",))
        self.assertEqual(self.record(report, "REQUEST_CHANGES")[0], 0)
        repaired = self.write_repaired([
            {"finding_id": "F-001", "locations": ["src/pay/order.py:1"], "remediation": "按幂等键去重"},
        ])
        self.rerun_validation()
        code, payload, err = self.freeze_delta(report, repaired)
        self.assertEqual(code, 0, err)
        freeze = Path(state.__file__).resolve().parents[2] / "code-review" / "scripts" / "freeze_review_snapshot.py"
        self.assertTrue(freeze.is_file(), f"跨 SKILL 验收需要 {freeze}")
        head = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        result = subprocess.run(
            [sys.executable, str(freeze), "--repo", str(self.root),
             "--base-sha", head, "--head-sha", head,
             "--target-type", "local_diff", "--target-locator", "test",
             "--emit-manifest", str(self.run_dir / "code-review-snapshot.json"),
             "--handoff", str(payload["review_handoff"])],
            capture_output=True, text=True, check=False, cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        block = self.read(self.run_dir / "code-review-snapshot.json")["handoff"]
        self.assertTrue(block["target_id_matched"])
        self.assertNotIn("validation_evidence", block, "DELTA 轮不得携带可复用证据")


class RepairScopeBranch(DeltaFixture):
    def test_in_scope_repair_passes(self):
        code, payload, err = self.cli("check-repair-scope", "--manifest", str(self.manifest),
                                      "--contract", str(self.contract))
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["proposal_gate_match"])
        self.assertEqual(payload["scope"]["verdict"], "OK")

    def test_out_of_scope_repair_fails(self):
        (self.root / "src" / "sneaky.py").write_text("escape\n", encoding="utf-8")
        code, payload, _ = self.cli("check-repair-scope", "--manifest", str(self.manifest),
                                    "--contract", str(self.contract))
        self.assertEqual(code, 1)
        self.assertEqual(payload["scope"]["out_of_scope"], ["src/sneaky.py"])

    def test_gate_field_change_fails(self):
        self.proposal.write_text(
            filled_proposal().replace("src/pay/order.py, tests/test_pay.py",
                                      "src/pay/order.py, tests/test_pay.py, src/pay/extra.py"),
            encoding="utf-8")
        code, payload, _ = self.cli("check-repair-scope", "--manifest", str(self.manifest),
                                    "--contract", str(self.contract))
        self.assertEqual(code, 1)
        self.assertFalse(payload["proposal_gate_match"])

    def test_tampered_contract_fails(self):
        contract = self.read(self.contract)
        contract["allowed_write_files"].append("src/any.py")
        self.contract.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        code, payload, _ = self.cli("check-repair-scope", "--manifest", str(self.manifest),
                                    "--contract", str(self.contract))
        self.assertEqual(code, 1)
        self.assertEqual(payload["scope"]["contract_integrity"], "TAMPERED")


if __name__ == "__main__":
    unittest.main()