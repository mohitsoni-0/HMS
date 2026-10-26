"""The schema-aware PostgreSQL backend (hotel_commons.db.backends.postgresql, review B2).

Django's own introspection finds nothing for a ``schema_table()`` name such as ``'booking"."booking'``, so
the schema editor silently skipped dropping an index and failed on a foreign key. These tests introspect
the real booking tables and run AlterField migration operations, forwards and backwards, on
schema-qualified tables. DDL is transactional in PostgreSQL, so the test transaction undoes all of it.
"""

import pytest
from django.db import connection, migrations, models
from django.db.backends.postgresql.introspection import DatabaseIntrospection as DjangoIntrospection
from django.db.migrations.state import ModelState, ProjectState

from hotel_commons.db.backends.postgresql.introspection import DatabaseIntrospection
from hotel_commons.db.schema import create_schema_operation, schema_table

pytestmark = [pytest.mark.db, pytest.mark.django_db]

BOOKING = schema_table("booking", "booking")
BOOKING_LOG = schema_table("booking", "booking_log")
SCHEMA = "hotel_commons_schema_test"
APP = "schematest"
PARENT = schema_table(SCHEMA, "parent")
CHILD = schema_table(SCHEMA, "child")


def test_the_configured_backend_is_schema_aware():
    assert connection.settings_dict["ENGINE"] == "hotel_commons.db.backends.postgresql"
    assert isinstance(connection.introspection, DatabaseIntrospection)


def test_get_constraints_resolves_schema_qualified_tables():
    with connection.cursor() as cursor:
        assert DjangoIntrospection(connection).get_constraints(cursor, BOOKING) == {}  # the defect
        constraints = connection.introspection.get_constraints(cursor, BOOKING)

    primary_keys = [c for c in constraints.values() if c["primary_key"]]
    assert [c["columns"] for c in primary_keys] == [["id"]]
    assert constraints["booking_guest_created_idx"]["index"] is True
    assert constraints["booking_guest_created_idx"]["columns"] == ["guest_id", "creation_date"]
    assert constraints["booking_dates_valid"]["check"] is True


def test_relations_primary_key_and_description_resolve_schema_qualified_tables():
    with connection.cursor() as cursor:
        relations = connection.introspection.get_relations(cursor, BOOKING_LOG)
        log_constraints = connection.introspection.get_constraints(cursor, BOOKING_LOG)
        primary_key = connection.introspection.get_primary_key_column(cursor, BOOKING)
        columns = {column.name for column in connection.introspection.get_table_description(cursor, BOOKING)}

    assert relations == {"log_id": ("id", BOOKING)}
    foreign_keys = [c["foreign_key"] for c in log_constraints.values() if c["foreign_key"]]
    assert foreign_keys == [(BOOKING, "id")]
    assert primary_key == "id"
    assert {"id", "guest_id", "check_in", "check_out", "booking_status"} <= columns


def test_introspection_restores_the_search_path():
    with connection.cursor() as cursor:
        cursor.execute("SHOW search_path")
        before = cursor.fetchone()[0]
        connection.introspection.get_constraints(cursor, BOOKING)
        cursor.execute("SHOW search_path")
        assert cursor.fetchone()[0] == before


def test_plain_table_names_behave_as_in_django():
    with connection.cursor() as cursor:
        ours = connection.introspection.get_constraints(cursor, "django_content_type")
        django = DjangoIntrospection(connection).get_constraints(cursor, "django_content_type")
    assert ours == django != {}


