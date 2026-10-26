"""HTTP API layer shared by every app (LLD §3.9): the response envelope, the DRF exception handler, the base
viewset, pagination, authentication, throttling and the OpenAPI pieces.

Nothing is re-exported: import from the module that defines a name.
"""
