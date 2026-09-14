#!/usr/bin/env python3
"""Synchronize the configured CodeKit Plugin version into package manifests.

``TARGET_FILES`` is the single authority for *which* files carry a copy of the
version. ``validate-plugin-manifests.py`` loads it from here rather than keeping its
own copy, and the ``git add`` list in ``lefthook.yml`` mirrors it (that one cannot be
imported, so mirror it by hand when a platform is added).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import installer_config as config  # noqa: E402


VERSION_FILE = Path("plugin-version.json")
TARGET_FILES = (
    Path(".claude-plugin/plugin.json"),
    Path(".codex-plugin/plugin.json"),
    Path(".opencode/package.json"),
    Path(".qoder-plugin/plugin.json"),
)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"JSON root must be an object: {path}")
    return value


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config_path = root / VERSION_FILE
    source_document = read_json(config_path)
    version = source_document.get("version")
    # The format itself comes from installer_config.SEMVER_PATTERN -- the same
    # pattern the installer enforces through validate_plugin_version(), so a version
    # that syncs can never be rejected by a later gate. The read and the CLI error
    # path stay local: installer_config.read_json() reports a missing file as an
    # empty object, which would mislabel "no plugin-version.json" as a bad version.
    if not isinstance(version, str) or not config.SEMVER_PATTERN.fullmatch(version):
        raise SystemExit(
            f"{config_path}: version must be a semantic version such as 1.0.0"
        )

    changed = []
    for relative_path in TARGET_FILES:
        path = root / relative_path
        document = read_json(path)
        if document.get("version") == version:
            continue
        document["version"] = version
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        changed.append(str(relative_path))

    if changed:
        print(f"plugin version synchronized to {version}: {', '.join(changed)}")
    else:
        print(f"plugin version already synchronized: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
