"""Confluence ToolsetManifest for registration with the global catalog.

Output schemas are derived from the Pydantic models in ``models.py``.
"""

from __future__ import annotations

from workflow_builder.toolsets.confluence.models import (
    ConfluenceComment,
    ConfluencePage,
    ConfluenceSpace,
    ConfluenceUser,
    CreatedPage,
    PageBody,
    SearchResult,
)
from workflow_builder.toolsets.manifest import (
    EffectClass,
    OperationSpec,
    ToolsetManifest,
)

_page_schema = ConfluencePage.model_json_schema()
_search_list_schema = {
    "type": "array",
    "items": SearchResult.model_json_schema(),
}

CONFLUENCE_MANIFEST = ToolsetManifest(
    id="confluence",
    version="1.0.0",
    summary=(
        "Confluence Cloud — search, create, update, delete pages; "
        "spaces; comments."
    ),
    description=(
        "Confluence Cloud REST API v2 integration. Supports CQL "
        "search, page CRUD, body retrieval, comments, space listing, "
        "and authenticated user profile."
    ),
    base_url="https://<org>.atlassian.net",
    auth={
        "type": "basic",
        "fields": [
            "CONFLUENCE_URL",
            "CONFLUENCE_EMAIL",
            "CONFLUENCE_API_TOKEN",
        ],
    },
    tools_module="workflow_builder.toolsets.confluence.tools",
    egress_hosts=["*.atlassian.net"],
    groups={
        "pages": [
            OperationSpec(
                id="pages.search",
                function="confluence_search_pages",
                summary="Search content with a CQL query.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "cql": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["cql"],
                },
                output_schema=_search_list_schema,
                pagination=True,
                idempotent=True,
            ),
            OperationSpec(
                id="pages.get",
                function="confluence_get_page",
                summary="Fetch a page by ID.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                    },
                    "required": ["page_id"],
                },
                output_schema=_page_schema,
                idempotent=True,
            ),
            OperationSpec(
                id="pages.create",
                function="confluence_create_page",
                summary="Create a new page.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "space_id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "parent_id": {"type": "string"},
                    },
                    "required": ["space_id", "title", "body"],
                },
                output_schema=CreatedPage.model_json_schema(),
                scopes=["write:confluence-content"],
            ),
            OperationSpec(
                id="pages.update",
                function="confluence_update_page",
                summary="Update an existing page.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "version": {"type": "integer"},
                    },
                    "required": ["page_id", "title", "body"],
                },
                output_schema=CreatedPage.model_json_schema(),
                scopes=["write:confluence-content"],
            ),
            OperationSpec(
                id="pages.delete",
                function="confluence_delete_page",
                summary="Delete a page.",
                effect=EffectClass.DESTRUCTIVE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                    },
                    "required": ["page_id"],
                },
                scopes=["write:confluence-content"],
            ),
            OperationSpec(
                id="pages.get_body",
                function="confluence_get_page_body",
                summary="Fetch the body content of a page.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                    },
                    "required": ["page_id"],
                },
                output_schema=PageBody.model_json_schema(),
                idempotent=True,
            ),
            OperationSpec(
                id="pages.get_comments",
                function="confluence_get_page_comments",
                summary="Fetch comments on a page.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 25},
                    },
                    "required": ["page_id"],
                },
                output_schema={
                    "type": "array",
                    "items": ConfluenceComment.model_json_schema(),
                },
                idempotent=True,
            ),
            OperationSpec(
                id="pages.add_comment",
                function="confluence_add_comment",
                summary="Add a comment to a page.",
                effect=EffectClass.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {
                        "page_id": {"type": "string"},
                        "comment": {"type": "string"},
                    },
                    "required": ["page_id", "comment"],
                },
                output_schema=ConfluenceComment.model_json_schema(),
                scopes=["write:confluence-content"],
            ),
        ],
        "spaces": [
            OperationSpec(
                id="spaces.list",
                function="confluence_list_spaces",
                summary="List all accessible spaces.",
                effect=EffectClass.READ,
                output_schema={
                    "type": "array",
                    "items": ConfluenceSpace.model_json_schema(),
                },
                idempotent=True,
            ),
            OperationSpec(
                id="spaces.get",
                function="confluence_get_space",
                summary="Get a space by ID.",
                effect=EffectClass.READ,
                input_schema={
                    "type": "object",
                    "properties": {
                        "space_id": {"type": "string"},
                    },
                    "required": ["space_id"],
                },
                output_schema=ConfluenceSpace.model_json_schema(),
                idempotent=True,
            ),
        ],
        "users": [
            OperationSpec(
                id="users.myself",
                function="confluence_get_myself",
                summary="Get the authenticated user's profile.",
                effect=EffectClass.READ,
                output_schema=ConfluenceUser.model_json_schema(),
                idempotent=True,
            ),
        ],
    },
)
