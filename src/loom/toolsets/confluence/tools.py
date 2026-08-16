"""Confluence step functions for use inside LOOM workflows.

Each function is decorated with @step so it can be called via ctx.step().
The ConfluenceClient is instantiated lazily on first call — credentials
come from CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN env vars.

All functions return typed Pydantic models from ``models.py``.

Usage in a generated workflow::

    from loom.toolsets.confluence.tools import (
        confluence_search_pages,
        confluence_create_page,
    )

    results = await ctx.step(confluence_search_pages, "type = page AND title ~ 'Onboarding'")
    page = await ctx.step(confluence_create_page, space_id, "My Page", "<p>Hello</p>")
"""

from __future__ import annotations

from pydantic import BaseModel

from loom import Retry, step
from loom.toolsets.confluence.models import (
    ConfluenceComment,
    ConfluencePage,
    ConfluenceSpace,
    ConfluenceUser,
    CreatedPage,
    PageBody,
    SearchResult,
)
from loom.toolsets.pagination import Results


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_search_pages(
    cql: str,
    limit: int = 20,
) -> Results[SearchResult]:
    """Search Confluence content using CQL.

    Args:
        cql: CQL query, e.g. ``"type = page AND title ~ 'Design'"``
        limit: Maximum results to return (default 20).

    Returns:
        List of SearchResult with content_id, title, type, space_key,
        excerpt, url, last_modified.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().search_pages(cql, limit)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_get_page(page_id: str) -> ConfluencePage:
    """Fetch a Confluence page by its ID.

    Args:
        page_id: Page ID (numeric string).

    Returns:
        ConfluencePage with id, title, status, space_id, version, url.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().get_page(page_id)


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def confluence_create_page(
    space_id: str,
    title: str,
    body: str,
    parent_id: str | None = None,
) -> CreatedPage:
    """Create a new Confluence page.

    Args:
        space_id: Space ID (get via confluence_list_spaces).
        title: Page title.
        body: HTML body (storage format), e.g. ``"<p>Hello</p>"``.
        parent_id: Optional parent page ID for nesting.

    Returns:
        CreatedPage with id, title, version, url.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().create_page(
        space_id, title, body, parent_id=parent_id
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def confluence_update_page(
    page_id: str,
    title: str,
    body: str,
    version: int | None = None,
) -> CreatedPage:
    """Update an existing Confluence page.

    Args:
        page_id: Page ID to update.
        title: New title.
        body: New HTML body (storage format).
        version: Version number (auto-incremented if omitted).

    Returns:
        CreatedPage with updated id, title, version, url.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().update_page(
        page_id, title, body, version=version
    )


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def confluence_delete_page(page_id: str) -> str:
    """Delete a Confluence page.

    Args:
        page_id: Page ID to delete.

    Returns:
        Confirmation string.
    """
    from loom.toolsets.confluence.client import get_default_client

    await get_default_client().delete_page(page_id)
    return f"Deleted page {page_id}"


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_get_page_body(page_id: str) -> PageBody:
    """Fetch the body content of a Confluence page.

    Args:
        page_id: Page ID.

    Returns:
        PageBody with page_id, title, body (HTML storage format).
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().get_page_body(page_id)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_get_page_comments(
    page_id: str,
    limit: int = 25,
) -> Results[ConfluenceComment]:
    """Fetch comments on a Confluence page.

    Args:
        page_id: Page ID.
        limit: Maximum comments to return (default 25).

    Returns:
        List of ConfluenceComment with id, body, author_id, created_at.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().get_page_comments(page_id, limit)


