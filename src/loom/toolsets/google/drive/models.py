"""Typed response models for the Google Drive toolset.

Drive returns a file as a bag of ~60 optional fields, none of which arrive
unless the request asks for them by name. These models are the subset a workflow
actually branches on, and the client asks for exactly that subset — so the
contract here and the ``fields`` mask on the wire are the same list, written
once.

Two shapes are worth knowing before writing against them.

A **folder is a file**, with the MIME type ``application/vnd.google-apps.folder``
and no content. :attr:`DriveFile.is_folder` says so directly rather than leaving
every caller to compare that string, which is the sort of constant that gets
mistyped once and then silently matches nothing.

A **Google-native document has no bytes**. A Doc, Sheet, or Slide is not stored
as a file; it is exported on demand into a format you choose. Downloading one
fails with a 403 that says ``fileNotDownloadable``, so :attr:`DriveFile.is_google_doc`
is here to let a caller route to an export *before* making a request that cannot
work.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FOLDER_MIME",
    "DriveFile",
    "DrivePermission",
    "SharedDrive",
]

#: The MIME type Drive gives a folder. A folder is an ordinary file with this
#: type and no content — there is no separate folder resource.
FOLDER_MIME = "application/vnd.google-apps.folder"

#: Google-native types are prefixed this way and hold no downloadable bytes.
_NATIVE_PREFIX = "application/vnd.google-apps."


class DriveFile(BaseModel):
    """One file or folder in Drive."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    mime_type: str = ""
    size: int = 0
    """Bytes. Always ``0`` for folders and Google-native docs, which store none."""
    parents: list[str] = Field(default_factory=list)
    """Folder ids. Drive allows only one parent for new files, but the field is
    a list because files created before that restriction can still have several."""
    description: str = ""
    starred: bool = False
    trashed: bool = False
    shared: bool = False
    owners: list[str] = Field(default_factory=list)
    """Owner email addresses. Empty on a shared drive, which owns its own files."""
    drive_id: str = ""
    """Set when the file lives on a shared drive rather than in My Drive."""
    web_view_link: str = ""
    """Opens the file in the Drive UI — the link to put in a message to a human."""
    web_content_link: str = ""
    """Direct download URL. Empty for Google-native docs, which have no bytes."""
    md5_checksum: str = ""
    created_time: str = ""
    modified_time: str = ""
    """RFC 3339. Compare against ``ctx.now()``, never ``datetime.now()``."""

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        """A Doc, Sheet, Slide, or Form — exportable, not downloadable.

        ``drive_download_file`` fails on these by design. Use
        ``drive_export_file`` with the format you want.
        """
        return self.mime_type.startswith(_NATIVE_PREFIX) and not self.is_folder


class DrivePermission(BaseModel):
    """Who can reach a file, and how."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: str = "user"
    """``user``, ``group``, ``domain``, or ``anyone``."""
    role: str = "reader"
    """``owner``, ``organizer``, ``fileOrganizer``, ``writer``, ``commenter``,
    or ``reader``."""
    email_address: str = ""
    """Empty for ``domain`` and ``anyone`` permissions, which name no person."""
    domain: str = ""
    display_name: str = ""
    deleted: bool = False
    pending_owner: bool = False


class SharedDrive(BaseModel):
    """A shared drive — a Drive owned by a team rather than a person."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    created_time: str = ""
    hidden: bool = False
