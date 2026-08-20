"""Who actually answered a human request.

``ApprovalOut.responder`` was read straight out of the answer payload, so the
audit record for a human decision said whatever the payload said. Nothing
stamped the *authenticated* caller onto it — ``AuthorizedFacade`` knew the
principal, checked that it owned the run, and then passed the body through
unchanged; ``runtime.approve()`` set no responder at all.

That matters more here than it would elsewhere. An approval is the act that
clears read-to-write taint, so it is the point at which a run regains
permission to write after reading something nobody reviewed. An unattested
record of who granted that is the wrong shape for the one decision most likely
to be asked about later.

The attested value travels under a reserved key so it cannot collide with, or
be forged by, a field the channel already uses: a payload may set
``responder``, but only the boundary that authenticated the request may set
:data:`ATTESTED_KEY`, and the boundary overwrites rather than merges.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ATTESTED_KEY", "attest", "responder_of"]

ATTESTED_KEY = "loom.responder"
"""Reserved payload key holding the authenticated principal's subject.

Namespaced like ``loom.principal`` and ``loom.middleware`` on
``ExecutionRecord.metadata``, and for the same reason: a reserved key is one a
caller cannot mean by accident.
"""


def attest(payload: Any, subject: str) -> Any:
    """Stamp *subject* onto an answer payload as the attested responder.

    Non-mapping payloads are wrapped rather than dropped — a channel that sends
    a bare ``True`` for an approval is a documented shape, and losing the
    identity because the answer was terse would defeat the point.
    """
    if not subject:
        return payload
    if isinstance(payload, dict):
        return {**payload, ATTESTED_KEY: subject}
    return {"answer": payload, ATTESTED_KEY: subject}


def responder_of(answer: Any, *, fallback: str = "") -> str:
    """The responder to record, preferring the attested identity.

    A self-declared ``responder`` is kept when nothing authenticated the
    request — an unauthenticated deployment is unchanged by any of this — but
    it never overrides an attested one. Where the two disagree the attested
    value wins and the claim is preserved beside it by the caller, so a
    mismatch is visible rather than resolved silently.
    """
    if not isinstance(answer, dict):
        return fallback
    attested = answer.get(ATTESTED_KEY)
    if attested:
        return str(attested)
    claimed = answer.get("responder")
    return str(claimed) if claimed else fallback


def claimed_responder(answer: Any) -> str:
    """What the payload said, regardless of what was attested."""
    if not isinstance(answer, dict):
        return ""
    claimed = answer.get("responder")
    return str(claimed) if claimed else ""
