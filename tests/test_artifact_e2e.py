"""End-to-end artifact staging, HTTP, MCP, and replay pinning."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from loom import Context, ExecutionStatus, Runtime, workflow
from loom.blobs.blob import BlobService, LocalBlobBackend
from loom.facade import LocalFacade
from loom.stores.memory import MemoryStore


@pytest.fixture
def blobs(tmp_path: Path) -> BlobService:
    return BlobService(
        LocalBlobBackend(tmp_path / "blobs", base_url="http://loom.test")
    )


@pytest.fixture
def runtime(blobs: BlobService) -> Runtime:
    return Runtime(store=MemoryStore(), blobs=blobs)


@workflow(name="e2e_stage")
async def e2e_stage(ctx: Context, body: str) -> str:
    await ctx.stage_artifact("report.md", body.encode(), mime="text/markdown")
    version = await ctx.commit_staged("report.md")
    return version.qualified_name


class TestWorkflowStagingE2E:
    async def test_stage_commit_and_replay_pin(self, runtime: Runtime) -> None:
        runtime.register(e2e_stage)
        first = await runtime.run(e2e_stage, "# v1")
        assert first.status is ExecutionStatus.COMPLETED
        assert first.output == "report.md@1"

        second = await runtime.run(e2e_stage, "# v2")
        assert second.output == "report.md@2"

        @workflow(name="e2e_read")
        async def reader(ctx: Context, _input: str) -> str:
            return (await ctx.get_artifact("report.md")).decode()

        runtime.register(reader)
        read = await runtime.run(reader, "go")
        assert read.output == "# v2"

        await runtime.artifacts.put("report.md", b"# v3")
        replayed = await runtime.replay(read.run_id)
        assert replayed.output == "# v2"


def _http_app(facade: LocalFacade):
    """Build the FastAPI app, or skip when the extra's Starlette is too new."""
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("fastapi")
    from loom.server.app import create_app

    try:
        return create_app(facade), httpx
    except TypeError as exc:
        if "on_startup" in str(exc):
            pytest.skip(f"fastapi/starlette mismatch: {exc}")
        raise


class TestFacadeAndHttp:
    async def test_local_facade_lists_and_reads(self, runtime: Runtime) -> None:
        runtime.register(e2e_stage)
        await runtime.run(e2e_stage, "hello")
        facade = LocalFacade(runtime)
        listed = await facade.list_artifacts()
        assert listed[0]["name"] == "report.md"
        payload = await facade.read_artifact("report.md")
        assert base64.b64decode(payload["content_b64"]) == b"hello"
        history = await facade.artifact_history("report.md")
        assert len(history) == 1

    async def test_http_list_and_content(self, runtime: Runtime) -> None:
        from loom.server import LoomClient

        runtime.register(e2e_stage)
        await runtime.run(e2e_stage, "via-http")
        facade = LocalFacade(runtime)
        app, httpx = _http_app(facade)
        transport = httpx.ASGITransport(app=app)
        async with LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        ) as client:
            listed = await client.list_artifacts()
            assert listed[0]["name"] == "report.md"
            payload = await client.read_artifact("report.md")
            assert base64.b64decode(payload["content_b64"]) == b"via-http"
            history = await client.artifact_history("report.md")
            assert history[0]["version"] == 1

            put = await client.put_artifact(
                "tiny.txt",
                base64.b64encode(b"tiny").decode(),
                mime="text/plain",
            )
            assert put["name"] == "tiny.txt"

    async def test_local_signed_download_and_upload(self, runtime: Runtime) -> None:
        from urllib.parse import parse_qs, urlparse

        runtime.register(e2e_stage)
        await runtime.run(e2e_stage, "signed")
        facade = LocalFacade(runtime)
        app, httpx = _http_app(facade)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://loom.test"
        ) as raw:
            info = await facade.artifact_url("report.md")
            parsed = urlparse(info["url"])
            query = parse_qs(parsed.query)
            response = await raw.get(
                parsed.path,
                params={
                    "expires": query["expires"][0],
                    "sig": query["sig"][0],
                    "method": query.get("method", ["GET"])[0],
                },
            )
            assert response.status_code == 200
            assert response.content == b"signed"

            bad = await raw.get(
                parsed.path,
                params={"expires": query["expires"][0], "sig": "0" * 64, "method": "GET"},
            )
            assert bad.status_code == 403

            session = await facade.upload_url("uploaded.txt", mime="text/plain")
            put_url = urlparse(session["url"])
            put_q = parse_qs(put_url.query)
            written = await raw.put(
                put_url.path,
                params={
                    "expires": put_q["expires"][0],
                    "sig": put_q["sig"][0],
                    "method": put_q.get("method", ["PUT"])[0],
                },
                content=b"from-client",
                headers={"Content-Type": "text/plain"},
            )
            assert written.status_code == 200
            confirmed = await facade.confirm_upload(session["upload_id"], "uploaded.txt")
            assert confirmed["name"] == "uploaded.txt"
            assert await runtime.artifacts.read("uploaded.txt") == b"from-client"

    async def test_same_answer_local_and_http(self, runtime: Runtime) -> None:
        from loom.server import LoomClient

        runtime.register(e2e_stage)
        await runtime.run(e2e_stage, "parity")
        local = LocalFacade(runtime)
        app, httpx = _http_app(local)
        transport = httpx.ASGITransport(app=app)
        client = LoomClient(
            http=httpx.AsyncClient(transport=transport, base_url="http://loom.test")
        )
        try:
            assert await local.list_artifacts() == await client.list_artifacts()
            assert await local.artifact_history("report.md") == await client.artifact_history(
                "report.md"
            )
        finally:
            await client.close()


class TestMcpArtifacts:
    async def test_list_and_put_tools(self, runtime: Runtime) -> None:
        from loom.mcp_server import tools

        runtime.register(e2e_stage)
        await runtime.run(e2e_stage, "mcp")
        facade = LocalFacade(runtime)
        payload = json.loads(await tools.list_artifacts(facade))
        assert payload["artifacts"][0]["name"] == "report.md"

        put = json.loads(
            await tools.put_artifact(
                facade,
                "mcp.txt",
                base64.b64encode(b"via-mcp").decode(),
                "text/plain",
            )
        )
        assert put["name"] == "mcp.txt"