@step(retry=Retry(max_attempts=2, initial_delay=1.0))
async def confluence_add_comment(
    page_id: str,
    comment: str,
) -> ConfluenceComment:
    """Add a comment to a Confluence page.

    Args:
        page_id: Page ID.
        comment: Plain-text comment body.

    Returns:
        ConfluenceComment with id, body, author_id, created_at.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().add_comment(page_id, comment)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_list_spaces(
    limit: int = 25,
) -> Results[ConfluenceSpace]:
    """List all accessible Confluence spaces.

    Args:
        limit: Maximum spaces to return (default 25).

    Returns:
        List of ConfluenceSpace with id, key, name, type, status.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().list_spaces(limit)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_get_space(space_id: str) -> ConfluenceSpace:
    """Get a Confluence space by ID.

    Args:
        space_id: Space ID.

    Returns:
        ConfluenceSpace with id, key, name, type, status, description.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().get_space(space_id)


@step(retry=Retry(max_attempts=3, initial_delay=1.0))
async def confluence_get_myself() -> ConfluenceUser:
    """Get the authenticated Confluence user's profile.

    Returns:
        ConfluenceUser with account_id, display_name, email.
    """
    from loom.toolsets.confluence.client import get_default_client

    return await get_default_client().get_myself()


# ---------------------------------------------------------------------------
# Auto-generated tool documentation
# ---------------------------------------------------------------------------


def _build_tool_docs() -> str:
    """Build CONFLUENCE_TOOL_DOCS from model schemas."""
    from loom.toolsets.confluence.models import (
        ConfluenceComment as _Comment,
    )
    from loom.toolsets.confluence.models import (
        ConfluencePage as _Page,
    )
    from loom.toolsets.confluence.models import (
        ConfluenceSpace as _Space,
    )
    from loom.toolsets.confluence.models import (
        ConfluenceUser as _User,
    )
    from loom.toolsets.confluence.models import (
        CreatedPage as _Created,
    )
    from loom.toolsets.confluence.models import (
        PageBody as _Body,
    )
    from loom.toolsets.confluence.models import (
        SearchResult as _Search,
    )

    def _fields(model: type[BaseModel]) -> str:
        """The field names a model declares, for the agent-facing docs.

        ``type[BaseModel]`` rather than ``type``: the bare form accepts any
        class and then calls a Pydantic classmethod on it, so passing the wrong
        thing is an AttributeError from inside a docs generator rather than an
        error where the mistake was made.
        """
        props = model.model_json_schema().get("properties", {})
        return ", ".join(props)

    return f"""\
## Available Confluence Tools

Import: from loom.toolsets.confluence.tools import <tool_name>
Usage:  result = await ctx.step(<tool_name>, arg1, arg2, ...)

Credentials are read automatically from env vars:
  CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN

All tools return typed Pydantic models. Use attribute access.

### Tools

confluence_search_pages(cql: str, limit: int = 20) -> list[SearchResult]
  Search content using CQL (Confluence Query Language).
  SearchResult fields: {_fields(_Search)}
  Examples:
    results = await ctx.step(confluence_search_pages, \
"type = page AND title ~ 'Design'")
    results = await ctx.step(confluence_search_pages, \
"space = DEV AND lastModified > now('-7d')", 10)

confluence_get_page(page_id: str) -> ConfluencePage
  Fetch a page by its ID.
  ConfluencePage fields: {_fields(_Page)}
    page = await ctx.step(confluence_get_page, "12345")
    print(page.title, page.status)

confluence_create_page(space_id, title, body, \
parent_id=None) -> CreatedPage
  Create a new page. body is HTML storage format.
  CreatedPage fields: {_fields(_Created)}
    created = await ctx.step(confluence_create_page, \
"65540", "My Page", "<p>Content here</p>")
    print(created.id, created.url)

confluence_update_page(page_id, title, body, \
version=None) -> CreatedPage
  Update an existing page. Version is auto-incremented if omitted.
    updated = await ctx.step(confluence_update_page, \
"12345", "Updated Title", "<p>New content</p>")

confluence_delete_page(page_id: str) -> str
  Delete a page. Returns confirmation string.
    await ctx.step(confluence_delete_page, "12345")

confluence_get_page_body(page_id: str) -> PageBody
  Fetch the body content of a page.
  PageBody fields: {_fields(_Body)}
    body = await ctx.step(confluence_get_page_body, "12345")
    print(body.body)  # HTML content

confluence_get_page_comments(page_id, limit=25) -> list[ConfluenceComment]
  Fetch comments on a page.
  ConfluenceComment fields: {_fields(_Comment)}

confluence_add_comment(page_id: str, comment: str) -> ConfluenceComment
  Add a comment to a page.
    c = await ctx.step(confluence_add_comment, "12345", "Looks good!")

confluence_list_spaces(limit: int = 25) -> list[ConfluenceSpace]
  List all accessible spaces.
  ConfluenceSpace fields: {_fields(_Space)}
    spaces = await ctx.step(confluence_list_spaces)
    for s in spaces: print(s.key, s.name)

confluence_get_space(space_id: str) -> ConfluenceSpace
  Get a space by ID.

confluence_get_myself() -> ConfluenceUser
  Get authenticated user.
  ConfluenceUser fields: {_fields(_User)}
    me = await ctx.step(confluence_get_myself)
    print(me.display_name)
"""


CONFLUENCE_TOOL_DOCS: str = _build_tool_docs()
