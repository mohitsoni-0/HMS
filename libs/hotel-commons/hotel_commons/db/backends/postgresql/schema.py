"""Schema editor for ``schema_table()`` names (LLD §3.15).

Some of Django's PostgreSQL DDL names an index or constraint on its own, and PostgreSQL resolves a bare
name through the search path, which does not hold the context schemas:

- ``DROP INDEX IF EXISTS <name>`` (RemoveIndex, AlterField dropping ``db_index``, a conditional unique)
  silently did nothing;
- ``ALTER INDEX <name> RENAME`` (RenameIndex) and ``SET CONSTRAINTS <name> IMMEDIATE`` (dropping a foreign
  key) failed with "does not exist".

Here those names are qualified with the table's schema, as Django already does for inline foreign keys.
Statements that name the table (``ALTER TABLE ... DROP CONSTRAINT``) need nothing: the table carries it.
"""

from typing import Any

from django.db.backends.ddl_references import Statement, Table
from django.db.backends.postgresql import schema
from django.db.backends.utils import split_identifier


class DatabaseSchemaEditor(schema.DatabaseSchemaEditor):
    sql_delete_fk_in_schema = (
        "SET CONSTRAINTS %(namespace)s.%(name)s IMMEDIATE; ALTER TABLE %(table)s DROP CONSTRAINT %(name)s"
    )

    def _delete_fk_sql(self, model: Any, name: str) -> Statement:
        namespace, _ = split_identifier(model._meta.db_table)
        if not namespace:
            return super()._delete_fk_sql(model, name)
        return Statement(
            self.sql_delete_fk_in_schema,
            namespace=self.quote_name(namespace),
            table=Table(model._meta.db_table, self.quote_name),
            name=self.quote_name(name),
        )

    def _delete_index_sql(self, model: Any, name: str, *args: Any, **kwargs: Any) -> Statement:
        statement = super()._delete_index_sql(model, name, *args, **kwargs)  # sql=, concurrently=
        return self._qualify(statement, "name", model, name)

    def _delete_constraint_sql(self, template: str, model: Any, name: str) -> Statement:
        statement = super()._delete_constraint_sql(template, model, name)
        if template in (self.sql_delete_index, self.sql_delete_index_concurrently):  # e.g. a conditional unique
            return self._qualify(statement, "name", model, name)
        return statement

    def _rename_index_sql(self, model: Any, old_name: str, new_name: str) -> Statement:
        statement = super()._rename_index_sql(model, old_name, new_name)
        return self._qualify(statement, "old_name", model, old_name)  # RENAME TO takes a bare name

    def _qualify(self, statement: Statement, part: str, model: Any, name: str) -> Statement:
        """Name the index ``"schema"."name"`` in ``statement`` when the model's table lives in a schema."""
        namespace, _ = split_identifier(model._meta.db_table)
        if namespace:
            statement.parts[part] = f"{self.quote_name(namespace)}.{self.quote_name(name)}"
        return statement
