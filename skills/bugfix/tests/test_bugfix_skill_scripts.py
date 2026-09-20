"""Bugfix Skill 校验脚本的回归测试。

覆盖两件事：
1. 提案模板与 ``validate_bugfix_proposal.py`` 的一致性（模板是唯一事实源）；
2. ``bugfix_state.py`` 的快照冻结、范围校验与验证执行语义。

运行：python3 -m unittest discover -s skills/bugfix/tests -p 'test_*.py'
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = SKILL_DIR / "assets" / "bugfix-proposal-template.md"


def load_script(name: str):
    path = SKILL_DIR / "scripts" / name
    module_name = name.removesuffix(".py").replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 必须先注册再 exec：否则 Python 3.14 的 @dataclass 处理找不到模块命名空间。
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


validator = load_script("validate_bugfix_proposal.py")
state = load_script("bugfix_state.py")

# 一份「填完即可通过」的最小合规提案：模板里每个占位符都必须被显式替换，
# 因此这张映射表同时也是模板改动的回归守卫。
FILLED = {
    "`CONFIRMED_BUG | NOT_A_BUG | INSUFFICIENT_EVIDENCE`": "CONFIRMED_BUG",
    "`High | Medium | Low` / `E3 | E2 | E1 | E0`": "High / E2",
    "`<repository, branch, HEAD SHA, working-tree digest>`": "repo=/w shop branch=main HEAD=1111111 tree=sha256:aaa",
    "`<PRD/TDD/项目规范的版本、章节和明确约束>`": "TDD v3 §4.2 要求扣款幂等",
    "`<可复核现象>`": "同一请求重复扣款 2 次",
    "`<期望与实际的最小差异>`": "期望扣款 1 次，实际 2 次",
    "`<用户或系统观察到的结果>`": "订单金额翻倍",
    "`<前置状态、输入、并发、配置、依赖状态>`": "客户端重试与网关超时同时发生",
    "`<精确说明错误决策或状态如何违反业务/技术不变量>`": (
        "charge() 返回 Map<String,Object> 时未校验幂等键，违反扣款唯一性不变量"
    ),
    "`<文档/代码/日志/命令/测试证据>`": "pay.log 第 201 行两次 charge 成功",
    "`<从输入到症状的因果步骤>`": "重试 → 新 request_id → 跳过幂等 → 二次扣款",
    "`<H01>`": "H02",
    "`Rejected/Supported/Insufficient`": "Rejected",
    "`<evidence>`": "上游重试日志显示 request_id 每次都重新生成",
    "`<BF-YYYYMMDD-short-name>`": "BF-20260909-order-retry",
    "`<integer>`": "1",
    "`<在正确责任层消除根因的方案>`": "在 charge() 入口按幂等键去重",
    "`<正确性、影响面、成本、兼容性、可验证性>`": "修复位于责任层，仅 2 文件，向后兼容",
    "`<HEAD SHA + working-tree/document digest>`": "1111111 + sha256:bbb",
    "`<repo-relative-path:symbol>`": "src/pay/order.py:charge",
    "`<change>`": "新增幂等键查询",
    "`<root-cause relation>`": "消除重复扣款根因",
    "`<exact repo-relative paths>`": "src/pay/order.py, tests/test_pay.py",
    "`<模块、接口、Schema、配置、依赖、业务行为>`": "不改对账 Job、不改 Schema",
    "`<business invariants and negative scope>`": "扣款唯一性、退款语义不变",
    "`<alternatives>`": "网关侧限流：只压症状，未消除根因",
    "`<...>`": "无影响",
    "`<implementation/test/release estimate>`": "0.5 人日 + 1 条回归测试",
    "`<residual risks>`": "历史脏数据需人工核对",
    "`<修复前失败、修复后通过的场景>`": "同幂等键重复扣款回归",
    "`<check>`": "回归测试",
    "`<command or method>`": "pytest tests/test_pay.py -k idempotent",
    "`<claim>`": "根因已消除",
    "`Yes/No`": "`Yes`",
    "`<仅列自动化无法证明的行为>`": "线上灰度 1 小时观察对账",
    "<proposal_id>": "BF-20260909-order-retry",
    "<proposal_version>": "1",
}


def filled_proposal(**overrides) -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    for source, target in FILLED.items():
        text = text.replace(source, target)
    for pattern, replacement in overrides.items():
        text = re.sub(pattern, replacement, text)
    return text


class ProposalFixture(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)

    def write(self, text: str) -> Path:
        path = self.root / "proposal.md"
        path.write_text(text, encoding="utf-8")
        return path

    def errors(self, text: str, *, draft: bool = False) -> list[str]:
        return validator.validate(self.write(text), TEMPLATE, draft)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = validator.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class TemplateDerivedSchema(ProposalFixture):
    def test_schema_matches_template(self):
        schema = validator.load_schema(TEMPLATE)
        self.assertEqual([spec.number for spec in schema.sections], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual(schema.status_label, "判定")
        self.assertEqual(schema.proposal_start, 3)
        # 「至少一项必需检查」的列名与 8 个影响面维度都来自模板本身。
        section4 = next(spec for spec in schema.sections if spec.number == "4")
        self.assertEqual(len(section4.required_rows), 8)
        self.assertIn("业务逻辑", section4.required_rows)
        section5 = next(spec for spec in schema.sections if spec.number == "5")
        self.assertEqual(section5.mandatory_column, "必需")

    def test_rejects_template_without_region_marker(self):
        broken = self.root / "broken-template.md"
        broken.write_text(re.sub(r"<!-- bugfix:proposal-required-start -->", "", TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
        with self.assertRaises(validator.ValidationError):
            validator.load_schema(broken)

    def test_rejects_template_missing_anchor_label(self):
        broken = self.root / "renamed-template.md"
        broken.write_text(
            TEMPLATE.read_text(encoding="utf-8").replace("**允许修改文件**", "**可改文件**"),
            encoding="utf-8",
        )
        with self.assertRaises(validator.ValidationError) as caught:
            validator.load_schema(broken)
        self.assertIn("允许修改文件", str(caught.exception))


class DraftAndContentModes(ProposalFixture):
    def test_draft_mode_accepts_unfilled_template(self):
        code, out, _ = self.run_cli(str(TEMPLATE), "--allow-placeholders")
        self.assertEqual(code, 0, out)
        self.assertIn("OK[draft]", out)

    def test_content_mode_rejects_unfilled_template(self):
        errors = self.errors(TEMPLATE.read_text(encoding="utf-8"))
        self.assertTrue(any("未替换的模板占位符" in error for error in errors), errors)

    def test_filled_sample_passes(self):
        self.assertEqual(self.errors(filled_proposal()), [])

    def test_generics_are_not_treated_as_placeholders(self):
        # 「根因」里含 Map<String,Object>，不应被误判成占位符。
        self.assertEqual(self.errors(filled_proposal()), [])

    def test_chinese_affirmative_column_accepted(self):
        text = filled_proposal().replace("`Yes`", "`是`")
        self.assertEqual(self.errors(text), [])

    def test_missing_mandatory_column_reported(self):
        text = filled_proposal().replace(
            "| 检查 | 命令/方法 | 证明内容 | 必需 |", "| 检查 | 命令/方法 | 证明内容 |"
        )
        errors = self.errors(text)
        self.assertTrue(any("缺少『必需』" in error for error in errors), errors)

    def test_no_required_check_reported(self):
        text = filled_proposal().replace("`Yes`", "`No`")
        errors = self.errors(text)
        self.assertTrue(any("至少需要一项" in error for error in errors), errors)

    def test_yes_in_other_table_does_not_satisfy_verification(self):
        text = filled_proposal().replace(
            "| H02 | Rejected | 上游重试日志显示 request_id 每次都重新生成 |",
            "| H02 | `Yes` | 上游重试日志显示 request_id 每次都重新生成 |",
        ).replace("| 回归测试 | pytest tests/test_pay.py -k idempotent | 根因已消除 | `Yes` |",
                  "| 回归测试 | pytest tests/test_pay.py -k idempotent | 根因已消除 | No |")
        errors = self.errors(text)
        self.assertTrue(any("至少需要一项" in error for error in errors), errors)


class GateSemantics(ProposalFixture):
    def test_missing_section_reported(self):
        text = filled_proposal().replace("## 4. 影响面与风险", "## 4. 影响面")
        errors = self.errors(text)
        self.assertIn("缺少必需章节：## 4. 影响面与风险", errors)

    def test_low_evidence_level_rejected(self):
        text = filled_proposal().replace("High / E2", "High / E1")
        errors = self.errors(text)
        self.assertTrue(any("证据等级不足" in error for error in errors), errors)

    def test_open_write_scope_rejected(self):
        text = filled_proposal().replace("src/pay/order.py, tests/test_pay.py", "全部相关文件按需修改")
        errors = self.errors(text)
        self.assertTrue(any("开放表述" in error for error in errors), errors)

    def test_absolute_path_rejected(self):
        text = filled_proposal().replace("src/pay/order.py, tests/test_pay.py", "/etc/pay.py, ../x.py")
        errors = self.errors(text)
        self.assertEqual(len([error for error in errors if "仓库相对路径" in error]), 2, errors)

    def test_approval_phrase_must_bind_version(self):
        text = filled_proposal().replace("- **方案版本**：1", "- **方案版本**：2")
        errors = self.errors(text)
        self.assertTrue(any("审批口令必须明确绑定" in error for error in errors), errors)

    def test_status_must_be_waiting_approval(self):
        text = filled_proposal().replace("- **状态**：`WAITING_APPROVAL`", "- **状态**：`APPROVED`")
        errors = self.errors(text)
        self.assertTrue(any("必须为 WAITING_APPROVAL" in error for error in errors), errors)

    def test_field_label_must_be_list_item(self):
        text = filled_proposal().replace("- **回归测试**：同幂等键重复扣款回归", "| **回归测试** | 同幂等键重复扣款回归 |")
        errors = self.errors(text)
        self.assertTrue(any("缺少字段：回归测试" in error for error in errors), errors)

    def test_impact_row_renamed_is_reported(self):
        text = filled_proposal().replace("| 业务逻辑 |", "| 业务 |")
        errors = self.errors(text)
        self.assertTrue(any("影响面缺少维度行：业务逻辑" in error for error in errors), errors)

    def as_not_a_bug(self, text: str) -> str:
        return text.replace(
            "- **判定**：CONFIRMED_BUG",
            "- **判定**：NOT_A_BUG\n\n本次不执行修复：客户端未按 TDD §4.2 传入幂等键。\n",
        )

    def test_not_a_bug_skips_proposal_sections_but_requires_no_fix(self):
        self.assertEqual(self.errors(self.as_not_a_bug(filled_proposal())), [])
        text = self.as_not_a_bug(filled_proposal()).replace("本次不执行修复", "后续再说")
        errors = self.errors(text)
        self.assertTrue(any("必须明确说明不执行修复" in error for error in errors), errors)

    def test_not_a_bug_does_not_enforce_proposal_field_values(self):
        text = self.as_not_a_bug(filled_proposal()).replace(
            "- **允许修改文件**：src/pay/order.py, tests/test_pay.py",
            "- **允许修改文件**：`<exact repo-relative paths>`",
        )
        # 非 CONFIRMED_BUG 不校验方案区字段内容；但残留占位符仍会被全局拦截。
        errors = self.errors(text)
        self.assertTrue(any("未替换的模板占位符" in error for error in errors), errors)
        self.assertFalse(any("字段没有有效内容" in error for error in errors), errors)
        self.assertEqual(self.errors(text, draft=True), [])

    def test_confirmed_bug_rejects_empty_proposal_field(self):
        text = filled_proposal().replace("- **修复成本**：0.5 人日 + 1 条回归测试", "- **修复成本**：")
        errors = self.errors(text)
        self.assertTrue(any("字段没有有效内容：修复成本" in error for error in errors), errors)

    def test_error_order_is_stable_across_processes(self):
        import os

        text = filled_proposal().replace("| 业务逻辑 |", "| 业务 |").replace("High / E2", "High / E1")
        path = self.write(text)
        outputs = set()
        for seed in range(3):
            result = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "validate_bugfix_proposal.py"), str(path)],
                capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONHASHSEED": str(seed)},
            )
            self.assertEqual(result.returncode, 1)
            outputs.add(result.stderr)
        self.assertEqual(len(outputs), 1, "同一输入的报错顺序必须可复现")


class GitRepoFixture(unittest.TestCase):
    """临时 git 仓库与冻结快照的公共夹具（本身不含测试）。"""

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git 不可用")
        self.workspace = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "gc.auto", "0"], check=True)
        (self.root / "src").mkdir()
        (self.root / "src" / "pay.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        self.doc = self.root / "docs" / "tdd.md"
        self.doc.write_text("# TDD\n\n扣款必须幂等。\n", encoding="utf-8")
        (self.root / "dirty.py").write_text("user work\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "init"], check=True)

    def start(self, run_id: str = "20260909-retry", documents: list[Path] | None = None) -> Path:
        argv = ["start", "--root", str(self.root), "--run-id", run_id]
        for document in ([self.doc] if documents is None else documents):
            argv += ["--documents", str(document)]
        out = io.StringIO()
        with redirect_stdout(out):
            code = state.main(argv)
        self.assertEqual(code, 0, out.getvalue())
        return self.root / ".codekit" / "bugfix" / run_id / "state.json"

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class SnapshotState(GitRepoFixture):
    def test_start_freezes_snapshot_and_preexisting_changes(self):
        manifest = self.start()
        data = self.read(manifest)
        self.assertEqual(len(data["snapshot"]["head_sha"]), 40)
        self.assertIn("dirty.py", data["snapshot"]["preexisting_changes"])
        self.assertEqual([doc["path"] for doc in data["snapshot"]["documents"]], [str(self.doc)])
        self.assertTrue((manifest.parent / "proposal.md").exists() is False)

    def test_run_dir_never_appears_in_git_status(self):
        manifest = self.start()
        manifest.parent.joinpath("proposal.md").write_text("草稿\n", encoding="utf-8")
        status = subprocess.run(
            ["git", "-C", str(self.root), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn(".codekit", status, "产物污染 git 状态会让快照与越界判定失真")

    def test_check_snapshot_detects_head_and_tree_drift(self):
        manifest = self.start()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(state.main(["check-snapshot", "--manifest", str(manifest)]), 0)
        (self.root / "dirty.py").write_text("user work changed\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(["check-snapshot", "--manifest", str(manifest)])
        self.assertEqual(code, 1)
        self.assertIn("working-tree", out.getvalue())
        subprocess.run(["git", "-C", str(self.root), "add", "dirty.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "shift head"], check=True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(["check-snapshot", "--manifest", str(manifest)])
        self.assertEqual(code, 1)
        self.assertIn("head", json.loads(out.getvalue())["drift"])

    def test_check_snapshot_detects_document_drift(self):
        manifest = self.start()
        self.doc.write_text("# TDD\n\n幂等性改为上游去重。\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(["check-snapshot", "--manifest", str(manifest)])
        self.assertEqual(code, 1)
        self.assertIn(f"document-changed:{self.doc}", json.loads(out.getvalue())["drift"])

    def test_record_and_verify_proposal_detects_edit_after_approval(self):
        manifest = self.start()
        proposal = manifest.parent / "proposal.md"
        proposal.write_text("批准执行 BF-20260909-order-retry v1\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(state.main([
                "record-proposal", "--manifest", str(manifest), "--proposal", str(proposal),
                "--proposal-id", "BF-20260909-order-retry", "--version", "1",
            ]), 0)
        approval = manifest.parent / "approval.txt"
        approval.write_text("批准执行 BF-20260909-order-retry v1，按当前绑定快照和允许修改文件实施。\n", encoding="utf-8")
        args = ["verify-approval", "--manifest", str(manifest), "--approval-file", str(approval),
                "--proposal", str(proposal)]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(state.main(args), 0, out.getvalue() + err.getvalue())
        proposal.write_text("批准执行 BF-20260909-order-retry v1（已被悄悄改写）\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(state.main(args), 1)
        self.assertIn("digest", out.getvalue() + err.getvalue())

    def test_verify_approval_rejects_stale_id_and_missing_phrase(self):
        manifest = self.start()
        proposal = manifest.parent / "proposal.md"
        proposal.write_text("批准执行 BF-20260909-order-retry v2\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            state.main(["record-proposal", "--manifest", str(manifest), "--proposal", str(proposal),
                        "--proposal-id", "BF-20260909-order-retry", "--version", "2"])
        approval = manifest.parent / "approval.txt"
        approval.write_text("看起来可以，继续吧\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(state.main([
                "verify-approval", "--manifest", str(manifest), "--approval-file", str(approval),
                "--proposal", str(proposal)]), 1)
        approval.write_text("批准执行 BF-20260909-order-retry v1\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(state.main([
                "verify-approval", "--manifest", str(manifest), "--approval-file", str(approval),
                "--proposal", str(proposal)]), 1)

    def test_verify_scope_allows_in_scope_and_flags_escapes(self):
        manifest = self.start()
        (self.root / "src" / "pay.py").write_text("def charge():\n    return 2\n", encoding="utf-8")
        (self.root / "src" / "extra.py").write_text("escape\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src/extra.py"], check=True)
        args = ["verify-scope", "--manifest", str(manifest), "--allowed", "src/pay.py", "--allowed", "dirty.py"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(args)
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue())
        self.assertEqual(report["out_of_scope"], ["src/extra.py"])
        self.assertEqual(report["preexisting_excluded"], ["dirty.py", "docs/tdd.md"])
        (self.root / "src" / "extra.py").unlink()
        subprocess.run(["git", "-C", str(self.root), "reset", "-q"], check=True)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(state.main(args), 0, out.getvalue() + err.getvalue())

    def test_verify_scope_rejects_unattributable_tracked_change(self):
        manifest = self.start()
        (self.root / "src" / "pay.py").write_text("def charge():\n    return 3\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(["verify-scope", "--manifest", str(manifest), "--allowed", "docs/"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["out_of_scope"], ["src/pay.py"])

    def test_verify_scope_accepts_multiple_allowed_paths(self):
        manifest = self.start()
        (self.root / "src" / "pay.py").write_text("def charge():\n    return 2\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_pay.py").write_text("def test_charge():\n    assert True\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main([
                "verify-scope", "--manifest", str(manifest),
                "--allowed", "src/pay.py", "tests/test_pay.py", "dirty.py",
            ])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["allowed"], ["src/pay.py", "tests/test_pay.py", "dirty.py"])


class ReviewModeGate(GitRepoFixture):
    """审查模式必须与已冻结资料一致；降级需用户确认。"""

    def record(self, manifest: Path, *argv: str) -> dict:
        proposal = manifest.parent / "proposal.md"
        proposal.write_text("批准执行 BF-20260909-order-retry v1\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = state.main([
                "record-proposal", "--manifest", str(manifest), "--proposal", str(proposal),
                "--proposal-id", "BF-20260909-order-retry", "--version", "1", *argv,
            ])
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())["proposal"]

    def test_lite_is_rejected_when_documents_are_frozen(self):
        manifest = self.start()
        with self.assertRaises(state.StateError):
            self.record(manifest, "--review-mode", "LITE")

    def test_full_without_documents_requires_confirmation(self):
        manifest = self.start(documents=[])
        with self.assertRaises(state.StateError):
            self.record(manifest, "--review-mode", "FULL")
        self.assertEqual(self.record(manifest, "--review-mode", "FULL", "--confirm-review-mode")["review_mode"], "FULL")

    def test_lite_is_allowed_without_documents(self):
        manifest = self.start(documents=[])
        self.assertEqual(self.record(manifest, "--review-mode", "LITE")["review_mode"], "LITE")

    def test_waived_always_requires_confirmation(self):
        manifest = self.start()
        with self.assertRaises(state.StateError):
            self.record(manifest, "--review-mode", "WAIVED")
        self.assertEqual(
            self.record(manifest, "--review-mode", "WAIVED", "--confirm-review-mode")["review_mode"], "WAIVED"
        )


class ValidationExecution(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.root / "bad.py").write_text("import sys\nprint('boom', file=sys.stderr)\nraise SystemExit(3)\n", encoding="utf-8")
        self.record = self.root / "validation-record.json"

    def run_validation(self, *argv: str) -> tuple[int, dict]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(["run-validation", "--record", str(self.record), *argv])
        payload = out.getvalue().strip()
        return code, json.loads(payload) if payload.startswith("{") else {"stderr": err.getvalue()}

    def test_records_real_exit_code(self):
        code, payload = self.run_validation("--cwd", str(self.root), "--", sys.executable, "ok.py")
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"], "PASS")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["cwd"], str(self.root))

    def test_failure_keeps_exit_code_and_output_tail(self):
        code, payload = self.run_validation("--cwd", str(self.root), "--", sys.executable, "bad.py")
        self.assertEqual(code, 1)
        self.assertEqual(payload["result"], "FAIL")
        self.assertEqual(payload["exit_code"], 3)
        self.assertIn("boom", payload["output_tail"])

    def test_identical_fingerprint_budget_is_enforced(self):
        for expected in (1, 1):
            code, _ = self.run_validation("--cwd", str(self.root), "--", sys.executable, "bad.py")
            self.assertEqual(code, expected)
        code, payload = self.run_validation("--cwd", str(self.root), "--", sys.executable, "bad.py")
        self.assertEqual(code, 3)
        self.assertEqual(payload["result"], "RETRY_BUDGET_EXCEEDED")
        self.assertGreaterEqual(payload["repeat_count"], 3)

    def test_rejects_shell_metacharacters_and_records_missing_program(self):
        for argv in (
            ("--", "python3", "ok.py; rm -rf /"),
            ("--", "python3|tee", "ok.py"),
            ("--", "python3", "../../etc/passwd"),
        ):
            with self.assertRaises(state.StateError):
                self.run_validation("--cwd", str(self.root), *argv)
        code, payload = self.run_validation("--cwd", str(self.root), "--", "definitely-not-a-command-xyz")
        self.assertEqual(code, 1)
        self.assertEqual(payload["result"], "NOT_AVAILABLE")

    def test_timeout_is_reported_not_silently_passed(self):
        (self.root / "slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        code, payload = self.run_validation("--cwd", str(self.root), "--timeout", "1", "--", sys.executable, "slow.py")
        self.assertEqual(code, 1)
        self.assertEqual(payload["result"], "TIMEOUT")
        self.assertIsNone(payload["exit_code"])


class CliFixture(GitRepoFixture):
    """直接跑 CLI 子命令并按 JSON 断言的公共夹具（本身不含测试）。"""

    def cli(self, *argv: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = state.main(list(argv))
        text = out.getvalue()
        return code, json.loads(text) if text.lstrip().startswith("{") else {}, err.getvalue()

    def add_long_document(self, name: str = "technical-doc.md", headings: int = 30) -> Path:
        lines = ["# 支付技术方案", ""]
        for i in range(1, headings + 1):
            lines += [f"## {i}. 约束 {i}", f"第 {i} 条不变量：扣款必须幂等且可回滚，违反时对账 Job 报警。"]
        path = self.root / "docs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class PreflightGate(CliFixture):
    """资料门禁与文档地图由脚本一次判定，不交给模型逐条复述。"""

    def test_preflight_freezes_valid_documents_and_writes_doc_map(self):
        doc = self.add_long_document()
        code, payload, _ = self.cli("preflight", "--root", str(self.root), "--run-id", "r1",
                                    "--feature-directory", "docs")
        self.assertEqual(code, 0)
        self.assertEqual(payload["documents_status"], "AVAILABLE")
        self.assertEqual(payload["suggested_review_mode"], "FULL")
        self.assertEqual([d["path"] for d in payload["documents"]], [str(doc)])
        self.assertTrue(payload["investigation_file"].endswith("investigation.md"))
        doc_map = (self.root / ".codekit" / "bugfix" / "r1" / "doc-map.md").read_text(encoding="utf-8")
        self.assertIn(doc.name, doc_map)
        self.assertRegex(doc_map, r"L\d+-L\d+ ## ")
        self.assertIn("禁止把整篇文档读入", doc_map)

    def test_preflight_rejects_template_draft_with_reason(self):
        self.add_long_document()
        draft = self.root / "docs" / "release-doc.md"
        draft.write_text("# 发布方案模板\n\n" + "\n".join(f"- 占位 <FIELD{i}>" for i in range(25)) + "\n",
                         encoding="utf-8")
        _, payload, _ = self.cli("preflight", "--root", str(self.root), "--run-id", "r2",
                                 "--feature-directory", "docs")
        rejected = {d["kind"]: d["reason"] for d in payload["rejected_documents"]}
        self.assertIn("template-or-draft", rejected["rpd"])
        self.assertNotIn("rpd", {d["kind"] for d in payload["documents"]})

    def test_preflight_does_not_fall_back_to_docs_when_feature_directory_missing(self):
        self.add_long_document()
        code, payload, _ = self.cli("preflight", "--root", str(self.root), "--run-id", "r3",
                                    "--feature-directory", "no-such-dir")
        self.assertEqual(payload["documents_status"], "NOT_AVAILABLE")
        self.assertEqual(payload["suggested_review_mode"], "LITE")
        self.assertEqual(code, 0)
        self.assertIn("directory-missing-or-unreadable", [d["reason"] for d in payload["rejected_documents"]])
        self.assertFalse((self.root / ".codekit" / "bugfix" / "r3" / "doc-map.md").exists())

    def test_default_output_is_a_summary_and_verbose_keeps_the_lists(self):
        doc = self.add_long_document()
        _, verbose, _ = self.cli("start", "--root", str(self.root), "--run-id", "c1",
                                 "--documents", str(doc), "--verbose")
        _, brief, _ = self.cli("start", "--root", str(self.root), "--run-id", "c2",
                               "--documents", str(doc))
        self.assertIn("preexisting_changes", verbose["snapshot"])
        self.assertNotIn("preexisting_changes", brief["snapshot"])
        self.assertEqual(brief["snapshot"]["preexisting_changes_count"], len(verbose["snapshot"]["preexisting_changes"]))
        self.assertNotIn("outline", brief["snapshot"]["documents"][0])
        self.assertIn("outline", verbose["snapshot"]["documents"][0])
        self.assertLess(len(json.dumps(brief, ensure_ascii=False)), len(json.dumps(verbose, ensure_ascii=False)))


class ApprovalBindingGranularity(CliFixture):
    """审批绑定承重字段；叙述性修订留痕放行，换稿与扩范围一律失败。"""

    def setUp(self):
        super().setUp()
        self.manifest = self.start()
        self.run_dir = self.manifest.parent
        self.proposal = self.run_dir / "proposal.md"
        self.proposal.write_text(filled_proposal(), encoding="utf-8")
        self.approval = self.run_dir / "approval.txt"
        self.approval.write_text("批准执行 BF-20260909-order-retry v1，按当前绑定快照和允许修改文件实施。\n",
                                 encoding="utf-8")
        code, _, err = self.cli("record-proposal", "--manifest", str(self.manifest),
                                "--proposal", str(self.proposal), "--proposal-id", "BF-20260909-order-retry",
                                "--version", "1", "--review-mode", "FULL")
        self.assertEqual(code, 0, err)

    def verify(self, *extra: str) -> tuple[int, dict]:
        code, payload, _ = self.cli("verify-approval", "--manifest", str(self.manifest),
                                    "--approval-file", str(self.approval),
                                    "--proposal", str(self.proposal), *extra)
        return code, payload

    def test_record_freezes_both_gate_and_full_text_digests(self):
        record = self.read(self.manifest)["proposal"]
        self.assertEqual(record["gate_bound"], "fields")
        self.assertNotEqual(record["gate_digest"], record["digest"])

    def test_narrative_edit_fails_and_names_the_editorial_escape_hatch(self):
        self.proposal.write_text(
            filled_proposal().replace("修复位于责任层，仅 2 文件，向后兼容", "修复位于 charge() 责任层，向后兼容且可回滚"),
            encoding="utf-8")
        code, payload = self.verify()
        self.assertEqual(code, 1)
        self.assertEqual(payload["failed_checks"], ["proposal_digest_match"])
        self.assertIn("--editorial", payload["note"])

    def test_editorial_waiver_passes_once_and_is_recorded(self):
        self.proposal.write_text(filled_proposal().replace("订单金额翻倍", "订单金额被翻倍计入"), encoding="utf-8")
        code, payload = self.verify("--editorial", "--editorial-note", "仅补充措辞，未改方案")
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["editorial"])
        revisions = self.read(self.manifest)["proposal"]["editorial_revisions"]
        self.assertEqual(len(revisions), 1)
        self.assertNotEqual(revisions[0]["from_digest"], revisions[0]["to_digest"])
        self.assertIn("仅补充措辞", revisions[0]["note"])

    def test_widened_write_scope_cannot_be_waived(self):
        self.proposal.write_text(
            filled_proposal().replace("src/pay/order.py, tests/test_pay.py",
                                      "src/pay/order.py, tests/test_pay.py, src/pay/extra.py"),
            encoding="utf-8")
        code, payload = self.verify("--editorial")
        self.assertEqual(code, 1)
        self.assertIn("proposal_gate_match", payload["failed_checks"])

    def test_rewritten_document_cannot_be_waived(self):
        self.proposal.write_text(filled_proposal() + "\n## 9. 另起一章\n\n" + "换稿内容。" * 60 + "\n",
                                 encoding="utf-8")
        code, payload = self.verify("--editorial")
        self.assertEqual(code, 1)
        self.assertIn("proposal_shape_match", payload["failed_checks"])

    def test_legacy_state_without_gate_digest_still_verifies(self):
        data = self.read(self.manifest)
        data["proposal"].pop("gate_digest")
        data["proposal"].pop("shape")
        (self.manifest).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, payload = self.verify()
        self.assertEqual(code, 0, payload)
        self.proposal.write_text(filled_proposal().replace("订单金额翻倍", "订单金额被翻倍计入"), encoding="utf-8")
        code, payload = self.verify()
        self.assertEqual(code, 1)
        self.assertNotIn("--editorial", payload["note"], "旧绑定只有全文语义，不得提供编辑性豁免")

    def test_verify_gates_combines_approval_and_snapshot_in_one_call(self):
        code, payload, _ = self.cli("verify-gates", "--manifest", str(self.manifest),
                                    "--approval-file", str(self.approval), "--proposal", str(self.proposal))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["approval"]["verdict"], "OK")
        self.assertEqual(payload["snapshot"]["verdict"], "OK")
        (self.root / "src" / "pay.py").write_text("def charge():\n    return 99\n", encoding="utf-8")
        code, payload, _ = self.cli("verify-gates", "--manifest", str(self.manifest),
                                    "--approval-file", str(self.approval), "--proposal", str(self.proposal))
        self.assertEqual(code, 1)
        self.assertEqual(payload["snapshot"]["drift"], ["working-tree"])
        self.assertEqual(payload["approval"]["verdict"], "FAIL")


class ContractAndPlan(CliFixture):
    """实施合同与验证计划由提案渲染，整份计划一次执行。"""

    def setUp(self):
        super().setUp()
        self.manifest = self.start()
        self.run_dir = self.manifest.parent
        self.proposal = self.run_dir / "proposal.md"
        self.proposal.write_text(filled_proposal(), encoding="utf-8")
        self.approval = self.run_dir / "approval.txt"
        self.approval.write_text("批准执行 BF-20260909-order-retry v1，按当前绑定快照和允许修改文件实施。\n",
                                 encoding="utf-8")
        self.assertEqual(self.cli("record-proposal", "--manifest", str(self.manifest), "--proposal", str(self.proposal),
                                  "--proposal-id", "BF-20260909-order-retry", "--version", "1")[0], 0)
        self.assertEqual(self.cli("verify-approval", "--manifest", str(self.manifest),
                                  "--approval-file", str(self.approval), "--proposal", str(self.proposal))[0], 0)

    def test_freeze_contract_derives_scope_digests_and_plan(self):
        code, payload, err = self.cli("freeze-contract", "--manifest", str(self.manifest))
        self.assertEqual(code, 0, err)
        contract = self.read(Path(payload["contract"]))
        self.assertEqual(contract["allowed_write_files"], ["src/pay/order.py", "tests/test_pay.py"])
        self.assertEqual(contract["start_head_sha"], self.read(self.manifest)["approval"]["approved_snapshot"]["head_sha"])
        self.assertEqual(contract["start_diff_digest"],
                         self.read(self.manifest)["approval"]["approved_snapshot"]["working_tree_digest"])
        self.assertTrue(contract["forbidden_changes"] and contract["behavior_to_preserve"])
        self.assertRegex(contract["contract_digest"], r"^[0-9a-f]{64}$")
        # CW2-a①③：交给 code-review `--handoff` 的自述块与另两家同一编码、同一形状。
        handoff = self.read(Path(payload["review_handoff"]))
        self.assertRegex(handoff["worktree_state"]["head_sha"], r"^[0-9a-f]{40}$")
        self.assertEqual(handoff["digest_scheme"], "sha256/hex")
        self.assertIsNone(handoff["committed_range"])
        self.assertEqual(handoff["produced_by"], "codekit:bugfix/scripts/bugfix_state.py")
        self.assertTrue(handoff["target_id"].endswith("@" + contract["start_head_sha"]))
        self.assertEqual(handoff["target_id"], payload["target_id"])
        plan = self.read(Path(payload["plan"]))
        self.assertEqual([c["proves"] for c in plan["checks"]], ["根因已消除"])
        self.assertTrue(plan["checks"][0]["required"])

    def test_review_handoff_survives_code_review_handoff_gate(self):
        # CW2-a 验收：本 Skill 真产出的 review-handoff.json 交给 code-review 的 `--handoff`
        # 比对器。A5 时代这条路径上三个 `==` 恒为 False（前缀编码 + 语义不同的 diff_digest），
        # 按 SKILL 字面执行只能每轮 BLOCKED。
        code, payload, err = self.cli("freeze-contract", "--manifest", str(self.manifest))
        self.assertEqual(code, 0, err)
        freeze = SKILL_DIR.parent / "code-review" / "scripts" / "freeze_review_snapshot.py"
        self.assertTrue(freeze.is_file(), f"跨 SKILL 验收需要 {freeze}")
        head = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()
        # 本 Skill 从不 commit，所以 code-review 复用的正是同一份工作树：base == head。
        manifest_path = self.run_dir / "code-review-snapshot.json"
        result = subprocess.run(
            [sys.executable, str(freeze), "--repo", str(self.root),
             "--base-sha", head, "--head-sha", head,
             "--target-type", "local_diff", "--target-locator", "test",
             "--emit-manifest", str(manifest_path), "--handoff", str(payload["review_handoff"])],
            capture_output=True, text=True, check=False, cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        block = self.read(manifest_path)["handoff"]
        self.assertEqual(block["sources"], [state.PRODUCED_BY])
        self.assertTrue(block["target_id_matched"])
        self.assertEqual(block["target_id"], payload["target_id"])
        # 只有双方共有且同一 material 的字段进比对；本 Skill 不带分层摘要，所以到此为止。
        self.assertEqual(block["compared_fields"], ["head_sha"])
        self.assertEqual(block["uncomparable_digests"], [])
        # 上游不含已提交范围而本轮也没有已提交内容 —— 这不是范围超集，不得凭空压低结论。
        self.assertEqual(block["limitations"], [])

    def test_freeze_contract_requires_approval(self):
        manifest = self.start(run_id="no-approval")
        with self.assertRaises(state.StateError) as caught:
            self.cli("freeze-contract", "--manifest", str(manifest))
        self.assertIn("verify-approval", str(caught.exception))

    def rewrite_plan(self, checks: list[dict]) -> Path:
        _, payload, _ = self.cli("freeze-contract", "--manifest", str(self.manifest))
        plan_path = Path(payload["plan"])
        plan = self.read(plan_path)
        plan["checks"] = checks
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return plan_path

    def test_run_plan_executes_every_item_in_one_call(self):
        self.rewrite_plan([
            {"name": "通过", "proves": "a", "required": True, "command": [sys.executable, "ok.py"]},
            {"name": "元字符", "proves": "b", "required": False, "command": ["sh", "-c", "echo x && echo y"]},
            {"name": "缺失程序", "proves": "c", "required": False, "command": ["definitely-not-a-real-binary"]},
        ])
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        code, payload, _ = self.cli("run-plan", "--manifest", str(self.manifest))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["executed"], 3)
        results = {i["name"]: i["result"] for i in payload["items"]}
        self.assertEqual(results["通过"], "PASS")
        self.assertEqual(results["元字符"], "NOT_AVAILABLE")
        self.assertEqual(results["缺失程序"], "NOT_AVAILABLE")
        record = self.read(self.run_dir / "validation-record.json")
        self.assertEqual(len(record["checks"]), 3)
        self.assertTrue(all(c["fingerprint"] for c in record["checks"]))

    def test_run_plan_fails_when_a_required_item_fails(self):
        self.rewrite_plan([
            {"name": "失败", "proves": "a", "required": True, "command": [sys.executable, "bad.py"]},
            {"name": "通过", "proves": "b", "required": False, "command": [sys.executable, "ok.py"]},
        ])
        (self.root / "ok.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (self.root / "bad.py").write_text("import sys\nprint('boom', file=sys.stderr)\nraise SystemExit(3)\n",
                                          encoding="utf-8")
        code, payload, _ = self.cli("run-plan", "--manifest", str(self.manifest))
        self.assertEqual(code, 1)
        self.assertEqual(payload["verdict"], "FAIL")
        self.assertEqual([row["name"] for row in payload["unsatisfied_required"]], ["失败"])
        self.assertEqual(payload["executed"], 2, "非必需项失败不影响其余项继续执行")

    def test_verify_scope_uses_contract_without_retyping_paths(self):
        _, payload, _ = self.cli("freeze-contract", "--manifest", str(self.manifest))
        (self.root / "src" / "pay").mkdir(exist_ok=True)
        (self.root / "src" / "pay" / "order.py").write_text("def charge():\n    return 2\n", encoding="utf-8")
        code, scoped, err = self.cli("verify-scope", "--manifest", str(self.manifest),
                                     "--contract", payload["contract"])
        self.assertEqual(code, 0, scoped)
        self.assertEqual(scoped["out_of_scope"], [])
        self.assertEqual(scoped["contract_integrity"], "OK")
        (self.root / "src" / "sneaky.py").write_text("escape\n", encoding="utf-8")
        code, scoped, _ = self.cli("verify-scope", "--manifest", str(self.manifest),
                                   "--contract", payload["contract"])
        self.assertEqual(code, 1)
        self.assertEqual(scoped["out_of_scope"], ["src/sneaky.py"])

    def test_verify_scope_fails_when_the_contract_is_edited_after_freeze(self):
        _, payload, _ = self.cli("freeze-contract", "--manifest", str(self.manifest))
        contract_path = Path(payload["contract"])
        contract = self.read(contract_path)
        contract["allowed_write_files"].append("src/any.py")
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src" / "any.py").write_text("wider\n", encoding="utf-8")
        code, scoped, _ = self.cli("verify-scope", "--manifest", str(self.manifest),
                                   "--contract", str(contract_path))
        self.assertEqual(code, 1, "手工扩围后的合同必须被完整性检查拦下")
        self.assertEqual(scoped["contract_integrity"], "TAMPERED")
        # 越界判定本身看不出来：allowed 正是从被改过的合同读出来的，只有摘要能识破。
        self.assertEqual(scoped["out_of_scope"], [])


class LayoutGuard(unittest.TestCase):
    def test_skill_md_documents_state_script_and_skill_dir_paths(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("run-manifest.json", text, "bugfix 没有 run-manifest，派发指令不应引用它")
        self.assertNotIn("python3 skills/bugfix/scripts/", text, "脚本调用必须用 SKILL_DIR 占位符")
        self.assertIn("bugfix_state.py", text)
        self.assertIn("SKILL_DIR", text)


if __name__ == "__main__":
    unittest.main()
