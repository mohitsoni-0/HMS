"""Booking endpoints (LLD §10.2 rows 11-15).

``BaseViewSet`` authenticates, throttles, checks roles and runs the IDOR rules before an action runs, so
a booking id that is not the caller's is a 404 before any use case sees it. Pay is a guest action even for
a caller who also owns the property: an owner can read a stranger's booking at their property, never pay
or cancel it. Pay outcomes are returned, not raised: declined (402) and expired (409) still carry
``{"booking", "payment"}`` in ``data``.
"""

from typing import Any, Final
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from hotel_commons.api.pagination import page_data
from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.api.views import BaseViewSet
from hotel_commons.exceptions import ValidationFailed
from hotel_commons.idempotency.decorators import idempotent
from hotel_commons.security.idor import IdorRule
from hotel_commons.security.permissions import IsAuthenticatedPrincipal, principal_of, require_roles
from hotel_commons.security.principal import Principal, Role
from rest_framework.request import Request
from rest_framework.response import Response

from booking import container
from booking.constants import (
    BOOKING_RESOURCE,
    IDEMPOTENCY_SCOPE_CANCEL,
    IDEMPOTENCY_SCOPE_CREATE,
    IDEMPOTENCY_SCOPE_PAY,
    PROPERTY_RESOURCE,
    THROTTLE_SCOPE_PAYMENT,
    THROTTLE_SCOPE_WRITE,
)
from booking.domain.booking import Booking
from booking.domain.value_objects import GuestContact
from booking.serializers.booking_serializers import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingListQuerySerializer,
    BookingPageSerializer,
    BookingPaymentResultSerializer,
    BookingPaymentSerializer,
    BookingSerializer,
)
from booking.services.commands import CancelBookingCommand, CreateBookingCommand, PayBookingCommand
from booking.services.pay_booking import PayBookingResult
from booking.status_code import BOOKING_EXPIRED, INVALID_BOOKING_REQUEST, LIST_FILTER_REQUIRED, PAYMENT_DECLINED

_GUEST_OR_ADMIN: Final = (IsAuthenticatedPrincipal, require_roles(Role.GUEST, Role.ADMIN))
_ANY_ROLE: Final = (IsAuthenticatedPrincipal, require_roles(Role.GUEST, Role.OWNER, Role.ADMIN))
_ERRORS: Final = {status: ERROR_ENVELOPE for status in (400, 401, 403, 404, 409, 422, 429)}
_BOOKING_ENVELOPE: Final = envelope(BookingSerializer, "BookingEnvelope")
_PAYMENT_ENVELOPE: Final = envelope(BookingPaymentResultSerializer, "BookingPaymentEnvelope")


