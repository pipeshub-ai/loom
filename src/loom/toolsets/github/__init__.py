"""GitHub toolset — repositories, issues, pull requests, comments, and search.

Three modules, the shape every shipped toolset uses. Nothing is re-exported:
importing this package must stay free, because the catalog reads the manifest
at registration and the code only when a tool is resolved.
"""
