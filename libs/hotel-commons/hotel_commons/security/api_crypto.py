"""API payload encryption (LLD §3.7): hybrid RSA-OAEP-256 (3072-bit or larger) + AES-256-GCM, wire format v1.

The client draws a fresh AES-256 key per request, wraps it with the server's RSA public key
(``X-Encrypted-Key``) and seals the JSON body as ``{"payload": "<b64(nonce | ciphertext | tag)>"}``. The
associated data ``v1|<direction>|<METHOD>|<path>|<timestamp>`` binds every payload to its direction, method,
path and timestamp, so a body cannot be replayed against another endpoint or turned into a response. The
server answers with the same AES key, a new nonce and the ``res`` direction.

Every failure to open a payload is the same ``ValidationFailed(PAYLOAD_DECRYPTION_FAILED)``, so a caller
learns nothing about which check failed.
"""

import base64
import binascii
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from hotel_commons.consts import ENCRYPTED_KEY_HEADER, PAYLOAD_ENCRYPTION_HEADER, PAYLOAD_TIMESTAMP_HEADER
from hotel_commons.exceptions import ValidationFailed
from hotel_commons.status_code import PAYLOAD_DECRYPTION_FAILED

logger = logging.getLogger(__name__)

WIRE_VERSION: Final = "v1"
ALGORITHM: Final = "RSA-OAEP-256+A256GCM"
PAYLOAD_FIELD: Final = "payload"
MAX_CLOCK_SKEW_SECONDS: Final = 300
MIN_RSA_KEY_BITS: Final = 3072
AES_KEY_BYTES: Final = 32
_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16

Direction = Literal["req", "res"]


def _oaep() -> padding.OAEP:
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)


def payload_aad(direction: Direction, method: str, path: str, timestamp: int) -> str:
    """The associated data of one payload: ``v1|req|POST|/hotel/v1/bookings|1767000000``."""
    return f"{WIRE_VERSION}|{direction}|{method.upper()}|{path}|{timestamp}"


def seal(aes_key: bytes, plaintext: bytes, *, aad: str) -> str:
    """AES-256-GCM with a fresh random 96-bit nonce; returns ``b64(nonce | ciphertext | tag)``."""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(aes_key).encrypt(nonce, plaintext, aad.encode())
    return base64.b64encode(nonce + sealed).decode("ascii")


def open_sealed(aes_key: bytes, payload_b64: str, *, aad: str) -> bytes:
    """The plaintext of a ``seal`` output, or ValidationFailed(PAYLOAD_DECRYPTION_FAILED)."""
    payload = _b64decode(payload_b64, "payload")
    if len(payload) < _NONCE_BYTES + _TAG_BYTES:
        raise _rejected("payload is truncated")
    try:
        return AESGCM(aes_key).decrypt(payload[:_NONCE_BYTES], payload[_NONCE_BYTES:], aad.encode())
    except InvalidTag:
        raise _rejected("authentication failed (tampered payload or wrong method, path or timestamp)") from None


class HybridPayloadCipher:
    """Server side: holds the RSA private key, unwraps request keys and opens or seals payloads."""

    def __init__(self, private_key_pem: bytes) -> None:
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < MIN_RSA_KEY_BITS:
            raise ValueError(f"The API payload key must be an RSA key of at least {MIN_RSA_KEY_BITS} bits")
        self._private_key = private_key
        self._public_key_pem = (
            private_key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode("ascii")
        )

    def public_key_pem(self) -> str:
        """The SubjectPublicKeyInfo PEM that clients wrap their AES keys with."""
        return self._public_key_pem

    def unwrap_key(self, wrapped_key_b64: str) -> bytes:
        """The 32-byte AES key inside an ``X-Encrypted-Key`` value, or ValidationFailed."""
        wrapped_key = _b64decode(wrapped_key_b64, "wrapped key")
        try:
            aes_key = self._private_key.decrypt(wrapped_key, _oaep())
        except ValueError:
            raise _rejected("wrapped key does not decrypt") from None
        if len(aes_key) != AES_KEY_BYTES:
            raise _rejected("wrapped key is not an AES-256 key")
        return aes_key

    def decrypt(self, aes_key: bytes, payload_b64: str, *, aad: str) -> bytes:
        return open_sealed(aes_key, payload_b64, aad=aad)

    def encrypt(self, aes_key: bytes, plaintext: bytes, *, aad: str) -> str:
        """Seal with a fresh nonce on every call, so equal plaintexts never give equal payloads."""
        return seal(aes_key, plaintext, aad=aad)


