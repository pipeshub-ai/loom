"""QuickBooks Online toolset — customers, sales receipts, invoices, payments.

Four modules, the shape every shipped toolset uses: ``client`` (httpx, OAuth
refresh, error classification), ``models`` (typed rows), ``tools`` (the
``@step`` surface), and ``manifest`` (pure metadata).

Nothing is re-exported here: importing the package must stay free, because the
catalog reads every manifest at registration and the code only when a tool is
actually resolved.
"""
