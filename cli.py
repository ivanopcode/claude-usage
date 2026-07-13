"""Small command dispatcher for the standalone Claude usage TUI."""

from __future__ import print_function

import argparse
import sys
from pathlib import Path

from collector import DEFAULT_DB_PATH, VERSION, scan
from usage_tui import MODES, main as usage_main


def _scan(argv):
    parser = argparse.ArgumentParser(
        prog="claude-usage scan",
        description="Incrementally index local Claude Code JSONL transcripts.",
    )
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    scan(projects_dir=args.projects_dir, db_path=args.db)
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--version", "-V", "version"):
        print(VERSION)
        return 0
    if args and args[0] == "scan":
        return _scan(args[1:])
    if args and args[0] in ("usage", "graph"):
        return usage_main(args[1:])
    if args and args[0] == "audit":
        return usage_main(["--audit"] + args[1:])
    if not args or args[0] in MODES or args[0].startswith("-"):
        return usage_main(args)

    print(
        "Unknown command: {}\n"
        "Use claude-usage [daily|weekly|cumulative], audit, or scan.".format(args[0]),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
