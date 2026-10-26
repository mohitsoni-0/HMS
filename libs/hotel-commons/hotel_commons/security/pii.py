"""PII at rest (LLD §3.6): AES-256-GCM encryption bound to its column, keyed blind indexes and display masks.

Stored values look like ``ENC_<key id>:<urlsafe base64(nonce | ciphertext | GCM tag)>``. The key id makes
rotation possible: new values use the active key while older values still decrypt with theirs. The
associated data (``"<PI type>|<column>"`` for model fields) binds a ciphertext to its column, so a value
copied into another column fails to decrypt.

This module never imports ``hotel_commons.models``; the model fields call into it.
"""

import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

from hotel_commons.exceptions import SecurityFailure
from hotel_commons.security.pi_type import PiType
from hotel_commons.status_code import SECURITY_ERROR

logger = logging.getLogger(__name__)

KEY_BYTES: Final = 32
_NONCE_BYTES: Final = 12
_TAG_BYTES: Final = 16
_KEY_ID: Final = re.compile(r"[A-Za-z0-9]{1,16}")
_NON_DIGITS: Final = re.compile(r"[^0-9]")
_WORD: Final = re.compile(r"\S+")
_PHONE_DIGITS: Final = 10
_VISIBLE_TAIL: Final = 4
_FIXED_MASK: Final = "****"


class Ciphertext(str):
    """Marker type: only ``PiiCipher.encrypt`` returns it, so only real ciphertext skips re-encryption.

    User input is a plain ``str`` even when it happens to start with ``ENC_``, so it is encrypted like
    any other value.
    """

    __slots__ = ()


class PiiCipher:
    """AES-256-GCM over a key ring ``{key id: 32-byte key}``; ``active_key_id`` encrypts, every key decrypts."""

    PREFIX: Final = "ENC_"

    def __init__(self, keys: Mapping[str, bytes], active_key_id: str) -> None:
        for key_id, key in keys.items():
            if not _KEY_ID.fullmatch(key_id):
                raise ValueError("PII key ids must match ^[A-Za-z0-9]{1,16}$")
            if len(key) != KEY_BYTES:
                raise ValueError(f"PII key {key_id!r} must be {KEY_BYTES} bytes")
        if active_key_id not in keys:
            raise ValueError("The active PII key id is not in the key ring")
        self._aeads = {key_id: AESGCM(bytes(key)) for key_id, key in keys.items()}
        self._active_key_id = active_key_id

    def encrypt(self, plaintext: str, *, aad: str) -> Ciphertext:
        """Encrypt under the active key with a fresh random 96-bit nonce; ``aad`` must be given again to decrypt."""
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aeads[self._active_key_id].encrypt(nonce, plaintext.encode(), aad.encode())
        token = base64.urlsafe_b64encode(nonce + sealed).decode("ascii")
        return Ciphertext(f"{self.PREFIX}{self._active_key_id}:{token}")

    def decrypt(self, value: str, *, aad: str) -> str:
        """The plaintext, or SecurityFailure(SECURITY_ERROR) for a malformed, tampered or foreign value."""
        key_id, payload = self._split(value)
        aead = self._aeads.get(key_id)
        if aead is None:
            raise _rejected(f"unknown key id {key_id!r}")
        try:
            plaintext = aead.decrypt(payload[:_NONCE_BYTES], payload[_NONCE_BYTES:], aad.encode())
        except InvalidTag:
            raise _rejected("authentication failed (tampered value or wrong column)") from None
        return plaintext.decode()

    @staticmethod
    def looks_encrypted(value: object) -> bool:
        """Prefix check for the database read path; never a reason to skip encrypting user input."""
        return isinstance(value, str) and value.startswith(PiiCipher.PREFIX)

    def _split(self, value: str) -> tuple[str, bytes]:
        if not self.looks_encrypted(value):
            raise _rejected("missing prefix")
        key_id, separator, token = value[len(self.PREFIX):].partition(":")
        if not separator:
            raise _rejected("missing key id separator")
        try:
            payload = base64.b64decode(token, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            raise _rejected("payload is not base64") from None
        if len(payload) < _NONCE_BYTES + _TAG_BYTES:
            raise _rejected("payload is truncated")
        return key_id, payload


class BlindIndexer:
    """Keyed HMAC-SHA256 of a normalised value: equal PII can be matched without being stored in clear."""

    def __init__(self, key: bytes) -> None:
        if len(key) < KEY_BYTES:
            raise ValueError(f"The blind-index key must be at least {KEY_BYTES} bytes")
        self._key = bytes(key)

    def digest(self, value: str, pi_type: PiType) -> str:
        """Hex digest of the value normalised for its type: EMAIL strip+lower; PHONE last 10 digits; else strip."""
        return hmac.new(self._key, _normalise(value, pi_type).encode(), hashlib.sha256).hexdigest()


class Masker:
    """Display masks: enough to recognise a value, never enough to recover it."""

    @staticmethod
    def mask(value: str | None, pi_type: PiType) -> str | None:
        """NAME "Rahul Sharma" -> "R**** S*****"; EMAIL / UPI_VPA "rahul@gmail.com" -> "r****@gmail.com";
        PHONE "9876543210" -> "******3210"; OTHER -> "****" + last 4. A value of 4 characters or fewer is
        never shown in full."""
        if value is None:
            return None
        return _MASKS.get(pi_type, _mask_other)(value)


@lru_cache(maxsize=1)
def get_pii_cipher() -> PiiCipher:
    """The process-wide cipher from ``PII_ENCRYPTION_KEYS`` and ``PII_ACTIVE_KEY_ID`` (``cache_clear()`` on change)."""
    return PiiCipher(settings.PII_ENCRYPTION_KEYS, settings.PII_ACTIVE_KEY_ID)


@lru_cache(maxsize=1)
def get_blind_indexer() -> BlindIndexer:
    """The process-wide blind indexer from ``PII_BLIND_INDEX_KEY`` (``cache_clear()`` after a change)."""
    return BlindIndexer(settings.PII_BLIND_INDEX_KEY)


def _rejected(reason: str) -> SecurityFailure:
    logger.warning("PII ciphertext rejected: %s", reason)
    return SecurityFailure(SECURITY_ERROR)


def _normalise(value: str, pi_type: PiType) -> str:
    if pi_type is PiType.EMAIL:
        return value.strip().lower()
    if pi_type is PiType.PHONE:
        return _NON_DIGITS.sub("", value)[-_PHONE_DIGITS:]
    return value.strip()


def _keep_first(text: str) -> str:
    return text[:1] + "*" * (len(text) - 1)


def _mask_words(value: str) -> str:
    return _WORD.sub(lambda word: _keep_first(word.group()), value)


def _mask_address(value: str) -> str:
    local, at, domain = value.rpartition("@")
    return f"{_keep_first(local)}@{domain}" if at else _keep_first(value)


def _mask_phone(value: str) -> str:
    if len(value) <= _VISIBLE_TAIL:
        return "*" * len(value)
    return "*" * (len(value) - _VISIBLE_TAIL) + value[-_VISIBLE_TAIL:]


def _mask_other(value: str) -> str:
    return _FIXED_MASK + (value[-_VISIBLE_TAIL:] if len(value) > _VISIBLE_TAIL else "")


_MASKS: Final[Mapping[PiType, Callable[[str], str]]] = {
    PiType.NAME: _mask_words,
    PiType.EMAIL: _mask_address,
    PiType.PHONE: _mask_phone,
    PiType.UPI_VPA: _mask_address,
    PiType.OTHER: _mask_other,
}
