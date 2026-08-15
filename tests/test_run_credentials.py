"""Per-run credentials: identity isolation, resolver, children, in-workflow asks."""

from __future__ import annotations

import asyncio

import pytest

from loom import Context, Runtime, step, workflow
from loom.agents.agent import Agent
from loom.agents.interaction import CallbackUserInteraction, UserQuestion, UserResponse
from loom.agents.messages import ToolCall
from loom.connectors.credentials import (
    MemoryCredentialStore,
    StoredCredential,
    resolve_bearer_token,
)
from loom.core.exceptions import ConfigurationError
from loom.core.models import ExecutionStatus
from loom.core.secret import Secret
from loom.facade import LocalFacade, RemoteFacade
from loom.identity.facade import AuthorizedFacade
from loom.identity.principal import Principal
from loom.stores import MemoryStore
from loom.testing.mock import MockModelProvider, mock_response


@step
async def read_jira_token() -> str:
    return await resolve_bearer_token("jira") or ""


class TestPerRunCredentials:
    async def test_token_passed_to_run_reaches_a_step_body(self) -> None:
        @workflow(name="uses_jira_token")
        async def uses_jira_token(ctx: Context, _: None = None) -> str:
            return await ctx.step(read_jira_token)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(uses_jira_token, credentials={"jira": "caller-token"})
        assert result.status is ExecutionStatus.COMPLETED
        assert result.output == "caller-token"
        record = await rt.get(result.run_id)
        assert record is not None
        assert record.metadata["loom.credential_names"] == ["jira"]
        assert "caller-token" not in str(record.metadata)

    async def test_terminal_run_clears_the_in_memory_store(self) -> None:
        @workflow(name="clears_creds")
        async def clears_creds(ctx: Context, _: None = None) -> str:
            return await ctx.step(read_jira_token)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(clears_creds, credentials={"jira": "tok"})
        assert result.run_id not in rt._run_credentials

    async def test_parked_run_without_credentials_does_not_use_env_fallback(self) -> None:
        @workflow(name="park_then_jira")
        async def park_then_jira(ctx: Context, _: None = None) -> str:
            await ctx.wait_for_event("go")
            return await ctx.step(read_jira_token)

        ambient = MemoryCredentialStore()
        await ambient.put("jira", StoredCredential(token=Secret("ambient-token")))
        rt = Runtime(store=MemoryStore(), credentials=ambient)
        parked = await rt.run(park_then_jira, credentials={"jira": "caller-token"})
        assert parked.status is ExecutionStatus.SUSPENDED
        rt._run_credentials.clear()
        await rt.send_event(parked.run_id, "go", None)

        record = None
        for _ in range(200):
            record = await rt.get(parked.run_id)
            assert record is not None
            if record.awaiting_event == "credential:jira":
                break
            await asyncio.sleep(0.01)
        else:
            assert record is not None
            raise AssertionError(
                f"expected credential:jira park, got {record.awaiting_event!r} "
                f"status={record.status}"
            )
        assert record.status is ExecutionStatus.SUSPENDED

    async def test_credential_resolver_re_supplies_on_resume(self) -> None:
        @workflow(name="park_then_resolved")
        async def park_then_resolved(ctx: Context, _: None = None) -> str:
            await ctx.wait_for_event("go")
            return await ctx.step(read_jira_token)

        async def resolver(record):
            store = MemoryCredentialStore()
            await store.put("jira", StoredCredential(token=Secret("resolved-token")))
            return store

        rt = Runtime(store=MemoryStore(), credential_resolver=resolver)
        parked = await rt.run(park_then_resolved, credentials={"jira": "original"})
        rt._run_credentials.clear()
        await rt.send_event(parked.run_id, "go", None)
        record = None
        for _ in range(200):
            record = await rt.get(parked.run_id)
            if record is not None and record.status is ExecutionStatus.COMPLETED:
                break
            await asyncio.sleep(0.01)
        else:
            assert record is not None
            raise AssertionError(
                f"expected completed, got {record.status} event={record.awaiting_event}"
            )
        final = await rt.result(parked.run_id)
        assert final.output == "resolved-token"

    async def test_two_concurrent_runs_stay_isolated(self) -> None:
        @workflow(name="isolated_jira")
        async def isolated_jira(ctx: Context, _: None = None) -> str:
            return await ctx.step(read_jira_token)

        rt = Runtime(store=MemoryStore())
        first, second = await asyncio.gather(
            rt.run(isolated_jira, credentials={"jira": "alpha"}),
            rt.run(isolated_jira, credentials={"jira": "beta"}),
        )
        assert {first.output, second.output} == {"alpha", "beta"}

    async def test_child_workflow_inherits_credentials_and_env(self) -> None:
        @workflow(name="child_reads_identity")
        async def child_reads_identity(ctx: Context, _: None = None) -> str:
            token = await ctx.step(read_jira_token)
            return f"{token}:{ctx.env['REGION']}"

        @workflow(name="parent_spawns_child")
        async def parent_spawns_child(ctx: Context, _: None = None) -> str:
            return await ctx.child(child_reads_identity)

        rt = Runtime(store=MemoryStore())
        result = await rt.run(
            parent_spawns_child,
            credentials={"jira": "from-parent"},
            env={"REGION": "eu"},
        )
        assert result.output == "from-parent:eu"


class TestInWorkflowAskIsJournaled:
    async def test_replay_does_not_re_ask(self) -> None:
        asked: list[str] = []

        def cb(question: UserQuestion) -> UserResponse:
            asked.append(question.question)
            return UserResponse(answer="blue")

        agent = Agent(
            name="colourist",
            model=MockModelProvider(
                responses=[
                    mock_response(
                        tool_calls=[
                            ToolCall(
                                name="ask_user",
                                arguments={"question": "favourite colour?"},
                            )
                        ]
                    ),
                    mock_response("the colour is blue"),
                ]
            ),
            user_interaction=CallbackUserInteraction(cb),
        )

        @workflow(name="asks_once")
        async def asks_once(ctx: Context, _: None = None) -> str:
            result = await ctx.agent(agent, "hi")
            return result.text()

        rt = Runtime(store=MemoryStore())
        first = await rt.run(asks_once)
        assert asked == ["favourite colour?"]
        assert "blue" in first.output
        asked.clear()
        replayed = await rt.replay(first.run_id)
        assert asked == []
        assert replayed.output == first.output


class TestFacadeCredentials:
    async def test_remote_facade_refuses_credentials(self) -> None:
        remote = RemoteFacade(client=object())
        with pytest.raises(ConfigurationError, match="loom connect"):
            await remote.start("x", None, credentials={"jira": "tok"})

    async def test_authorized_facade_refuses_credentials(self) -> None:
        inner = LocalFacade(Runtime(store=MemoryStore()))
        facade = AuthorizedFacade(
            inner, Principal(subject="alice", scopes=frozenset({"runs:write"}))
        )
        with pytest.raises(ConfigurationError, match="AuthorizedFacade"):
            await facade.start("x", None, credentials={"jira": "tok"})

    async def test_local_facade_honours_credentials_and_env(self) -> None:
        @workflow(name="facade_echo")
        async def facade_echo(ctx: Context, _: None = None) -> str:
            token = await ctx.step(read_jira_token)
            return f"{token}:{ctx.env['REGION']}"

        rt = Runtime(store=MemoryStore())
        rt.register(facade_echo)
        started = await LocalFacade(rt).start(
            "facade_echo",
            None,
            env={"REGION": "us"},
            credentials={"jira": "via-facade"},
        )
        assert started["output"] == "via-facade:us"
        assert "loom.env" not in (started.get("metadata") or {})
