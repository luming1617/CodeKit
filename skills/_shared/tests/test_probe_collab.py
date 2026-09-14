#!/usr/bin/env python3
"""`skills/_shared/scripts/probe-collab.py` 的契约测试。

这个模块既是 7 个技能的派发门禁，也是安装器唯一的 Codex 配置写入实现
（`scripts/installer_codex.py` 按路径加载它），所以 TOML 保真、判定阈值与
「写不成就逐字节回滚」这三件事必须由它自己的用例锁住，而不是靠调用方顺带覆盖。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SHARED_DIR))

import importlib.util

spec = importlib.util.spec_from_file_location("codekit_probe_collab", SHARED_DIR / "probe-collab.py")
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


CONFIG = (
    'model = "gpt-5"\n'
    "approval_policy = \"on-request\"\n"
    "\n"
    "[features]\n"
    "web_search_request = false\n"
    "multi_agent_v2 = false  # off until restart\n"
    "\n"
    "[mcp_servers.gitnexus]\n"
    'command = "gitnexus"\n'
)
# flag 打开时这一族是一起出现的，这才是工具面的形状。
TOOLS = "tools: spawn_agent list_agents wait_agent send_message interrupt_agent"


class TomlPatchTests(unittest.TestCase):
    def test_preserves_everything_outside_the_one_key(self) -> None:
        updated, changed = probe.enable_multi_agent(CONFIG)
        self.assertTrue(changed)
        self.assertIn('model = "gpt-5"', updated)
        self.assertIn("web_search_request = false", updated)
        self.assertIn("[mcp_servers.gitnexus]", updated)
        # 行尾注释与其前面的空白原样保留，只替换值本身。
        self.assertIn("multi_agent_v2 = true  # off until restart", updated)
        self.assertFalse(probe.enable_multi_agent(updated)[1])

    def test_creates_the_table_without_touching_other_tables(self) -> None:
        updated, changed = probe.enable_multi_agent('[a.features]\nx = 1\n')
        self.assertTrue(changed)
        self.assertEqual(updated, '[a.features]\nx = 1\n\n[features]\nmulti_agent_v2 = true\n')

    def test_crlf_file_keeps_its_line_terminator(self) -> None:
        updated, changed = probe.enable_multi_agent("[features]\r\nmulti_agent_v2 = false\r\n")
        self.assertTrue(changed)
        self.assertIn("multi_agent_v2 = true\r\n", updated)


class ClassifyTests(unittest.TestCase):
    def test_only_a_measured_empty_surface_counts_as_unavailable(self) -> None:
        self.assertEqual(probe.classify(1, TOOLS), probe.UNKNOWN)
        self.assertEqual(probe.classify(0, "no agent tools here"), probe.UNAVAILABLE)
        self.assertEqual(probe.classify(0, TOOLS), probe.AVAILABLE)

    def test_one_name_in_prose_is_not_a_tool_surface(self) -> None:
        """技能正文里就会出现 ``spawn_agent``，单命中不能算已开启。

        误判成 available 的代价最坏：安装器静默判定 already-on，既不写开关也不提示。
        """
        self.assertEqual(
            probe.classify(0, "派发子代理时用 spawn_agent 这个工具"), probe.UNAVAILABLE
        )

    def test_similar_tool_names_do_not_count(self) -> None:
        # 宿主自带的 ``send_message_to_chat_session`` 不是这族工具，词边界不能放行它。
        self.assertEqual(probe.classify(0, "send_message_to_chat_session"), probe.UNAVAILABLE)


class EnableOrchestrationTests(unittest.TestCase):
    def _home(self, root: Path, text: str = CONFIG) -> Path:
        path = probe.config_path(root)
        path.parent.mkdir()
        path.write_text(text, encoding="utf-8")
        return path

    def test_enabled_keeps_the_change_when_the_reprobe_sees_the_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._home(Path(directory))
            outcome = probe.enable("codex", Path(directory), lambda argv, timeout: (0, TOOLS))
            self.assertEqual(probe.ENABLED, outcome["action"])
            self.assertIn("multi_agent_v2 = true", path.read_text(encoding="utf-8"))

    def test_rolled_back_byte_for_byte_when_the_surface_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._home(Path(directory))
            before = path.read_bytes()
            outcome = probe.enable("codex", Path(directory), lambda argv, timeout: (0, ""))
            self.assertEqual(probe.ROLLED_BACK, outcome["action"])
            self.assertEqual(before, path.read_bytes())

    def test_noop_when_the_flag_is_already_true_yet_the_surface_is_empty(self) -> None:
        """flag 已是 true 却看不到工具：如实说「未改动配置」，不冒充成回滚过。"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._home(Path(directory), "[features]\nmulti_agent_v2 = true\n")
            before = path.read_bytes()
            outcome = probe.enable("codex", Path(directory), lambda argv, timeout: (0, ""))
            self.assertEqual("noop", outcome["action"])
            self.assertEqual(before, path.read_bytes())

    def test_hosts_without_a_measured_switch_are_refused_and_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outcome = probe.enable("claude", root, lambda argv, timeout: (0, TOOLS))
            self.assertEqual("refused", outcome["action"])
            self.assertFalse((root / ".claude").exists())

    def test_removal_of_a_created_file_on_rollback(self) -> None:
        """原本没有配置文件时，回滚必须把文件删掉，而不是留一个空文件。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".codex").mkdir()
            outcome = probe.enable("codex", root, lambda argv, timeout: (0, ""))
            self.assertEqual(probe.ROLLED_BACK, outcome["action"])
            self.assertFalse(probe.config_path(root).exists())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
