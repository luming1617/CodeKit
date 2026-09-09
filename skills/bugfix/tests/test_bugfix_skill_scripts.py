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
                capture_output=True, text=True, check=False,
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


class LayoutGuard(unittest.TestCase):
    def test_skill_md_documents_state_script_and_skill_dir_paths(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("run-manifest.json", text, "bugfix 没有 run-manifest，派发指令不应引用它")
        self.assertNotIn("python3 skills/bugfix/scripts/", text, "脚本调用必须用 SKILL_DIR 占位符")
        self.assertIn("bugfix_state.py", text)
        self.assertIn("SKILL_DIR", text)


if __name__ == "__main__":
    unittest.main()
