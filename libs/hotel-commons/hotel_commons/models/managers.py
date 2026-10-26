"""Query set and managers of every ``AddCommonField`` model (LLD §3.5).

The bulk write paths do what ``AddCommonField.save`` does for one row: stamp the audit columns, fill the PII
companions and append log rows, in one transaction. ``models.base`` imports this module to declare
``AddCommonField``'s managers, so the names this module needs from ``models.base`` are imported inside the
methods that use them.
"""

from collections.abc import Iterable
from typing import Any

from django.db import models, transaction

from hotel_commons.exceptions import NotFound
from hotel_commons.models.log_model import LogModelRegistry, create_bulk_log_entries
from hotel_commons.models.pii_companions import companion_values_for_update, secure_pii_companions
from hotel_commons.status_code import StatusCode


class CustomUpdateManager(models.QuerySet):
    """Default query set (``objects = CustomUpdateManager.as_manager()``); the name follows the reference library."""

    @staticmethod
    def set_logModel(logModel: str, model: str) -> None:  # noqa: N802, N803 - reference library's spelling
        """Register ``logModel`` ('app.XLog') as the log model of ``model`` ('app.X'); called in X's class body."""
        LogModelRegistry.register(model=model, log_model=logModel)

    def update(self, **kwargs: Any) -> int:
        """UPDATE with audit stamps and PII companions, logging every row the update matched."""
        from hotel_commons.models.base import audit_values

        values = {**kwargs, **audit_values(self.model, adding=False), **companion_values_for_update(self.model, kwargs)}
        log_cls = LogModelRegistry.log_model_for(self.model)
        if log_cls is None:
            return super().update(**values)  # one statement: atomic on its own
        with transaction.atomic(using=self.db, savepoint=False):
            # Lock and capture the matched rows first: the update may change the columns the filter reads.
            pks = list(self.select_for_update().values_list("pk", flat=True))
            updated = super().update(**values)
            create_bulk_log_entries(log_cls, self.model._base_manager.using(self.db).filter(pk__in=pks))
        return updated

    update.alters_data = True

    def bulk_create(self, objs: Iterable[models.Model], **kwargs: Any) -> list[models.Model]:
        """INSERT many rows with audit stamps and PII companions. Log rows are skipped with ``ignore_conflicts``,
        which does not report which rows were inserted, so a skipped row would get a phantom log."""
        from hotel_commons.models.base import stamp_common_fields

        objs = list(objs)
        for obj in objs:
            stamp_common_fields(obj, {})
            secure_pii_companions(obj, {})
        log_cls = None if kwargs.get("ignore_conflicts") else LogModelRegistry.log_model_for(self.model)
        if log_cls is None:
            return super().bulk_create(objs, **kwargs)
        with transaction.atomic(using=self.db, savepoint=False):
            created = super().bulk_create(objs, **kwargs)
            create_bulk_log_entries(log_cls, created)
        return created

    bulk_create.alters_data = True

    def bulk_update(self, objs: Iterable[models.Model], fields: Iterable[str], batch_size: int | None = None) -> int:
        """UPDATE many rows; ``fields`` is widened with the audit columns and the PII companions."""
        from hotel_commons.models.base import stamp_common_fields

        objs = list(objs)
        save_kwargs: dict[str, Any] = {"update_fields": list(fields)}
        for obj in objs:
            stamp_common_fields(obj, save_kwargs)
            secure_pii_companions(obj, save_kwargs)
        # Django runs each batch through QuerySet.update(); a plain query set keeps update() above from stamping
        # and logging those statements a second time.
        plain = models.QuerySet(self.model, using=self.db)
        with transaction.atomic(using=self.db, savepoint=False):
            updated = plain.bulk_update(objs, save_kwargs["update_fields"], batch_size=batch_size)
            log_cls = LogModelRegistry.log_model_for(self.model)
            if log_cls is not None:
                create_bulk_log_entries(log_cls, objs)
        return updated

    bulk_update.alters_data = True

    def get_or_not_found(self, status_code: StatusCode, /, **lookup: Any) -> models.Model:
        """``get(**lookup)``, raising ``NotFound(status_code, resource=<model name>)`` when there is no such row."""
        try:
            return self.get(**lookup)
        except self.model.DoesNotExist:
            raise NotFound(status_code, resource=self.model.__name__) from None


class ActiveQuerySetManager(models.Manager.from_queryset(CustomUpdateManager)):  # type: ignore[misc]
    """``objects_active``: only rows whose status is ACTIVE."""

    def get_queryset(self) -> CustomUpdateManager:
        from hotel_commons.models.base import RecordStatus

        return super().get_queryset().filter(status=RecordStatus.ACTIVE)
