"""OneNote step functions for use inside LOOM workflows.

    from loom.toolsets.microsoft.onenote.tools import onenote_create_page

    page = await ctx.step(onenote_create_page, section_id=s,
                          title="Standup 2026-01-05",
                          body_html="<p>Shipped the importer.</p>")

**A page's content is HTML.** ``onenote_get_page_content`` returns a string of
markup, not a model, and ``onenote_create_page`` writes one. The page's title
comes from the HTML document's ``<title>`` — there is no title field to set
afterwards — which is why the create tool takes a title and a body separately
and assembles the document for you.

**Which notebooks.** Under delegated credentials these act on the signed-in
person's notebooks. Under an app-only token there is no signed-in person, so set
``MS_ONENOTE_USER``, or ``MS_ONENOTE_SITE`` / ``MS_ONENOTE_GROUP`` for a team or
group notebook. Note also that Microsoft's OneNote overview states the API does
not support app-only authentication at all, while its per-operation pages list
an application permission — the two disagree, so treat app-only OneNote as
unsupported until you have seen it work in your tenant.

Retries are per operation. Reads retry; **creating a page or a section does
not**, because OneNote has no idempotency key and a retry after a timeout
leaves two pages.
"""

from __future__ import annotations

from loom import Retry, step
from loom.toolsets.microsoft.onenote.models import (
    Notebook,
    OneNotePage,
    OneNoteSection,
    SectionGroup,
)
from loom.toolsets.pagination import Results

_READ = Retry(max_attempts=3, initial_delay=1.0)
_IDEMPOTENT_WRITE = Retry(max_attempts=2, initial_delay=1.0)
_UNSAFE_WRITE = Retry(max_attempts=1)


# -- notebooks and sections --------------------------------------------------


@step(retry=_READ)
async def onenote_list_notebooks(limit: int = 100) -> Results[Notebook]:
    """List the notebooks this account can reach.

    Resolve a notebook here before writing into it.

    Args:
        limit: Maximum notebooks. Defaults to 100.

    Returns:
        Results of Notebook, with ``web_url`` to open each in the browser.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().list_notebooks(limit=limit)


@step(retry=_READ)
async def onenote_get_notebook(notebook_id: str) -> Notebook:
    """Fetch one notebook by id.

    Args:
        notebook_id: The notebook's id.

    Returns:
        Notebook with display name, timestamps, and open-in links.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().get_notebook(notebook_id)


@step(retry=_READ)
async def onenote_list_sections(
    notebook_id: str = "", limit: int = 100
) -> Results[OneNoteSection]:
    """List sections — in one notebook, or everywhere this account can reach.

    Passing no notebook is the useful default when looking for somewhere to
    write: each section comes back with its parent notebook attached, so one
    call answers "where could this go".

    Args:
        notebook_id: Restrict to one notebook. Omit for every reachable section.
        limit: Maximum sections. Defaults to 100.

    Returns:
        Results of OneNoteSection, each carrying ``notebook_name`` — without it
        two same-named sections in different notebooks are indistinguishable.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().list_sections(notebook_id, limit=limit)


@step(retry=_READ)
async def onenote_list_section_groups(
    notebook_id: str = "", limit: int = 100
) -> Results[SectionGroup]:
    """List section groups, the optional folder level between notebook and section.

    Most tenants never create one, so an empty result is the common case rather
    than a sign of a permissions problem.

    Args:
        notebook_id: Restrict to one notebook. Omit for all.
        limit: Maximum section groups. Defaults to 100.

    Returns:
        Results of SectionGroup. They nest, so ``section_groups_url`` is how to
        walk further down.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().list_section_groups(notebook_id, limit=limit)