@dataclass(frozen=True)
class EncryptedRequest:
    """What a client sends (``headers``, ``body``) and keeps to open the response (``aes_key`` and the AAD parts)."""

    headers: dict[str, str]
    body: dict[str, str] | None
    aes_key: bytes = field(repr=False)
    method: str
    path: str
    timestamp: int


class HybridPayloadClient:
    """Client side, for tests and ``scripts/encrypted_request.py``: encrypts requests and opens responses.

    ``path`` is the URL path the server sees; a query string, if given, is not part of the AAD.
    """

    def __init__(self, server_public_key_pem: bytes, *, clock: Callable[[], float] = time.time) -> None:
        public_key = serialization.load_pem_public_key(server_public_key_pem)
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < MIN_RSA_KEY_BITS:
            raise ValueError(f"The server key must be an RSA key of at least {MIN_RSA_KEY_BITS} bits")
        self._public_key = public_key
        self._clock = clock

    def encrypt_request(self, method: str, path: str, body: Mapping[str, Any] | None) -> EncryptedRequest:
        """A fresh AES key wrapped for the server, the v1 headers and, unless ``body`` is None, the sealed body."""
        method = method.upper()
        url_path = urlsplit(path).path
        timestamp = int(self._clock())
        aes_key = AESGCM.generate_key(bit_length=AES_KEY_BYTES * 8)
        wrapped_key = base64.b64encode(self._public_key.encrypt(aes_key, _oaep())).decode("ascii")
        sealed_body = None
        if body is not None:
            plaintext = json.dumps(body, separators=(",", ":")).encode()
            sealed_body = {PAYLOAD_FIELD: seal(aes_key, plaintext, aad=payload_aad("req", method, url_path, timestamp))}
        headers = {
            PAYLOAD_ENCRYPTION_HEADER: WIRE_VERSION,
            ENCRYPTED_KEY_HEADER: wrapped_key,
            PAYLOAD_TIMESTAMP_HEADER: str(timestamp),
        }
        return EncryptedRequest(
            headers=headers, body=sealed_body, aes_key=aes_key, method=method, path=url_path, timestamp=timestamp
        )

    def decrypt_response(self, request: EncryptedRequest, response_body: Mapping[str, Any]) -> dict[str, Any]:
        """The JSON envelope inside an encrypted response to ``request``, or ValidationFailed."""
        payload = response_body.get(PAYLOAD_FIELD)
        if not isinstance(payload, str):
            raise _rejected("response has no payload")
        aad = payload_aad("res", request.method, request.path, request.timestamp)
        plaintext = open_sealed(request.aes_key, payload, aad=aad)
        try:
            return json.loads(plaintext)
        except ValueError:
            raise _rejected("response payload is not JSON") from None


@lru_cache(maxsize=1)
def get_payload_cipher() -> HybridPayloadCipher:
    """The process-wide cipher, loaded from ``API_RSA_PRIVATE_KEY_PATH`` on first use (``cache_clear()`` on change)."""
    return HybridPayloadCipher(Path(settings.API_RSA_PRIVATE_KEY_PATH).read_bytes())


def _b64decode(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise _rejected(f"{what} is not base64") from None


def _rejected(reason: str) -> ValidationFailed:
    logger.warning("Encrypted API payload rejected: %s", reason)
    return ValidationFailed(PAYLOAD_DECRYPTION_FAILED)
