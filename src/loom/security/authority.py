"""What a run is allowed to do.

An `Authority` is the answer to "may it?", carried alongside a run and consulted
on every effect. It is deliberately not an identity: LOOM does not know who is
asking, does not authenticate, and does not partition. It knows only what the
run in front of it may invoke, and whether this is a rehearsal.

Setting one is the host's job. LOOM never derives an authority from a token, a
header, or a session — it accepts one, or runs unrestricted, which is the
default and what a bare `Runtime()` does.

>>> from loom.security.authority import Authority
>>> Authority().is_unrestricted
True
>>> Authority(dry_run=True).is_unrestricted
False
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from loom.security.grants import GrantSet

__all__ = ["Authority"]


@dataclass(frozen=True)
class Authority:
    """The permissions and mode a run executes under.

    Immutable on purpose: an authority that could be edited after a run started
    would let permissions widen mid-flight, and the run would no longer be
    bounded by what it was admitted under.
    """

    grant: GrantSet = field(default_factory=GrantSet)
    """What may be invoked. Declared here, enforced by the effect broker; an
    empty grant set means "unrestricted", which is what keeps configuring a
    broker from breaking every workflow that never declared anything."""

    dry_run: bool = False
    """Perform reads, refuse writes. Enforced by the effect broker."""

    @property
    def is_unrestricted(self) -> bool:
        """True when this authority narrows nothing.

        The default is indistinguishable from having none, which is what keeps
        a bare ``Runtime()`` on the cheap path.
        """
        return self.grant.is_empty and not self.dry_run

    def narrowed(self, **changes: object) -> Authority:
        """A copy with fields replaced.

        Only ever used to *reduce* permissions in practice; the name says which
        direction the code is meant to travel.
        """
        return replace(self, **changes)  # type: ignore[arg-type]
