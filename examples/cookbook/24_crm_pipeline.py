"""Example 24 — A CRM workflow across Salesforce and HubSpot.

The shape almost every CRM automation has: resolve a company by *name*, read
what is open against it, and write a follow-up. The interesting part is the
resolution step — every write in either CRM takes an id, and a name passed
where an id belongs matches nothing and reports **no error**.

Runs with no credentials: the toolset calls are wrapped so the example shows
the shape and the reasoning rather than requiring two CRM accounts.

Run:
    python3 examples/cookbook/24_crm_pipeline.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import header, log

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@step
async def resolve_account(query: str) -> dict:
    """Turn a company name into the id every write needs.

    In a credentialed run this is::

        from loom.toolsets.salesforce.tools import salesforce_find_accounts
        found = await ctx.step(salesforce_find_accounts, query=query, limit=5)
    """
    return {"id": "001xx000003DGb2AAG", "name": query, "industry": "Software"}


@step
async def open_opportunities(account_id: str) -> list[dict]:
    """Open deals for that account.

    Credentialed::

        from loom.toolsets.salesforce.tools import salesforce_find_opportunities
        await ctx.step(salesforce_find_opportunities,
                       account_id=account_id, open_only=True)
    """
    return [
        {"name": "ACME renewal", "stage": "Negotiation", "amount": 50000.0},
        {"name": "ACME expansion", "stage": "Discovery", "amount": 18000.0},
    ]


@step
async def hubspot_owner_for(email: str) -> dict:
    """Resolve a person to the owner id HubSpot assignments take.

    Credentialed::

        from loom.toolsets.hubspot.tools import hubspot_list_owners
        owners = await ctx.step(hubspot_list_owners, email=email)
    """
    return {"id": "512", "email": email, "full_name": "Ada Lovelace"}


@step
async def log_followup(owner_id: str, subject: str, amount: float) -> str:
    """Write the follow-up.

    Credentialed, and deliberately *not* retried — neither CRM has an
    idempotency key, so a retry after a timeout files a second deal::

        from loom.toolsets.hubspot.tools import hubspot_create_deal
        await ctx.step(hubspot_create_deal, properties={...})
    """
    return f"deal:{owner_id}:{subject}:{amount:.0f}"


@workflow(name="crm_pipeline")
async def crm_pipeline(ctx: Context, payload: dict) -> dict:
    """Resolve → read → write, with the resolution first."""
    account = await ctx.step(resolve_account, query=payload["company"])
    deals = await ctx.step(open_opportunities, account_id=account["id"])
    owner = await ctx.step(hubspot_owner_for, email=payload["owner_email"])

    total = sum(deal["amount"] for deal in deals)
    reference = await ctx.step(
        log_followup,
        owner_id=owner["id"],
        subject=f"{account['name']} pipeline review",
        amount=total,
    )
    return {
        "account": account["name"],
        "account_id": account["id"],
        "open_deals": len(deals),
        "pipeline_value": total,
        "owner": owner["full_name"],
        "reference": reference,
    }


async def main() -> None:
    header("CRM PIPELINE")
    runtime = Runtime(store=MemoryStore())
    runtime.register(crm_pipeline)

    result = await runtime.run(
        crm_pipeline, {"company": "ACME", "owner_email": "ada@example.com"}
    )
    out = result.output
    log("account", f"{out['account']} ({out['account_id']})")
    log("pipeline", f"{out['open_deals']} open deal(s), {out['pipeline_value']:.0f}")
    log("owner", out["owner"])
    log("wrote", out["reference"])

    header("WHY RESOLVE FIRST")
    log("note", "Every CRM write takes an id, never a name.")
    log("note", "A name passed where an id belongs matches nothing —")
    log("note", "and returns zero rows with no error, which reads as")
    log("note", "'nothing to do' rather than as a bug.")
    log("note", "loom toolset salesforce   # shows which op resolves what")


if __name__ == "__main__":
    asyncio.run(main())
