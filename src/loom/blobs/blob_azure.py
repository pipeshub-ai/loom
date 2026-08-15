"""Azure Blob Storage backend.

Requires ``azure-storage-blob``. The SDK is imported lazily so this module
can be imported without the extra installed; construction via
:func:`~loom.blobs.blob.blob_backend_from_url` is what
raises the install line.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from loom.blobs.blob import (
    BlobMeta,
    BlobNotFoundError,
    _require_signed_method,
)
from loom.core.exceptions import ConfigurationError


class AzureBlobBackend:
    """Blob storage on Azure Blob Storage.

    Parameters
    ----------
    container:
        Target container. It must already exist.
    connection_string:
        Azure connection string. Preferred when available.
    account_url:
        ``https://<account>.blob.core.windows.net``. Used with *credential*
        when *connection_string* is empty.
    credential:
        Token credential, shared key, or ``None`` for anonymous.
    account_name / account_key:
        Needed for service SAS when signing without a connection string.
    prefix:
        Key prefix within the container.
    """

    def __init__(
        self,
        container: str,
        *,
        connection_string: str = "",
        account_url: str = "",
        credential: Any = None,
        account_name: str = "",
        account_key: str = "",
        prefix: str = "loom/blobs",
    ) -> None:
        if not connection_string and not account_url:
            raise ConfigurationError(
                "AzureBlobBackend needs a connection_string or account_url. "
                "Pass one, or set $AZURE_STORAGE_CONNECTION_STRING."
            )
        self._container = container
        self._connection_string = connection_string
        self._account_url = account_url.rstrip("/")
        self._credential = credential
        self._account_name = account_name
        self._account_key = account_key
        self._prefix = prefix.strip("/")
        self._client: Any = None
        if connection_string:
            self._parse_connection_string(connection_string)

    def _parse_connection_string(self, value: str) -> None:
        parts: dict[str, str] = {}
        for item in value.split(";"):
            if "=" in item:
                key, _, rest = item.partition("=")
                parts[key] = rest
        self._account_name = self._account_name or parts.get("AccountName", "")
        self._account_key = self._account_key or parts.get("AccountKey", "")
        blob_endpoint = parts.get("BlobEndpoint", "")
        if blob_endpoint:
            self._account_url = blob_endpoint.rstrip("/")
        elif self._account_name and not self._account_url:
            self._account_url = (
                f"https://{self._account_name}.blob.core.windows.net"
            )

    def _key_for(self, ref: str) -> str:
        return f"{self._prefix}/{ref[:2]}/{ref}"

    def _container_client(self) -> Any:
        if self._client is not None:
            return self._client
        from azure.storage.blob.aio import ContainerClient

        if self._connection_string:
            self._client = ContainerClient.from_connection_string(
                self._connection_string, self._container
            )
        else:
            self._client = ContainerClient(
                self._account_url, self._container, credential=self._credential
            )
        return self._client

    async def close(self) -> None:
        """Release the cached container client."""
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def put(self, ref: str, data: bytes, mime: str) -> None:
        from azure.storage.blob import ContentSettings

        client = self._container_client()
        blob = client.get_blob_client(self._key_for(ref))
        await blob.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=mime),
        )

    async def get(self, ref: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        client = self._container_client()
        blob = client.get_blob_client(self._key_for(ref))
        try:
            stream = await blob.download_blob()
            return await stream.readall()
        except ResourceNotFoundError as exc:
            raise BlobNotFoundError(ref) from exc

    async def exists(self, ref: str) -> bool:
        client = self._container_client()
        blob = client.get_blob_client(self._key_for(ref))
        return bool(await blob.exists())

    async def delete(self, ref: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        client = self._container_client()
        blob = client.get_blob_client(self._key_for(ref))
        try:
            await blob.delete_blob()
        except ResourceNotFoundError:
            return

    async def signed_url(
        self,
        ref: str,
        *,
        method: str = "GET",
        expires_in: int = 3600,
        content_type: str | None = None,
    ) -> str:
        """Azure SAS URL via ``generate_blob_sas``."""
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        verb = _require_signed_method(method)
        if not self._account_name or not self._account_key:
            raise ConfigurationError(
                "AzureBlobBackend.signed_url() needs an account name and key "
                "(connection_string, or account_name + account_key). "
                "Token credentials require a user-delegation SAS, which this "
                "backend does not generate."
            )
        if not self._account_url:
            self._account_url = (
                f"https://{self._account_name}.blob.core.windows.net"
            )
        permission = (
            BlobSasPermissions(read=True)
            if verb == "GET"
            else BlobSasPermissions(write=True, create=True)
        )
        expiry = datetime.now(UTC) + timedelta(seconds=expires_in)
        sas_kwargs: dict[str, Any] = {
            "account_name": self._account_name,
            "container_name": self._container,
            "blob_name": self._key_for(ref),
            "account_key": self._account_key,
            "permission": permission,
            "expiry": expiry,
        }
        if content_type and verb == "PUT":
            sas_kwargs["content_type"] = content_type
        token = generate_blob_sas(**sas_kwargs)
        key = self._key_for(ref)
        return f"{self._account_url}/{self._container}/{key}?{token}"

    async def head(self, ref: str) -> BlobMeta:
        from azure.core.exceptions import ResourceNotFoundError

        client = self._container_client()
        blob = client.get_blob_client(self._key_for(ref))
        try:
            props = await blob.get_blob_properties()
        except ResourceNotFoundError as exc:
            raise BlobNotFoundError(ref) from exc
        mime = "application/octet-stream"
        content_settings = getattr(props, "content_settings", None)
        if content_settings is not None and content_settings.content_type:
            mime = content_settings.content_type
        return BlobMeta(
            ref=ref,
            size=int(getattr(props, "size", 0) or 0),
            mime=mime,
            etag=str(getattr(props, "etag", "") or "").strip('"'),
            last_modified=getattr(props, "last_modified", None),
        )
