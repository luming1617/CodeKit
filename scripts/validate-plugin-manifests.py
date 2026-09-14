#!/usr/bin/env python3
"""Validate CodeKit plugin manifests: real JSON, consistent identity and version.

The pre-commit ``validate-plugins`` hook used to be an ``echo`` placeholder. This
script makes it meaningful: every manifest must parse, agree on the plugin name and
the single version source, and point at files that actually exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402

ROOT = SCRIPT_DIR.parent
PLUGIN_NAME = "codekit"
SKILLS_DIR = ROOT / "skills"
# Which manifests carry the version is owned by sync-plugin-version.py; its filename
# carries a hyphen, so it is reached through the shared path loader (which also keeps
# a single module instance). Re-declaring the list here is how the two scripts drift
# apart and a bumped version lands in some manifests but not the others.
SYNC_SCRIPT = SCRIPT_DIR / "sync-plugin-version.py"
SYNC_MODULE_NAME = "codekit_sync_plugin_version"


def version_targets() -> tuple[Path, ...]:
    """Version-bearing manifests, read from the list the synchronizer writes."""
    return tuple(config.load_module(SYNC_SCRIPT, SYNC_MODULE_NAME).TARGET_FILES)


class ManifestError(ValueError):
    """A manifest is missing, malformed, or inconsistent with its siblings."""


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise ManifestError(f"missing manifest: {path.relative_to(ROOT)}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path.relative_to(ROOT)}: invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError(f"{path.relative_to(ROOT)}: expected a JSON object")
    return document


def check_version_source() -> str:
    document = load_json(ROOT / "plugin-version.json")
    version = str(document.get("version", ""))
    # Shared with the synchronizer and the installer (installer_config.SEMVER_PATTERN);
    # a stricter local rule used to reject versions the other two had just accepted.
    if not config.SEMVER_PATTERN.fullmatch(version):
        raise ManifestError(
            f"plugin-version.json: {version!r} is not a semantic version such as 1.0.0"
        )
    return version


def assert_name_and_version(path: Path, version: str) -> dict:
    document = load_json(path)
    relative = path.relative_to(ROOT)
    if document.get("name") != PLUGIN_NAME:
        raise ManifestError(f"{relative}: name must be {PLUGIN_NAME!r}, got {document.get('name')!r}")
    if document.get("version") != version:
        raise ManifestError(
            f"{relative}: version {document.get('version')!r} != plugin-version.json {version!r}"
        )
    return document


def check_marketplaces() -> None:
    for relative in (".claude-plugin/marketplace.json", ".codex-plugin/marketplace.json"):
        document = load_json(ROOT / relative)
        plugins = document.get("plugins")
        if not isinstance(plugins, list) or not plugins:
            raise ManifestError(f"{relative}: plugins must be a non-empty list")
        entry = next((item for item in plugins if item.get("name") == PLUGIN_NAME), None)
        if entry is None:
            raise ManifestError(f"{relative}: no {PLUGIN_NAME!r} plugin entry")
        source = entry.get("source")
        normalized = source.get("path") if isinstance(source, dict) else source
        if normalized != "./":
            raise ManifestError(f"{relative}: plugin source must point at './', got {normalized!r}")
        if not str(entry.get("description", "")).strip():
            raise ManifestError(f"{relative}: plugin entry needs a description")


def check_skill_tree() -> int:
    if not SKILLS_DIR.is_dir():
        raise ManifestError("skills/ directory is missing")
    shared = SKILLS_DIR / "_shared"
    if (shared / "SKILL.md").exists():
        raise ManifestError("skills/_shared must not look like a skill (it carries shared prompts only)")
    if (shared / "agents").exists():
        raise ManifestError("skills/_shared/agents would be picked up by the global agent installer")
    skills = sorted(path for path in SKILLS_DIR.iterdir() if path.is_dir() and (path / "SKILL.md").is_file())
    if not skills:
        raise ManifestError("no skills found (expected skills/<name>/SKILL.md)")
    for skill in skills:
        if not (skill / "openai.yaml").is_file():
            raise ManifestError(f"skills/{skill.name}: missing openai.yaml")
    return len(skills)


def check_opencode_entry() -> None:
    package = load_json(ROOT / ".opencode" / "package.json")
    main = str(package.get("main", ""))
    if not main:
        raise ManifestError(".opencode/package.json: main is required")
    if not (ROOT / ".opencode" / Path(main)).is_file():
        raise ManifestError(f".opencode/package.json: main points at a missing file: {main}")


def main() -> int:
    try:
        version = check_version_source()
        for relative in version_targets():
            assert_name_and_version(ROOT / relative, version)
        check_marketplaces()
        check_opencode_entry()
        count = check_skill_tree()
    except ManifestError as exc:
        print(f"plugin manifests: {exc}", file=sys.stderr)
        return 1
    print(f"validated CodeKit manifests (v{version}, {count} skills)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
