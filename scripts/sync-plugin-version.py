#!/usr/bin/env python3
"""Synchronize the configured CodeKit Plugin version into package manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path


VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
VERSION_FILE = Path("plugin-version.json")
TARGET_FILES = (
    Path(".claude-plugin/plugin.json"),
    Path(".codex-plugin/plugin.json"),
    Path(".opencode/package.json"),
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
    config = read_json(config_path)
    version = config.get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
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
