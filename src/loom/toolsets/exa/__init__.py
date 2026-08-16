"""Exa toolset — neural web search, similar-page lookup, and page contents.

Four modules, the shape every shipped toolset uses: ``client`` (httpx, auth,
error classification), ``models`` (typed rows), ``tools`` (the ``@step``
surface), and ``manifest`` (pure metadata, imported by the catalog without
pulling in httpx).

Nothing is re-exported here: importing the package must stay free, because the
catalog reads every manifest at registration and the code only when a tool is
actually resolved.
"""
