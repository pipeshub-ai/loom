"""DuckDuckGo toolset — web, news, and image search with no API key.

Four modules, the shape every shipped toolset uses: ``client``, ``models``,
``tools``, and ``manifest``.

**Read the client's docstring before using this.** DuckDuckGo publishes no
web-search API; this toolset is built on the third-party ``ddgs`` package,
which parses search result pages. That is a different reliability contract from
Exa and Tavily, and the client says exactly where the edges are.

Nothing is re-exported here: importing the package must stay free, because the
catalog reads every manifest at registration and the code only when a tool is
actually resolved.
"""
