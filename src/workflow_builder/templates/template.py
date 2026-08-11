"""Workflow template system — export, parameterize, instantiate.

Templates are workflow files with placeholder parameters that can be
filled in at instantiation time, enabling quick-start patterns for
common use cases.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class TemplateParam(BaseModel):
    """A single parameter placeholder within a template."""

    name: str
    description: str = ""
    type: str = "string"
    """Parameter type: string, int, bool, or enum."""
    default: Any = None
    required: bool = True
    enum_values: list[str] = Field(default_factory=list)


class TemplateManifest(BaseModel):
    """A complete workflow template with metadata and parameterized source."""

    id: str
    name: str
    description: str = ""
    category: str = "general"
    """Category: crm, billing, devops, data, or general."""
    tags: list[str] = Field(default_factory=list)
    parameters: list[TemplateParam] = Field(default_factory=list)
    source: str = ""
    """Template source code with ``{{ param_name }}`` placeholders."""
    toolsets: list[str] = Field(default_factory=list)
    version: str = "1.0.0"


class TemplateError(Exception):
    """Raised when template validation or instantiation fails."""


class TemplateEngine:
    """Export and instantiate workflow templates."""

    def __init__(self) -> None:
        self._registry: dict[str, TemplateManifest] = {}

    def register(self, template: TemplateManifest) -> None:
        """Add a template to the registry."""
        self._registry[template.id] = template

    def unregister(self, template_id: str) -> None:
        """Remove a template from the registry."""
        self._registry.pop(template_id, None)

    def get(self, template_id: str) -> TemplateManifest | None:
        """Look up a template by id."""
        return self._registry.get(template_id)

    def list_templates(self, category: str | None = None) -> list[TemplateManifest]:
        """Return all templates, optionally filtered by category."""
        templates = list(self._registry.values())
        if category is not None:
            templates = [t for t in templates if t.category == category]
        return templates

    def instantiate(self, template: TemplateManifest, params: dict[str, Any]) -> str:
        """Fill in template placeholders and return the resulting code.

        Raises:
            TemplateError: If required parameters are missing or enum
                values are invalid.
        """
        # Validate required params
        for p in template.parameters:
            if p.required and p.name not in params and p.default is None:
                msg = f"Missing required parameter: {p.name}"
                raise TemplateError(msg)

        # Validate enum params
        for p in template.parameters:
            if p.enum_values and p.name in params and str(params[p.name]) not in p.enum_values:
                msg = (
                    f"Invalid value for parameter '{p.name}': "
                    f"{params[p.name]!r} (must be one of {p.enum_values})"
                )
                raise TemplateError(msg)

        # Build effective values (params + defaults)
        values: dict[str, str] = {}
        for p in template.parameters:
            if p.name in params:
                values[p.name] = str(params[p.name])
            elif p.default is not None:
                values[p.name] = str(p.default)

        # Replace placeholders
        result = template.source
        for name, value in values.items():
            result = re.sub(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", value, result)

        return result
