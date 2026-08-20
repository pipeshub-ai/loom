"""Looking at the system before writing code against it.

The coding agent could always ask what *loom* offers. It could never ask
anything about the world its code would run in, so it wrote against whatever the
spec's author remembered and found out only if the code crashed. The failure
that motivated this: asked to "collect every visible form control", the agent
generated `querySelectorAll('input, textarea, select')` — correct for the spec,
and it returned nothing, because that page builds its controls out of `div`s. The
run completed, reported zero fields, and no check could see it. Rendering the
page was the only thing that could have said so.

The property under test throughout is that looking changes nothing. A probe is
handed to a model, so read-only has to be demonstrable rather than promised.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from loom.agents.coding_tools import build_coding_tools, make_observe_tool
from loom.agents.probes import (
    HttpProbe,
    Observation,
    Probe,
    ProbeError,
    ProbeRegistry,
    default_probes,
)
from loom.testing.conformance import verify_probe

# ---------------------------------------------------------------------------
# A server that records how it was accessed
# ---------------------------------------------------------------------------

PAGE = b"""<!doctype html><title>Booking</title>
<div role="button">Confirm and continue</div>
<button>Next</button>
"""


class _Recorder(BaseHTTPRequestHandler):
    methods: ClassVar[list[str]] = []

    def _respond(self, body: bytes, kind: str) -> None:
        type(self).methods.append(self.command)
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # dispatched on by name
        if self.path == "/api":
            self._respond(json.dumps({"rows": [{"id": 1, "name": "a"}] * 40}).encode(),
                          "application/json")
        else:
            self._respond(PAGE, "text/html")

    def do_HEAD(self) -> None:
        self._respond(b"", "text/html")

    def do_POST(self) -> None:
        type(self).methods.append(self.command)
        self.send_response(405)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Quiet: the handler's default writes to stderr on every request."""


@pytest.fixture
def site():
    _Recorder.methods = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


class TestTheContract:
    async def test_the_shipped_http_probe_conforms(self, site) -> None:
        """Including the part an author would not write a test for: that only
        read methods were used. The code obviously does not write — right up
        until a redirect, a retry, or a helpfully-added fallback means it does."""
        await verify_probe(
            HttpProbe(),
            target=f"{site}/api",
            methods_seen=lambda: _Recorder.methods,
        )

    def test_a_probe_is_structurally_recognised(self) -> None:
        assert isinstance(HttpProbe(), Probe)

    async def test_an_unreachable_target_raises_probe_error(self) -> None:
        """Not a generic exception. The caller separates "could not look" from
        "the code is wrong", and collapsing them puts a model to work repairing
        nothing."""
        with pytest.raises(ProbeError):
            await HttpProbe(timeout=1.0).observe("http://127.0.0.1:9/nothing")


class TestWhatItReports:
    async def test_a_json_response_comes_back_as_its_shape(self, site) -> None:
        """Field names are what the agent guesses at, and a guessed field name
        is a workflow that runs and returns nulls."""
        observation = await HttpProbe().observe(f"{site}/api")

        assert "JSON" in observation.summary
        assert '"name"' in observation.detail

    async def test_a_long_array_is_stood_down_to_one_entry(self, site) -> None:
        """Forty identical rows say the same thing about shape as one does, in
        three orders of magnitude less context."""
        observation = await HttpProbe().observe(f"{site}/api")

        assert observation.detail.count('"id"') == 1
        assert "40 total" in observation.detail

    async def test_html_says_to_look_again_with_a_browser(self, site) -> None:
        """The escalation the agent needs. Fetching a client-rendered page over
        HTTP returns the shell, and a selector written against that shell finds
        nothing at runtime — the exact shape of the original failure."""
        observation = await HttpProbe().observe(site)

        assert "probe='browser'" in observation.summary


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


class _Fake:
    def __init__(self, probe_id: str, *, handles: str = "", boom: bool = False) -> None:
        self.id = probe_id
        self._handles = handles
        self._boom = boom
        self.looked = 0

    def supports(self, target: str) -> bool:
        if self._boom:
            raise RuntimeError("this probe is broken")
        return self._handles in target

    async def observe(self, target: str, *, hint: str = "") -> Observation:
        self.looked += 1
        return Observation(target=target, summary=f"seen by {self.id}", probe=self.id)


