"""OneNote over Microsoft Graph — pure httpx, no vendor SDK.

**A page's content is an HTML document, not JSON.** That single fact shapes the
whole client:

* reading content is ``GET /pages/{id}/content`` returning ``text/html``, so it
  comes back as a ``str`` rather than a model;
* creating a page ``POST``s an HTML document with ``Content-Type: text/html``,
  and the document's ``<title>`` *is* the page title — there is no title field;
* updating is a ``PATCH`` carrying an array of ``{target, action, content}``
  commands, not a replacement document.

A caller handed only "post some HTML" would reliably produce a page with an
empty title and one unstyled line, so :meth:`OneNoteClient.create_page` builds
the document from a title and a body, and accepts raw HTML only when asked.

**App-only support is contradictory in Microsoft's own reference.** The API
overview states "The Microsoft Graph OneNote API doesn't support app-only
authentication", while the per-operation pages list an ``Notes.ReadWrite.All``
application permission. This client does not refuse app-only outright — that
would break callers for whom it works — but every path here is a user path, so
the shared ``user_root`` refusal already covers the case that certainly cannot
work: app-only with nobody named.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loom.toolsets.microsoft.auth import (
    GRAPH_BASE_URL,
    MicrosoftAuth,
    get_default_auth,
)
from loom.toolsets.microsoft.errors import GraphPermanentError
from loom.toolsets.microsoft.http import GraphSession
from loom.toolsets.microsoft.onenote.models import (
    Notebook,
    OneNotePage,
    OneNoteSection,
    SectionGroup,
)
from loom.toolsets.microsoft.scope import user_root
from loom.toolsets.pagination import Results

if TYPE_CHECKING:
    import httpx

__all__ = ["OneNoteClient"]

#: Actions ``PATCH /pages/{id}/content`` accepts. Validated rather than passed
#: through: an unrecognised action is a 400 whose message names the enum and not
#: the argument, which reads as a malformed request rather than a typo.
_PATCH_ACTIONS = frozenset({"append", "prepend", "insert", "replace", "delete"})


class OneNoteClient:
    """Notebooks, sections, and pages in OneNote."""

    def __init__(
        self,
        auth: MicrosoftAuth | None = None,
        *,
        base_url: str = GRAPH_BASE_URL,
        user_id: str = "",
        site_id: str = "",
        group_id: str = "",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = auth or get_default_auth()
        self._user_id = user_id
        self._site_id = site_id
        self._group_id = group_id
        self._session = GraphSession(
            self._auth, base_url, timeout=timeout, transport=transport
        )

    # -- addressing ----------------------------------------------------------

    def _root(self) -> str:
        """The OneNote container: a user, a SharePoint site, or a group.

        A site or group notebook is addressable with no user involved, so those
        are checked first and work under an app-only token; a personal notebook
        reduces to "whose", and takes the shared refusal.
        """
        if self._site_id:
            return f"/sites/{self._site_id}/onenote"
        if self._group_id:
            return f"/groups/{quote(self._group_id, safe='')}/onenote"
        return (
            user_root(
                self._auth,
                self._user_id,
                workload="a set of notebooks",
                env_hint="MS_ONENOTE_USER",
                alternatives=(
                    "pass site_id= or group_id= for a team or group notebook"
                ),
            )
            + "/onenote"
        )

    # -- notebooks and sections ----------------------------------------------

    async def list_notebooks(self, *, limit: int = 100) -> Results[Notebook]:
        return await self._session.paginate(
            f"{self._root()}/notebooks",
            limit=limit,
            page_size=100,
            row=Notebook.from_api,
        )

    async def get_notebook(self, notebook_id: str) -> Notebook:
        return Notebook.from_api(
            await self._session.get(
                f"{self._root()}/notebooks/{quote(notebook_id, safe='')}"
            )
        )

    async def list_sections(
        self, notebook_id: str = "", *, limit: int = 100
    ) -> Results[OneNoteSection]:
        """List a notebook's sections, or every section the user can reach.

        Passing no notebook is the useful default for finding somewhere to
        write: OneNote returns each section with its parent notebook attached,
        so one call answers "where could this go".
        """
        path = (
            f"{self._root()}/notebooks/{quote(notebook_id, safe='')}/sections"
            if notebook_id
            else f"{self._root()}/sections"
        )
        return await self._session.paginate(
            path,
            limit=limit,
            # Without this the parent notebook is absent and a section listed
            # across notebooks cannot be told from a same-named one elsewhere.
            params={"$expand": "parentNotebook"},
            page_size=100,
            row=OneNoteSection.from_api,
        )

    async def list_section_groups(
        self, notebook_id: str = "", *, limit: int = 100
    ) -> Results[SectionGroup]:
        path = (
            f"{self._root()}/notebooks/{quote(notebook_id, safe='')}/sectionGroups"
            if notebook_id
            else f"{self._root()}/sectionGroups"
        )
        return await self._session.paginate(
            path, limit=limit, page_size=100, row=SectionGroup.from_api
        )

    async def create_section(
        self, notebook_id: str, section_name: str
    ) -> OneNoteSection:
        return OneNoteSection.from_api(
            await self._session.post(
                f"{self._root()}/notebooks/{quote(notebook_id, safe='')}/sections",
                json={"displayName": section_name},
            )
            or {}
        )

    # -- pages ---------------------------------------------------------------

    async def list_pages(
        self, section_id: str = "", *, limit: int = 100, order_by: str = ""
    ) -> Results[OneNotePage]:
        path = (
            f"{self._root()}/sections/{quote(section_id, safe='')}/pages"
            if section_id
            else f"{self._root()}/pages"
        )
        params: dict[str, Any] = {"$expand": "parentSection,parentNotebook"}
        if order_by:
            params["$orderby"] = order_by
        return await self._session.paginate(
            path, limit=limit, params=params, page_size=100, row=OneNotePage.from_api
        )

    async def search_pages(self, query: str, *, limit: int = 50) -> Results[OneNotePage]:
        """Search page titles and content across everything the user can reach."""
        return await self._session.paginate(
            f"{self._root()}/pages",
            limit=limit,
            params={
                "$search": query,
                "$expand": "parentSection,parentNotebook",
            },
            page_size=100,
            row=OneNotePage.from_api,
        )

    async def get_page(self, page_id: str) -> OneNotePage:
        return OneNotePage.from_api(
            await self._session.get(
                f"{self._root()}/pages/{quote(page_id, safe='')}",
                **{"$expand": "parentSection,parentNotebook"},
            )
        )

    async def get_page_content(
        self, page_id: str, *, include_ids: bool = False
    ) -> str:
        """Fetch a page's HTML.

        ``include_ids`` adds ``data-id`` attributes to the elements, which is
        what makes a targeted :meth:`patch_page` possible — without them the
        only addressable targets are ``body`` and ``title``.
        """
        raw = await self._session.download(
            f"{self._root()}/pages/{quote(page_id, safe='')}/content",
            includeIDs="true" if include_ids else None,
        )
        return raw.decode("utf-8", errors="replace")

    async def create_page(
        self,
        section_id: str,
        title: str,
        body_html: str = "",
        *,
        full_html: str = "",
        created: str = "",
    ) -> OneNotePage:
        """Create a page from a title and a body fragment.

        The document is assembled here because OneNote takes a *whole* HTML
        document and reads the page title out of its ``<title>``. A caller who
        posts a bare fragment gets a page with an empty title and no structure,
        and nothing reports a problem.

        ``full_html`` bypasses the assembly for a caller who has a complete
        document already; ``title`` and ``body_html`` are then ignored.
        """
        document = full_html or _page_document(title, body_html, created)
        raw = await self._session.send_bytes(
            "POST",
            f"{self._root()}/sections/{quote(section_id, safe='')}/pages",
            content=document.encode("utf-8"),
            content_type="text/html",
        )
        return OneNotePage.from_api(raw or {})

    async def patch_page(
        self,
        page_id: str,
        content: str,
        *,
        target: str = "body",
        action: str = "append",
        position: str = "",
    ) -> bool:
        """Change part of a page.

        Args mirror OneNote's own command shape, because inventing a friendlier
        one would hide which targets are addressable: ``body``, ``title``, or a
        ``data-id`` obtained from :meth:`get_page_content` with
        ``include_ids=True``.
        """
        if action not in _PATCH_ACTIONS:
            raise GraphPermanentError(
                f"action must be one of {sorted(_PATCH_ACTIONS)}, not {action!r}.",
                status=0,
                code="invalidPatchAction",
            )
        command: dict[str, Any] = {
            "target": target,
            "action": action,
            "content": content,
        }
        if position:
            command["position"] = position
        await self._session.request(
            "PATCH",
            f"{self._root()}/pages/{quote(page_id, safe='')}/content",
            json=[command],
        )
        return True

    async def delete_page(self, page_id: str) -> bool:
        await self._session.delete(
            f"{self._root()}/pages/{quote(page_id, safe='')}"
        )
        return True


def _page_document(title: str, body_html: str, created: str) -> str:
    """Assemble the HTML document OneNote expects from a page create.

    The shape is the reference's own: a document whose ``<title>`` becomes the
    page title and whose ``<body>`` becomes its content.
    """
    meta = f'\n    <meta name="created" content="{_escape(created)}" />' if created else ""
    return (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <head>\n"
        f"    <title>{_escape(title)}</title>{meta}\n"
        "  </head>\n"
        f"  <body>{body_html}</body>\n"
        "</html>"
    )


def _escape(value: str) -> str:
    """Escape text destined for an HTML *attribute or element* position.

    Only the title and the created timestamp go through this. The body is
    passed through untouched, because the caller is supplying markup on
    purpose — escaping it would put visible tags on the page.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Process-wide default
# ---------------------------------------------------------------------------


