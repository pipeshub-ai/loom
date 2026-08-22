"""Where a toolset's configuration comes from, decided outside the toolset.

Twenty-seven clients read ``os.environ`` in ``__init__`` and cache the result in
a module-level singleton for the life of the process. The constructors already
take parameters; nothing on the call path can pass them, because every one of
the 390 call sites is a ``@step`` carrying business arguments only. So the
environment is not *a* source, it is the only one, and adding a second means
editing twenty-seven files.

This module is the seam that removes that. A :class:`CredentialProvider` says
what it has; a :class:`ChainProvider` says which wins; a :class:`ToolsetSession`
holds the chain for a run. Clients become functions of the values they are
handed.

**The shape is not novel and should not be.** Airbyte declares a connector
specification, stores credentials centrally, and injects them into every
execution; botocore resolves through a ``Session`` holding an ordered provider
chain and hands a service client the result. LOOM already had the declaration
half — :class:`~loom.toolsets.manifest.AuthSpec` is that specification, down to
``AuthField.mode`` being its ``oneOf`` — and none of the injection half.

**What is deliberately not a provider: the credential store.**
``resolve_bearer_token`` is not a field value. It selects a different *wire
scheme* — Bearer instead of the Basic that ``JIRA_EMAIL``/``JIRA_API_TOKEN``
produce — and the clients that use it call it **on every request, never
cached**, so a token the store refreshes mid-run is picked up immediately.
Folding it into a field resolved once per run would quietly destroy that, which
is a worse outcome than the asymmetry of leaving it where it is.
:attr:`ResolvedCredentials.credential` carries the store *key* through to the
client so the per-request lookup keeps working unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from loom.toolsets.manifest import AuthSpec

__all__ = [
    "ChainProvider",
    "CredentialProvider",
    "EnvironmentProvider",
    "ResolvedCredentials",
    "StaticProvider",
    "ToolsetSession",
    "default_providers",
]


@runtime_checkable
class CredentialProvider(Protocol):
    """One place configuration values can come from.

    A host with a vault implements this and nothing else.

    **Absence is an absent key, never an exception.** A provider is asked about
    every toolset, and most sources hold values for a few — so "I don't have
    it" is the ordinary answer and has to be cheap. Raising would make the
    first provider in a chain able to stop the ones behind it, which is the
    opposite of what a chain is for.

    A provider is also never asked to interpret what it returns: which values
    are required, which alternative they satisfy, and which win are all decided
    by the chain against the declaration. That is what keeps a new source to
    one method.
    """

    @property
    def id(self) -> str:
        """Stable name, recorded in :attr:`ResolvedCredentials.sources` so
        "where did this token come from" is answerable. It is the first
        question when a call 401s and today there is nothing that can answer it.

        Read-only in the protocol so a frozen implementation satisfies it — and
        because nothing should be reassigning a provider's identity anyway. A
        plain attribute still conforms.
        """
        ...

    async def supply(self, spec: AuthSpec) -> Mapping[str, str]:
        """Whatever this source has for *spec*. Absent keys simply absent."""
        ...


@dataclass(frozen=True)
class ResolvedCredentials:
    """What a chain found, and where each value came from."""

    toolset: str
    values: Mapping[str, str] = field(default_factory=dict)
    mode: str = ""
    """Which declared alternative these values satisfy. ``""`` when the toolset
    declares none, which is most of them."""
    missing: tuple[str, ...] = ()
    """What the nearest mode still lacks. Empty means ready to construct."""
    sources: Mapping[str, str] = field(default_factory=dict)
    """Field name to the id of the provider that supplied it."""
    credential: str = ""
    """The ``CredentialStore`` key this toolset's client reads per request, from
    :attr:`AuthSpec.credential`. Carried rather than resolved — see the module
    docstring on why the store is not a provider."""

    @property
    def complete(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        """One line, for ``loom doctor`` and for a refusal that has to explain
        itself. Names the provider per value, because "set JIRA_URL" is unhelpful
        to somebody who has set it in a place this chain does not read."""
        if not self.values:
            return f"{self.toolset}: nothing configured"
        supplied = ", ".join(
            f"{name}<-{self.sources.get(name, '?')}" for name in sorted(self.values)
        )
        tail = f"; missing {', '.join(self.missing)}" if self.missing else ""
        mode = f" via {self.mode}" if self.mode else ""
        return f"{self.toolset}{mode}: {supplied}{tail}"


@dataclass(frozen=True)
class EnvironmentProvider:
    """The process environment — what every toolset reads today.

    First in the default chain, so a deployment that sets environment variables
    and composes nothing keeps behaving exactly as it did. That ordering is the
    compatibility guarantee of this whole change, and it is why this is a
    provider at all rather than a fallback inside the chain: as a provider it
    can be reordered, replaced, or dropped by a host that wants none of it.
    """

    id: str = "environment"
    environ: Mapping[str, str] | None = None
    """Read from ``os.environ`` when ``None``. An explicit mapping is what makes
    this testable without touching the real environment — the reason the thing
    it replaces was hard to test at all."""

    async def supply(self, spec: AuthSpec) -> Mapping[str, str]:
        source = os.environ if self.environ is None else self.environ
        return {
            f.name: source[f.name]
            for f in spec.fields
            if source.get(f.name)
        }


@dataclass(frozen=True)
class StaticProvider:
    """Values a host supplies directly — per tenant, per run, from a vault.

    The provider that makes two tenants in one process possible, and the one
    tests use instead of monkeypatching a module global.
    """

    values: Mapping[str, str]
    id: str = "static"

    async def supply(self, spec: AuthSpec) -> Mapping[str, str]:
        # Filtered to what the toolset declares. A host passing one mapping for
        # everything should not have Stripe's key handed to Slack because both
        # were in the dict.
        return {f.name: self.values[f.name] for f in spec.fields if self.values.get(f.name)}


@dataclass(frozen=True)
class ChainProvider:
    """Several providers, in order. Earlier wins.

    Precedence is positional and nothing else: no scoring, no "most specific",
    no provider able to claim priority for itself. A host that wants its vault
    to beat the environment puts it first, and can see that it did.
    """

    providers: Sequence[CredentialProvider]
    id: str = "chain"

    async def supply(self, spec: AuthSpec) -> Mapping[str, str]:
        """The merged values, so a chain is itself a provider and nests."""
        merged, _ = await self.merge(spec)
        return merged

    async def merge(self, spec: AuthSpec) -> tuple[dict[str, str], dict[str, str]]:
        """Merged values, and which provider supplied each.

        Public because attribution is the useful half and ``supply`` has to
        throw it away to satisfy the protocol. A session calling a private
        method of its own collaborator would be the same coupling this module
        exists to remove, one layer up.
        """
        values: dict[str, str] = {}
        sources: dict[str, str] = {}
        for provider in self.providers:
            # A nesting provider is asked to attribute its own values, so a host
            # that groups its sources and hands the group over as one still gets
            # per-source attribution. Collapsing them to the group's id would
            # answer "chain" to "where did this token come from", which is
            # exactly as useful as answering nothing.
            inner = getattr(provider, "merge", None)
            if callable(inner):
                supplied, attribution = await inner(spec)
            else:
                supplied = await provider.supply(spec)
                attribution = {}
            for name, value in supplied.items():
                if name not in values and value:
                    values[name] = value
                    sources[name] = attribution.get(name) or provider.id
        return values, sources


def default_providers() -> tuple[CredentialProvider, ...]:
    """What a Runtime composes when a host asks for nothing.

    Exactly today's behaviour: the environment, and nothing else. A default
    chain that reached further would change what an existing deployment does on
    upgrade, which is the one thing this must not do.
    """
    return (EnvironmentProvider(),)


@dataclass(frozen=True)
class ToolsetSession:
    """The chain a run resolves through.

    Per run rather than per process, which is the whole point: the singleton
    being replaced is constructed once from the first environment it sees and
    is then the only credential set the process can ever have. A session is a
    value, so two of them coexist and a refreshed credential is picked up by
    the next run rather than never.
    """

    providers: Sequence[CredentialProvider] = field(default_factory=default_providers)

    async def resolve(self, toolset: str, spec: AuthSpec) -> ResolvedCredentials:
        """Everything known about *toolset*, and how complete it is.

        Never raises for missing values. A caller deciding whether to build a
        client, and a ``loom doctor`` reporting what a machine has, want the
        same answer shaped the same way — and only the first of those treats
        an incomplete one as a failure.
        """
        chain = ChainProvider(self.providers)
        values, sources = await chain.merge(spec)
        mode, missing = spec.nearest_mode(values)
        return ResolvedCredentials(
            toolset=toolset,
            values=values,
            mode=mode,
            missing=missing,
            sources=sources,
            credential=spec.credential,
        )
