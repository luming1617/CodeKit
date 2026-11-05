#!/usr/bin/env python3
"""Guard for the role-body single-source check in validate-skill-layout.py.

The check exists because a role's standing requirements were written twice -- once in
``agents/<role>.md`` (injected by the host at dispatch) and again in
``references/<role>-prompt.md`` (pasted into the task) -- and the two copies drifted apart.
These tests pin both directions: the shipped skills must pass, and a reintroduced double
body must actually be reported.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
LAYOUT_SCRIPT = SCRIPT_DIR / "validate-skill-layout.py"

_spec = importlib.util.spec_from_file_location("validate_skill_layout", LAYOUT_SCRIPT)
assert _spec and _spec.loader
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)

ROLE_BODY = """---
name: review-demo
description: demo role
mode: subagent
tools: Read
---

你是只读 Demo Reviewer。

要求：
1. 只读检查目标仓库与快照内的文件，不修改任何被版本控制的内容。
2. 每个候选问题包含精确位置、触发路径、影响和待检查反证。
3. 对不可访问内容明确记录分析边界。
"""

PROMPT_SINGLE = """# Demo Prompt

```text
你是只读 Demo Reviewer，执行步骤 99。

## 输入
<Review Snapshot>

## 本步约束
1. 读取 Diff 读物前先核对其 sha256 与输入的 diff_artifact.digest 一致。

## 输出
严格按 contracts.md 的 demo 节输出。
```
"""

PROMPT_DOUBLE = """# Demo Prompt

```text
你是只读 Demo Reviewer，执行步骤 99。

## 输入
<Review Snapshot>

## 工作要求
1. 只读检查目标仓库与快照内的文件，不修改任何被版本控制的内容。
2. 每个候选问题包含精确位置、触发路径、影响和待检查反证。

## 输出
严格按 contracts.md 的 demo 节输出。
```
"""


PROMPT_ONE_DUP = """# Demo Prompt

```text
你是只读 Demo Reviewer，执行步骤 99。

## 输入
<Review Snapshot>

## 本步约束
1. 只读检查目标仓库与快照内的文件，不修改任何被版本控制的内容。

## 输出
严格按 contracts.md 的 demo 节输出。
```
"""


def _write_skill(root: Path, prompt_body: str) -> Path:
    skill = root / "demo-skill"
    (skill / "agents").mkdir(parents=True)
    (skill / "references").mkdir(parents=True)
    (skill / "agents" / "review-demo.md").write_text(ROLE_BODY, encoding="utf-8")
    (skill / "references" / "demo-prompt.md").write_text(prompt_body, encoding="utf-8")
    return skill


class RoleBodyDupesTests(unittest.TestCase):
    def test_shipped_skills_pass_the_real_gate(self) -> None:
        skills = [
            path for path in sorted(layout.SKILLS_DIR.iterdir())
            if path.is_dir() and (path / "SKILL.md").is_file()
        ]
        problems: list[str] = []
        for skill in skills:
            layout.check_role_body_dupes(skill, problems)
        self.assertEqual(problems, [], "仓库内的角色正文已经出现双份")

    def test_gate_is_not_vacuous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hit = _write_skill(root / "double", PROMPT_DOUBLE)
            problems: list[str] = []
            layout.check_role_body_dupes(hit, problems)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("restate the same role body in two places", problems[0])

            clean = _write_skill(root / "single", PROMPT_SINGLE)
            problems = []
            layout.check_role_body_dupes(clean, problems)
            self.assertEqual(problems, [])

            # 门槛是"同一对正文重合 ≥2 句"：单句偶合不算双份正文，避免误伤其它 Skill。
            one = _write_skill(root / "one", PROMPT_ONE_DUP)
            problems = []
            layout.check_role_body_dupes(one, problems)
            self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
