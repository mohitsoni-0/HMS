"""PII companion columns (LLD §3.6): ``masked_<field>`` and ``<field>_hash`` beside each encrypted column.

The companions are derived from the plaintext on every write path, so list screens and owner views read the
mask without decrypting, and equality lookups use the blind index instead of the ciphertext. A PII column is
any concrete field with a ``pi_type`` (duck typing); a companion is filled when the model declares it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from django.db import models

from hotel_commons.security.pi_type import PiType
from hotel_commons.security.pii import Ciphertext, Masker, get_blind_indexer


@dataclass(frozen=True, slots=True)
class _PiiColumn:
    name: str
    attname: str
    pi_type: PiType
    masked_attname: str | None
    hash_attname: str | None


def secure_pii_companions(instance: models.Model, save_kwargs: dict[str, Any]) -> None:
    """Fill the companions of every PII column this save writes, widening ``update_fields`` with them."""
    update_fields = save_kwargs.get("update_fields")
    written = None if update_fields is None else set(update_fields)
    filled: list[str] = []
    for column in _pii_columns(type(instance)):
        if written is not None and column.name not in written and column.attname not in written:
            continue
        plaintext = getattr(instance, column.attname)
        if isinstance(plaintext, Ciphertext):
            continue  # stored as-is (e.g. key-rotation tooling): its companions are already right
        for attname, value in _companions(column, plaintext).items():
            setattr(instance, attname, value)
            filled.append(attname)
    widen_update_fields(save_kwargs, filled)


def companion_values_for_update(model_cls: type[models.Model], kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """The companion values to add to ``QuerySet.update(**kwargs)`` for the PII columns it sets."""
    values: dict[str, Any] = {}
    for column in _pii_columns(model_cls):
        if column.name not in kwargs:
            continue
        value = kwargs[column.name]
        if hasattr(value, "resolve_expression"):
            # An expression is compiled to SQL around the field, so it would store plaintext or another
            # column's ciphertext; only plain values go through the field's encryption.
            raise TypeError(f"{column.name} is encrypted: update it with a plain value, not an SQL expression")
        if not isinstance(value, Ciphertext):
            values.update(_companions(column, value))
    return values


def widen_update_fields(save_kwargs: dict[str, Any], names: Iterable[str]) -> None:
    """Add ``names`` to ``save_kwargs["update_fields"]`` when the save is limited to some fields."""
    update_fields = save_kwargs.get("update_fields")
    if update_fields is not None:
        save_kwargs["update_fields"] = list(dict.fromkeys([*update_fields, *names]))


def _companions(column: _PiiColumn, value: Any) -> dict[str, str | None]:
    plaintext = None if value is None else str(value)
    companions: dict[str, str | None] = {}
    if column.masked_attname:
        companions[column.masked_attname] = Masker.mask(plaintext, column.pi_type)
    if column.hash_attname:
        companions[column.hash_attname] = (
            None if plaintext is None else get_blind_indexer().digest(plaintext, column.pi_type)
        )
    return companions


@lru_cache(maxsize=None)
def _pii_columns(model_cls: type[models.Model]) -> tuple[_PiiColumn, ...]:
    fields = {field.name: field for field in model_cls._meta.concrete_fields}
    columns = []
    for field in fields.values():
        pi_type = getattr(field, "pi_type", None)
        if pi_type is None:
            continue
        masked, hashed = fields.get(f"masked_{field.name}"), fields.get(f"{field.name}_hash")
        columns.append(
            _PiiColumn(
                name=field.name,
                attname=field.attname,
                pi_type=pi_type,
                masked_attname=masked.attname if masked else None,
                hash_attname=hashed.attname if hashed else None,
            )
        )
    return tuple(columns)
