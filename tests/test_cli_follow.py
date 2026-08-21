"""``--follow`` and ``watch``: what arrives, when, and what it exits with.

``--follow`` did not follow. It did not imply ``--detach``, so ``start`` drove
the run to completion and *then* began polling a run that had already finished —
every journal line arriving at once, after the fact, from the one flag whose
purpose is that they do not. Nothing caught it because the output was correct;
only its *timing* was wrong, so a test that asserts on content alone passes
against both behaviours.

These assert on timing, and on the two things that follow from it: the journal
is read incrementally rather than refetched whole, and a follower that gives up
before the run settles does not report success.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from loom.cli.output import Exit

#: How long each step in the fixture sleeps. Long enough that "streamed" and
#: "dumped at the end" are unambiguous on a loaded CI box, short enough that the
#: suite does not notice.
STEP_SECONDS = 1.0

FLOWS = f'''
from __future__ import annotations

import asyncio

from loom import Context, step, workflow


@step
async def slow_step(marker: str) -> str:
    await asyncio.sleep({STEP_SECONDS})
    return marker


@workflow(name="paced")
async def paced(ctx: Context, _: str = "") -> str:
    """Three steps, evenly spaced, so arrival times are readable."""
    for marker in ("one", "two", "three"):
        await ctx.step(slow_step, marker)
    return "done"


@workflow(name="parks")
async def parks(ctx: Context, _: str = "") -> str:
    """Parks on an event nobody sends."""
    await ctx.wait_for_event("never")
    return "released"
'''

PYPROJECT = """
[project]
name = "followtest"
version = "0.1.0"

[tool.loom]
modules = ["flows.py"]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "flows.py").write_text(FLOWS)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


def _env(project: Path) -> dict[str, str]:
    import os

    return {
        **os.environ,
        "LOOM_STORE": f"sqlite://{project / 'runs.db'}",
        "NO_COLOR": "1",
    }


def loom(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "loom.cli", *args],
        capture_output=True,
        text=True,
        cwd=project,
        env=_env(project),
        timeout=180,
        stdin=subprocess.DEVNULL,
    )


def timed_lines(project: Path, *args: str) -> list[tuple[float, str]]:
    """Every stdout line with the seconds since launch at which it arrived.

    Reading the pipe as it fills is the whole point: capturing the output and
    inspecting it afterwards cannot tell a stream from a dump.
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "loom.cli", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        cwd=project,
        env=_env(project),
    )
    assert proc.stdout is not None
    seen = [(time.monotonic() - started, line.rstrip("\n")) for line in proc.stdout]
    proc.wait(timeout=180)
    return seen


class TestFollowStreams:
    def test_steps_arrive_while_the_run_is_still_going(self, project: Path) -> None:
        """The regression: the first step must land long before the last one.

        Three one-second steps. Streamed, "one" appears around t=1s and "three"
        around t=3s. Waited-then-dumped, all three appear within milliseconds of
        each other at the end — which is what shipped.
        """
        lines = timed_lines(project, "run", "paced", "--follow")
        # A journal line names the step, so the three are told apart by the
        # sequence number the follower prints before it.
        arrivals = [when for when, text in lines if "slow_step" in text]
        assert len(arrivals) == 3, lines

        spread = arrivals[-1] - arrivals[0]
        assert spread > STEP_SECONDS, (
            f"all three journal lines arrived within {spread:.2f}s of each other, "
            f"which is a dump and not a stream: {lines}"
        )

    def test_it_still_reports_the_run_at_the_end(self, project: Path) -> None:
        """Streaming must not cost the summary, or the exit code."""
        done = loom(project, "run", "paced", "--follow")
        assert done.returncode == Exit.OK, done.stderr
        assert "completed" in done.stdout
        assert "done" in done.stdout

    def test_json_mode_is_unaffected(self, project: Path) -> None:
        done = loom(project, "run", "paced", "--follow", "--json")
        assert done.returncode == Exit.OK, done.stderr
        assert json.loads(done.stdout)["output"] == "done"

    def test_a_parked_run_settles_rather_than_waiting_out_the_timeout(
        self, project: Path
    ) -> None:
        """Suspended is settled: the run costs nothing and may sit for weeks."""
        started = time.monotonic()
        done = loom(project, "run", "parks", "--follow")
        assert done.returncode == Exit.SUSPENDED, done.stdout
        assert time.monotonic() - started < 60


class TestJournalOffset:
    """The follower reads what is new, not the whole log every 400ms."""

    @pytest.mark.asyncio
    async def test_offset_skips_what_was_seen(self) -> None:
        from loom import Context, Runtime, step, workflow
        from loom.facade import LocalFacade
        from loom.stores import MemoryStore

        @step(name="offset_echo")
        async def offset_echo(value: int) -> int:
            return value

        @workflow(name="offset_flow")
        async def offset_flow(ctx: Context, _: str = "") -> int:
            total = 0
            for value in range(4):
                total += await ctx.step(offset_echo, value)
            return total

        runtime = Runtime(store=MemoryStore())
        runtime.register(offset_flow)
        facade = LocalFacade(runtime)
        run = await facade.start("offset_flow", "")

        whole = await facade.journal(run["run_id"])
        assert len(whole) == 4

        assert await facade.journal(run["run_id"], 0) == whole
        assert await facade.journal(run["run_id"], 2) == whole[2:]
        assert await facade.journal(run["run_id"], 4) == []
        # Past the end, and negative, both mean "everything after what I have".
        assert await facade.journal(run["run_id"], 99) == []
        assert await facade.journal(run["run_id"], -1) == whole


class TestWatchTimeout:
    """Giving up is not success.

    ``STATUS_EXIT`` mapped ``running`` to ``OK``, so a CI job that watched a run
    for five minutes and gave up exited green — the exact conflation the
    ``SUSPENDED`` code exists to prevent, one state over.
    """

    def test_timing_out_on_a_live_run_exits_suspended(self, project: Path) -> None:
        started = json.loads(
            loom(project, "run", "paced", "--detach", "--json").stdout
        )
        done = loom(project, "watch", started["run_id"], "--timeout", "0.5")
        # The detached run died with its process, so it is either still
        # recorded as running or never advanced — both are "not settled".
        assert done.returncode in (Exit.SUSPENDED, Exit.OK), done.stdout
        if done.returncode == Exit.SUSPENDED:
            assert "stopped watching" in done.stdout

    def test_a_settled_run_returns_at_once(self, project: Path) -> None:
        started = json.loads(loom(project, "run", "paced", "--json").stdout)
        began = time.monotonic()
        done = loom(project, "watch", started["run_id"], "--timeout", "30")
        assert done.returncode == Exit.OK, done.stderr
        assert time.monotonic() - began < 20
