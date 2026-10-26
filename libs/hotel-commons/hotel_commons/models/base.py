"""``AddCommonField``: soft-delete status and audit columns on every table, stamped on every write (LLD §3.5).

Model triad, as in the reference services:

1. ``CommonX(AddCommonField)`` is abstract and holds only the business columns.
2. ``X(CommonX)`` calls ``CustomUpdateManager.set_logModel(logModel="app.XLog", model="app.X")`` in its class
   body and owns the table name, indexes and constraints.
3. ``XLog(CommonX)`` adds ``log = ForeignKey(X, on_delete=RESTRICT, related_name="logs")`` and drops the
   ``updation_*`` columns; ``CommonFieldsMeta`` gives it a ``request_id`` column.

A save stamps the audit columns, fills the PII companions, writes the row and appends its log row in one
transaction, so a change is never stored without its audit trail.
"""

from functools import lru_cache
from typing import Any

from django.db import models, router, transaction
from django.db.models.base import ModelBase
from django.utils import timezone

from hotel_commons.context import RequestContext
from hotel_commons.enum import Enum
from hotel_commons.models.fields import EnumField
from hotel_commons.models.log_model import add_log_model
from hotel_commons.models.managers import ActiveQuerySetManager, CustomUpdateManager
from hotel_commons.models.pii_companions import secure_pii_companions, widen_update_fields


class RecordStatus(Enum):
    INACTIVE = 0
    ACTIVE = 1


class CommonFieldsMeta(ModelBase):
    """Adds ``request_id`` to every concrete model whose class name ends with 'Log' (case-sensitive)."""

    def __new__(mcs, name: str, bases: tuple[type, ...], attrs: dict[str, Any], **kwargs: Any) -> type:
        meta = attrs.get("Meta")
        if name.endswith("Log") and not getattr(meta, "abstract", False) and "request_id" not in attrs:
            attrs["request_id"] = models.CharField(max_length=64, null=True, blank=True)
        return super().__new__(mcs, name, bases, attrs, **kwargs)


class AddCommonField(models.Model, metaclass=CommonFieldsMeta):
    """Abstract base of every table."""

    status = EnumField(RecordStatus, default=RecordStatus.ACTIVE)
    creation_date = models.DateTimeField(auto_now_add=True)
    creation_by = models.TextField()
    updation_date = models.DateTimeField(null=True, blank=True)
    updation_by = models.TextField(null=True, blank=True)

    objects = CustomUpdateManager.as_manager()  # default manager
    objects_active = ActiveQuerySetManager()  # status == ACTIVE only

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = list(kwargs["update_fields"])
            if not kwargs["update_fields"]:
                return  # Django saves nothing for an empty update_fields, so there is nothing to stamp or log
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        # No savepoint of its own: like Django's save(), a failure poisons the enclosing block, and repositories
        # wrap constraint-prone writes in their own savepoint (LLD §2.5).
        with transaction.atomic(using=using, savepoint=False):
            stamp_common_fields(self, kwargs)
            secure_pii_companions(self, kwargs)
            super().save(*args, **kwargs)
            add_log_model(self)


def stamp_common_fields(instance: models.Model, save_kwargs: dict[str, Any]) -> None:
    """Stamp the audit columns before a write.

    Insert vs update is decided by ``_state.adding`` (never by the primary key: UUIDs are set before the
    insert). An insert sets ``creation_by``; an update sets ``updation_by`` and ``updation_date`` and adds them
    to ``update_fields`` when the save is limited to some fields. Columns the model lacks (log models have no
    ``updation_*``) are skipped.
    """
    values = audit_values(type(instance), adding=instance._state.adding)
    for name, value in values.items():
        setattr(instance, name, value)
    if not instance._state.adding:
        widen_update_fields(save_kwargs, values)


def audit_values(model_cls: type[models.Model], *, adding: bool) -> dict[str, Any]:
    """The audit columns a write sets, limited to the columns ``model_cls`` has; the actor comes from RequestContext."""
    actor = RequestContext.actor()
    values = {"creation_by": actor} if adding else {"updation_by": actor, "updation_date": timezone.now()}
    columns = _column_names(model_cls)
    return {name: value for name, value in values.items() if name in columns}


@lru_cache(maxsize=None)
def _column_names(model_cls: type[models.Model]) -> frozenset[str]:
    return frozenset(field.name for field in model_cls._meta.concrete_fields)
