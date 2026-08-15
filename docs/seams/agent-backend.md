# AgentBackend

*Which agent framework runs ctx.agent().*

Defined in `loom/agents/backend.py`.

<!-- BEGIN GENERATED — do not edit below this line -->

Pluggable agent execution backend.

Implementations own the turn loop, tool dispatch, and model calls.
The workflow engine only sees ``AgentResult`` at the boundary.

The ``tools`` parameter carries LOOM ``Tool`` objects resolved from
the ``ToolsetRegistry``. Each backend converts them to its native
format (one adapter function per framework).

## Contract

### `run(self, prompt: 'str', *, tools: 'list[Any] | None' = None, history: 'list[Message] | None' = None, agent_id: 'str' = '', max_turns: 'int | None' = None) -> 'AgentResult[Any]'`

Execute the agent with the given prompt and tools.

## Implementations

- `agents.backend.BuiltInBackend`
- `agents.backends.agno.AgnoBackend`
- `agents.backends.langchain.LangChainBackend`
- `agents.backends.pydantic_ai.PydanticAIBackend`
- `agents.checks.Check`
- `agents.checks.CheckPipeline`
- `agents.stages.CompileStage`
- `agents.stages.StaticStage`
- `agents.stages.LintStage`
- `agents.stages.TypeStage`
- `agents.stages.SmokeStage`
- `agents.stages.ReplayStage`
- `agents.stages.CritiqueStage`
- `agents.stages.CoverageStage`
- `agents.stages.ResolutionStage`
- `agents.stages.GrantStage`
- `nodes.agentic.ClassifyNode`
- `nodes.agentic.ExtractStructuredNode`
- `nodes.agentic.SummarizeNode`
- `nodes.agentic.JudgeNode`
- `nodes.base.Node`
- `nodes.control.SwitchNode`
- `nodes.control.FilterNode`
- `nodes.control.DedupeNode`
- `nodes.control.BatchNode`
- `nodes.control.ThrottleNode`
- `nodes.guard.nodes.SchemaGuard`
- `nodes.guard.nodes.PolicyGuard`
- `nodes.guard.nodes.PiiGuard`
- `nodes.guard.nodes.BudgetGuard`
- `nodes.guard.nodes.ContentGuard`
- `nodes.human.nodes.ApprovalNode`
- `nodes.human.nodes.ChoiceNode`
- `nodes.human.nodes.FormNode`
- `nodes.human.nodes.ReviewNode`
- `nodes.human.nodes.EscalateNode`
- `nodes.io.HttpRequestNode`
- `nodes.io.WaitForWebhookNode`
- `nodes.transform.MapFieldsNode`
- `nodes.transform.TemplateNode`
- `nodes.transform.ExtractNode`
- `nodes.transform.JoinNode`
- `nodes.transform.RedactNode`
- `runtime.engine.Runtime`

## Consumers

- *(none found)*

<!-- END GENERATED -->
