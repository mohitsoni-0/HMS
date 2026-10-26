"""Introspection that understands ``schema_table()`` names such as ``'booking"."booking'`` (LLD §3.15).

Django's PostgreSQL introspection looks a table up by ``relname = <db_table>`` among the tables visible on
the search path, so a schema-qualified ``db_table`` matched nothing: the schema editor then found no index
or foreign key to drop, and an AlterField either skipped the change silently or failed (review B2).

Each table-level lookup here splits the name, puts its schema first on the search path for the duration of
the call, runs Django's own query with the bare table name, and reports table and sequence names back
schema-qualified.
``get_primary_key_column(s)`` go through ``get_constraints``, so they are covered too.

``table_names()`` and ``django_table_names()`` keep Django's behaviour on purpose: ``flush`` and
``TransactionTestCase`` therefore still leave the context schemas alone, which keeps the reference data
seeded by migrations (the amenity catalogue) between transactional tests.
"""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from django.db import DatabaseError
from django.db.backends.postgresql import introspection
from django.db.backends.utils import split_identifier

from hotel_commons.db.schema import schema_table


class DatabaseIntrospection(introspection.DatabaseIntrospection):
    def get_table_description(self, cursor: Any, table_name: str) -> list[Any]:
        schema, table = split_identifier(table_name)
        if not schema:
            return super().get_table_description(cursor, table_name)
        with _schema_first(cursor, schema):
            # Both of Django's queries (the catalogue and SELECT * FROM <table>) resolve the bare name there.
            return super().get_table_description(cursor, table)

    def get_sequences(self, cursor: Any, table_name: str, table_fields: Any = ()) -> list[dict[str, Any]]:
        schema, table = split_identifier(table_name)
        if not schema:
            return super().get_sequences(cursor, table_name, table_fields)
        with _schema_first(cursor, schema):
            sequences = super().get_sequences(cursor, table, table_fields)
        # Qualified like the table, so the schema editor's DROP / ALTER SEQUENCE find the sequence too.
        return [{**sequence, "name": _same_schema(schema, sequence["name"]), "table": table_name}
                for sequence in sequences]

    def get_relations(self, cursor: Any, table_name: str) -> dict[str, tuple[str, str]]:
        schema, table = split_identifier(table_name)
        if not schema:
            return super().get_relations(cursor, table_name)
        with _schema_first(cursor, schema):
            relations = super().get_relations(cursor, table)
        # Django only reports foreign keys within the table's own schema, so the target shares it.
        return {column: (target_column, _same_schema(schema, target)) for column, (target_column, target) in
                relations.items()}

    def get_constraints(self, cursor: Any, table_name: str) -> dict[str, dict[str, Any]]:
        schema, table = split_identifier(table_name)
        if not schema:
            return super().get_constraints(cursor, table_name)
        with _schema_first(cursor, schema):
            constraints = super().get_constraints(cursor, table)
        for constraint in constraints.values():
            target = constraint.get("foreign_key")
            if target and not split_identifier(target[0])[0]:
                constraint["foreign_key"] = (_same_schema(schema, target[0]), target[1])
        return constraints


@contextmanager
def _schema_first(cursor: Any, schema: str) -> Iterator[None]:
    """Run the block with ``schema`` first on the search path, then restore the previous path.

    Inside a transaction that the block aborted, the restore cannot run, and need not: the rollback that
    follows undoes the change as well.
    """
    cursor.execute("SELECT current_setting('search_path')")
    previous = cursor.fetchone()[0]
    cursor.execute("SELECT set_config('search_path', %s, false)", [f'"{schema}", {previous}'])
    try:
        yield
    except BaseException:
        with suppress(DatabaseError):
            cursor.execute("SELECT set_config('search_path', %s, false)", [previous])
        raise
    cursor.execute("SELECT set_config('search_path', %s, false)", [previous])


def _same_schema(schema: str, table: str) -> str:
    """``table`` qualified with ``schema`` in the ``schema_table`` form, unless it is not a plain identifier."""
    try:
        return schema_table(schema, table)
    except ValueError:
        return table
