#!/usr/bin/env python3
"""Regression tests for the staged compatibility plugin sources."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import installer_sources as sources  # noqa: E402


VERSION = "1.2.3"


def write(path: Path, content: str) -> Path:
    """Create a file with its parent directories, as a fixture repository needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def build_repo(root: Path, *, with_qoder: bool = True) -> Path:
    """Create the smallest repository layout the source builder accepts."""
    repo = root / "repo"
    write(repo / "plugin-version.json", json.dumps({"version": VERSION}))
    # Codex 只读 `.agents/plugins/marketplace.json`；`.codex-plugin/` 只是插件元数据。
    write(repo / ".agents" / "plugins" / "marketplace.json", json.dumps({"name": "co-daming"}))
    for directory in (".claude-plugin", ".codex-plugin"):
        write(repo / directory / "plugin.json", json.dumps({"name": "codekit", "version": "0.0.1"}))
        write(repo / directory / "marketplace.json", json.dumps({"name": "co-daming"}))
    write(repo / ".opencode" / "package.json", json.dumps({"name": "codekit", "version": "0.0.1"}))
    write(repo / ".opencode" / "plugins" / "codekit.js", "export default {};\n")
    if with_qoder:
        write(repo / ".qoder-plugin" / "plugin.json", json.dumps({"name": "codekit", "version": "0.0.1"}))
    for skill in ("demo", "other"):
        write(repo / "skills" / skill / "SKILL.md", f"# {skill}\n")
        write(repo / "skills" / skill / "openai.yaml", "interface:\n  display_name: 演示\n")
    return repo


class QoderStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.repo = build_repo(base)
        self.state_dir = base / "state"
        self.market, self.opencode, self.qoder = sources.prepare(self.repo, self.state_dir)

    def test_manifest_is_staged_with_the_synchronized_version(self) -> None:
        manifest = self.qoder / ".qoder-plugin" / "plugin.json"
        self.assertTrue(manifest.is_file())
        document = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(document["name"], "codekit")
        self.assertEqual(document["version"], VERSION)

    def test_every_skill_is_copied_verbatim(self) -> None:
        self.assertEqual(
            sorted(path.name for path in self.qoder.iterdir() if path.is_dir()),
            [".qoder-plugin", "skills"],
        )
        self.assertEqual(
            sorted(path.name for path in (self.qoder / "skills").iterdir()),
            sorted(path.name for path in (self.repo / "skills").iterdir()),
        )
        self.assertTrue((self.qoder / "skills" / "demo" / "SKILL.md").is_file())

    def test_no_root_agents_directory_is_staged(self) -> None:
        # Plugin-local agents would land in Qoder's `codekit:<name>` subagent
        # namespace, while CodeKit's skills schedule bare global names.
        self.assertFalse((self.qoder / "agents").exists())

    def test_skill_interface_files_are_not_transcribed(self) -> None:
        # That transcription only serves Codex/OpenCode skill discovery.
        self.assertFalse((self.qoder / "skills" / "demo" / "agents" / "openai.yaml").exists())
        self.assertTrue((self.market / "skills" / "demo" / "agents" / "openai.yaml").is_file())

    def test_codex_marketplace_is_staged_at_the_path_codex_reads(self) -> None:
        # Codex 0.154 只在 <root>/.agents/plugins/marketplace.json 找清单；放在
        # `.codex-plugin/` 会被判为 "marketplace root does not contain a supported
        # manifest"。
        staged = self.market / ".agents" / "plugins" / "marketplace.json"
        self.assertTrue(staged.is_file())
        self.assertEqual(json.loads(staged.read_text(encoding="utf-8"))["name"], "co-daming")

    def test_rebuilding_replaces_the_stage_without_leftovers(self) -> None:
        write(self.qoder / "stale.txt", "left behind by an older build")
        again = sources.prepare(self.repo, self.state_dir)
        self.assertEqual(again[2], self.qoder)
        self.assertFalse((self.qoder / "stale.txt").exists())
        staging_parent = self.state_dir / "qoder-source"
        self.assertEqual(
            sorted(path.name for path in staging_parent.iterdir()), ["codekit"]
        )

    def test_repository_version_and_skills_are_staged_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _market, _opencode, qoder = sources.prepare(REPO_DIR, Path(directory))
            document = json.loads((qoder / ".qoder-plugin" / "plugin.json").read_text(encoding="utf-8"))
            expected = json.loads((REPO_DIR / "plugin-version.json").read_text(encoding="utf-8"))
            self.assertEqual(document["version"], expected["version"])
            self.assertEqual(
                sorted(path.name for path in (qoder / "skills").iterdir() if path.is_dir()),
                sorted(path.name for path in (REPO_DIR / "skills").iterdir() if path.is_dir()),
            )
            self.assertTrue((qoder / "skills").is_dir())
            self.assertFalse((qoder / "agents").exists())


