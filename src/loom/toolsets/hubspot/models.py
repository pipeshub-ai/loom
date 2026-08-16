"""Typed shapes for the HubSpot CRM API v3.

HubSpot puts every field inside a ``properties`` bag of strings — an amount is
``"50000"``, a boolean is ``"true"`` — so the typed models coerce as they
flatten. A workflow comparing ``deal.amount > 10000`` should not have to know
that the CRM sends numbers as text.

:class:`HubSpotObject` keeps the bag intact for object types this module does
not model, which is most of them: HubSpot's shape is identical across contacts,
companies, deals, tickets, and any custom object an org defines.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


def _number(value: Any) -> float:
    """A HubSpot numeric property, which arrives as a string or as null."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _flag(value: Any) -> bool:
    """A HubSpot boolean property, which arrives as ``"true"`` or ``"false"``."""
    return str(value).lower() == "true"


class HubSpotObject(BaseModel):
    """Any CRM object, with its property bag left intact."""

    id: str = ""
    object_type: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    archived: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any], object_type: str = "") -> HubSpotObject:
        return cls(
            id=str(raw.get("id", "") or ""),
            object_type=object_type,
            properties=raw.get("properties") or {},
            created_at=raw.get("createdAt") or "",
            updated_at=raw.get("updatedAt") or "",
            archived=bool(raw.get("archived", False)),
        )


class HubSpotContact(BaseModel):
    """A person."""

    id: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    phone: str = ""
    lifecycle_stage: str = ""
    owner_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HubSpotContact:
        p = raw.get("properties") or {}
        first, last = p.get("firstname") or "", p.get("lastname") or ""
        return cls(
            id=str(raw.get("id", "") or ""),
            email=p.get("email") or "",
            first_name=first,
            last_name=last,
            # Composed here so a caller has one field to show a person, rather
            # than joining two that are each often empty.
            full_name=" ".join(part for part in (first, last) if part),
            company=p.get("company") or "",
            phone=p.get("phone") or "",
            lifecycle_stage=p.get("lifecyclestage") or "",
            owner_id=str(p.get("hubspot_owner_id") or ""),
            created_at=raw.get("createdAt") or "",
            updated_at=raw.get("updatedAt") or "",
        )


class HubSpotCompany(BaseModel):
    """A company."""

    id: str = ""
    name: str = ""
    domain: str = ""
    industry: str = ""
    city: str = ""
    country: str = ""
    owner_id: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HubSpotCompany:
        p = raw.get("properties") or {}
        return cls(
            id=str(raw.get("id", "") or ""),
            name=p.get("name") or "",
            domain=p.get("domain") or "",
            industry=p.get("industry") or "",
            city=p.get("city") or "",
            country=p.get("country") or "",
            owner_id=str(p.get("hubspot_owner_id") or ""),
        )


class HubSpotDeal(BaseModel):
    """A deal."""

    id: str = ""
    name: str = ""
    stage: str = ""
    pipeline: str = ""
    amount: float = 0.0
    close_date: str = ""
    owner_id: str = ""
    is_closed: bool = False

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HubSpotDeal:
        p = raw.get("properties") or {}
        return cls(
            id=str(raw.get("id", "") or ""),
            name=p.get("dealname") or "",
            stage=p.get("dealstage") or "",
            pipeline=p.get("pipeline") or "",
            amount=_number(p.get("amount")),
            close_date=p.get("closedate") or "",
            owner_id=str(p.get("hubspot_owner_id") or ""),
            is_closed=_flag(p.get("hs_is_closed")),
        )


class HubSpotOwner(BaseModel):
    """A HubSpot owner — the target of every assignment."""

    id: str = ""
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HubSpotOwner:
        first, last = raw.get("firstName") or "", raw.get("lastName") or ""
        return cls(
            id=str(raw.get("id", "") or ""),
            email=raw.get("email") or "",
            first_name=first,
            last_name=last,
            full_name=" ".join(part for part in (first, last) if part),
        )


class HubSpotAccount(BaseModel):
    """The account a token belongs to.

    HubSpot's answer to "whoami", and deliberately not called that: a private
    app token authenticates an *app against a portal*, not a person, so there
    is no user to report. The portal id is what identifies it.
    """

    portal_id: str = ""
    account_type: str = ""
    time_zone: str = ""
    currency: str = ""
    data_hosting_location: str = ""

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> HubSpotAccount:
        return cls(
            portal_id=str(raw.get("portalId", "") or ""),
            account_type=raw.get("accountType") or "",
            time_zone=raw.get("timeZone") or "",
            currency=raw.get("companyCurrency") or "",
            data_hosting_location=raw.get("dataHostingLocation") or "",
        )
