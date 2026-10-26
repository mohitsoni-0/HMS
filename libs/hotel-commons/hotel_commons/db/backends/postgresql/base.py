"""Django's PostgreSQL backend made aware of ``schema_table()`` names (LLD §3.15).

Only introspection and foreign-key removal differ from ``django.db.backends.postgresql``; the connection,
operations, the rest of the schema editor and test database creation are Django's own.
"""

from django.db.backends.postgresql import base

from hotel_commons.db.backends.postgresql.introspection import DatabaseIntrospection
from hotel_commons.db.backends.postgresql.schema import DatabaseSchemaEditor


class DatabaseWrapper(base.DatabaseWrapper):
    introspection_class = DatabaseIntrospection
    SchemaEditorClass = DatabaseSchemaEditor
