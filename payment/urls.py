"""Payment routes, mounted at ``hotel/<API_VERSION>/`` by the project's ``urls.py`` (LLD §10.1)."""

from django.urls import path

from payment.views.payment_views import PaymentViewSet

urlpatterns = [
    path("payments/<uuid:payment_id>", PaymentViewSet.as_view({"get": "retrieve"}), name="payment-detail"),
]
