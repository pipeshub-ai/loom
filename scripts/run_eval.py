#!/usr/bin/env python
"""Run the coding-agent eval suite and gate on no regression.

    python scripts/run_eval.py --baseline evals/baseline.json
    python scripts/run_eval.py --write-baseline evals/baseline.json
    python scripts/run_eval.py --model claude-sonnet-5 --stratify

Exit codes are the contract, the same way the CLI's are:

    0   every metric is at or above the committed baseline
    1   a metric regressed past its tolerance
    2   usage, or the suite could not run at all (no model key, no dataset)

Deliberately a comparison and not an absolute bar. A threshold picked today is
one nobody can adopt tomorrow; a baseline ratchets on its own as the numbers
improve, and it can be committed and reviewed in a diff.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loom.eval import (
    EvalRunner,
    StructuralJudge,
    compare,
    load_reference_dataset,
)

#: Models the suite is stratified over when ``--stratify`` is passed.
#:
#: `phases/phase-7` is entirely about small-model compatibility, so a single
#: number from one large model answers the wrong question: what matters is
#: whether the prompt and the verification pipeline carry a weaker model to a
#: working workflow.
STRATA = ("claude-haiku-4-5", "claude-sonnet-5", "gpt-5.6-terra")


def _build_agent(model_name: str):
    """A coding agent for *model_name*, or ``None`` with a reason on stderr."""
    from loom.agents import providers
    from loom.agents.coding_agent import WorkflowCodingAgent
    from loom.toolsets.registry import get_registry, register_available_toolsets

    provider = providers.from_env(model_name)
    if provider is None:
        print(
            f"no API key for {model_name}: set ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY or GEMINI_API_KEY",
            file=sys.stderr,
        )
        return None
    register_available_toolsets()
    return WorkflowCodingAgent(model=provider, tool_registry=get_registry())


async def _run(args: argparse.Namespace) -> int:
    dataset = load_reference_dataset(args.specs)
    if not dataset.cases:
        print(f"no *_spec.txt files under {args.specs}", file=sys.stderr)
        return 2

    models = list(STRATA) if args.stratify else [args.model]
    reports = []
    for model_name in models:
        agent = _build_agent(model_name)
        if agent is None:
            return 2
        runner = EvalRunner(
            coder=agent,
            judge=StructuralJudge(),
            max_concurrency=args.concurrency,
            model=model_name,
        )
        report = await runner.run(dataset)
        print(report.render())
        print()
        reports.append(report)

    if args.write_baseline:
        target = Path(args.write_baseline)
        reports[0].write(target)
        print(f"baseline written to {target}")
        return 0

    if not args.baseline:
        return 0

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(
            f"no baseline at {baseline_path} — run with "
            "--write-baseline to create one",
            file=sys.stderr,
        )
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    regressions = compare(baseline, reports[0])
    if regressions:
        print("REGRESSED:", file=sys.stderr)
        for regression in regressions:
            print(f"  {regression}", file=sys.stderr)
        return 1
    print("no regression against baseline")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument(
        "--stratify",
        action="store_true",
        help=f"run every model in {', '.join(STRATA)}",
    )
    parser.add_argument("--specs", default=None, help="directory of *_spec.txt")
    parser.add_argument("--baseline", default=None, help="gate against this report")
    parser.add_argument("--write-baseline", default=None, help="record a new baseline")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
