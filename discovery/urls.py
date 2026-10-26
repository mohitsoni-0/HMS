"""Discovery routes, mounted at ``hotel/<API_VERSION>/discovery/`` (LLD §10.1)."""

from django.urls import path

from discovery.views.search_views import SearchViewSet

urlpatterns = [
    path("search", SearchViewSet.as_view({"get": "search"}), name="discovery-search"),
]
