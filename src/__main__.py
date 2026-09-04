#!/usr/bin/env python3
"""Console entry point.

With no arguments this runs the stdio MCP server, which is how both CLIs launch it.
The subcommands are operator tools and are deliberately *not* exposed as MCP tools.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-duet",
        description=(
            "Local stdio MCP coordinator for a Claude -> Codex -> Claude "
            "implement/review/reconcile workflow. Run with no arguments to serve."
        ),
    )
    parser.add_argument("--version", action="version", version=f"agent-duet {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.toml (default: $XDG_CONFIG_HOME/agent-duet/config.toml)",
    )
    sub = parser.add_subparsers(dest="command")

    serve_cmd = sub.add_parser("serve", help="run the stdio MCP server (the default)")
    serve_cmd.set_defaults(command="serve")

    worker_cmd = sub.add_parser("worker", help="internal: execute one run (not an MCP tool)")
    worker_cmd.add_argument("--run-id", required=True)

    doctor_cmd = sub.add_parser("doctor", help="report installation and configuration health")
    doctor_cmd.set_defaults(command="doctor")

    gc_cmd = sub.add_parser("gc", help="list, and with --apply remove, old terminal-run artifacts")
    gc_cmd.add_argument("--older-than", type=int, default=30, metavar="DAYS")
    gc_cmd.add_argument(
        "--apply", action="store_true", help="actually remove the listed paths"
    )

    logs_cmd = sub.add_parser(
        "logs",
        help="dump everything: process logs, run timeline, child argv, output, artifacts",
    )
    logs_cmd.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="run id or unique prefix (default: the most recent run)",
    )
    logs_cmd.add_argument("--tail", type=int, default=200, metavar="LINES")
    logs_cmd.add_argument("--full", action="store_true", help="print whole files, no tailing")

    sub.add_parser("runs", help="list every recorded run, newest first")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    config_path: Path | None = args.config
    if config_path is not None:
        os.environ["AGENT_DUET_CONFIG"] = str(config_path)

    command = getattr(args, "command", None)

    if command in (None, "serve"):
        from .server import RUNTIME, serve

        RUNTIME.config_path = config_path
        serve()
        return 0

    if command == "worker":
        from .worker import worker_main

        return worker_main(args.run_id, config_path)

    if command == "doctor":
        from .server import doctor

        return doctor(config_path)

    if command == "gc":
        from .server import gc

        return gc(args.older_than, apply=args.apply, config_path=config_path)

    if command == "logs":
        from .diagnostics import show_logs

        return show_logs(
            args.run_id, tail=args.tail, full=args.full, config_path=config_path
        )

    if command == "runs":
        from .diagnostics import list_runs

        return list_runs(config_path)

    raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
