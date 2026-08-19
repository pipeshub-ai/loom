"""Stripe toolset — customers, payments, invoices, refunds, and events.

Five modules: ``client`` (httpx, form encoding, idempotency, error
classification), ``models`` (typed rows), ``tools`` (the ``@step`` surface),
``manifest`` (pure metadata), and ``source`` (webhook deliveries as an
``EventSource``).

Nothing is re-exported here: importing the package must stay free, because the
catalog reads every manifest at registration and the code only when a tool is
actually resolved.
"""
