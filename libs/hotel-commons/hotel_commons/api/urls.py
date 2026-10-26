"""Commons routes, mounted at ``hotel/<API_VERSION>/`` (LLD §3.9, §10.2 rows 1-3 and 17). No trailing slashes.

``dev/tokens`` exists only when ``ENVIRONMENT`` is ``local``. The OpenAPI schema and Swagger UI are not
rate-limited; their authentication and permissions come from ``SPECTACULAR_SETTINGS`` (``SERVE_AUTHENTICATION``
and ``SERVE_PERMISSIONS``), which drf-spectacular's views read by default.
"""

from django.conf import settings
from django.urls import URLPattern, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from hotel_commons.api.encryption_views import PublicKeyView
from hotel_commons.api.system_views import DevTokenView, HealthView
from hotel_commons.consts import ENVIRONMENT_LOCAL

urlpatterns: list[URLPattern] = [
    path("health", HealthView.as_view(), name="health"),
    path("security/public-key", PublicKeyView.as_view(), name="security-public-key"),
    path("schema", SpectacularAPIView.as_view(throttle_classes=[]), name="schema"),
    path("docs", SpectacularSwaggerView.as_view(url_name="schema", throttle_classes=[]), name="docs"),
]

if settings.ENVIRONMENT.lower() == ENVIRONMENT_LOCAL:
    urlpatterns.append(path("dev/tokens", DevTokenView.as_view(), name="dev-tokens"))
