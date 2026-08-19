# EmbeddingProvider

*One embedding vendor behind two methods — document and query.*

Defined in `loom/knowledge/embeddings.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Turns text into vectors. One integration point per vendor.

## Contract

### `embed_documents(self, texts: 'Sequence[str]') -> 'list[Vector]'`

Embed text that is being *stored*.

### `embed_query(self, text: 'str') -> 'Vector'`

Embed text that is being *searched for*.

## Implementations

- `knowledge.embeddings.MockEmbeddings`
- `knowledge.providers.OpenAIEmbeddings`
- `knowledge.providers.GeminiEmbeddings`

## Consumers

- `knowledge.__init__`

<!-- END GENERATED -->