class TestSelection:
    def test_local_registration_beats_the_parent(self) -> None:
        """A caller registering its own `http` means to replace the shipped one,
        not to be shadowed by it."""
        parent = ProbeRegistry()
        parent.register(_Fake("http", handles="http"))
        mine = _Fake("http", handles="http")
        child = ProbeRegistry(parent=parent)
        child.register(mine)

        assert child.for_target("http://x.test") is mine

    def test_a_broken_probe_does_not_hide_the_working_ones(self) -> None:
        """Selection runs over every registered probe, so one that raises from
        `supports` would otherwise remove the rest from consideration."""
        registry = ProbeRegistry()
        registry.register(_Fake("broken", boom=True))
        working = _Fake("good", handles="http")
        registry.register(working)

        assert registry.for_target("http://x.test") is working

    def test_an_empty_registry_is_falsy(self) -> None:
        """The question every caller actually asks, and what decides whether the
        agent is offered the tool at all."""
        assert not ProbeRegistry()
        assert default_probes(browser=False)


# ---------------------------------------------------------------------------
# What the agent is given
# ---------------------------------------------------------------------------


class TestTheAgentSurface:
    def test_no_probes_means_no_tool(self) -> None:
        """The degradation that keeps this phase safe: an agent in an
        environment with nothing to look at is exactly the agent that shipped
        before probes existed."""
        names = {t.name for t in build_coding_tools()}

        assert "observe_target" not in names
        assert len(names) == 10

    def test_an_empty_registry_means_no_tool(self) -> None:
        """Present-and-always-failing is worse than absent: it spends context
        every turn to say "not configured" and teaches the model to distrust the
        one capability that would have told it the truth."""
        names = {
            t.name
            for t in build_coding_tools(
                registry=None, validator=None, node_registry=None,
                probes=ProbeRegistry(),
            )
        }

        assert "observe_target" not in names

    def test_a_registered_probe_offers_the_tool(self) -> None:
        registry = ProbeRegistry()
        registry.register(_Fake("fake", handles="http"))
        names = {
            t.name
            for t in build_coding_tools(
                registry=None, validator=None, node_registry=None, probes=registry
            )
        }

        assert "observe_target" in names

    async def test_the_model_can_name_the_probe_it_wants(self) -> None:
        registry = ProbeRegistry()
        cheap, thorough = _Fake("cheap", handles="http"), _Fake("thorough", handles="http")
        registry.register(cheap)
        registry.register(thorough)
        tool = make_observe_tool(registry)

        await tool.fn("http://x.test")
        assert (cheap.looked, thorough.looked) == (1, 0), "the first that supports it"

        await tool.fn("http://x.test", probe="thorough")
        assert thorough.looked == 1, "named, so chosen"

    async def test_an_unknown_probe_name_says_what_there_is(self) -> None:
        registry = ProbeRegistry()
        registry.register(_Fake("http", handles="http"))
        tool = make_observe_tool(registry)

        answer = json.loads(await tool.fn("http://x.test", probe="ghost"))

        assert "ghost" in answer["error"]
        assert answer["probes"] == "http"

    async def test_a_failed_look_is_reported_as_not_observed(self) -> None:
        """Phrased so the model does not go looking for code to repair."""

        class Broken(_Fake):
            async def observe(self, target: str, *, hint: str = "") -> Observation:
                raise ProbeError("the site is down")

        registry = ProbeRegistry()
        registry.register(Broken("http", handles="http"))

        answer = json.loads(await make_observe_tool(registry).fn("http://x.test"))

        assert answer["observed"] is False
        assert "down" in answer["error"]

    async def test_evidence_is_described_rather_than_inlined(self, site) -> None:
        """A screenshot is worth keeping and not worth pasting into a prompt."""
        from loom.blobs.attachment import Attachment

        class WithShot(_Fake):
            async def observe(self, target: str, *, hint: str = "") -> Observation:
                return Observation(
                    target=target,
                    summary="looked",
                    evidence=(Attachment.from_bytes("page.png", b"x" * 999,
                                                    mime="image/png"),),
                    probe=self.id,
                )

        registry = ProbeRegistry()
        registry.register(WithShot("shot", handles="http"))

        answer = json.loads(await make_observe_tool(registry).fn("http://x.test"))

        assert answer["evidence"] == [
            {"filename": "page.png", "mime": "image/png", "size": 999}
        ]