def test_alter_field_round_trip_on_schema_qualified_tables():
    state = ProjectState()
    state = _apply(state, [
        create_schema_operation(SCHEMA),
        migrations.CreateModel(
            "Parent", [("id", models.BigAutoField(primary_key=True))], options={"db_table": PARENT}
        ),
        migrations.CreateModel(
            "Child",
            [
                ("id", models.BigAutoField(primary_key=True)),
                ("code", models.CharField(max_length=20, db_index=True)),
                ("parent", models.ForeignKey(f"{APP}.Parent", on_delete=models.CASCADE)),
            ],
            options={"db_table": CHILD},
        ),
    ])
    assert _index_columns() == {"code", "parent_id"}
    assert _foreign_key_count() == 1
    with connection.cursor() as cursor:
        sequences = connection.introspection.get_sequences(cursor, CHILD)
    child_sequence = schema_table(SCHEMA, "child_id_seq")
    assert [(s["name"], s["table"], s["column"]) for s in sequences] == [(child_sequence, CHILD, "id")]

    drop_index = migrations.AlterField("Child", "code", models.CharField(max_length=20))
    drop_constraint = migrations.AlterField(
        "Child", "parent", models.ForeignKey(f"{APP}.Parent", on_delete=models.CASCADE, db_constraint=False)
    )
    altered = _apply(state, [drop_index, drop_constraint])
    assert _index_columns() == {"parent_id"}  # Django's own introspection left the code index in place
    assert _foreign_key_count() == 0  # ...and raised "Found wrong number (0) of foreign key constraints"

    _unapply(state, altered, [drop_index, drop_constraint])
    assert _index_columns() == {"code", "parent_id"}
    assert _foreign_key_count() == 1


def test_index_and_conditional_unique_operations_on_schema_qualified_tables():
    state = _apply(ProjectState(), [
        create_schema_operation(SCHEMA),
        migrations.CreateModel(
            "Child",
            [("id", models.BigAutoField(primary_key=True)), ("code", models.CharField(max_length=20))],
            options={"db_table": CHILD},
        ),
    ])
    add_index = migrations.AddIndex("Child", models.Index(fields=["code"], name="child_code_idx"))
    rename_index = migrations.RenameIndex("Child", new_name="child_code_renamed_idx", old_name="child_code_idx")
    add_unique = migrations.AddConstraint(
        "Child", models.UniqueConstraint(fields=["code"], condition=models.Q(id__gt=0), name="child_code_uniq")
    )
    state = _apply(state, [add_index, rename_index, add_unique])
    assert _index_names() == {"child_code_renamed_idx", "child_code_uniq"}  # RenameIndex used to fail

    state = _apply(state, [
        migrations.RemoveIndex("Child", "child_code_renamed_idx"),  # DROP INDEX IF EXISTS used to do nothing
        migrations.RemoveConstraint("Child", "child_code_uniq"),
    ])
    assert _index_names() == set()


def _apply(state: ProjectState, operations: list) -> ProjectState:
    for operation in operations:
        new_state = state.clone()
        operation.state_forwards(APP, new_state)
        with connection.schema_editor(atomic=False, collect_sql=False) as editor:
            operation.database_forwards(APP, editor, state, new_state)
        state = new_state
    return state


def _unapply(first: ProjectState, last: ProjectState, operations: list) -> None:
    """Reverse ``operations`` (applied to ``first``, giving ``last``) in reverse order, as migrate does."""
    states = [first]
    for operation in operations[:-1]:
        states.append(states[-1].clone())
        operation.state_forwards(APP, states[-1])
    states.append(last)
    for index in reversed(range(len(operations))):
        with connection.schema_editor(atomic=False, collect_sql=False) as editor:
            operations[index].database_backwards(APP, editor, states[index + 1], states[index])


def _index_columns() -> set[str]:
    """Columns of the non-primary-key indexes on the child table, read straight from the catalogue."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attname FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE n.nspname = %s AND c.relname = 'child' AND NOT i.indisprimary
            """,
            [SCHEMA],
        )
        return {row[0] for row in cursor.fetchall()}


def _foreign_key_count() -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = 'child' AND con.contype = 'f'
            """,
            [SCHEMA],
        )
        return cursor.fetchone()[0]


def _index_names() -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = %s AND tablename = 'child' AND indexname <> 'child_pkey'",
            [SCHEMA],
        )
        return {row[0] for row in cursor.fetchall()}
