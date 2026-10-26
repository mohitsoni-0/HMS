"""One PostgreSQL database, one schema per bounded context (LLD §3.15).

Every model sets ``db_table = schema_table("<context>", "<table>")`` and each app's ``0001_initial`` starts
with ``create_schema_operation("<context>")``. All schemas share the ``default`` connection, so a use case
spanning contexts stays one ACID transaction, while each schema remains the unit of a future split
(``pg_dump --schema=booking``) with no cross-schema foreign keys to untangle.
"""

import re
from typing import Final

from django.db import migrations

_IDENTIFIER: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")  # lower-case PostgreSQL identifier, at most 63 bytes


def schema_table(schema: str, table: str) -> str:
    """db_table value that Django quotes as "schema"."table" on PostgreSQL: ``'booking"."booking'``."""
    return f'{_identifier(schema)}"."{_identifier(table)}'


def create_schema_operation(schema: str) -> migrations.RunSQL:
    """The first operation of an app's initial migration; reversing it leaves the schema in place."""
    return migrations.RunSQL(f'CREATE SCHEMA IF NOT EXISTS "{_identifier(schema)}"', reverse_sql=migrations.RunSQL.noop)


def _identifier(name: str) -> str:
    # The quoting trick above only holds for plain names; anything else would break it or inject SQL.
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"{name!r} is not a lower-case PostgreSQL identifier")
    return name
