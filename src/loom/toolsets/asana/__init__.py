"""Asana toolset — tasks, projects, comments, and the people assigned to them.

Three modules, the shape every shipped toolset uses: ``client`` (httpx, auth,
paging, error classification), ``tools`` (the ``@step`` surface), and
``manifest`` (pure metadata, imported by the catalog without pulling in httpx).

Nothing is re-exported here: importing the package must stay free, because the
catalog reads every manifest at registration and the code only when a tool is
actually resolved.
"""
