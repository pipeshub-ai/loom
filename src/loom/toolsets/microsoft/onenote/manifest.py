"""OneNote ToolsetManifest — pure metadata, no client import."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from loom.toolsets.manifest import EffectClass, OperationSpec, ToolsetManifest
from loom.toolsets.microsoft.onenote.models import (
    Notebook,
    OneNotePage,
    OneNoteSection,
    SectionGroup,
)


def _array(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "array", "items": model.model_json_schema()}


ONENOTE_MANIFEST = ToolsetManifest(
    id="onenote",
    version="1.0.0",
    summary="OneNote — notebooks, sections, and pages.",
    description=(
        "Microsoft Graph v1.0. Browse and search notebooks, sections and pages; "
        "read and write page content. A PAGE'S CONTENT IS HTML, not JSON: "
        "onenote_get_page_content returns a markup string, and "
        "onenote_create_page writes an HTML document whose <title> becomes the "
        "page title — there is no title field to set separately, which is why "
        "the create tool takes title and body_html and assembles the document. "
        "Changing part of a page uses target/action commands, and targeting "
        "anything but 'body' or 'title' needs data-ids from "
        "onenote_get_page_content(include_ids=True). Set MS_ONENOTE_USER, or "
        "MS_ONENOTE_SITE / MS_ONENOTE_GROUP for a team notebook. Note that "
        "Microsoft's OneNote overview says the API does not support app-only "
        "authentication while its per-operation pages list an application "
        "permission; the two disagree, so treat app-only as unsupported until "
        "verified in your tenant."
    ),
    base_url="https://graph.microsoft.com/v1.0",
    auth={
        "type": "oauth2",
        "fields": [
            "MS_TENANT_ID",
            "MS_CLIENT_ID",
            "MS_CLIENT_SECRET",
            "MS_REFRESH_TOKEN",
            "MS_GRAPH_ACCESS_TOKEN",
            # Read by the shared auth layer, so declared here: the Azure SDK
            # trio is what a host already has in its environment, and
            # MS_AUTHORITY_HOST is the only way to reach a national cloud.
            # Omitting them told `loom toolset` users to set MS_* variables
            # they did not need.
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "MS_AUTHORITY_HOST",
            "MS_ONENOTE_USER",
            "MS_ONENOTE_SITE",
            "MS_ONENOTE_GROUP",
        ],
    },
    tools_module="loom.toolsets.microsoft.onenote.tools",
    egress_hosts=["graph.microsoft.com", "login.microsoftonline.com"],
    groups={
        "notebooks": [
            OperationSpec(
                id="notebooks.list",
                function="onenote_list_notebooks",
                summary="List the notebooks this account can reach.",
                description="Resolve a notebook before writing into it.",
                resolves="notebook",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(Notebook),
            ),
            OperationSpec(
                id="notebooks.get",
                function="onenote_get_notebook",
                summary="Fetch one notebook by id.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=Notebook.model_json_schema(),
            ),
        ],
        "sections": [
            OperationSpec(
                id="sections.list",
                function="onenote_list_sections",
                summary="List sections, in one notebook or across all.",
                description=(
                    "Omitting the notebook lists every reachable section with "
                    "its parent notebook attached — one call to answer 'where "
                    "could this page go'. Resolve a section before creating a "
                    "page: pages are written to a section id."
                ),
                resolves="section",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(OneNoteSection),
            ),
            OperationSpec(
                id="sections.list_groups",
                function="onenote_list_section_groups",
                summary="List section groups, the optional folder level.",
                description="Most tenants have none, so empty is the normal case.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(SectionGroup),
            ),
            OperationSpec(
                id="sections.create",
                function="onenote_create_section",
                summary="Create a section in a notebook.",
                description="Not retried: a retry leaves two sections.",
                effect=EffectClass.WRITE,
                output_schema=OneNoteSection.model_json_schema(),
            ),
        ],
        "pages": [
            OperationSpec(
                id="pages.list",
                function="onenote_list_pages",
                summary="List pages in a section, or across all.",
                description="Metadata only; content is fetched separately.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(OneNotePage),
            ),
            OperationSpec(
                id="pages.search",
                function="onenote_search_pages",
                summary="Search page titles and content.",
                effect=EffectClass.READ,
                idempotent=True,
                pagination=True,
                output_schema=_array(OneNotePage),
            ),
            OperationSpec(
                id="pages.get",
                function="onenote_get_page",
                summary="Fetch one page's metadata.",
                effect=EffectClass.READ,
                idempotent=True,
                output_schema=OneNotePage.model_json_schema(),
            ),
            OperationSpec(
                id="pages.get_content",
                function="onenote_get_page_content",
                summary="Fetch a page's HTML content as a string.",
                description=(
                    "include_ids=True adds data-ids, which is what makes a "
                    "targeted edit possible later."
                ),
                effect=EffectClass.READ,
                idempotent=True,
                output_schema={"type": "string"},
            ),
            OperationSpec(
                id="pages.create",
                function="onenote_create_page",
                summary="Create a page from a title and an HTML body.",
                description=(
                    "The document is assembled for you because the page title "
                    "comes from its <title>; posting a bare fragment makes an "
                    "untitled page and reports no error. Not retried."
                ),
                effect=EffectClass.WRITE,
                output_schema=OneNotePage.model_json_schema(),
            ),
            OperationSpec(
                id="pages.patch",
                function="onenote_append_to_page",
                summary="Append to, or otherwise change, part of a page.",
                description=(
                    "target is 'body', 'title', or a data-id from "
                    "onenote_get_page_content(include_ids=True)."
                ),
                effect=EffectClass.WRITE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
            OperationSpec(
                id="pages.delete",
                function="onenote_delete_page",
                summary="Delete a page.",
                effect=EffectClass.DESTRUCTIVE,
                idempotent=True,
                output_schema={"type": "boolean"},
            ),
        ],
    },
)
