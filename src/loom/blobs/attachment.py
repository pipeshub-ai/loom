"""Attachments — files moving through a workflow.

A step that fetches a PDF should not return naked ``bytes``. The filename and
content type are part of the value, and every downstream step otherwise has to
be told out of band what it is holding.

An :class:`Attachment` carries the bytes *or* a ``blob:`` reference to them,
alongside the metadata. Large payloads are offloaded automatically by the
journal's size check, so the same object works for a 2 KB CSV and a 200 MB
video — the difference is only whether the bytes travel inline.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_serializer, field_validator


class Attachment(BaseModel):
    """A named binary payload with its content type.

    Exactly one of ``data`` or ``ref`` is populated: ``data`` while the bytes
    are in hand, ``ref`` once they have been written to blob storage.
    """

    filename: str
    mime: str = "application/octet-stream"
    size: int = 0
    """Bytes. Set even when the payload lives behind a ``ref``, so a caller can
    decide whether to download without downloading."""
    data: bytes | None = Field(default=None, repr=False)
    """Inline content. ``None`` when the payload lives in blob storage."""
    ref: str | None = None
    """A ``blob:<sha256>`` reference, once :meth:`offload` has run."""
    sha256: str = ""
    """Content digest, stable across inline and offloaded forms."""
    metadata: dict[str, Any] = Field(default_factory=dict)

    # -- construction --------------------------------------------------------

    @classmethod
    def from_bytes(
        cls,
        filename: str,
        data: bytes,
        *,
        mime: str | None = None,
        **metadata: Any,
    ) -> Attachment:
        """Build an attachment from bytes, guessing the type from the name."""
        return cls(
            filename=filename,
            mime=mime or _guess_mime(filename),
            size=len(data),
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            metadata=metadata,
        )

    @classmethod
    def from_path(
        cls, path: str | Path, *, mime: str | None = None, **metadata: Any
    ) -> Attachment:
        """Read a file from disk into an attachment."""
        resolved = Path(path)
        return cls.from_bytes(
            resolved.name, resolved.read_bytes(), mime=mime, **metadata
        )

    @classmethod
    def from_text(
        cls,
        filename: str,
        text: str,
        *,
        mime: str | None = None,
        encoding: str = "utf-8",
        **metadata: Any,
    ) -> Attachment:
        """Build an attachment from text."""
        return cls.from_bytes(
            filename,
            text.encode(encoding),
            mime=mime or _guess_mime(filename) or "text/plain",
            **metadata,
        )

    # -- access --------------------------------------------------------------

    @property
    def is_offloaded(self) -> bool:
        """True when the bytes live in blob storage rather than on this object."""
        return self.data is None and self.ref is not None

    async def read(self, blobs: Any | None = None) -> bytes:
        """Return the content, fetching from blob storage when offloaded.

        Raises :class:`ValueError` when the payload is offloaded and no blob
        service was supplied — better than returning empty bytes and letting a
        downstream step write a zero-length file.
        """
        if self.data is not None:
            return self.data
        if self.ref is None:
            return b""
        if blobs is None:
            raise ValueError(
                f"attachment {self.filename!r} is stored at {self.ref} but no blob "
                "service was given; pass blobs=runtime.blobs"
            )
        return await blobs.load(self.ref)

    def text(self, encoding: str = "utf-8") -> str:
        """Decode inline content as text. Only valid before offloading."""
        if self.data is None:
            raise ValueError(
                f"attachment {self.filename!r} has no inline data; "
                "await attachment.read(blobs) first"
            )
        return self.data.decode(encoding)

    # -- blob lifecycle ------------------------------------------------------

    async def offload(self, blobs: Any) -> Attachment:
        """Write the content to blob storage and return a reference-only copy.

        The original is left untouched, so a caller holding the bytes keeps
        them.
        """
        if self.is_offloaded or self.data is None:
            return self
        ref = await blobs.store(self.data, self.mime)
        return self.model_copy(update={"data": None, "ref": ref})

    # -- serialization -------------------------------------------------------

    @field_serializer("data")
    def _dump_data(self, value: bytes | None) -> str | None:
        """Base64 so the journal stays JSON, and round-trips byte-exact."""
        return None if value is None else base64.b64encode(value).decode("ascii")

    @field_validator("data", mode="before")
    @classmethod
    def _load_data(cls, value: Any) -> Any:
        if isinstance(value, str):
            return base64.b64decode(value)
        return value


def _guess_mime(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"
