"""Minimal CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workflow_builder import Workflow, WorkflowExecutor, __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workflow-builder")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a workflow definition file")
    run_parser.add_argument("path", type=Path, help="Path to a workflow JSON file")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "run":
        data = json.loads(args.path.read_text(encoding="utf-8"))
        workflow = Workflow.model_validate(data)
        result = WorkflowExecutor().run(workflow)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0 if result.status == "completed" else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
