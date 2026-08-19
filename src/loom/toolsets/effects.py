"""What one operation does to the world, resolved once and checkable in CI.

`EffectClass` answers *how much damage* — an ordinal scale the grant syntax,
hook filters and taint policy all key on. It cannot answer two other questions
that decide whether an agent should be allowed to proceed: **whether the effect
reaches outside the deployment**, and **whether anything undoes it**. Gmail is
the standing example — `gmail_trash_message` is DESTRUCTIVE and recoverable for
thirty days, `gmail_send_message` is WRITE and recoverable by nothing — so a
policy that blocks DESTRUCTIVE and permits WRITE stops the reversible operation
and allows the irreversible one.

:class:`EffectProfile` carries the ordinal class *and* those facets as one
frozen value. Consumers depend on this type rather than on six loose fields, so
adding a seventh facet later touches :func:`derive_effect_profile` and nothing
else.

**Nothing consumes this yet.** It is added ahead of its consumers so the shape
can be reviewed before taint, grants, hooks and the MCP projection depend on
it. See ``phases/phase-12-effect-classification.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from loom.toolsets.manifest import EffectClass, OperationSpec

__all__ = [
    "EffectProfile",
    "Source",
    "derive_effect_profile",
    "resolve_effect",
    "scope_is_readonly",
    "verb_disagreement",
]

Source = Literal["declared", "derived", "default"]
"""Where an :attr:`EffectProfile.effect` came from.

``declared`` was asserted by whoever wrote the manifest; ``derived`` was
computed from the client or the scopes; ``default`` is the fail-safe backstop.
The distinction is not bookkeeping — a third-party manifest registered through
a ``loom_toolset`` entry point can assert anything, and telling a computed
classification from a claimed one is what lets a deployment require the former.
"""

#: HTTP verb → what it implies. Measured against the 320 operations LOOM ships:
#: where a tool function resolves to a single-verb client method, the verb
#: implies the declared class in 89 of 91 cases. Both exceptions are the same
#: shape — a search issued as a POST — which is why a disagreement is *reported*
#: rather than applied.
VERB_EFFECT: dict[str, EffectClass] = {
    "GET": EffectClass.READ,
    "HEAD": EffectClass.READ,
    "OPTIONS": EffectClass.READ,
    "POST": EffectClass.WRITE,
    "PUT": EffectClass.WRITE,
    "PATCH": EffectClass.WRITE,
    "DELETE": EffectClass.DESTRUCTIVE,
}

#: Scope fragments that mean read-only. Not a heuristic in the way a verb list
#: is: across the shipped operations that declare scopes, every one carrying
#: such a fragment is declared READ.
#:
#: Matched by :func:`scope_is_readonly`, never by a bare substring test —
#: ``.read`` is a prefix of ``.readwrite``, so ``Sites.ReadWrite.All`` matches
#: it and a write scope reads as read-only. That is not hypothetical: it fired
#: the moment real Graph scopes were added to OneDrive and SharePoint.
READONLY_SCOPE_FRAGMENTS: tuple[str, ...] = (
    "readonly", ".read", "/read", "read.all", "_read", ":read",
)

#: Any of these anywhere in a scope string disqualifies it from being read-only,
#: whatever else it matches.
WRITE_SCOPE_MARKERS: tuple[str, ...] = ("write", "manage", "send", "full", "admin")


def scope_is_readonly(scopes: list[str] | tuple[str, ...]) -> bool:
    """Do these scopes grant reading and nothing else?

    Every scope must look read-only. One broad scope alongside a narrow one
    grants what the broad one grants — ``["Files.Read", "Files.ReadWrite"]`` is
    write access, and answering ``True`` there would classify a write as a read.
    """
    if not scopes:
        return False
    return all(
        any(fragment in (s := scope.lower()) for fragment in READONLY_SCOPE_FRAGMENTS)
        and not any(marker in s for marker in WRITE_SCOPE_MARKERS)
        for scope in scopes
    )

#: Deliberately absent: a scope-based derivation for
#: :attr:`EffectProfile.access_control`.
#:
#: An earlier draft of this module claimed one, on the reasoning that a
#: provider naming a permissions scope tells you the operation changes who can
#: reach data. **Measured against the shipped toolsets, it finds nothing.**
#: Google covers permissions with the *broad* scope — ``drive_share_file`` and
#: ``drive_remove_permission`` both declare ``auth/drive``, identical to an
#: ordinary write — and the whole Microsoft family declares no scopes at all.
#: Slack is the lone case that would work (``channels:manage``).
#:
#: So ``access_control`` is a **declared** facet, like ``undone_by``: nothing in
#: a manifest today implies it. Shipping a derivation that silently matched zero
#: operations would have been worse than none, because the flag would read as
#: computed when it was only ever absent.

_RANK: dict[EffectClass, int] = {
    EffectClass.READ: 0,
    EffectClass.WRITE: 1,
    EffectClass.DESTRUCTIVE: 2,
}


@dataclass(frozen=True, slots=True)
class EffectProfile:
    """Everything a policy needs to know about one operation's side effect."""

    effect: EffectClass = EffectClass.WRITE
    """How much damage. Defaults to the cautious end, as ``EffectCall`` does."""

    open_world: bool = True
    """Does this reach outside the deployment's trust boundary?

    ``False`` for pure computation and for stores the deployment owns. This is
    what a read-to-write taint rule should key on instead of ``READ`` — under
    the current rule, filtering a list the run was handed counts as reading the
    world, and the next write is refused. MCP calls the same axis
    ``openWorldHint``: a web search is open, a memory tool is not.
    """

    reversible: bool = False
    """Can the effect be undone, by an operation in this toolset?"""

    idempotent: bool = False
    """Does calling it again with the same arguments add nothing?

    Distinct from :attr:`reversible`: re-sending an email is not idempotent and
    also not reversible, while re-deleting a record is idempotent and not
    reversible. Retry safety reads this one; approval policy reads the other.
    """

    access_control: bool = False
    """Does it change who can reach data, rather than the data itself?"""

    undone_by: str = ""
    """The operation id that reverses this one, when one exists."""

    effect_by: dict[str, dict[str, EffectClass]] = field(default_factory=dict)
    """Argument-dependent overrides, applied per call by :func:`resolve_effect`."""

    source: Source = "default"
    """Whether :attr:`effect` was declared, derived, or defaulted."""

    @property
    def irreversible(self) -> bool:
        """It happened, outside this deployment, and nothing undoes it.

        The "send mail" predicate, and the one an approval gate should use.
        Deliberately not the same as ``effect is DESTRUCTIVE``: a destructive
        operation with an inverse is recoverable, and an additive one that
        leaves the building is not.
        """
        return self.open_world and not self.reversible


