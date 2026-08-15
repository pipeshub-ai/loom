# BlobBackend

*Content-addressed storage for oversized values.*

Defined in `loom/blobs/blob.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Async protocol for content-addressed blob persistence.

## Contract

### `delete(self, ref: 'str') -> 'None'`

Remove the blob identified by *ref*.  No-op if missing.

### `exists(self, ref: 'str') -> 'bool'`

Return ``True`` if *ref* is stored in the backend.

### `get(self, ref: 'str') -> 'bytes'`

Retrieve the blob identified by *ref*.

### `put(self, ref: 'str', data: 'bytes', mime: 'str') -> 'None'`

Store *data* under the given *ref* with content type *mime*.

## Implementations

- `blobs.blob.LocalBlobBackend`
- `blobs.blob.S3BlobBackend`
- `blobs.blob_azure.AzureBlobBackend`
- `blobs.blob_gcs.GCSBlobBackend`

## Consumers

- `blobs.__init__`

<!-- END GENERATED -->