class BookingViewSet(BaseViewSet):
    permission_classes_by_action = {
        "create": _GUEST_OR_ADMIN,
        "list": _ANY_ROLE,
        "retrieve": _ANY_ROLE,
        "pay": _GUEST_OR_ADMIN,
        "cancel": _GUEST_OR_ADMIN,
    }
    throttle_scope_by_action = {
        "create": THROTTLE_SCOPE_WRITE,
        "pay": THROTTLE_SCOPE_PAYMENT,
        "cancel": THROTTLE_SCOPE_WRITE,
    }
    idor_rules = {
        "list": (IdorRule(PROPERTY_RESOURCE, "property_id", source="query", relations=frozenset({"owner"})),),
        "retrieve": (IdorRule(BOOKING_RESOURCE, "booking_id", relations=frozenset({"guest", "owner"})),),
        "pay": (IdorRule(BOOKING_RESOURCE, "booking_id", relations=frozenset({"guest"})),),
        "cancel": (IdorRule(BOOKING_RESOURCE, "booking_id", relations=frozenset({"guest"})),),
    }

    @extend_schema(
        tags=["bookings"],
        summary="List bookings",
        description="Guests see their own bookings; owners pass one of their property_ids; admins pass "
        "property_id or guest_id.",
        parameters=[BookingListQuerySerializer],
        responses={200: envelope(BookingPageSerializer, "BookingPageEnvelope"), **_ERRORS},
    )
    def list(self, request: Request) -> Response:
        query = BookingListQuerySerializer(data=request.query_params, context=self.get_serializer_context())
        query.is_valid(raise_exception=True)
        params = query.validated_data
        bookings, total = _list_bookings(principal_of(request), params)
        items = BookingSerializer(bookings, many=True, context=self.get_serializer_context()).data
        return api_response(page_data(items, page=params["page"], page_size=params["page_size"], total=total))

    @extend_schema(
        tags=["bookings"],
        summary="Book a room type",
        description="Prepaid bookings hold the rooms until paid or expired; pay-at-property bookings are "
        "confirmed at once. Idempotency-Key is optional.",
        request=BookingCreateSerializer,
        responses={201: _BOOKING_ENVELOPE, **_ERRORS},
    )
    @idempotent(IDEMPOTENCY_SCOPE_CREATE, required=False)
    def create(self, request: Request) -> Response:
        payload = BookingCreateSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        command = _create_command(principal_of(request), payload.validated_data)
        booking = container.create_booking_service().execute(command)
        return api_response(self._render(booking), http_status=201)

    @extend_schema(tags=["bookings"], summary="Get a booking", responses={200: _BOOKING_ENVELOPE, **_ERRORS})
    def retrieve(self, request: Request, booking_id: UUID) -> Response:
        return api_response(self._render(container.booking_query_service().get(booking_id)))

    @extend_schema(
        tags=["bookings"],
        summary="Pay for a prepaid booking",
        description="Idempotency-Key is required; repeating it replays the first result.",
        request=BookingPaymentSerializer,
        responses={
            **_ERRORS,
            200: _PAYMENT_ENVELOPE,
            402: OpenApiResponse(_PAYMENT_ENVELOPE, description="Declined; the booking stays PENDING_PAYMENT."),
            409: OpenApiResponse(
                _PAYMENT_ENVELOPE,
                description="Hold expired (BOOKING_EXPIRED). Other conflicts return the error envelope.",
            ),
        },
    )
    @idempotent(IDEMPOTENCY_SCOPE_PAY)
    def pay(self, request: Request, booking_id: UUID) -> Response:
        payload = BookingPaymentSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        result = container.pay_booking_service().execute(
            PayBookingCommand(
                booking_id=booking_id,
                payer_id=principal_of(request).user_id,
                method=payload.validated_data["method"].name,
                method_details=payload.method_details(),
                idempotency_key=request.idempotency_key,
            )
        )
        data = BookingPaymentResultSerializer(result, context=self.get_serializer_context()).data
        return _pay_response(result, data)

    @extend_schema(
        tags=["bookings"],
        summary="Cancel a booking",
        description="Refunds by the policy snapshotted at booking time. A refund the gateway rejects returns "
        "502 and leaves the booking unchanged; retry.",
        request=BookingCancelSerializer,
        responses={200: _BOOKING_ENVELOPE, 502: ERROR_ENVELOPE, **_ERRORS},
    )
    @idempotent(IDEMPOTENCY_SCOPE_CANCEL, required=False)
    def cancel(self, request: Request, booking_id: UUID) -> Response:
        payload = BookingCancelSerializer(data=request.data, context=self.get_serializer_context())
        payload.is_valid(raise_exception=True)
        booking = container.cancel_booking_service().execute(
            CancelBookingCommand(
                booking_id=booking_id,
                actor_id=principal_of(request).user_id,
                reason=payload.validated_data.get("reason") or None,
            )
        )
        return api_response(self._render(booking))

    def _render(self, booking: Booking) -> dict[str, Any]:
        return BookingSerializer(booking, context=self.get_serializer_context()).data


def _create_command(principal: Principal, data: dict[str, Any]) -> CreateBookingCommand:
    return CreateBookingCommand(
        guest_id=data.get("on_behalf_of_user_id") or principal.user_id,  # only an admin may send it (403 else)
        room_type_id=data["room_type_id"],
        check_in=data["check_in"],
        check_out=data["check_out"],
        rooms=data["rooms"],
        guests=data["guests"],
        booking_type=data["booking_type"],
        contact=GuestContact(name=data["guest_name"], email=data["guest_email"], phone=data["guest_phone"]),
    )


def _list_bookings(principal: Principal, params: dict[str, Any]) -> tuple[list[Booking], int]:
    """Scope the list by role (LLD §10.2). An owner's property_id has already passed the IDOR rule."""
    queries = container.booking_query_service()
    paging = {"page": params["page"], "page_size": params["page_size"]}
    property_id = params.get("property_id")
    if principal.has_any_role(Role.ADMIN):
        if params.get("guest_id"):
            return queries.list_for_guest(params["guest_id"], property_id=property_id, **paging)
        if property_id is not None:
            return queries.list_for_property(property_id, **paging)
        raise ValidationFailed(INVALID_BOOKING_REQUEST, reason="property_id or guest_id is required")
    if property_id is not None and principal.has_any_role(Role.OWNER):
        return queries.list_for_property(property_id, **paging)
    if principal.has_any_role(Role.GUEST):
        return queries.list_for_guest(principal.user_id, property_id=property_id, **paging)
    raise ValidationFailed(LIST_FILTER_REQUIRED)


def _pay_response(result: PayBookingResult, data: dict[str, Any]) -> Response:
    if result.outcome == "PAID":
        return api_response(data)
    if result.outcome == "DECLINED":
        reason = result.payment.failure_message or result.payment.failure_code
        return api_response(
            data,
            http_status=PAYMENT_DECLINED.http_status,
            status=PAYMENT_DECLINED,
            message=PAYMENT_DECLINED.render(reason=reason),
        )
    return api_response(data, http_status=BOOKING_EXPIRED.http_status, status=BOOKING_EXPIRED)
