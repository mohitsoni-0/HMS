"""Database plumbing (LLD §3.4, §3.15): the Django unit of work, the one-schema-per-context helpers and the
backend that introspects them, and the mapping of lock / statement timeouts to retryable errors.

Nothing is re-exported: import from the module that defines a name.
"""
