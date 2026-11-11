#!/usr/bin/env python3
"""Print the maintained Replicator handoff guide."""

import argparse
from pathlib import Path
import sys


def build_parser():
    parser = argparse.ArgumentParser(
        prog="replicator",
        description="Print instructions for an explicitly requested manual handoff.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    guide = commands.add_parser("guide", help="print the maintained handoff guide")
    guide.add_argument("--print", dest="print_guide", action="store_true", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "guide" and args.print_guide:
        sys.stdout.buffer.write(Path(__file__).with_name("guide.md").read_bytes())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
