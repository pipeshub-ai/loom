"""Typed shapes for the Salesforce REST API.

Salesforce wraps every record in an ``attributes`` envelope carrying its type
and REST path, and returns field names in the org's own casing (``Name``,
``StageName``, ``Account.Name``). Both are unwrapped here so a workflow reasons
about ``opportunity.amount`` rather than about an envelope.

The generic :class:`SalesforceRecord` exists because Salesforce's shape is
identical for every object including custom ones — an org's ``Deal__c`` is
reachable without a library change. The typed models are conveniences over the
five objects a CRM workflow touches daily.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _relation(raw: dict[str, Any], relation: str, field: str) -> str:
    """A field from a related object, which Salesforce nests or omits.

    ``SELECT Account.Name`` returns ``{"Account": {"Name": …}}`` — or
    ``{"Account": null}`` when the lookup is empty, which is a different thing
    from the key being absent and would be an AttributeError either way.
    """
    nested = raw.get(relation)
    return (nested or {}).get(field) or "" if isinstance(nested, dict | type(None)) else ""


class SalesforceRecord(BaseModel):
    """Any sObject, with the envelope unwrapped and the rest left as-is."""

    id: str = ""
    type: str = ""
    url: str = ""
    """The record's own REST path, straight from ``attributes.url``."""
    fields: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SalesforceRecord:
        attributes = raw.get("attributes") or {}
        return cls(
            id=str(raw.get("Id", "") or ""),
            type=attributes.get("type") or "",
            url=attributes.get("url") or "",
            fields={k: v for k, v in raw.items() if k != "attributes"},
        )


class SalesforceAccount(BaseModel):
    """A company."""

    id: str = ""
    name: str = ""
    industry: str = ""
    website: str = ""
    phone: str = ""
    owner: str = ""
    created_date: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SalesforceAccount:
        return cls(
            id=str(raw.get("Id", "") or ""),
            name=raw.get("Name") or "",
            industry=raw.get("Industry") or "",
            website=raw.get("Website") or "",
            phone=raw.get("Phone") or "",
            owner=_relation(raw, "Owner", "Name"),
            created_date=raw.get("CreatedDate") or "",
        )


class SalesforceContact(BaseModel):
    """A person at an account."""

    id: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    title: str = ""
    account_id: str = ""
    account_name: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SalesforceContact:
        return cls(
            id=str(raw.get("Id", "") or ""),
            name=raw.get("Name") or "",
            email=raw.get("Email") or "",
            phone=raw.get("Phone") or "",
            title=raw.get("Title") or "",
            account_id=str(raw.get("AccountId", "") or ""),
            account_name=_relation(raw, "Account", "Name"),
        )


class SalesforceOpportunity(BaseModel):
    """A deal."""

    id: str = ""
    name: str = ""
    stage: str = ""
    amount: float = 0.0
    close_date: str = ""
    account_id: str = ""
    account_name: str = ""
    owner: str = ""
    is_closed: bool = False
    is_won: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SalesforceOpportunity:
        return cls(
            id=str(raw.get("Id", "") or ""),
            name=raw.get("Name") or "",
            # StageName, not Stage. Getting it wrong yields an empty string
            # rather than an error, so it is worth naming here.
            stage=raw.get("StageName") or "",
            amount=float(raw.get("Amount") or 0),
            close_date=raw.get("CloseDate") or "",
            account_id=str(raw.get("AccountId", "") or ""),
            account_name=_relation(raw, "Account", "Name"),
            owner=_relation(raw, "Owner", "Name"),
            is_closed=bool(raw.get("IsClosed", False)),
            is_won=bool(raw.get("IsWon", False)),
        )


class SalesforceUser(BaseModel):
    """A Salesforce user — the target of every owner assignment."""

    id: str = ""
    name: str = ""
    email: str = ""
    username: str = ""
    is_active: bool = True

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SalesforceUser:
        return cls(
            id=str(raw.get("Id", "") or ""),
            name=raw.get("Name") or "",
            email=raw.get("Email") or "",
            username=raw.get("Username") or "",
            is_active=bool(raw.get("IsActive", True)),
        )


class SalesforceWriteResult(BaseModel):
    """What a create returns, and what an update or delete is reported as.

    Salesforce answers a create with ``{id, success, errors}`` and an update or
    delete with 204 and no body at all — so the same model is filled in by the
    client for the latter, rather than leaving a caller to tell an empty
    response from a failed one.
    """

    id: str = ""
    success: bool = False
    errors: list[str] = Field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SalesforceWriteResult:
        return cls(
            id=str(raw.get("id", "") or ""),
            success=bool(raw.get("success", False)),
            errors=[str(e) for e in (raw.get("errors") or [])],
        )
