"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from loom import Context, Runtime, step, workflow
from loom.stores.memory import MemoryStore


@pytest.fixture(autouse=True, scope="session")
def never_touch_the_real_keychain() -> Iterator[None]:
    """Keep the whole suite away from the developer's OS keychain.

    ``default_key_provider`` prefers ``LOOM_CREDENTIAL_KEY``, then the OS
    keyring, then a generated file. So any test constructing an
    ``EncryptedFileCredentialStore`` without an explicit provider reaches the
    second one — and on macOS that is a modal password prompt, which does not
    fail the test so much as stop it: the run hangs until somebody notices a
    dialog and knows the login password.

    Two files already defended themselves with a fake keyring backend. Doing it
    here instead means a test cannot forget, which is the only version of this
    that survives someone adding the next credential test. Pinning the key
    rather than faking the backend also takes the priority order's own first
    branch — "explicit, portable, what CI and containers use".

    That "cannot forget" was once only half true: ``KeyringCredentialStore``
    built its key provider by hand instead of calling ``default_key_provider``,
    so it went round this fixture entirely and the two files' fake backends
    were the only thing holding. It routes through the order now, so this
    covers it — and the handful of tests that mean to exercise the *keyring*
    branch say so with an explicit ``monkeypatch.delenv``.

    Session-scoped and set only when absent, so a developer or CI that has
    deliberately exported one keeps it. The value is a well-formed Fernet
    key spelling out what it is — the store validates it, so a placeholder
    that merely looks like base64 fails at construction.
    """
    import os

    previous = os.environ.get("LOOM_CREDENTIAL_KEY")
    if previous is None:
        os.environ["LOOM_CREDENTIAL_KEY"] = "bG9vbS10ZXN0cy1vbmx5LW5vdC1hLXJlYWwtc2VjcmU="
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LOOM_CREDENTIAL_KEY", None)


@pytest.fixture(autouse=True)
def isolated_catalog() -> Iterator[object]:
    """Snapshot and restore the process-global toolset catalog.

    ``register_toolset`` writes to a module-level singleton, so without this a
    test that registers a manifest changes what every later test sees — the
    kind of ordering dependency that only shows up in a full-suite run.
    """
    from loom.toolsets.registry import get_catalog

    catalog = get_catalog()
    saved = dict(catalog._manifests)
    saved_toolsets = dict(getattr(catalog, "_toolsets", {}))
    try:
        yield catalog
    finally:
        catalog._manifests.clear()
        catalog._manifests.update(saved)
        if hasattr(catalog, "_toolsets"):
            catalog._toolsets.clear()
            catalog._toolsets.update(saved_toolsets)
        # The function indexes are derived from `_manifests` and cached until
        # a registration clears them, so restoring the store is not restoring
        # the catalogue: without this, a test that registered toolsets left
        # `effect_of`/`profile_of` answering for them in every later test.
        catalog.invalidate()


@pytest.fixture
def memory_store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def runtime(memory_store: MemoryStore) -> Runtime:
    return Runtime(store=memory_store)


@step
async def add_one(x: int) -> int:
    return x + 1


@step
async def greet(name: str) -> str:
    return f"Hello, {name}!"


@step
async def failing_step(x: int) -> int:
    raise ValueError(f"intentional failure on {x}")


@workflow
async def identity_workflow(ctx: Context, data: str) -> str:
    return data


@workflow
async def greet_workflow(ctx: Context, name: str) -> str:
    return await ctx.step(greet, name)


@pytest.fixture(autouse=True)
def _no_fakes_leak_between_tests():
    """Undo any toolset fakes a test installed.

    ``install_fakes`` rewrites a toolset module in place — correct in the smoke
    subprocess, which is discarded immediately, and a leak anywhere else. Left
    alone, the first test to install one silently replaces those tools for the
    rest of the session, and the failure surfaces as an unrelated test
    disagreeing about what a function returns.
    """
    yield
    from loom.agents.fakes import uninstall_fakes

    uninstall_fakes()
