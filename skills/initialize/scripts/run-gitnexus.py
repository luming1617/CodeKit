#!/usr/bin/env python3
"""Run GitNexus with an explicit fallback for a sandboxed global registry.

GitNexus stores its local index in the repository but registers repositories in
``GITNEXUS_HOME/registry.json``.  A sandbox may allow the former and reject the
latter.  This adapter makes that downgrade visible and keeps the registration
for this worker in a disposable directory instead of silently losing the
actual GitNexus error.

The disposable directory is deleted when the process exits, so a run that stays
on this path leaves an index that ``status`` accepts while ``list``/``query``/
``context``/``impact``/``trace``/``cypher``/``check``/``detect-changes`` and the
MCP graph queries answer ``No indexed repositories found``.  Callers that need a
usable index must pass ``--use-global-registry`` after the user authorized the
global write, and must verify the registration instead of trusting this exit
code.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _common import gitnexus_home, likely_writable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--use-global-registry",
        action="store_true",
        help="user authorized global writes: register in the real GITNEXUS_HOME",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a GitNexus command is required after --")

    original_dir = gitnexus_home()
    authorized = args.use_global_registry or os.environ.get(
        "CODEKIT_GITNEXUS_USE_GLOBAL_REGISTRY"
    ) == "1"
    if authorized:
        if likely_writable(original_dir):
            completed = subprocess.run(command, cwd=args.root, text=True)
            return completed.returncode
        print(
            "initialize warning: --use-global-registry was requested but "
            f"{original_dir} is not writable; falling back to a disposable registry",
            file=sys.stderr,
        )

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
