"""Field-level security (LLD §3.7): the engine behind the access kwargs every Hotel*Field accepts (§3.14).

- Writes: a field the caller sends whose ``restrict_roles`` the caller holds none of is refused with
  ``Forbidden(FIELD_ACCESS_DENIED, field=<dotted name>)`` (403), at any depth of nested serializers and lists,
  before any validation runs.
- Reads: a field with ``read_roles`` the caller holds none of, and whose ``subject_attr`` does not name the
  caller, is masked with ``Masker`` when it declares ``mask_as`` and dropped from the output otherwise.

The caller is ``self.context["request"].principal`` when the view passed the request, else
``RequestContext.principal()`` (``ANONYMOUS`` outside a request, which holds no role and is nobody's subject).
Fields are read by duck typing (``restrict_roles``, ``read_roles``, ``subject_attr``, ``mask_as``), so this
module does not depend on the serializer package.
"""

import logging
from collections.abc import Mapping
from typing import Any

from rest_framework import serializers
from rest_framework.fields import get_attribute

from hotel_commons.context import RequestContext
from hotel_commons.exceptions import Forbidden
from hotel_commons.security.pii import Masker
from hotel_commons.security.principal import Principal
from hotel_commons.status_code import FIELD_ACCESS_DENIED

logger = logging.getLogger(__name__)


class FieldLevelSecurityMixin:
    """Mixed into BaseHotelSerializer and BaseHotelModelSerializer, ahead of the DRF serializer class."""

    def to_internal_value(self, data: Any) -> Any:
        if not self._has_secured_ancestor():  # the outermost secured serializer checks the whole payload
            _check_writes(self, data, self._caller(), prefix="")
        return super().to_internal_value(data)

    def to_representation(self, instance: Any) -> Any:
        representation = super().to_representation(instance)
        caller = self._caller()
        for field in self._readable_fields:
            if field.field_name in representation and not _can_read(field, caller, instance):
                _hide(representation, field)
        return representation

    def _caller(self) -> Principal:
        principal = getattr(self.context.get("request"), "principal", None)
        return principal if principal is not None else RequestContext.principal()

    def _has_secured_ancestor(self) -> bool:
        parent = self.parent
        while parent is not None:
            if isinstance(parent, FieldLevelSecurityMixin):
                return True
            parent = parent.parent
        return False


def _check_writes(serializer: serializers.Serializer, data: Any, caller: Principal, prefix: str) -> None:
    if not isinstance(data, Mapping):
        return  # the serializer itself reports a payload that is not an object
    for field in serializer._writable_fields:
        if field.field_name in data:
            _check_field(field, data[field.field_name], caller, f"{prefix}{field.field_name}")


def _check_field(field: serializers.Field, value: Any, caller: Principal, path: str) -> None:
    allowed = getattr(field, "restrict_roles", ())
    if allowed and not caller.has_any_role(*allowed):
        logger.warning("Write to restricted field %s denied", path)
        raise Forbidden(FIELD_ACCESS_DENIED, field=path)
    if isinstance(field, serializers.Serializer):
        _check_writes(field, value, caller, prefix=f"{path}.")
    elif isinstance(field, serializers.ListSerializer | serializers.ListField) and isinstance(value, list | tuple):
        for item in value:
            _check_field(field.child, item, caller, path)


def _can_read(field: serializers.Field, caller: Principal, instance: Any) -> bool:
    readers = getattr(field, "read_roles", ())
    if not readers or caller.has_any_role(*readers):
        return True
    subject_attr = getattr(field, "subject_attr", None)
    return bool(subject_attr) and caller.is_authenticated and _subject_of(instance, subject_attr) == caller.user_id


def _subject_of(instance: Any, subject_attr: str) -> str | None:
    try:
        subject = get_attribute(instance, subject_attr.split("."))
    except (AttributeError, KeyError):
        return None
    return None if subject is None else str(subject)


def _hide(representation: dict[str, Any], field: serializers.Field) -> None:
    mask_as = getattr(field, "mask_as", None)
    if mask_as is None:
        del representation[field.field_name]
        return
    value = representation[field.field_name]
    representation[field.field_name] = Masker.mask(None if value is None else str(value), mask_as)
