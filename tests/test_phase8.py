"""Tests for Phase 8 — Reference Workflows.

These tests **run** the ten reference workflows. That is the whole point of the
file, and it is what the version it replaced did not do: every assertion there
was ``wf_file.read_text()`` plus a substring check, so the suite reported 104
passing tests over ten workflows while five of them raised ``TypeError`` on
import. A gate that reads source text cannot tell a working example from a
broken one — ``Retry(delay=...)`` resolves every name and compiles cleanly.

Each workflow is driven through :func:`loom.testing.run_with`, which seeds the
journal with what each step already returned. A seeded entry means exactly what
a recorded one means, so nothing here is a stand-in that can drift from the step
it stands in for — and no test needs a network, a credential, or a mocked HTTP
client.

Three things are asserted per workflow:

* it **imports** — the check the old suite could not make;
* it **runs** to the output its spec describes, over seeded facts;
* it **replays** identically, via :func:`loom.testing.assert_replays`.

The structural tests that remain — a spec file exists, a module has a docstring
— are kept because they are true statements about the files rather than claims
about behaviour they cannot observe.
"""

from __future__ import annotations

import ast
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from loom.runtime.journal import EntryKind
from loom.runtime.workflow import WorkflowDefinition
from loom.testing import assert_replays, given, run_with

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
_REF_DIR = _EXAMPLES / "reference"
_SPEC_DIR = _EXAMPLES / "reference_specs"

WORKFLOW_FILES = sorted(_REF_DIR.glob("wf*.py"))
SPEC_FILES = sorted(_SPEC_DIR.glob("wf*_spec.txt"))

if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))


def _module(wf_file: Path) -> Any:
    """Import one reference workflow as ``reference.<name>``."""
    return importlib.import_module(f"reference.{wf_file.stem}")


def _workflow_of(module: Any) -> WorkflowDefinition:
    """The single ``@workflow`` a reference module defines."""
    found = [v for v in vars(module).values() if isinstance(v, WorkflowDefinition)]
    assert len(found) == 1, f"{module.__name__} defines {len(found)} workflows, want 1"
    return found[0]


# ---------------------------------------------------------------------------
# Cases — the facts each workflow's steps already produced
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """One workflow, the input it takes, and the journal it runs against.

    ``facts`` is built lazily from the module because the seeds are the
    module's own Pydantic models: a case that constructed them at import time
    would need the models before the file under test had been imported.
    """

    stem: str
    build: Any
    "``(module) -> (payload, [given(...), ...], check)``"

    variant: str = ""
    """Names a second path through the same workflow.

    A workflow with a human gate has two outcomes that matter and only one of
    them is the happy path; testing the approval and calling it covered would
    leave "what happens when somebody says no" unasserted."""

    id: str = field(default="")

    def __post_init__(self) -> None:
        base = self.stem.split("_")[0]
        self.id = f"{base}-{self.variant}" if self.variant else base


