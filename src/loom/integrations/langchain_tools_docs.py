"""Tool documentation for the agent backend's web research capabilities.

Describes what tools the runtime's agent backend has access to,
so the coding agent generates prompts that explicitly reference them.
"""

from __future__ import annotations

LANGCHAIN_TOOL_DOCS = """\
## Agent capabilities (ctx.agent)

The runtime has an AI agent with web research tools.
Call it via: result = await ctx.agent("prompt")
result.output is the agent's text response (str).

### Available tools the agent can use

1. **duckduckgo_results_json** — web search, returns snippets + URLs
2. **fetch_url** — fetches a URL and returns its text content (3000 chars)

### IMPORTANT: reference tool names in your prompts

When writing prompts for ctx.agent(), you MUST explicitly tell the
agent which tools to use. DO NOT write vague prompts like "search
the web". Instead, be specific:

GOOD prompt (references tools by name):
    result = await ctx.agent(
        "Use duckduckgo_results_json to search for 3 recent "
        "articles about: " + query + ".\\n"
        "For each result, use fetch_url to read the article page.\\n"
        "Return each article as:\\n"
        "TITLE: <title>\\n"
        "URL: <url>\\n"
        "SUMMARY: <2-sentence summary>\\n"
    )

BAD prompt (vague, doesn't reference tools):
    result = await ctx.agent("Find AI articles")

### Usage patterns

Research with tool instructions:
    result = await ctx.agent(
        f"Use duckduckgo_results_json to find 5 articles about '{query}'. "
        "Then use fetch_url on each URL to read the full article. "
        "Return TITLE/URL/SUMMARY for each."
    )

Deduplication — pass seen URLs:
    result = await ctx.agent(
        f"Use duckduckgo_results_json to find articles about '{query}'. "
        f"Skip these URLs (already covered):\\n{seen_urls_text}\\n"
        "Use fetch_url to read each new article. "
        "Return TITLE/URL/SUMMARY for each new article only."
    )

### Key rules
- ctx.agent(prompt) returns AgentResult with .output (str)
- Always name the tools in your prompt so the agent uses them
- Do NOT import any agent framework in your workflow code
- ctx.agent() is journaled — safe in durable workflows
- Use await rt.start_scheduler(interval=5.0) with ctx.sleep()
"""