@step(retry=_UNSAFE_WRITE)
async def onenote_create_section(notebook_id: str, section_name: str) -> OneNoteSection:
    """Create a section in a notebook.

    Not retried: OneNote has no idempotency key, so a retry after a timeout
    leaves two sections with the same name.

    Args:
        notebook_id: The notebook to create the section in.
        section_name: Name for the new section.

    Returns:
        The created OneNoteSection, including the id pages are written to.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().create_section(notebook_id, section_name)


# -- pages -------------------------------------------------------------------


@step(retry=_READ)
async def onenote_list_pages(
    section_id: str = "", limit: int = 100, order_by: str = ""
) -> Results[OneNotePage]:
    """List pages in a section, or across everything this account can reach.

    Args:
        section_id: Restrict to one section. Omit for all pages.
        limit: Maximum pages. Defaults to 100.
        order_by: OData ``$orderby``, e.g. ``"lastModifiedDateTime desc"``.

    Returns:
        Results of OneNotePage — metadata only. Fetch the HTML separately with
        ``onenote_get_page_content``.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().list_pages(
        section_id, limit=limit, order_by=order_by
    )


@step(retry=_READ)
async def onenote_search_pages(query: str, limit: int = 50) -> Results[OneNotePage]:
    """Search page titles and content.

    Args:
        query: Text to search for.
        limit: Maximum pages. Defaults to 50.

    Returns:
        Results of OneNotePage, each with its section and notebook named.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().search_pages(query, limit=limit)


@step(retry=_READ)
async def onenote_get_page(page_id: str) -> OneNotePage:
    """Fetch one page's metadata.

    Args:
        page_id: The page's id.

    Returns:
        OneNotePage with title, timestamps, and its section and notebook. The
        content is not included — use ``onenote_get_page_content``.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().get_page(page_id)


@step(retry=_READ)
async def onenote_get_page_content(page_id: str, include_ids: bool = False) -> str:
    """Fetch a page's HTML content.

    Args:
        page_id: The page to read.
        include_ids: Add ``data-id`` attributes to the elements. Needed if you
            intend to change part of the page afterwards — without them the
            only targets ``onenote_append_to_page`` can address are ``body``
            and ``title``.

    Returns:
        The page as an HTML document string.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().get_page_content(
        page_id, include_ids=include_ids
    )


@step(retry=_UNSAFE_WRITE)
async def onenote_create_page(
    section_id: str,
    title: str,
    body_html: str = "",
    full_html: str = "",
    created: str = "",
) -> OneNotePage:
    """Create a page in a section.

    The HTML document OneNote requires is assembled from ``title`` and
    ``body_html``, because the page's title comes from the document's
    ``<title>`` and posting a bare fragment produces an untitled page with no
    structure and no error.

    Not retried: a retry after a timeout creates a second page.

    Args:
        section_id: The section to create the page in.
        title: Page title. Becomes the document's ``<title>``.
        body_html: Page content as HTML, e.g. ``"<p>Notes.</p>"``. Passed
            through unescaped, because it is markup on purpose.
        full_html: A complete HTML document to post as-is. When given,
            ``title`` and ``body_html`` are ignored.
        created: ISO-8601 creation timestamp to record on the page.

    Returns:
        The created OneNotePage, including its id and web URL.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().create_page(
        section_id, title, body_html, full_html=full_html, created=created
    )


@step(retry=_IDEMPOTENT_WRITE)
async def onenote_append_to_page(
    page_id: str,
    content: str,
    target: str = "body",
    action: str = "append",
    position: str = "",
) -> bool:
    """Change part of a page's content.

    Retried once: appending the same content to the same target twice is
    visible but recoverable, and the far more common failure is a timeout on a
    change that did land.

    Args:
        page_id: The page to change.
        content: HTML to apply.
        target: ``"body"``, ``"title"``, or a ``data-id`` from
            ``onenote_get_page_content(include_ids=True)``.
        action: ``"append"`` (default), ``"prepend"``, ``"insert"``,
            ``"replace"``, or ``"delete"``.
        position: ``"before"`` or ``"after"``, for ``insert``.

    Returns:
        True when the change was accepted.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().patch_page(
        page_id, content, target=target, action=action, position=position
    )


@step(retry=_IDEMPOTENT_WRITE)
async def onenote_delete_page(page_id: str) -> bool:
    """Delete a page.

    Retried once: deleting an already-deleted page is a 404, not a second
    deletion.

    Args:
        page_id: The page to delete.

    Returns:
        True when the delete was accepted.
    """
    from loom.toolsets.microsoft.onenote.client import get_default_client

    return await get_default_client().delete_page(page_id)
