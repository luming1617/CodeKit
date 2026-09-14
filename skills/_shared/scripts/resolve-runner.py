#!/usr/bin/env python3
"""Resolve a CodeKit-managed tool and report the state this session actually sees.

Thin CLI over ``codekit_runtime``.  Exists because the answer is needed from a shell
as much as from Python: when ``codekit:initialize`` reports a tool as unavailable,
running this in the *same* session is what turns "it says tool-missing" into a named
state plus the one action that fixes it.

Exit codes are distinct per state so a shell can branch without parsing text.
``1`` is deliberately unused: it is too easy to confuse with "the script crashed".

    0  ok
    3  not-installed
    4  not-readable      (installed, but this session is denied reading it)
    5  not-executable    (readable, but it cannot start here)
    2  bad invocation

``--explain`` adds the per-stage trace (override -> toolchain.json -> PATH ->
known directories) with the measured state of each candidate, which is what turns
"the tool is unavailable" into "the PATH hit was refused, the manifest hit won".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import codekit_runtime as runtime  # noqa: E402

EXIT_CODES = {
    runtime.OK: 0,
    runtime.NOT_INSTALLED: 3,
    runtime.NOT_READABLE: 4,
    runtime.NOT_EXECUTABLE: 5,
}
TOOLS = runtime.KNOWN_TOOLS


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve a CodeKit-managed tool against the current session"
    )
    parser.add_argument("--tool", required=True, choices=TOOLS)
    parser.add_argument(
        "--json", action="store_true", help="emit one JSON object instead of a line"
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="show every resolution stage and why it did or did not win",
    )
    parser.add_argument("--home", type=Path, help="override the home directory to inspect")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    home = args.home
    resolution = runtime.resolve_tool(args.tool, home=home)
    if args.json:
        payload = resolution.to_json(explain=args.explain)
        payload["hint"] = runtime.state_hint(args.tool, resolution.state, home=home)
        payload["toolchain_root"] = str(runtime.toolchain_root(home))
        payload["manifest"] = str(runtime.manifest_path(home))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.explain:
        print(runtime.format_explanation(resolution, home=home))
    else:
        print(runtime.format_report(resolution, home=home))
    return EXIT_CODES.get(resolution.state, 2)


if __name__ == "__main__":
    raise SystemExit(main())
