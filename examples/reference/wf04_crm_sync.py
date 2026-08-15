"""Workflow: Airtable to CRM Sync."""

from __future__ import annotations

from pydantic import BaseModel

from loom import Context, OnError, Retry, step, workflow

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class SyncConfig(BaseModel):
    """Input configuration for the CRM sync workflow."""

    airtable_base: str
    airtable_table: str = "Contacts"
    crm_endpoint: str = "https://api.crm.example/v1"
    slack_webhook: str = ""
    batch_size: int = 100


class AirtableRecord(BaseModel):
    """A record fetched from Airtable."""

    record_id: str
    name: str
    email: str
    company: str
    status: str = "new"
    fields: dict[str, object] | None = None


class SyncResult(BaseModel):
    """Summary returned by the workflow."""

    records_synced: int
    records_failed: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def fetch_records(
    base_id: str,
    table: str,
    batch_size: int,
) -> list[AirtableRecord]:
    """Fetch records from Airtable that need syncing."""
    import httpx

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://api.airtable.com/v0/{base_id}/{table}",
            params={
                "filterByFormula": "{Synced}=FALSE()",
                "maxRecords": batch_size,
            },
            headers={
                "Authorization": "Bearer placeholder_token",
            },
        )
        resp.raise_for_status()
        rows = resp.json().get("records", [])

    return [
        AirtableRecord(
            record_id=r["id"],
            name=r["fields"].get("Name", ""),
            email=r["fields"].get("Email", ""),
            company=r["fields"].get("Company", ""),
            status=r["fields"].get("Status", "new"),
            fields=r.get("fields"),
        )
        for r in rows
    ]


@step(
    retry=Retry(
        max_attempts=3,
        initial_delay=1.0,
        max_delay=10.0,
    ),
    on_error=OnError.CONTINUE,
)
async def upsert_to_crm(
    record: AirtableRecord,
    crm_endpoint: str,
) -> bool:
    """Upsert a single record into the CRM system."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{crm_endpoint}/contacts",
            json={
                "external_id": record.record_id,
                "name": record.name,
                "email": record.email,
                "company": record.company,
                "status": record.status,
            },
        )
        resp.raise_for_status()

    return True


@step(retry=Retry(max_attempts=2))
async def mark_synced(
    base_id: str,
    table: str,
    record_ids: list[str],
) -> int:
    """Mark records as synced in Airtable."""
    import httpx

    marked = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for rid in record_ids:
            resp = await client.patch(
                f"https://api.airtable.com/v0/{base_id}/{table}/{rid}",
                json={"fields": {"Synced": True}},
                headers={
                    "Authorization": "Bearer placeholder_token",
                },
            )
            if resp.is_success:
                marked += 1

    return marked


@step(retry=Retry(max_attempts=2))
async def notify_slack(
    webhook_url: str,
    result: SyncResult,
) -> None:
    """Post a sync summary to Slack."""
    import httpx

    if not webhook_url:
        return

    text = (
        f"CRM Sync complete: {result.records_synced} synced, "
        f"{result.records_failed} failed"
    )
    if result.errors:
        text += f"\nErrors: {', '.join(result.errors[:5])}"

    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(webhook_url, json={"text": text})


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow(name="crm_sync", version="1")
async def crm_sync(
    ctx: Context,
    config: SyncConfig,
) -> SyncResult:
    """Fetch Airtable records, upsert to CRM, mark synced."""
    records = await ctx.step(
        fetch_records,
        config.airtable_base,
        config.airtable_table,
        config.batch_size,
    )

    if not records:
        return SyncResult(
            records_synced=0,
            records_failed=0,
            errors=[],
        )

    # Upsert each record; on_error=CONTINUE lets us collect failures
    synced_ids: list[str] = []
    errors: list[str] = []

    for record in records:
        ok = await ctx.step(
            upsert_to_crm, record, config.crm_endpoint,
        )
        if ok:
            synced_ids.append(record.record_id)
        else:
            errors.append(f"Failed to sync: {record.email}")

    # Mark successfully synced records in Airtable
    if synced_ids:
        await ctx.step(
            mark_synced,
            config.airtable_base,
            config.airtable_table,
            synced_ids,
        )

    result = SyncResult(
        records_synced=len(synced_ids),
        records_failed=len(errors),
        errors=errors,
    )

    # Notify team
    await ctx.step(notify_slack, config.slack_webhook, result)

    return result
