"""P0-1 验证证据复用接线的回归与对抗测试。

覆盖三件事：
1. 验证记录的工作树绑定（head_sha/diff_digest/command_text）与 freeze-review 导出形状；
2. 真产物交给 code-review `--handoff` 比对器的跨技能验收（含逐文件绑定与反向集合核对）；
3. 对抗用例：树漂移、新增文件、陈旧记录、必需项未 PASS、缺审批时不得导出或复用。

运行：python3 -m unittest discover -s skills/code-fix/tests -p 'test_*.py'
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = SKILL_DIR / "assets" / "code-fix-proposal-template.md"


def load_script(name: str):
    path = SKILL_DIR / "scripts" / name
    module_name = name.removesuffix(".py").replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


state = load_script("code_fix_state.py")
validator = load_script("validate_code_fix_proposal.py")

# 与 test_code_fix_skill_scripts.py 同一份「填完即可通过」映射的机械字段子集：
# 本文件只关心 freeze-review 之前的最短合规路径，判断性字段用同一口径填充。
FILLED = {
    "`CONFIRMED_BUG | NOT_A_BUG | INSUFFICIENT_EVIDENCE`": "CONFIRMED_BUG",
    "`High | Medium | Low` / `E3 | E2 | E1 | E0`": "High / E2",
    "`<repository, branch, HEAD SHA, working-tree digest>`": "repo=/w shop branch=main HEAD=1111111 tree=aaa",
    "`<PRD/TDD/项目规范的版本、章节和明确约束>`": "TDD v3 §4.2 要求扣款幂等",
    "`<可复核现象>`": "同一请求重复扣款 2 次",
    "`<期望与实际的最小差异>`": "期望扣款 1 次，实际 2 次",
    "`<用户或系统观察到的结果>`": "订单金额翻倍",
    "`<前置状态、输入、并发、配置、依赖状态>`": "客户端重试与网关超时同时发生",
    "`<精确说明错误决策或状态如何违反业务/技术不变量>`": "charge() 未校验幂等键，违反扣款唯一性不变量",
    "`<文档/代码/日志/命令/测试证据>`": "pay.log 第 201 行两次 charge 成功",
    "`<从输入到症状的因果步骤>`": "重试 → 新 request_id → 跳过幂等 → 二次扣款",
    "`<H01>`": "H02",
    "`Rejected/Supported/Insufficient`": "Rejected",
    "`<evidence>`": "上游重试日志显示 request_id 每次都重新生成",
    "`<BF-YYYYMMDD-short-name>`": "BF-20260909-order-retry",
    "`<integer>`": "1",
    "`<在正确责任层消除根因的方案>`": "在 charge() 入口按幂等键去重",
    "`<正确性、影响面、成本、兼容性、可验证性>`": "修复位于责任层，仅 2 文件，向后兼容",
    "`<HEAD SHA + working-tree/document digest>`": "1111111 + bbb",
    "`<repo-relative-path:symbol>`": "src/pay/order.py:charge",
    "`<change>`": "新增幂等键查询",
    "`<root-cause relation>`": "消除重复扣款根因",
    "`<exact repo-relative paths>`": "src/pay/order.py, tests/test_pay.py",
    "`<模块、接口、Schema、配置、依赖、业务行为>`": "不改对账 Job、不改 Schema",
    "`<business invariants and negative scope>`": "扣款唯一性、退款语义不变",
    "`<alternatives>`": "网关侧限流：只压症状，未消除根因",
    "`<S 档小修复必填：业务建模只覆盖症状路径上的哪些参与者/权限/实体/不变量；M 档写不裁剪>`": (
        "S 档：建模只覆盖扣款路径的调用方权限与扣款唯一性不变量"
    ),
    "`<...>`": "无影响",
    "`<implementation/test/release estimate>`": "0.5 人日 + 1 条回归测试",
    "`<residual risks>`": "历史脏数据需人工核对",
    "`<修复前失败、修复后通过的场景>`": "同幂等键重复扣款回归",
    "`<check>`": "回归测试",
    "`<command or method>`": "ok.py",
    "`<claim>`": "根因已消除",
    "`Yes/No`": "`Yes`",
    "`<仅列自动化无法证明的行为>`": "线上灰度 1 小时观察对账",
    "<proposal_id>": "BF-20260909-order-retry",
    "<proposal_version>": "1",
}


def filled_proposal() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    for source, target in FILLED.items():
        text = text.replace(source, target)
    return text


class FreezeReviewFixture(unittest.TestCase):
    """走完 审批 → 合同 → 实施 → 验证 的最短合规路径，停在 freeze-review 之前。"""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git 不可用")
        self.workspace = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.workspace.cleanup)
        # macOS 临时目录符号链接：夹具先 realpath，与脚本内部 resolve 口径一致。
        self.root = Path(self.workspace.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "gc.auto", "0"], check=True)
        (self.root / "src").mkdir()
        (self.root / "src" / "pay.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "init"], check=True)
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.root / "bad.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        self.assertEqual(self.cli("start", "--root", str(self.root), "--run-id", "20260909-retry")[0], 0)
        self.manifest = self.root / ".codekit" / "code-fix" / "20260909-retry" / "state.json"
        self.run_dir = self.manifest.parent
        self.proposal = self.run_dir / "proposal.md"
        self.proposal.write_text(filled_proposal(), encoding="utf-8")
        self.approval = self.run_dir / "approval.txt"
        self.approval.write_text("批准执行 BF-20260909-order-retry v1，按当前绑定快照和允许修改文件实施。\n",
                                 encoding="utf-8")
        self.assertEqual(self.cli("record-proposal", "--manifest", str(self.manifest), "--proposal", str(self.proposal),
                                  "--proposal-id", "BF-20260909-order-retry", "--version", "1",
                                  "--review-mode", "LITE")[0], 0)
        self.assertEqual(self.cli("verify-approval", "--manifest", str(self.manifest),
                                  "--approval-file", str(self.approval), "--proposal", str(self.proposal))[0], 0)
        _, payload, _ = self.cli("freeze-contract", "--manifest", str(self.manifest))
        self.contract = Path(payload["contract"])
        # 实施前的最小 handoff；实施后工作树已漂移，freeze-contract 的内联门禁会拒绝重跑，
        # 这正是 P0-2 的预期行为，所以在这里先留底。
        self.minimal_handoff = self.read(Path(payload["review_handoff"]))
        # 实施：只改契约允许的文件，另加回归测试脚本（未跟踪）。
        (self.root / "src" / "pay").mkdir()
        (self.root / "src" / "pay" / "order.py").write_text("def charge():\n    return 1  # fixed\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_pay.py").write_text("def test_charge():\n    assert True\n", encoding="utf-8")
        self.rewrite_plan([
            {"name": "回归", "proves": "根因已消除", "required": True, "command": [sys.executable, "ok.py"]},
        ])

    def cli(self, *argv: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(list(argv))
        text = out.getvalue()
        return code, json.loads(text) if text.lstrip().startswith("{") else {}, err.getvalue()

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def rewrite_plan(self, checks: list[dict]) -> None:
        plan_path = self.run_dir / "validation-plan.json"
        plan = self.read(plan_path)
        plan["checks"] = checks
        # 模拟合法的新版合同：同步合同冻结的验证计划并重算合同自摘要；
        # 只改 plan 不改合同的篡改形态由对抗用例覆盖。
        contract_path = self.run_dir / "implementation-contract.json"
        contract = self.read(contract_path)
        contract["validation_commands"] = checks
        contract["contract_digest"] = state.canonical_digest(
            {key: value for key, value in contract.items() if key != "contract_digest"}
        )
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        plan["contract_digest"] = contract["contract_digest"]
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def run_plan(self) -> tuple[int, dict]:
        code, payload, _ = self.cli("run-plan", "--manifest", str(self.manifest))
        return code, payload

    def freeze_review(self) -> tuple[int, dict, str]:
        return self.cli("freeze-review", "--manifest", str(self.manifest))

    def downstream_freeze(self, handoff_path: Path) -> subprocess.CompletedProcess:
        """把本 Skill 真产物交给 code-review 的 --handoff 比对器（跨技能验收）。"""
        freeze = SKILL_DIR.parent / "code-review" / "scripts" / "freeze_review_snapshot.py"
        self.assertTrue(freeze.is_file(), f"跨 SKILL 验收需要 {freeze}")
        head = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        return subprocess.run(
            [sys.executable, str(freeze), "--repo", str(self.root),
             "--base-sha", head, "--head-sha", head,
             "--target-type", "local_diff", "--target-locator", "test",
             "--emit-manifest", str(self.run_dir / "code-review-snapshot.json"),
             "--handoff", str(handoff_path)],
            capture_output=True, text=True, check=False, cwd=self.root,
        )


class RecordBinding(FreezeReviewFixture):
    def test_records_carry_worktree_binding_and_command_text(self):
        code, _ = self.run_plan()
        self.assertEqual(code, 0)
        record = self.read(self.run_dir / "validation-record.json")
        entry = record["checks"][0]
        self.assertRegex(entry["diff_digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(entry["head_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(entry["command_text"], " ".join([sys.executable, "ok.py"]))
        self.assertEqual(entry["name"], "回归")

    def test_command_text_roundtrips_spaces_and_quotes(self):
        code, payload, _ = self.cli("run-validation", "--manifest", str(self.manifest),
                                    "--proves", "带空格参数", "--", sys.executable, "ok.py", "a b", "c'd")
        self.assertEqual(code, 0, payload)
        record = self.read(self.run_dir / "validation-record.json")
        entry = record["checks"][-1]
        import shlex
        self.assertEqual(shlex.split(entry["command_text"]), [sys.executable, "ok.py", "a b", "c'd"])

    def test_non_git_cwd_records_null_binding(self):
        with tempfile.TemporaryDirectory() as plain:
            (Path(plain) / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            record = Path(plain) / "record.json"
            code, payload, _ = self.cli("run-validation", "--cwd", plain, "--record", str(record),
                                        "--", sys.executable, "ok.py")
            self.assertEqual(code, 0, payload)
            entry = self.read(record)["checks"][0]
            self.assertIsNone(entry["diff_digest"])
            self.assertIsNone(entry["head_sha"])


class FreezeReviewExport(FreezeReviewFixture):
    def test_export_shape_mirrors_code_impl_evidence(self):
        self.assertEqual(self.run_plan()[0], 0)
        code, payload, err = self.freeze_review()
        self.assertEqual(code, 0, err)
        evidence = self.read(Path(payload["validation_evidence"]))
        self.assertEqual(set(evidence), {"produced_by", "target_id", "worktree_diff_digest", "worktree_files", "records"})
        self.assertEqual(evidence["produced_by"], state.PRODUCED_BY)
        self.assertEqual(evidence["target_id"], payload["target_id"])
        self.assertEqual(evidence["worktree_diff_digest"], payload["worktree_diff_digest"])
        self.assertTrue(all(set(item) == {"path", "digest"} for item in evidence["worktree_files"]))
        # 逐文件摘要口径与 code-impl/_worktree_file_digest 一致：sha256: 前缀。
        self.assertTrue(all(item["digest"].startswith("sha256:") for item in evidence["worktree_files"]))
        record = evidence["records"][0]
        self.assertEqual(record["name"], "回归")
        self.assertEqual(record["result"], "PASS")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["diff_digest"], evidence["worktree_diff_digest"])
        self.assertIsInstance(record["command"], str)
        # .codekit 整体排除：产物目录不得出现在证据的工作树清单里。
        self.assertFalse(any(item["path"].startswith(".codekit") for item in evidence["worktree_files"]))

    def test_handoff_enriched_and_target_unchanged(self):
        self.assertEqual(self.run_plan()[0], 0)
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 0)
        handoff = self.read(Path(payload["review_handoff"]))
        self.assertEqual(handoff["target_id"], self.minimal_handoff["target_id"])
        self.assertIsNone(handoff["committed_range"])
        self.assertEqual(handoff["worktree_state"]["diff_digest"], payload["worktree_diff_digest"])
        self.assertTrue(all(":" in item for item in handoff["worktree_state"]["changed_files"]))
        self.assertIn("A:src/pay/order.py", handoff["worktree_state"]["changed_files"])
        self.assertEqual(handoff["validation_evidence"]["path"], payload["validation_evidence"])
        freezes = self.read(self.manifest)["review_freezes"]
        self.assertEqual(len(freezes), 1)
        self.assertEqual(freezes[0]["diff_digest"], payload["worktree_diff_digest"])

    def test_stale_records_excluded_after_tree_moves(self):
        self.assertEqual(self.run_plan()[0], 0)
        (self.root / "src" / "pay" / "order.py").write_text("def charge():\n    return 2  # moved\n", encoding="utf-8")
        # 同名同命令重跑：最新记录绑定新树，旧记录不得被导出复用
        self.assertEqual(self.run_plan()[0], 0)
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 0)
        evidence = self.read(Path(payload["validation_evidence"]))
        self.assertEqual(payload["exported_records"], 1, "树动过之后旧记录不得被导出复用")
        record = evidence["records"][0]
        self.assertEqual(record["diff_digest"], payload["worktree_diff_digest"])

    def test_all_stale_without_rerun_refuses_freeze(self):
        # M1：验证后动树且不重跑——必需项在当前树上无 PASS，不得冻结出一棵从未验证过的树
        self.assertEqual(self.run_plan()[0], 0)
        (self.root / "src" / "pay" / "order.py").write_text("def charge():\n    return 9  # moved\n", encoding="utf-8")
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 1)
        self.assertEqual(payload["unsatisfied_required"], ["回归"])
        self.assertFalse((self.run_dir / "review-validation-evidence.json").exists())

    def test_required_not_pass_refuses_export(self):
        self.rewrite_plan([
            {"name": "失败", "proves": "a", "required": True, "command": [sys.executable, "bad.py"]},
        ])
        self.assertEqual(self.run_plan()[0], 1)
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 1)
        self.assertEqual(payload["unsatisfied_required"], ["失败"])
        self.assertFalse((self.run_dir / "review-validation-evidence.json").exists())

    def test_missing_record_refuses_export(self):
        with self.assertRaises(state.StateError) as caught:
            self.freeze_review()
        self.assertIn("run-plan", str(caught.exception))

    def test_missing_approval_refuses_export(self):
        self.assertEqual(self.cli("start", "--root", str(self.root), "--run-id", "no-approval")[0], 0)
        manifest = self.root / ".codekit" / "code-fix" / "no-approval" / "state.json"
        with self.assertRaises(state.StateError):
            self.cli("freeze-review", "--manifest", str(manifest))


class FreezeReviewGateAdversarial(FreezeReviewFixture):
    """H1/M1 对抗用例：同名覆盖、RV 自动名碰撞、plan 篡改、全陈旧树。"""

    def test_duplicate_required_name_different_command_cannot_mask(self):
        # 两行同名必需检查：第一条 FAIL、第二条 PASS，不得互相掩蔽
        self.rewrite_plan([
            {"name": "回归", "proves": "a", "required": True, "command": [sys.executable, "bad.py"]},
            {"name": "回归", "proves": "b", "required": True, "command": [sys.executable, "ok.py"]},
        ])
        code, _ = self.run_plan()
        self.assertEqual(code, 1)
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 1, "同名异命令的必需 FAIL 不得被同名 PASS 掩蔽")
        self.assertIn("回归", payload["unsatisfied_required"])

    def test_rv_auto_name_collision_cannot_mask_required_fail(self):
        # 必需检查恰好叫 RV-002 且 FAIL；随后 run-validation 自动得名 RV-002 PASS（精确碰撞）
        self.rewrite_plan([
            {"name": "RV-002", "proves": "a", "required": True, "command": [sys.executable, "bad.py"]},
        ])
        self.assertEqual(self.run_plan()[0], 1)
        code, _, _ = self.cli("run-validation", "--manifest", str(self.manifest),
                              "--proves", "无关检查", "--", sys.executable, "ok.py")
        self.assertEqual(code, 0)
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 1, "RV 自动名碰撞不得掩蔽必需 FAIL")
        self.assertIn("RV-002", payload["unsatisfied_required"])

    def test_plan_required_flag_tamper_rejected(self):
        # 把失败项的 required 改成 false 但不更新合同摘要：plan-合同绑定必须拒绝
        self.rewrite_plan([
            {"name": "失败", "proves": "a", "required": True, "command": [sys.executable, "bad.py"]},
        ])
        self.assertEqual(self.run_plan()[0], 1)
        plan_path = self.run_dir / "validation-plan.json"
        plan = self.read(plan_path)
        plan["checks"][0]["required"] = False
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 1)
        self.assertTrue(payload["plan_contract_mismatch"])

    def test_plan_command_swap_with_stale_digest_rejected(self):
        # 换掉失败命令但保留旧摘要：同样被 plan-合同绑定拒绝
        self.rewrite_plan([
            {"name": "回归", "proves": "a", "required": True, "command": [sys.executable, "bad.py"]},
        ])
        self.assertEqual(self.run_plan()[0], 1)
        plan_path = self.run_dir / "validation-plan.json"
        plan = self.read(plan_path)
        plan["checks"][0]["command"] = [sys.executable, "ok.py"]
        plan["checks"][0]["command_text"] = " ".join([sys.executable, "ok.py"])
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 1)
        self.assertTrue(payload["plan_contract_mismatch"])


class CrossSkillHandoff(FreezeReviewFixture):
    def test_enriched_handoff_passes_downstream_freeze_with_evidence(self):
        self.assertEqual(self.run_plan()[0], 0)
        code, payload, _ = self.freeze_review()
        self.assertEqual(code, 0)
        result = self.downstream_freeze(Path(payload["review_handoff"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        block = self.read(self.run_dir / "code-review-snapshot.json")["handoff"]
        self.assertTrue(block["target_id_matched"])
        self.assertIn("head_sha", block["compared_fields"])
        # diff_digest 的 material 与 code-review 自家不同：记 uncomparable，不是失败。
        self.assertEqual(len(block["uncomparable_digests"]), 1)
        self.assertEqual(block["limitations"], [])
        evidence = block["validation_evidence"]
        self.assertEqual(evidence["path"], str(Path(payload["validation_evidence"]).resolve()))
        self.assertEqual(evidence["worktree_diff_digest"], payload["worktree_diff_digest"])

    def test_tree_drift_after_freeze_is_rejected_downstream(self):
        self.assertEqual(self.run_plan()[0], 0)
        _, payload, _ = self.freeze_review()
        (self.root / "src" / "pay" / "order.py").write_text("def charge():\n    return 9  # drifted\n", encoding="utf-8")
        result = self.downstream_freeze(Path(payload["review_handoff"]))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("drifted", result.stderr)

    def test_new_file_after_freeze_is_rejected_downstream(self):
        self.assertEqual(self.run_plan()[0], 0)
        _, payload, _ = self.freeze_review()
        (self.root / "sneaky.py").write_text("gained after freeze\n", encoding="utf-8")
        result = self.downstream_freeze(Path(payload["review_handoff"]))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("gained", result.stderr)

    def test_tampered_evidence_file_is_rejected_downstream(self):
        self.assertEqual(self.run_plan()[0], 0)
        _, payload, _ = self.freeze_review()
        evidence_path = Path(payload["validation_evidence"])
        evidence = self.read(evidence_path)
        evidence["records"][0]["command"] += " --extra"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        result = self.downstream_freeze(Path(payload["review_handoff"]))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("digest", result.stderr)


if __name__ == "__main__":
    unittest.main()