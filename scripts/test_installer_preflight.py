#!/usr/bin/env python3
"""系统依赖预检的契约：三平台同一份判据、同一份指引、同一个 Python 下限。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402
import installer_platform as platform_layer  # noqa: E402
import installer_preflight as preflight  # noqa: E402


class FakeProbe:
    """把命令名映射到版本行；未登记的命令视为不存在，``""`` 表示存在但读不出版本。"""

    def __init__(self, **commands: str) -> None:
        self.commands = commands

    def __call__(self, command, version_args):  # type: ignore[no-untyped-def]
        if command not in self.commands:
            return False, ""
        return True, self.commands[command]


def by_label(problems) -> dict[str, preflight.Finding]:
    return {problem.label: problem for problem in problems}


class FindingsTests(unittest.TestCase):
    def test_missing_command_is_reported_as_not_installed(self) -> None:
        problems = by_label(
            preflight.findings(
                platform_layer.platform_for("Linux"),
                FakeProbe(npm="10.0.0", git="git version 2.4", curl="curl 8.0"),
            )
        )
        self.assertEqual("未安装", problems["Node.js"].current)
        self.assertEqual(">= 24.0.0", problems["Node.js"].required)

    def test_too_old_version_is_reported_with_the_measured_value(self) -> None:
        problems = by_label(
            preflight.findings(
                platform_layer.platform_for("Linux"),
                FakeProbe(
                    node="v22.1.0", npm="10.0.0", git="git version 2.4", curl="curl 8.0"
                ),
            )
        )
        self.assertEqual("v22.1.0", problems["Node.js"].current)

    def test_a_new_enough_version_produces_no_finding(self) -> None:
        problems = preflight.findings(
            platform_layer.platform_for("Darwin"),
            FakeProbe(
                node="v24.16.0", npm="11.0.0", git="git version 2.4", curl="curl 8.0"
            ),
        )
        self.assertEqual([], problems)

    def test_any_of_command_group_is_satisfied_by_one_candidate(self) -> None:
        # curl 不在、wget 在：仍然算满足（Windows 上 curl 可能只是 PowerShell 别名）。
        problems = preflight.findings(
            platform_layer.platform_for("Linux"),
            FakeProbe(
                node="v24.16.0", npm="11.0.0", git="git version 2.4", wget="GNU Wget 1.21"
            ),
        )
        self.assertEqual([], problems)

    def test_unreadable_version_fails_only_where_a_minimum_is_required(self) -> None:
        problems = by_label(
            preflight.findings(
                platform_layer.platform_for("Linux"),
                FakeProbe(node="", npm="", git="", curl="curl 8.0"),
            )
        )
        self.assertEqual("unknown", problems["Node.js"].current)
        self.assertNotIn("git", problems)
        self.assertNotIn("npm", problems)

    def test_guidance_is_present_for_every_supported_platform(self) -> None:
        for key in (platform_layer.MACOS, platform_layer.LINUX, platform_layer.WINDOWS):
            with self.subTest(key):
                for dependency in preflight.DEPENDENCIES:
                    self.assertTrue(dependency.guidance.get(key))


class Recorder:
    """只记录调用，不做格式化——报告文案不属于本用例的断言对象。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.successes: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def success(self, message: str) -> None:
        self.successes.append(message)


class CheckTests(unittest.TestCase):
    @staticmethod
    def installer(platform_key: str):
        installer = type("FakeInstaller", (), {})()
        installer.platform = platform_layer.platform_for(platform_key)
        installer.logger = Recorder()
        return installer

    def test_check_aborts_and_reports_every_unmet_dependency(self) -> None:
        installer = self.installer("Linux")
        with patch.object(preflight, "default_probe", FakeProbe(node="v22.1.0")):
            self.assertFalse(preflight.check(installer))
        reported = "\n".join(installer.logger.errors + installer.logger.infos)
        self.assertIn("Node.js", reported)
        self.assertIn("v22.1.0", reported)
        self.assertIn("安装已中止", installer.logger.errors[-1])
        self.assertEqual([], installer.logger.successes)

    def test_check_passes_a_host_that_has_everything(self) -> None:
        installer = self.installer("Windows")
        with patch.object(
            preflight,
            "default_probe",
            FakeProbe(
                node="v24.16.0", npm="11.0.0", git="git version 2.4", curl="curl 8.0"
            ),
        ):
            self.assertTrue(preflight.check(installer))
        self.assertEqual(1, len(installer.logger.successes))
        self.assertEqual([], installer.logger.errors)


class MinimumPythonTests(unittest.TestCase):
    """Python 下限只有一处声明，但入口脚本必须自己复述一遍——两者不能漂移。"""

    def test_the_declared_floor_matches_the_entry_points(self) -> None:
        expected = "(" + ", ".join(str(part) for part in config.MINIMUM_PYTHON) + ")"
        self.assertEqual((3, 10), config.MINIMUM_PYTHON)
        for name in ("entrypoint.sh", "entrypoint.ps1"):
            with self.subTest(name):
                text = (SCRIPT_DIR / name).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_an_older_interpreter_is_refused(self) -> None:
        with self.assertRaises(ValueError) as raised:
            config.require_python((3, 9))
        self.assertIn(config.MINIMUM_PYTHON_TEXT, str(raised.exception))

    def test_the_running_interpreter_satisfies_the_floor(self) -> None:
        config.require_python()


if __name__ == "__main__":
    unittest.main()