def _wf01(m: Any) -> Any:
    """Two leads: one already in the CRM and skipped, one drafted and approved."""
    from loom.agents.result import AgentResult
    from loom.nodes.agentic import ExtractStructuredOut
    from loom.nodes.human import ReviewOut

    payload = m.LeadConfig(describe="companies building durable execution", max_leads=2)

    def extracted(name: str, email: str) -> ExtractStructuredOut:
        return ExtractStructuredOut(
            values={"name": name, "email": email, "company": "Acme", "title": "CTO"},
            parsed=True,
        )

    facts = [
        given(m.find_companies, returns=["https://acme.test", "https://known.test"]),
        given(m.read_pages, returns=["Acme builds engines.", "Known Ltd builds looms."]),
        given(
            "node:agent.extract_structured",
            returns=extracted("Ada", "ada@acme.test"),
            occurrence=0,
        ),
        given(
            "node:agent.extract_structured",
            returns=extracted("Grace", "grace@known.test"),
            occurrence=1,
        ),
        # The second lead is already a contact, so it is skipped before any
        # mail is written — which is why only one draft is seeded below.
        given(m.existing_contact, returns="", occurrence=0),
        given(m.existing_contact, returns="contact-42", occurrence=1),
        given(
            "write_email",
            kind=EntryKind.AGENT,
            returns=AgentResult(output="Hi Ada — noticed Acme builds engines."),
        ),
        given(m.draft_email, returns="draft-1"),
        given(
            "node:human.review_edit",
            returns=ReviewOut(
                content="Hi Ada — noticed Acme builds engines.",
                approved=True,
                edited=False,
                responder="reviewer@example.com",
            ),
        ),
        given(m.record_lead, returns="contact-1"),
        given(m.send_draft, returns="msg-1"),
        given(m.report_to_slack, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.total_leads == 2
        assert out.emails_sent == 1
        assert out.skipped_existing == 1
        assert out.rejected_by_reviewer == 0
        assert out.errors == []

    return payload, facts, check


def _wf02(m: Any) -> Any:
    """Two channels, both published, after an edited review."""
    from loom.agents.result import AgentResult
    from loom.nodes.human import ReviewOut

    payload = m.ContentConfig(
        niche="durable execution",
        confluence_space_id="ENG",
    )
    facts = [
        given(
            m.find_topic,
            returns=m.Topic(
                title="Journals beat retries",
                summary="Because a replay serves what happened.",
                citations=["https://source.test/1"],
            ),
        ),
        given(
            "write:slack",
            kind=EntryKind.AGENT,
            returns=AgentResult(output="slack draft"),
        ),
        given(
            "write:confluence",
            kind=EntryKind.AGENT,
            returns=AgentResult(output="confluence draft"),
        ),
        # The reviewer edits, and what they wrote is what goes out — not the
        # draft they were shown beside it.
        given(
            "node:human.review_edit",
            returns=ReviewOut(
                content="[slack]\nedited slack\n\n=====\n\n[confluence]\nedited page",
                approved=True,
                edited=True,
                responder="editor@example.com",
            ),
        ),
        given(
            m.publish,
            returns=m.Published(channel="slack", reference="ts-1"),
            occurrence=0,
        ),
        given(
            m.publish,
            returns=m.Published(channel="confluence", reference="https://wiki/1"),
            occurrence=1,
        ),
    ]

    def check(out: Any) -> None:
        assert out.topic == "Journals beat retries"
        assert out.approved is True
        assert [row.channel for row in out.published] == ["slack", "confluence"]
        assert out.landed == 2

    return payload, facts, check


def _wf02_rejected(m: Any) -> Any:
    """A reviewer says no, and nothing is posted."""
    from loom.agents.result import AgentResult
    from loom.nodes.human import ReviewOut

    payload = m.ContentConfig(niche="durable execution")
    facts = [
        given(m.find_topic, returns=m.Topic(title="A topic", summary="…")),
        given("write:slack", kind=EntryKind.AGENT, returns=AgentResult(output="draft")),
        given(
            "node:human.review_edit",
            returns=ReviewOut(content="", approved=False, responder="editor@example.com"),
        ),
    ]

    def check(out: Any) -> None:
        assert out.approved is False
        assert out.published == []

    return payload, facts, check


def _wf02_partial_failure(m: Any) -> Any:
    """One channel fails. The other still lands, and the result says so."""
    from loom.agents.result import AgentResult
    from loom.nodes.human import ReviewOut

    payload = m.ContentConfig(niche="durable execution", confluence_space_id="ENG")
    facts = [
        given(m.find_topic, returns=m.Topic(title="A topic", summary="…")),
        given("write:slack", kind=EntryKind.AGENT, returns=AgentResult(output="a")),
        given("write:confluence", kind=EntryKind.AGENT, returns=AgentResult(output="b")),
        given(
            "node:human.review_edit",
            returns=ReviewOut(content="whatever", approved=True, edited=False),
        ),
        given(
            m.publish,
            returns=m.Published(channel="slack", reference="ts-1"),
            occurrence=0,
        ),
        # `on_error=CONTINUE` hands back the declared fallback.
        given(m.publish, returns=None, occurrence=1),
    ]

    def check(out: Any) -> None:
        assert out.landed == 1
        assert len(out.published) == 1, "a failed channel contributes no row"
        assert out.approved is True

    return payload, facts, check


def _wf03(m: Any) -> Any:
    """Four messages: a meeting, spam, an unsure one, and a confident urgent."""
    from loom.nodes.agentic import ClassifyOut, ExtractStructuredOut

    def message(n: str) -> dict[str, str]:
        return {
            "id": n,
            "thread_id": f"t-{n}",
            "subject": f"subject {n}",
            "sender": "a@example.com",
            "body": "body",
        }

    payload = m.TriageConfig(max_emails=4)
    facts = [
        given(
            m.fetch_unread,
            returns={
                "messages": [message(n) for n in ("m1", "s1", "u1", "x1")],
                # The page cap cut the inbox short, and the result says so
                # rather than reporting four as the total.
                "complete": False,
            },
        ),
        given(m.resolve_label, returns="Label_7"),
        given(
            "node:agent.classify",
            returns=ClassifyOut(label="meeting", confident=True),
            occurrence=0,
        ),
        given(
            "node:agent.classify",
            returns=ClassifyOut(label="spam", confident=True),
            occurrence=1,
        ),
        # Unsure: routed for review rather than filed by the guess.
        given(
            "node:agent.classify",
            returns=ClassifyOut(label="support", confident=False),
            occurrence=2,
        ),
        given(
            "node:agent.classify",
            returns=ClassifyOut(label="urgent", confident=True),
            occurrence=3,
        ),
        # From here the seeds are interleaved, because a journal is positional
        # and this is the order the body reaches them: m1 is a meeting, s1 is
        # spam and skipped entirely, u1 goes to review, x1 is urgent.
        given(m.route, returns=None, occurrence=0),  # m1 → #calendar
        given(
            "node:agent.extract_structured",
            returns=ExtractStructuredOut(
                values={
                    "title": "Design sync",
                    "start": "2026-03-01T15:00:00Z",
                    "end": "2026-03-01T16:00:00Z",
                },
                parsed=True,
            ),
        ),
        given(m.create_meeting, returns="event-1"),
        given(m.label_thread, returns=True, occurrence=0),  # m1
        given(m.route, returns=None, occurrence=1),  # u1 → review
        given(m.route, returns=None, occurrence=2),  # x1 → #ops-urgent
        given(m.label_thread, returns=True, occurrence=1),  # x1
    ]

    def check(out: Any) -> None:
        assert out.total == 4
        assert out.complete is False, "the page cap must be reported, not hidden"
        assert out.needs_review == 1
        assert out.meetings_created == 1
        # spam is dropped, the unsure one went to review, so two were routed.
        assert out.routed == 2
        assert out.category_counts == {
            "meeting": 1,
            "spam": 1,
            "support": 1,
            "urgent": 1,
        }

    return payload, facts, check


def _wf04(m: Any) -> Any:
    """Three records: one created, one updated, one that fails to upsert."""
    from loom.nodes.control import BatchOut

    def row(n: str) -> dict[str, str]:
        return {
            "record_id": f"003{n}",
            "email": f"{n}@example.com",
            "first_name": n,
            "last_name": "Example",
            "company": "Acme",
        }

    rows = [row("ada"), row("grace"), row("alan")]
    payload = m.SyncConfig(batch_size=3, approve_above=100)
    facts = [
        given(m.read_ready, returns={"records": rows, "complete": True}),
        given("node:control.batch", returns=BatchOut(batches=[rows], count=1)),
        given(m.upsert, returns={"id": "1", "action": "created"}, occurrence=0),
        given(m.mark_synced, returns=True, occurrence=0),
        given(m.upsert, returns={"id": "2", "action": "updated"}, occurrence=1),
        given(m.mark_synced, returns=True, occurrence=1),
        # The third fails. `on_error=CONTINUE` hands back the declared
        # fallback, and the body counts it — the write-back is skipped, so the
        # record stays flagged and the next run picks it up again.
        given(m.upsert, returns=None, occurrence=2),
        given(m.report, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.records_read == 3
        assert out.created == 1
        assert out.updated == 1
        assert out.failed == 1
        assert out.approved is True
        assert out.errors == ["alan@example.com: upsert failed"]

    return payload, facts, check


def _wf04_refused(m: Any) -> Any:
    """A suspicious volume, and a person says no. Nothing is written."""
    from loom.nodes.human import ApprovalOut

    rows = [
        {
            "record_id": f"003{n}",
            "email": f"c{n}@example.com",
            "first_name": "C",
            "last_name": str(n),
            "company": "Acme",
        }
        for n in range(3)
    ]
    payload = m.SyncConfig(approve_above=2)
    facts = [
        given(m.read_ready, returns={"records": rows, "complete": False}),
        given(
            "node:human.approval",
            returns=ApprovalOut(approved=False, responder="ops@example.com"),
        ),
        given(m.report, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.approved is False
        assert out.created == 0 and out.updated == 0
        assert out.complete is False, "the partial read must still be reported"

    return payload, facts, check


def _wf05(m: Any) -> Any:
    """Three variants, one already said, one duplicated, the best published."""
    from loom.agents.result import AgentResult
    from loom.nodes.agentic import JudgeOut
    from loom.nodes.control import DedupeOut
    from loom.nodes.human import ApprovalOut

    payload = m.PublisherConfig(
        topic="durable execution",
        variants=3,
        targets=[m.Target(kind="slack", channel="C1")],
    )
    a = m.Variant(text="first angle").fingerprinted()
    b = m.Variant(text="second angle").fingerprinted()

    facts = [
        given("variant:0", kind=EntryKind.AGENT, returns=AgentResult(output="first angle")),
        given("variant:1", kind=EntryKind.AGENT, returns=AgentResult(output="second angle")),
        # The third repeats the first, word for word.
        given("variant:2", kind=EntryKind.AGENT, returns=AgentResult(output="first angle")),
        given(
            "node:control.dedupe",
            returns=DedupeOut(items=[a.model_dump(), b.model_dump()], removed=1),
        ),
        # `a` was published by an earlier run, so only `b` is fresh.
        given(m.unseen, returns=[b]),
        given(
            f"judge:{b.fingerprint[:8]}",
            returns=JudgeOut(passed=True, score=0.8, reason="specific and plain"),
        ),
        given("node:human.approval", returns=ApprovalOut(approved=True, responder="ada")),
        given(
            m.publish,
            returns=m.PublishResult(kind="slack", channel="C1", reference="ts-1"),
        ),
    ]

    def check(out: Any) -> None:
        assert out.generated == 3
        assert out.duplicates_in_batch == 1
        assert out.already_said == 1
        assert out.approved is True
        assert out.chosen_score == 0.8
        assert out.chosen_reason == "specific and plain"
        assert out.landed == 1

    return payload, facts, check


def _wf05_all_said_before(m: Any) -> Any:
    """Everything was published already. Nothing posts, and that is correct."""
    from loom.agents.result import AgentResult
    from loom.nodes.control import DedupeOut

    payload = m.PublisherConfig(topic="durable execution", variants=1)
    only = m.Variant(text="already said").fingerprinted()
    facts = [
        given("variant:0", kind=EntryKind.AGENT, returns=AgentResult(output="already said")),
        given("node:control.dedupe", returns=DedupeOut(items=[only.model_dump()], removed=0)),
        given(m.unseen, returns=[]),
    ]

    def check(out: Any) -> None:
        assert out.already_said == 1
        assert out.published == []
        assert out.approved is True, "nothing was refused — there was nothing to refuse"

    return payload, facts, check


def _wf05_refused(m: Any) -> Any:
    """A person declines, so nothing posts and history is not written."""
    from loom.agents.result import AgentResult
    from loom.nodes.agentic import JudgeOut
    from loom.nodes.control import DedupeOut
    from loom.nodes.human import ApprovalOut

    payload = m.PublisherConfig(
        topic="durable execution",
        variants=1,
        targets=[m.Target(kind="slack", channel="C1")],
    )
    only = m.Variant(text="a post").fingerprinted()
    facts = [
        given("variant:0", kind=EntryKind.AGENT, returns=AgentResult(output="a post")),
        given("node:control.dedupe", returns=DedupeOut(items=[only.model_dump()], removed=0)),
        given(m.unseen, returns=[only]),
        given(
            f"judge:{only.fingerprint[:8]}",
            returns=JudgeOut(passed=True, score=0.5, reason="fine"),
        ),
        given("node:human.approval", returns=ApprovalOut(approved=False, responder="ada")),
    ]

    def check(out: Any) -> None:
        assert out.approved is False
        assert out.published == []

    return payload, facts, check


def _wf06(m: Any) -> Any:
    """A two-page PDF read under a one-page cap, so `truncated` is exercised."""
    from loom.blobs.artifact import ArtifactVersion
    from loom.blobs.attachment import Attachment
    from loom.nodes.agentic import ExtractStructuredOut, SummarizeOut
    from loom.nodes.documents import Page, ParseDocumentOut

    payload = m.DocConfig(message_id="msg-1", max_pages=1)
    facts = [
        given(
            m.first_attachment,
            returns=Attachment.from_bytes(
                "invoice.pdf", b"%PDF-1.4 fake", mime="application/pdf"
            ),
        ),
        given(
            "node:transform.parse_document",
            returns=ParseDocumentOut(
                text="Invoice 42 for Acme. Total 1,200 GBP.",
                pages=[Page(number=1, text="Invoice 42 for Acme.")],
                page_count=2,
                truncated=True,
                format="pdf",
                filename="invoice.pdf",
            ),
        ),
        given(
            "extract_fields",
            returns=ExtractStructuredOut(
                values={
                    "title": "Invoice 42",
                    "counterparty": "Acme",
                    "total_amount": "1,200 GBP",
                },
                parsed=True,
            ),
        ),
        given(
            "write_summary",
            returns=SummarizeOut(summary="Acme owes 1,200 GBP against invoice 42."),
        ),
        # `ctx.put_artifact` is a durable call and occupies a position, so it
        # is seeded like any other. Leaving it out would let it run for real
        # and shift every seed after it by one.
        given(
            "artifact:put:document:msg-1",
            returns=ArtifactVersion(
                name="document:msg-1", version=1, ref="blob:abc", mime="text/plain"
            ),
        ),
        given(m.file_summary, returns="drive-file-1"),
    ]

    def check(out: Any) -> None:
        assert out.filename == "invoice.pdf"
        assert out.format == "pdf"
        assert out.parsed.title == "Invoice 42"
        assert out.parsed.total_amount == "1,200 GBP"
        assert out.summary.startswith("Acme owes")
        assert out.drive_file_id == "drive-file-1"
        assert out.artifact
        assert out.page_count == 2
        assert out.truncated is True, (
            "the page cap fired, and a caller acting on the summary has to be "
            "able to see that the document was only partly read"
        )

    return payload, facts, check


def _wf07(m: Any) -> Any:
    """One prospect, two competitors, approved and published."""
    from loom.agents.result import AgentResult
    from loom.nodes.human import ReviewOut

    payload = m.BattleCardConfig(
        company_name="Northwind",
        competitor_names=["Rival A", "Rival B"],
        our_product="LOOM",
        space_id="SALES",
    )
    facts = [
        given(
            m.research,
            returns=m.Research(
                subject="Northwind",
                summary="Northwind runs a large logistics estate.",
                citations=["https://northwind.test/about"],
            ),
            occurrence=0,
        ),
        given(
            m.research,
            returns=m.Research(subject="Rival A", summary="Rival A sells a canvas."),
            occurrence=1,
        ),
        given(
            m.research,
            returns=m.Research(subject="Rival B", summary="Rival B sells a queue."),
            occurrence=2,
        ),
        given(
            "compare:Rival A",
            kind=EntryKind.AGENT,
            returns=AgentResult(output="We replay; they restart."),
        ),
        given(
            "compare:Rival B",
            kind=EntryKind.AGENT,
            returns=AgentResult(output="We journal; they retry."),
        ),
        given(
            "node:human.review_edit",
            returns=ReviewOut(
                content="h1. Northwind\n\nchecked by a human",
                approved=True,
                edited=True,
                responder="rep@example.com",
            ),
        ),
        given(m.publish, returns="https://wiki.test/SALES/northwind"),
        given(m.announce, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.company == "Northwind"
        assert out.prospect.citations == ["https://northwind.test/about"]
        assert [c.competitor for c in out.comparisons] == ["Rival A", "Rival B"]
        assert out.approved is True
        assert out.published_url == "https://wiki.test/SALES/northwind"

    return payload, facts, check


def _wf07_rejected(m: Any) -> Any:
    """The other branch: a reviewer says no, and nothing is published."""
    from loom.nodes.human import ReviewOut

    payload = m.BattleCardConfig(company_name="Northwind", competitor_names=[])
    facts = [
        given(m.research, returns=m.Research(subject="Northwind", summary="…")),
        given(
            "node:human.review_edit",
            returns=ReviewOut(content="", approved=False, responder="rep@example.com"),
        ),
    ]

    def check(out: Any) -> None:
        assert out.approved is False
        assert out.published_url == "", "a rejected card must not be published"

    return payload, facts, check


def _wf08(m: Any) -> Any:
    """Prep, then a transcript that is not ready on the first look."""
    from loom.nodes.agentic import ExtractStructuredOut, SummarizeOut
    from loom.nodes.human import ReviewOut

    payload = m.MeetingConfig(asana_workspace="ws-1", transcript_wait_hours=3)
    facts = [
        given(
            m.next_meeting,
            returns=m.Meeting(
                event_id="evt-1",
                title="Design sync",
                start="2026-03-01T15:00:00Z",
                attendees=["ada@example.com", "grace@example.com"],
                conference_id="conferenceRecords/abc",
            ),
        ),
        given(
            m.research_attendee,
            returns=m.Attendee(email="ada@example.com", background="Builds engines."),
            occurrence=0,
        ),
        # One lookup fails; the brief is still worth posting without it.
        given(m.research_attendee, returns=None, occurrence=1),
        given("node:agent.summarize", returns=SummarizeOut(summary="Ada builds engines.")),
        given(m.post_brief, returns="1700000000.1"),
        # Meet reports a transcript before its Drive file exists, so the first
        # look is empty and the run sleeps rather than concluding "never".
        given(m.transcript_text, returns="", occurrence=0),
        # `ctx.sleep` reads the clock through `ctx.now()` before it parks, and
        # both are journaled — a durable wait has to replay to the same wake
        # time, so the moment it was computed from is recorded too.
        given("now", kind=EntryKind.SIDE_EFFECT, returns="2026-03-01T16:00:00+00:00"),
        given("sleep", kind=EntryKind.SLEEP, returns=None),
        given(
            m.transcript_text,
            returns="Ada agreed to send the deck by Friday.",
            occurrence=1,
        ),
        given(
            "node:agent.extract_structured",
            returns=ExtractStructuredOut(
                values={
                    "items": [
                        {
                            "description": "Send the deck",
                            "assignee_email": "ada@example.com",
                            "due_on": "2026-03-06",
                        }
                    ]
                },
                parsed=True,
            ),
        ),
        given(
            "node:human.review_edit",
            returns=ReviewOut(
                content="- Send the deck", approved=True, responder="lead@example.com"
            ),
        ),
        given(m.file_task, returns="task-1"),
    ]

    def check(out: Any) -> None:
        assert out.event_id == "evt-1"
        assert out.attendees_researched == 1, "the failed lookup must not be counted"
        assert out.brief_posted is True
        assert out.transcript_found is True
        assert out.tasks_filed == 1
        assert out.tasks_declined == 0

    return payload, facts, check


def _wf08_no_meeting(m: Any) -> Any:
    """An empty calendar window is a fact, not a failure."""
    payload = m.MeetingConfig()
    facts = [given(m.next_meeting, returns=None)]

    def check(out: Any) -> None:
        assert out.event_id == ""
        assert out.brief_posted is False

    return payload, facts, check


def _stripe_event(m: Any, **overrides: Any) -> Any:
    """A settled payment as Stripe's event API returns it."""
    from loom.toolsets.stripe.models import StripeEvent

    return StripeEvent(
        id="evt_1",
        type="payment_intent.succeeded",
        data_object={
            "id": "pi_1",
            "status": "succeeded",
            "amount": 4200,
            "currency": "usd",
            "receipt_email": "ada@example.com",
            "description": "Order A-1",
            **overrides,
        },
    )


def _wf09(m: Any) -> Any:
    """A new customer, a filed receipt. The path the spec describes."""
    from loom.toolsets.pagination import Results
    from loom.toolsets.quickbooks.models import (
        QuickBooksCustomer,
        QuickBooksSalesReceipt,
    )

    payload = m.EtlConfig(event_id="evt_1")
    facts = [
        given(m.stripe_get_event, returns=_stripe_event(m)),
        given(m.to_decimal, returns=m.Money(amount=42.0, currency="USD")),
        # No receipt carries this payment id yet, so it has not been filed.
        given(m.quickbooks_find_sales_receipts, returns=Results([])),
        given(m.quickbooks_find_customer, returns=None),
        given(
            m.quickbooks_create_customer,
            returns=QuickBooksCustomer(id="qb-1", display_name="ada@example.com"),
        ),
        given(
            m.quickbooks_create_sales_receipt,
            returns=QuickBooksSalesReceipt(id="rc-1", customer_id="qb-1", total=42.0),
        ),
        given(m.notify_finance, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.payment_id == "pi_1"
        assert out.settled is True
        assert out.customer_id == "qb-1"
        assert out.customer_created is True
        assert out.receipt_id == "rc-1"
        assert out.already_filed is False
        assert out.amount == 42.0
        assert out.currency == "USD"

    return payload, facts, check


def _wf09_redelivery(m: Any) -> Any:
    """Stripe retries for three days. The second delivery must file nothing.

    QuickBooks has no idempotency key, so this is the workflow's own check —
    and it is the branch that matters most, because a duplicated sales receipt
    is an accounting problem rather than a tidy-up.
    """
    from loom.toolsets.pagination import Results
    from loom.toolsets.quickbooks.models import QuickBooksSalesReceipt

    payload = m.EtlConfig(event_id="evt_1")
    facts = [
        given(m.stripe_get_event, returns=_stripe_event(m)),
        given(m.to_decimal, returns=m.Money(amount=42.0, currency="USD")),
        given(
            m.quickbooks_find_sales_receipts,
            returns=Results(
                [QuickBooksSalesReceipt(id="rc-1", customer_id="qb-1", total=42.0)]
            ),
        ),
        given(m.notify_finance, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.already_filed is True
        assert out.receipt_id == "rc-1"
        assert out.customer_created is False, "nothing may be created twice"

    return payload, facts, check


def _wf09_unsettled(m: Any) -> Any:
    """A payment that has not settled books nothing, and is not a failure."""
    payload = m.EtlConfig(event_id="evt_1")
    facts = [
        given(m.stripe_get_event, returns=_stripe_event(m, status="processing")),
    ]

    def check(out: Any) -> None:
        assert out.settled is False
        assert out.receipt_id == ""
        assert out.amount == 0.0

    return payload, facts, check


def _wf09_refused(m: Any) -> Any:
    """An amount above the threshold, and a person says no."""
    from loom.nodes.human import ApprovalOut
    from loom.toolsets.pagination import Results
    from loom.toolsets.quickbooks.models import QuickBooksCustomer

    payload = m.EtlConfig(event_id="evt_1", approve_above=100.0)
    facts = [
        given(m.stripe_get_event, returns=_stripe_event(m, amount=900_000)),
        given(m.to_decimal, returns=m.Money(amount=9000.0, currency="USD")),
        given(m.quickbooks_find_sales_receipts, returns=Results([])),
        given(
            m.quickbooks_find_customer,
            returns=QuickBooksCustomer(id="qb-9", display_name="Ada"),
        ),
        given(
            "node:human.approval",
            returns=ApprovalOut(approved=False, responder="finance@example.com"),
        ),
        given(m.notify_finance, returns=None),
    ]

    def check(out: Any) -> None:
        assert out.approved is False
        assert out.receipt_id == "", "a refused receipt must not be filed"
        assert out.customer_id == "qb-9"

    return payload, facts, check


def _wf10_ingest(m: Any) -> Any:
    """Ingest, one answered question, then the caller says end."""
    from loom.agents.result import AgentResult
    from loom.blobs.attachment import Attachment
    from loom.knowledge import Chunk, Match
    from loom.nodes.documents import Page, ParseDocumentOut
    from loom.nodes.knowledge import ChunkOut, IndexOut, SearchOut

    payload = m.ChatbotConfig(
        file_id="drive-1", session_id="s-1", questions_per_run=5
    )
    chunk = Chunk(id="c1", text="A journal is a per-run log.", source="drive-1")
    facts = [
        given(
            m.fetch_document,
            returns=Attachment.from_bytes(
                "paper.pdf", b"%PDF-1.4 fake", mime="application/pdf"
            ),
        ),
        given(
            "node:transform.parse_document",
            returns=ParseDocumentOut(
                text="A journal is a per-run log.",
                pages=[Page(number=1, text="A journal is a per-run log.")],
                page_count=1,
                truncated=False,
                format="pdf",
                filename="paper.pdf",
            ),
        ),
        given("node:knowledge.chunk", returns=ChunkOut(chunks=[chunk], count=1)),
        given(
            "node:knowledge.index",
            returns=IndexOut(namespace="s-1", indexed=1, model="mock"),
        ),
        given(m.reply, returns="ts-0", occurrence=0),  # the "Ready" post
        given(
            "user_question",
            kind=EntryKind.EVENT,
            returns=m.ChatMessage(question="What is a journal?", asked_by="ada"),
        ),
        given(
            "node:knowledge.search",
            returns=SearchOut(
                matches=[Match(chunk=chunk, score=0.82)],
                found=1,
                dropped_below_threshold=0,
            ),
        ),
        given(
            "answer",
            kind=EntryKind.AGENT,
            returns=AgentResult(output="A per-run log of durable operations."),
        ),
        given(m.reply, returns="ts-1", occurrence=1),
        given("user_question", kind=EntryKind.EVENT, returns=m.ChatMessage(question="end")),
    ]

    def check(out: Any) -> None:
        assert out.session_id == "s-1"
        assert out.chunks_indexed == 1
        assert out.questions_answered == 1
        assert out.unanswered == 0
        assert out.ended_by == "end"

    return payload, facts, check


def _wf10_nothing_found(m: Any) -> Any:
    """Every match below the threshold. The workflow says so.

    The branch this whole subsystem exists for: a search always returns
    something, and an unthresholded answer would be fluent and built from rows
    the score already called irrelevant.
    """
    from loom.nodes.knowledge import SearchOut

    payload = m.ChatbotConfig(
        file_id="drive-1", session_id="s-1", questions_per_run=5, indexed=12
    )
    facts = [
        # `indexed` is non-zero, so the ingest branch is skipped entirely —
        # which is what a successor run after `continue_as_new` looks like.
        given(
            "user_question",
            kind=EntryKind.EVENT,
            returns=m.ChatMessage(question="What is the share price?"),
        ),
        given(
            "node:knowledge.search",
            returns=SearchOut(matches=[], found=0, dropped_below_threshold=3),
        ),
        given(m.reply, returns="ts-1"),
        given("user_question", kind=EntryKind.EVENT, returns=m.ChatMessage(question="end")),
    ]

    def check(out: Any) -> None:
        assert out.chunks_indexed == 12, "a successor must not re-index"
        assert out.questions_answered == 0
        assert out.unanswered == 1
        assert out.ended_by == "end"

    return payload, facts, check


def _wf10_timeout(m: Any) -> Any:
    """Nobody asks anything. Not a failure."""
    payload = m.ChatbotConfig(file_id="drive-1", session_id="s-1", indexed=4)
    facts = [given("user_question", kind=EntryKind.EVENT, returns=None)]

    def check(out: Any) -> None:
        assert out.ended_by == "timeout"
        assert out.questions_answered == 0

    return payload, facts, check


CASES = [
    Case("wf01_lead_outreach", _wf01),
    Case("wf02_content_pipeline", _wf02),
    Case("wf02_content_pipeline", _wf02_rejected, variant="rejected"),
    Case("wf02_content_pipeline", _wf02_partial_failure, variant="partial"),
    Case("wf03_inbox_triage", _wf03),
    Case("wf04_crm_sync", _wf04),
    Case("wf04_crm_sync", _wf04_refused, variant="refused"),
    Case("wf05_social_publisher", _wf05),
    Case("wf05_social_publisher", _wf05_all_said_before, variant="all-said"),
    Case("wf05_social_publisher", _wf05_refused, variant="refused"),
    Case("wf06_doc_extraction", _wf06),
    Case("wf07_battle_cards", _wf07),
    Case("wf07_battle_cards", _wf07_rejected, variant="rejected"),
    Case("wf08_meeting_prep", _wf08),
    Case("wf08_meeting_prep", _wf08_no_meeting, variant="empty-calendar"),
    Case("wf09_stripe_etl", _wf09),
    Case("wf09_stripe_etl", _wf09_redelivery, variant="redelivery"),
    Case("wf09_stripe_etl", _wf09_unsettled, variant="unsettled"),
    Case("wf09_stripe_etl", _wf09_refused, variant="refused"),
    Case("wf10_pdf_chatbot", _wf10_ingest),
    Case("wf10_pdf_chatbot", _wf10_nothing_found, variant="below-threshold"),
    Case("wf10_pdf_chatbot", _wf10_timeout, variant="timeout"),
]


# ---------------------------------------------------------------------------
# The suite has to cover every file, or it is a sample
# ---------------------------------------------------------------------------


def test_all_10_workflow_files_exist() -> None:
    assert len(WORKFLOW_FILES) == 10, (
        f"Expected 10 workflow files, found {len(WORKFLOW_FILES)}: "
        f"{[f.name for f in WORKFLOW_FILES]}"
    )


def test_every_workflow_has_a_case() -> None:
    """A workflow with no case is a workflow nothing runs.

    This is the check that would have caught the old suite: it grew a tenth
    file and reported on it without ever executing it.
    """
    assert {c.stem for c in CASES} == {f.stem for f in WORKFLOW_FILES}


# ---------------------------------------------------------------------------
# Import — the check the substring suite could not make
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wf_file", WORKFLOW_FILES, ids=[f.stem for f in WORKFLOW_FILES])
def test_workflow_imports(wf_file: Path) -> None:
    """Importing is what a wrong keyword argument fails.

    ``Retry(delay=2.0)`` compiles, lints, and passes any grep for ``Retry(``.
    It raises ``TypeError`` here.
    """
    module = _module(wf_file)
    assert _workflow_of(module).name


@pytest.mark.parametrize("wf_file", WORKFLOW_FILES, ids=[f.stem for f in WORKFLOW_FILES])
def test_workflow_has_docstring(wf_file: Path) -> None:
    tree = ast.parse(wf_file.read_text())
    assert ast.get_docstring(tree), f"{wf_file.name} missing module docstring"


# ---------------------------------------------------------------------------
# Execution and replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_workflow_runs(case: Case) -> None:
    """Run the workflow over seeded facts and check what it produced."""
    module = _module(_REF_DIR / f"{case.stem}.py")
    workflow = _workflow_of(module)
    payload, facts, check = case.build(module)

    result = await run_with(workflow, payload, *facts)

    assert result.status.value == "completed", (
        f"{workflow.name} did not complete: {result.error}"
    )
    check(result.output)


@pytest.mark.asyncio()
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_workflow_replays(case: Case) -> None:
    """A second pass over the same journal must produce the same answer.

    Anything reading a clock, a random source, or unjournaled state directly
    diverges here — which is the failure a crash-resumed run shows as doing
    something the first attempt did not.
    """
    module = _module(_REF_DIR / f"{case.stem}.py")
    workflow = _workflow_of(module)
    payload, facts, _ = case.build(module)

    from loom.runtime.engine import Runtime
    from loom.stores.memory import MemoryStore

    runtime = Runtime(store=MemoryStore())
    await run_with(workflow, payload, *facts, runtime=runtime)
    await assert_replays(workflow, payload, runtime=runtime)


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def test_all_10_spec_files_exist() -> None:
    assert len(SPEC_FILES) == 10, f"Expected 10 spec files, found {len(SPEC_FILES)}"


@pytest.mark.parametrize("wf_file", WORKFLOW_FILES, ids=[f.stem for f in WORKFLOW_FILES])
def test_spec_exists_for_workflow(wf_file: Path) -> None:
    spec_path = _SPEC_DIR / f"{wf_file.stem.split('_')[0]}_spec.txt"
    assert spec_path.exists(), f"No spec file for {wf_file.name}: want {spec_path.name}"


@pytest.mark.parametrize("spec_file", SPEC_FILES, ids=[f.stem for f in SPEC_FILES])
def test_spec_not_empty(spec_file: Path) -> None:
    assert len(spec_file.read_text().strip()) > 50, f"{spec_file.name} is too short"
