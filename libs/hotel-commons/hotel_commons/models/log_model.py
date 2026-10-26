"""The log-model strategy (LLD §3.5, HLD §8.3): every write to a model appends a full snapshot to its ``XLog``.

``X`` registers its log model by label in its class body (``CustomUpdateManager.set_logModel``); the label
is resolved lazily, once the app registry is ready, and cached. Log rows are written in the same transaction
as the change they record, with the actor and the request id, and are never updated.
"""

from collections.abc import Iterable
from typing import Any, ClassVar, Final

from django.apps import apps
from django.db import models

from hotel_commons.context import RequestContext
from hotel_commons.exceptions import SecurityFailure
from hotel_commons.status_code import LOG_MODEL_NOT_FOUND

_NOT_SNAPSHOTTED: Final = frozenset(
    {"id", "creation_date", "creation_by", "updation_date", "updation_by", "request_id", "log"}
)


class LogModelRegistry:
    """Maps 'app_label.Model' -> 'app_label.ModelLog'; resolved lazily with ``apps.get_model`` and cached."""

    _log_models: ClassVar[dict[str, str]] = {}
    _resolved: ClassVar[dict[str, type[models.Model]]] = {}

    @classmethod
    def register(cls, model: str, log_model: str) -> None:
        key = model.lower()
        cls._log_models[key] = log_model
        cls._resolved.pop(key, None)

    @classmethod
    def log_model_for(cls, model_cls: type[models.Model]) -> type[models.Model] | None:
        """The log model of ``model_cls``, None when it keeps no log; SecurityFailure when misconfigured."""
        key = model_cls._meta.label_lower
        log_model = cls._log_models.get(key)
        if log_model is None:
            return None
        if key not in cls._resolved:
            try:
                cls._resolved[key] = apps.get_model(log_model)
            except (LookupError, ValueError):
                raise SecurityFailure(LOG_MODEL_NOT_FOUND, model=model_cls._meta.label) from None
        return cls._resolved[key]


def snapshot_values(instance: models.Model, log_cls: type[models.Model]) -> dict[str, Any]:
    """attname -> value of every concrete column the log model shares with ``instance``, minus identity and audit
    columns (the log row gets its own id, creator, timestamp and request id)."""
    shared = {field.attname for field in instance._meta.concrete_fields}
    return {
        field.attname: getattr(instance, field.attname)
        for field in log_cls._meta.concrete_fields
        if not field.primary_key and field.name not in _NOT_SNAPSHOTTED and field.attname in shared
    }


def add_log_model(instance: models.Model) -> models.Model | None:
    """Append the snapshot row of ``instance`` to its log model, if it has one."""
    log_cls = LogModelRegistry.log_model_for(type(instance))
    if log_cls is None:
        return None
    log_row = log_cls(**snapshot_values(instance, log_cls), log=instance, request_id=RequestContext.request_id())
    log_row.save(force_insert=True)
    return log_row


def create_bulk_log_entries(log_cls: type[models.Model], instances: Iterable[models.Model]) -> None:
    """Append snapshot rows for ``instances`` with one bulk insert."""
    request_id = RequestContext.request_id()
    rows = [
        log_cls(**snapshot_values(instance, log_cls), log=instance, request_id=request_id) for instance in instances
    ]
    if rows:
        log_cls._default_manager.bulk_create(rows)
