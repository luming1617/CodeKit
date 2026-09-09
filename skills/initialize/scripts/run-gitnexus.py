#!/usr/bin/env python3
"""Run GitNexus with an explicit fallback for a sandboxed global registry.

GitNexus stores its local index in the repository but registers repositories in
``GITNEXUS_HOME/registry.json``.  A sandbox may allow the former and reject the
latter.  This adapter makes that downgrade visible and keeps the registration
for this worker in a disposable directory instead of silently losing the
actual GitNexus error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def registry_dir() -> Path:
    configured = os.environ.get("GITNEXUS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".gitnexus"


def likely_writable(path: Path) -> bool:
    """Check the nearest existing directory without creating external files."""
    cursor = path if path.is_dir() else path.parent
    while not cursor.exists() and cursor != cursor.parent:
        cursor = cursor.parent
    return os.access(cursor, os.W_OK)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a GitNexus command is required after --")

    original_dir = registry_dir()
    if os.environ.get("CODEKIT_GITNEXUS_USE_GLOBAL_REGISTRY") == "1" and likely_writable(original_dir):
        completed = subprocess.run(command, cwd=args.root, text=True)
        return completed.returncode

    try:
        with tempfile.TemporaryDirectory(prefix="codekit-gitnexus-home-") as temporary:
            fallback = Path(temporary) / "registry"
            env = os.environ.copy()
            env["GITNEXUS_HOME"] = str(fallback)
            print(
                "initialize warning: GitNexus global registry is not writable "
                f"or is intentionally isolated ({original_dir}); using disposable "
                f"GITNEXUS_HOME={fallback}",
                file=sys.stderr,
            )
            completed = subprocess.run(command, cwd=args.root, text=True, env=env)
            return completed.returncode
    except OSError as error:
        print(
            f"initialize error: GitNexus registry fallback could not run: {error}",
            file=sys.stderr,
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
