# BlobBackend

*Content-addressed storage for oversized values.*

Defined in `workflow_builder/storage/blob.py`.

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

- `storage.blob.LocalBlobBackend`
- `storage.blob.S3BlobBackend`
- `storage.blob_azure.AzureBlobBackend`
- `storage.blob_gcs.GCSBlobBackend`

## Consumers

- `storage.__init__`

<!-- END GENERATED -->
