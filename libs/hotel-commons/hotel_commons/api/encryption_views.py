"""The public half of the API payload key (LLD §3.7), served at ``security/public-key``.

Clients fetch it once, wrap a fresh AES-256 key per request with it, and send that as ``X-Encrypted-Key``.
The path is on the encryption bypass list, so it is always served in plaintext.
"""

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from hotel_commons.api.response import api_response
from hotel_commons.api.schema import ERROR_ENVELOPE, envelope
from hotel_commons.security.api_crypto import ALGORITHM, get_payload_cipher
from hotel_commons.serializers.fields import HotelCharField

_PUBLIC_KEY_SCHEMA = inline_serializer(
    name="PayloadPublicKey",
    fields={
        "algorithm": HotelCharField(help_text=f"Always {ALGORITHM}."),
        "public_key_pem": HotelCharField(help_text="RSA public key (SubjectPublicKeyInfo PEM)."),
    },
)


class PublicKeyView(APIView):
    """GET: ``{"algorithm": "RSA-OAEP-256+A256GCM", "public_key_pem": "-----BEGIN PUBLIC KEY-----..."}``."""

    permission_classes = [AllowAny]
    throttle_scope = "default"

    @extend_schema(responses={200: envelope(_PUBLIC_KEY_SCHEMA, "PayloadPublicKeyEnvelope"), 429: ERROR_ENVELOPE})
    def get(self, request: Request) -> Response:
        return api_response({"algorithm": ALGORITHM, "public_key_pem": get_payload_cipher().public_key_pem()})