def derive_effect_profile(
    op: OperationSpec,
    *,
    verb: str = "",
    has_client: bool = True,
) -> EffectProfile:
    """Resolve *op*'s profile. Precedence, highest first:

    1. **what the author declared** — ``op.model_fields_set``, which survives
       every construction path a manifest arrives by, including
       ``model_validate_json`` for one loaded from a package entry point.
    2. **what the client's HTTP verb says** — see :data:`VERB_EFFECT`.
    3. **what the scopes say** — a read-only scope has never disagreed.
    4. **the fail-safe default** — WRITE, open-world, irreversible.

    A derivation **never lowers a declared class**. An author who wrote
    DESTRUCTIVE over a ``GET`` knows something the verb does not — a soft-delete
    endpoint, a request that triggers a workflow. The reverse case, where the
    verb implies *more* than the author declared, is a likely mistake but is
    still not applied here: it is reported by :func:`verb_disagreement`, because
    silently overriding a declaration makes the manifest stop meaning what it
    says.

    Args:
        op: The operation to classify.
        verb: The HTTP verb the client issues, when one could be recovered.
        has_client: Whether this toolset talks to a remote service at all. The
            default for :attr:`~EffectProfile.open_world` when the manifest
            does not say.
    """
    declared = op.model_fields_set

    if "effect" in declared:
        effect: EffectClass = op.effect
        source: Source = "declared"
    elif (implied := VERB_EFFECT.get(verb.upper())) is not None:
        effect, source = implied, "derived"
    elif scope_is_readonly(op.scopes):
        effect, source = EffectClass.READ, "derived"
    else:
        effect, source = EffectClass.WRITE, "default"

    return EffectProfile(
        effect=effect,
        open_world=has_client,
        reversible=op.reversible,
        idempotent=op.idempotent
        if "idempotent" in declared
        else effect is EffectClass.READ,
        access_control=op.access_control,
        undone_by=op.undone_by,
        effect_by=dict(op.effect_by),
        source=source,
    )


#: Verbs whose implication is strong enough to contradict a declaration.
#:
#: ``POST`` is deliberately excluded, and not to excuse a failing check. GET
#: means read and DELETE means destroy in essentially every REST API; POST
#: means *both* — creating a resource and running a query whose parameters are
#: too complex for a URL. Both exceptions found across the shipped toolsets are
#: that second case (`hubspot_search_objects`, `outlook_get_schedule`), and
#: treating POST as contradicting a READ declaration would report every
#: search-by-body endpoint as a defect until somebody suppressed the check.
#:
#: POST still *derives* WRITE where nothing is declared: guessing the cautious
#: way costs an approval, and this list is only about overruling a human.
CONTRADICTING_VERBS: frozenset[str] = frozenset(
    {"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"}
)


def resolve_effect(
    declared: EffectClass,
    effect_by: dict[str, dict[str, EffectClass]],
    arguments: dict[str, Any],
) -> EffectClass:
    """Apply an :attr:`OperationSpec.effect_by` table to one call's arguments.

    For the handful of operations whose class is a property of the *call* and
    not of the operation — ``io.http_request`` with ``method="DELETE"``, and
    the generic CRUD entry points. Returns *declared* unchanged when no rule
    matches, which is the case for every operation that declares no table.

    Strictly first-match by declaration order, and a value that is not in the
    table keeps *declared*: an unrecognised method must not fall through to a
    read.
    """
    for parameter, mapping in effect_by.items():
        if parameter not in arguments:
            continue
        value = arguments[parameter]
        key = value.upper() if isinstance(value, str) else value
        matched = mapping.get(key)
        if matched is not None:
            return matched
    return declared


def verb_disagreement(op: OperationSpec, verb: str) -> str | None:
    """Why *op*'s declared class disagrees with the verb its client issues.

    Returns ``None`` when they agree, when no verb was recovered, or when the
    author declared a *stricter* class than the verb implies — that last case
    is a judgement worth keeping, not a defect.

    What is reported is the other direction: an operation declared READ that
    issues a ``DELETE``, or declared WRITE that issues one. That is how
    ``drive_trash_file`` reads as harmless.

    ``POST`` never contradicts — see :data:`CONTRADICTING_VERBS`.
    """
    if verb.upper() not in CONTRADICTING_VERBS:
        return None
    implied = VERB_EFFECT.get(verb.upper())
    if implied is None or "effect" not in op.model_fields_set:
        return None
    if _RANK[implied] <= _RANK[op.effect]:
        return None
    return (
        f"{op.id} is declared {op.effect.value} but its client issues "
        f"{verb.upper()}, which implies {implied.value}. Either the "
        f"declaration understates the effect, or this is a read issued as a "
        f"{verb.upper()} — declare it and say which in a comment."
    )