class StagingHygieneTests(unittest.TestCase):
    """开发机上的缓存不属于插件载荷，不能装进宿主。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_development_debris_is_not_staged(self) -> None:
        repo = build_repo(self.base)
        demo = repo / "skills" / "demo"
        write(demo / "tests" / "__pycache__" / "test_demo.cpython-314.pyc", "x")
        write(demo / "scripts" / "state.pyc", "x")
        write(demo / ".pytest_cache" / "CACHEDIR.TAG", "x")
        write(demo / ".DS_Store", "x")
        write(repo / "skills" / ".DS_Store", "x")
        write(repo / ".claude-plugin" / "__pycache__" / "gen.pyc", "x")
        # skills 自带可运行的测试套件：测试文件是载荷的一部分，只排除缓存。
        write(demo / "tests" / "test_demo.py", "# real test\n")

        market, opencode, qoder = sources.prepare(repo, self.base / "state")

        for stage in (market, opencode, qoder):
            names = {path.name for path in stage.rglob("*")}
            self.assertNotIn("__pycache__", names)
            self.assertNotIn(".pytest_cache", names)
            self.assertNotIn(".DS_Store", names)
            self.assertEqual(
                [path for path in stage.rglob("*") if path.suffix in {".pyc", ".pyo"}],
                [],
            )
        self.assertTrue((qoder / "skills" / "demo" / "tests" / "test_demo.py").is_file())
        self.assertTrue((market / "skills" / "demo" / "tests" / "test_demo.py").is_file())
        self.assertTrue(
            (opencode / "plugins" / "codekit" / "skills" / "demo" / "tests" / "test_demo.py").is_file()
        )

    def test_only_the_marketplace_manifest_is_staged_from_agents(self) -> None:
        # `.agents/skills/gitnexus*/` 是本机跑过 `gitnexus analyze --skills` 的产物，
        # 在 .gitignore 里且没有宿主读者；跟着 `.agents` 整目录复制会让同一份仓库在
        # 两台机器上装出不同载荷。
        repo = build_repo(self.base)
        write(repo / ".agents" / "skills" / "gitnexus-cli" / "SKILL.md", "# generated here\n")

        market, _opencode, _qoder = sources.prepare(repo, self.base / "state")

        self.assertTrue((market / ".agents" / "plugins" / "marketplace.json").is_file())
        self.assertFalse((market / ".agents" / "skills").exists())


class SourceValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_missing_qoder_manifest_is_reported_as_an_incomplete_source(self) -> None:
        repo = build_repo(self.base, with_qoder=False)
        with self.assertRaises(ValueError) as caught:
            sources.prepare(repo, self.base / "state")
        message = str(caught.exception)
        self.assertIn("marketplace source is incomplete", message)
        self.assertIn(".qoder-plugin", message)

    def test_incomplete_source_leaves_no_stage_behind(self) -> None:
        repo = build_repo(self.base, with_qoder=False)
        state_dir = self.base / "state"
        with self.assertRaises(ValueError):
            sources.prepare(repo, state_dir)
        self.assertFalse((state_dir / "qoder-source" / "codekit").exists())
        self.assertFalse((state_dir / "marketplace-source" / "codekit").exists())

    def test_non_semantic_version_is_rejected(self) -> None:
        repo = build_repo(self.base)
        write(repo / "plugin-version.json", json.dumps({"version": "v1"}))
        with self.assertRaises(ValueError):
            sources.prepare(repo, self.base / "state")

    def test_skill_without_interface_metadata_is_rejected(self) -> None:
        repo = build_repo(self.base)
        (repo / "skills" / "demo" / "openai.yaml").unlink()
        with self.assertRaises(ValueError):
            sources.prepare(repo, self.base / "state")

    def test_market_stage_survives_a_qoder_failure_unchanged(self) -> None:
        state_dir = self.base / "state"
        market, _opencode, qoder = sources.prepare(build_repo(self.base), state_dir)
        marker = market / ".claude-plugin" / "plugin.json"
        before = marker.read_text(encoding="utf-8")
        shutil.rmtree(qoder)
        broken = build_repo(self.base / "second", with_qoder=False)
        with self.assertRaises(ValueError):
            sources.prepare(broken, state_dir)
        self.assertEqual(marker.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
