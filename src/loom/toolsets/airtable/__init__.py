"""Airtable toolset — rows in a base, and the schema that names their columns.

Four modules, the shape every shipped toolset uses: ``client``, ``models``,
``tools``, and ``manifest``. Nothing is re-exported here: importing the package
must stay free, because the catalog reads every manifest at registration and
the code only when a tool is actually resolved.
"""