class TestTheModelIsToldTheyExist:
    """A capability nothing points at is a capability nobody uses.

    Watched on the task probes were built for: with `observe_target` in the
    tool list and a URL in the spec, the agent spent forty turns reading the
    tool docs of every integration it had — quickbooks, sharepoint, teams,
    tavily — and reached for the probe once, on an unrelated URL, at the last
    turn. It ran out of budget having written nothing. Told the probes exist and
    when they apply, the same task took two turns.
    """

    def test_the_block_names_the_probes_and_when_to_use_them(self) -> None:
        registry = ProbeRegistry()
        registry.register(_Fake("http", handles="http"))
        registry.register(_Fake("browser", handles="http"))

        block = registry.prompt_block()

        assert "observe_target" in block
        assert "`browser`" in block and "`http`" in block
        assert "read-only" in block.lower()

    def test_nothing_to_look_at_says_nothing(self) -> None:
        """The prompt must not advertise a tool that is not in the tool list."""
        assert ProbeRegistry().prompt_block() == ""

    def test_the_agent_prompt_carries_it(self) -> None:
        """Built through the real constructor: the block has to survive however
        `build_system_prompt` assembles its parts."""
        from loom.agents.coding_agent import WorkflowCodingAgent

        registry = ProbeRegistry()
        registry.register(_Fake("http", handles="http"))

        prompt = WorkflowCodingAgent(_NoModel(), probes=registry).build_system_prompt()

        assert "observe_target" in prompt
        assert "`http`" in prompt

    def test_an_agent_without_probes_says_nothing_about_them(self) -> None:
        """The prompt must not advertise a tool that is not in the tool list."""
        from loom.agents.coding_agent import WorkflowCodingAgent

        prompt = WorkflowCodingAgent(_NoModel()).build_system_prompt()

        assert "observe_target" not in prompt


class _NoModel:
    """Enough of a ModelProvider to construct an agent. Never called."""

    model_name = "none"

    async def complete(self, request):  # pragma: no cover - never invoked
        raise AssertionError("the prompt is built without calling the model")


# ---------------------------------------------------------------------------
# The browser probe
# ---------------------------------------------------------------------------


class TestTheBrowserProbe:
    @pytest.fixture(autouse=True)
    def _needs_playwright(self):
        pytest.importorskip("playwright.async_api")

    async def test_it_counts_both_populations(self, site) -> None:
        """The sentence that was missing. "0 native, 1 role-based" is not a
        description of the page — it is the reason the obvious selector returns
        nothing, which no amount of re-reading the code would reveal."""
        from loom.agents.probes import BrowserProbe

        observation = await BrowserProbe(settle_ms=200).observe(site)

        assert "0 native form control(s)" in observation.summary
        assert "1 role-based widget(s)" in observation.summary
        assert "will find nothing here" in observation.summary

    async def test_it_brings_back_a_screenshot(self, site) -> None:
        from loom.agents.probes import BrowserProbe

        observation = await BrowserProbe(settle_ms=200).observe(site)

        assert [a.filename for a in observation.evidence] == ["page.png"]
        assert observation.evidence[0].mime == "image/png"

    async def test_it_only_reads(self, site) -> None:
        """Rendering a page issues requests. None of them may write."""
        from loom.agents.probes import BrowserProbe

        await BrowserProbe(settle_ms=200).observe(site)

        assert set(_Recorder.methods) <= {"GET", "HEAD"}

    async def test_it_conforms(self, site) -> None:
        from loom.agents.probes import BrowserProbe

        await verify_probe(
            BrowserProbe(settle_ms=200),
            target=site,
            methods_seen=lambda: _Recorder.methods,
        )
