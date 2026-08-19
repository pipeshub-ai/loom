# VectorStore

*Where vectors live, and how similarity is searched.*

Defined in `loom/knowledge/store.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Somewhere vectors live, and can be searched by similarity.

## Contract

### `count(self, namespace: 'str') -> 'int'`

How many rows the namespace holds.

### `delete(self, namespace: 'str', ids: 'Sequence[str] | None' = None) -> 'int'`

Delete rows by id, or the whole namespace when *ids* is ``None``.

### `query(self, namespace: 'str', vector: 'Vector', *, top_k: 'int' = 5, where: 'Mapping[str, Any] | None' = None, model: 'str' = '') -> 'list[Match]'`

The *top_k* closest rows, each with its score.

### `upsert(self, namespace: 'str', chunks: 'Sequence[Chunk]', vectors: 'Sequence[Vector]', *, model: 'str') -> 'int'`

Write chunks and their vectors. Returns how many rows were stored.

## Implementations

- `knowledge.store.StoreBackedVectorStore`

## Consumers

- `knowledge.__init__`

<!-- END GENERATED -->
