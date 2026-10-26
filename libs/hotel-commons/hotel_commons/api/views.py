"""``BaseViewSet``: the request pipeline every API viewset inherits (LLD §3.9).

``initial`` runs, in this order: content negotiation and versioning (as DRF does), authentication,
throttling, permissions, then the IDOR rules of the action. Throttling comes before permissions so a
caller probing for roles or ids is rate-limited like everyone else, and IDOR comes last so only a caller
allowed to use the action ever reaches the ownership lookup.

Per action, a viewset declares its permissions (``permission_classes_by_action``), its rate-limit scope
(``throttle_scope_by_action``) and its IDOR rules (``idor_rules``); anything undeclared falls back to the
class-wide permissions, the ``"default"`` scope and no rule.
"""

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from hotel_commons.api.authentication import PrincipalAuthentication
from hotel_commons.api.throttling import DEFAULT_SCOPE, RedisRateLimitThrottle
from hotel_commons.consts import RATE_LIMIT_LIMIT_HEADER, RATE_LIMIT_REMAINING_HEADER
from hotel_commons.security.idor import IdorGuard, IdorRule
from hotel_commons.security.permissions import IsAuthenticatedPrincipal, principal_of


class BaseViewSet(GenericViewSet):
    authentication_classes = [PrincipalAuthentication]
    permission_classes = [IsAuthenticatedPrincipal]
    throttle_classes = [RedisRateLimitThrottle]
    permission_classes_by_action: ClassVar[Mapping[str, Sequence[type[BasePermission]]]] = {}
    throttle_scope_by_action: ClassVar[Mapping[str, str]] = {}
    idor_rules: ClassVar[Mapping[str, Sequence[IdorRule]]] = {}

    def get_permissions(self) -> list[BasePermission]:
        classes = self.permission_classes_by_action.get(self.action, self.permission_classes)
        return [permission() for permission in classes]

    def get_throttle_scope(self) -> str:
        return self.throttle_scope_by_action.get(self.action, DEFAULT_SCOPE)

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        self.format_kwarg = self.get_format_suffix(**kwargs)
        request.accepted_renderer, request.accepted_media_type = self.perform_content_negotiation(request)
        request.version, request.versioning_scheme = self.determine_version(request, *args, **kwargs)
        self.perform_authentication(request)
        self.check_throttles(request)
        self.check_permissions(request)
        self.check_idor(request, kwargs)

    def check_idor(self, request: Request, path_kwargs: Mapping[str, Any]) -> None:
        """Apply the action's IDOR rules; the body is parsed only when a rule reads it."""
        rules = self.idor_rules.get(self.action, ())
        if not rules:
            return
        body = request.data if any(rule.source == "body" for rule in rules) else {}
        IdorGuard().check(
            principal_of(request),
            rules,
            path_kwargs=path_kwargs,
            body=body if isinstance(body, Mapping) else {},
            query=request.query_params,
        )

    def finalize_response(self, request: Request, response: Response, *args: Any, **kwargs: Any) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)
        decision = getattr(request, "rate_limit_decision", None)
        if decision is not None:
            response[RATE_LIMIT_LIMIT_HEADER] = str(decision.limit)
            response[RATE_LIMIT_REMAINING_HEADER] = str(decision.remaining)
        return response
