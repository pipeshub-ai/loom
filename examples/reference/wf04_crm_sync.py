"""Workflow: CRM Sync — Salesforce to HubSpot.

Nightly: take the contacts Salesforce has marked ready, upsert each into
HubSpot, mark them synced back in Salesforce, and report what happened.

The original was Airtable → HubSpot. LOOM has no Airtable toolset, and this
version keeps every property that workflow demonstrated — batched upsert,
continue-on-error, write-back, notify — against two CRMs that are actually
shipped. The Airtable variant returns when that toolset does; nothing in the
shape below changes when it does.

What this shows, and why each part is the way it is:

* **Resolve before you write.** Every HubSpot write takes an id. A contact is
  found by email first; passing a name where an id belongs matches nothing and
  reports **no error**, which reads as "already up to date".

* **One record failing does not fail the run.** ``on_error=OnError.CONTINUE``
  with an explicit ``fallback`` — the fallback is what makes "it failed"
  distinguishable from "it returned a falsy value", which is the bug the old
  version was one refactor away from.

* **Write back only what actually synced.** The Salesforce update runs per
  record, after its own upsert succeeded. A batch write-back after the loop
  would mark records synced that were not.

* **Coverage is carried.** ``salesforce_query`` returns ``Results``, so
  ``.complete`` says whether the whole ready set was read. A run that synced
  one page and reported "all done" is the failure this exists to prevent.

* **A person is asked above a threshold.** A nightly sync of six records needs
  nobody. A sync of six hundred is either a genuine backlog or a bad query, and
  the difference is worth one approval — ``human.approval`` parks the run at no
  cost until somebody says.

Credentials: ``SALESFORCE_*``, ``HUBSPOT_ACCESS_TOKEN``, ``SLACK_BOT_TOKEN``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from loom import Context, OnError, Retry, step, workflow
from loom.nodes.control import BatchIn
from loom.nodes.human import ApprovalIn
from loom.security.grants import GrantSet
from loom.toolsets.hubspot.tools import (
    hubspot_create_contact,
    hubspot_get_contact_by_email,
    hubspot_update_object,
)
from loom.toolsets.salesforce.tools import salesforce_query, salesforce_update_record
from loom.toolsets.slack.tools import slack_post_message
from loom.triggers import Schedule

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class SyncConfig(BaseModel):
    """What to sync, and when to ask a person first."""

    ready_field: str = "Sync_To_HubSpot__c"
    """The Salesforce checkbox that marks a contact ready. A custom field, so
    it ends in ``__c`` — Salesforce rejects the name without it."""

    batch_size: int = 200
    slack_channel: str = "#crm-sync"
    approve_above: int = 100
    """Ask a person before syncing more than this many records in one run.

    Not a safety rail against the CRM — it is a check on the *query*. A sudden
    ten-fold jump is more often a filter that stopped filtering than a real
    backlog."""


class ContactRecord(BaseModel):
    """One Salesforce contact, ready to sync."""

    record_id: str
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""


class SyncResult(BaseModel):
    """Summary returned by the workflow."""

    records_read: int = 0
    complete: bool = True
    """Whether the whole ready set was read, or the page cap cut it short."""

    created: int = 0
    updated: int = 0
    failed: int = 0
    approved: bool = True
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def read_ready(ready_field: str, limit: int) -> dict[str, object]:
    """Contacts Salesforce has flagged, plus whether that is all of them.

    The literal is not interpolated from user input — ``ready_field`` names a
    column, and SOQL string literals elsewhere in this codebase are escaped for
    exactly the reason ``O'Brien`` is the most predictable surname in any CRM.
    """
    found = await salesforce_query(
        soql=(
            "SELECT Id, Email, FirstName, LastName, Account.Name FROM Contact "
            f"WHERE {ready_field} = true AND Email != null"
        ),
        limit=limit,
    )
    return {
        "records": [
            {
                "record_id": row.id,
                "email": row.fields.get("Email", ""),
                "first_name": row.fields.get("FirstName", "") or "",
                "last_name": row.fields.get("LastName", "") or "",
                "company": (row.fields.get("Account") or {}).get("Name", "") or "",
            }
            for row in found
        ],
        "complete": found.complete,
    }


@step(retry=Retry(max_attempts=2, initial_delay=0.5), on_error=OnError.CONTINUE, fallback=None)
async def upsert(record: ContactRecord) -> dict[str, str] | None:
    """Create or update the HubSpot contact. ``None`` means it did not sync.

    ``fallback=None`` is load-bearing. Without it a failed step returns ``None``
    anyway — but by accident, and a step whose success value is ``0``, ``""`` or
    ``[]`` under the same pattern would count a success as a failure. Saying it
    is how the next reader knows which was meant.

    Resolution comes first because a HubSpot write takes an id.
    """
    existing = await hubspot_get_contact_by_email(email=record.email)
    properties = {
        "email": record.email,
        "firstname": record.first_name,
        "lastname": record.last_name,
        "company": record.company,
    }
    if existing:
        await hubspot_update_object(
            object_type="contacts", object_id=existing.id, properties=properties
        )
        return {"id": existing.id, "action": "updated"}
    created = await hubspot_create_contact(properties=properties)
    return {"id": created.id, "action": "created"}


@step(retry=Retry(max_attempts=2, initial_delay=0.5), on_error=OnError.CONTINUE, fallback=False)
async def mark_synced(record_id: str, ready_field: str) -> bool:
    """Clear the ready flag, so the next run does not re-read this contact.

    Per record and after its own upsert, not batched at the end: a batch
    write-back marks records synced that were not.
    """
    await salesforce_update_record(
        sobject="Contact", record_id=record_id, values={ready_field: False}
    )
    return True


@step(retry=Retry(max_attempts=1))
async def report(channel: str, result: SyncResult) -> None:
    """Post the summary. Not retried — a retry posts it twice."""
    coverage = "" if result.complete else " (partial read — more are waiting)"
    await slack_post_message(
        channel=channel,
        text=(
            f"CRM sync: {result.created} created, {result.updated} updated, "
            f"{result.failed} failed of {result.records_read}{coverage}"
        ),
    )


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(
    name="crm_sync",
    version="2",
    triggers=[Schedule(cron="0 2 * * *", timezone="UTC")],
    grants=GrantSet(toolsets=["salesforce", "hubspot", "slack"]),
)
async def crm_sync(ctx: Context, config: SyncConfig) -> SyncResult:
    """Read the ready set, upsert each, write back, and report."""
    read = await ctx.step(read_ready, config.ready_field, config.batch_size)
    rows = list(read["records"])  # type: ignore[arg-type]
    complete = bool(read["complete"])

    if not rows:
        return SyncResult(records_read=0, complete=complete)

    records = [ContactRecord(**row) for row in rows]

    # A big jump is more often a broken filter than a real backlog, and the
    # run costs nothing while it waits to be told which.
    if len(records) > config.approve_above:
        approval = await ctx.node(
            "human.approval",
            ApprovalIn(
                subject="crm-sync-volume",
                prompt=(
                    f"{len(records)} contacts are flagged ready — "
                    f"more than the usual {config.approve_above}. Sync them all?"
                ),
            ),
        )
        if not approval.approved:
            result = SyncResult(
                records_read=len(records), complete=complete, approved=False
            )
            await ctx.step(report, config.slack_channel, result)
            return result

    created = 0
    updated = 0
    failed = 0
    errors: list[str] = []

    # Batched so the loop is readable at 200 records and does not hold every
    # intermediate in one comprehension. `control.batch` is a rule, not
    # judgement, so it is a node rather than an agent.
    batches = await ctx.node(
        "control.batch", BatchIn(items=[r.model_dump() for r in records], size=25)
    )

    for group in batches.batches:
        for row in group:
            record = ContactRecord(**row)
            outcome = await ctx.step(upsert, record)
            if outcome is None:
                failed += 1
                errors.append(f"{record.email}: upsert failed")
                continue
            if outcome["action"] == "created":
                created += 1
            else:
                updated += 1
            await ctx.step(mark_synced, record.record_id, config.ready_field)

    result = SyncResult(
        records_read=len(records),
        complete=complete,
        created=created,
        updated=updated,
        failed=failed,
        errors=errors,
    )
    await ctx.step(report, config.slack_channel, result)
    return result
