"""Salesforce toolset — SOQL, generic sObject CRUD, and CRM finders.

Three modules, the shape every shipped toolset uses. Nothing is re-exported:
the catalog reads the manifest at registration and the code only when a tool is
resolved, and importing this package must stay free.
"""
