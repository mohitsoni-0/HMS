"""Fixtures for integration tests whose work really commits (``django_db(transaction=True)``).

Racing threads each run on their own database connection, so nothing they write can be rolled back at the
end of the test. ``truncate_context_tables`` empties every bounded-context table afterwards instead, keeping
only the reference rows that migrations seed (the amenity catalogue).
"""

from collections.abc import Iterator

import pytest
from django.apps import apps
from django.db import connection

CONTEXT_APPS = ("catalog", "inventory", "pricing", "discovery", "booking", "payment")
SEEDED_BY_MIGRATIONS = {("catalog", "amenity")}


@pytest.fixture
def truncate_context_tables() -> Iterator[None]:
    """After the test, TRUNCATE every table of the context apps except the migration-seeded ones."""
    yield
    tables = [
        connection.ops.quote_name(model._meta.db_table)
        for model in apps.get_models()
        if model._meta.app_label in CONTEXT_APPS
        and (model._meta.app_label, model._meta.model_name) not in SEEDED_BY_MIGRATIONS
        and model._meta.managed
        and not model._meta.proxy
    ]
    with connection.cursor() as cursor:
        cursor.execute(f"TRUNCATE {', '.join(tables)} CASCADE")
