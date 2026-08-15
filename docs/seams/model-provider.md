# ModelProvider

*One model vendor behind one method.*

Defined in `workflow_builder/agents/models.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

The single integration point for any LLM vendor.

## Contract

### `complete(self, request: 'ModelRequest') -> 'ModelResponse'`

## Implementations

- `agents.providers.anthropic_provider.AnthropicProvider`
- `agents.providers.gemini_provider.GeminiProvider`
- `agents.providers.openai_provider.OpenAIProvider`
- `testing.mock.MockModelProvider`

## Consumers

- `agents.agent`

<!-- END GENERATED -->
