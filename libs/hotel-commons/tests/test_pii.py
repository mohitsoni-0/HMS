"""PII at rest (LLD §3.6): the AES-256-GCM cipher, the blind index, the masks and the PII model fields.

Pure unit tests: no Django settings and no database. The encrypted model field is driven through its own
write path (``get_prep_value``) and read path (``from_db_value``), with a fixed key ring patched in place
of the settings-backed cipher.
"""

import base64
import hashlib
import hmac

import pytest
from django.core.exceptions import FieldError

from hotel_commons.exceptions import SecurityFailure
from hotel_commons.models import fields as model_fields
from hotel_commons.models.fields import BlindIndexField, EncryptedTextField
from hotel_commons.security.pi_type import PiType
from hotel_commons.security.pii import BlindIndexer, Ciphertext, Masker, PiiCipher
from hotel_commons.status_code import SECURITY_ERROR

KEY_1 = bytes(range(32))
KEY_2 = bytes(range(32, 64))
EMAIL_AAD = "EMAIL|guest_email"


@pytest.fixture
def cipher() -> PiiCipher:
    return PiiCipher({"k1": KEY_1, "k2": KEY_2}, active_key_id="k1")


def _payload(stored: str) -> bytes:
    return base64.urlsafe_b64decode(stored.split(":", 1)[1])


def _with_payload(stored: str, payload: bytes) -> str:
    prefix = stored.split(":", 1)[0]
    return f"{prefix}:{base64.urlsafe_b64encode(payload).decode()}"


def _hmac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


# --- PiiCipher ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("plaintext", ["rahul@example.com", "राहुल शर्मा", ""])
def test_round_trip_returns_the_plaintext(cipher, plaintext):
    stored = cipher.encrypt(plaintext, aad=EMAIL_AAD)

    assert isinstance(stored, Ciphertext)
    assert stored.startswith("ENC_k1:")
    assert cipher.decrypt(stored, aad=EMAIL_AAD) == plaintext


def test_ciphertext_hides_the_plaintext_behind_nonce_and_tag(cipher):
    stored = cipher.encrypt("rahul@example.com", aad=EMAIL_AAD)

    assert "rahul" not in stored
    assert len(_payload(stored)) == 12 + len("rahul@example.com") + 16  # nonce | ciphertext | GCM tag


def test_every_encryption_uses_a_fresh_nonce(cipher):
    first = cipher.encrypt("rahul@example.com", aad=EMAIL_AAD)
    second = cipher.encrypt("rahul@example.com", aad=EMAIL_AAD)

    assert first != second
    assert _payload(first)[:12] != _payload(second)[:12]


def test_wrong_associated_data_is_rejected(cipher):
    stored = cipher.encrypt("rahul@example.com", aad=EMAIL_AAD)

    with pytest.raises(SecurityFailure) as error:
        cipher.decrypt(stored, aad="EMAIL|contact_email")
    assert error.value.status_code is SECURITY_ERROR


@pytest.mark.parametrize("position", [0, 12, -1], ids=["nonce", "ciphertext", "tag"])
def test_tampered_ciphertext_is_rejected(cipher, position):
    stored = cipher.encrypt("rahul@example.com", aad=EMAIL_AAD)
    payload = bytearray(_payload(stored))
    payload[position] ^= 0x01

    with pytest.raises(SecurityFailure):
        cipher.decrypt(_with_payload(stored, bytes(payload)), aad=EMAIL_AAD)


@pytest.mark.parametrize(
    "value",
    [
        "rahul@example.com",
        "ENC_k1",
        "ENC_k1:%%%not-base64%%%",
        "ENC_k1:" + base64.urlsafe_b64encode(b"too short").decode(),
    ],
    ids=["no-prefix", "no-separator", "not-base64", "truncated"],
)
def test_malformed_values_are_rejected(cipher, value):
    with pytest.raises(SecurityFailure):
        cipher.decrypt(value, aad=EMAIL_AAD)


def test_unknown_key_id_is_rejected(cipher):
    foreign = PiiCipher({"k9": KEY_2}, active_key_id="k9").encrypt("rahul@example.com", aad=EMAIL_AAD)

    with pytest.raises(SecurityFailure):
        cipher.decrypt(foreign, aad=EMAIL_AAD)


def test_values_written_under_a_previous_key_still_decrypt_after_rotation(cipher):
    rotated = PiiCipher({"k1": KEY_1, "k2": KEY_2}, active_key_id="k2")
    written_before = cipher.encrypt("rahul@example.com", aad=EMAIL_AAD)
    written_after = rotated.encrypt("rahul@example.com", aad=EMAIL_AAD)

    assert written_after.startswith("ENC_k2:")
    assert rotated.decrypt(written_before, aad=EMAIL_AAD) == "rahul@example.com"


def test_user_input_that_looks_encrypted_is_encrypted_again(cipher):
    stored = cipher.encrypt("ENC_x:y", aad=EMAIL_AAD)

    assert stored != "ENC_x:y"
    assert cipher.decrypt(stored, aad=EMAIL_AAD) == "ENC_x:y"


@pytest.mark.parametrize(
    ("keys", "active_key_id"),
    [
        ({"k1": b"sixteen byte key"}, "k1"),
        ({"k-1": KEY_1}, "k-1"),
        ({"": KEY_1}, ""),
        ({"k" * 17: KEY_1}, "k" * 17),
        ({"k1": KEY_1}, "k2"),
    ],
    ids=["short-key", "bad-key-id", "empty-key-id", "long-key-id", "inactive-key-id"],
)
def test_invalid_key_ring_is_refused(keys, active_key_id):
    with pytest.raises(ValueError):
        PiiCipher(keys, active_key_id=active_key_id)


def test_looks_encrypted_checks_the_prefix():
    assert PiiCipher.looks_encrypted("ENC_k1:abc")
    assert not PiiCipher.looks_encrypted("rahul@example.com")
    assert not PiiCipher.looks_encrypted(None)


# --- Masker ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pi_type", "value", "masked"),
    [
        (PiType.NAME, "Rahul Sharma", "R**** S*****"),
        (PiType.EMAIL, "rahul@gmail.com", "r****@gmail.com"),
        (PiType.PHONE, "9876543210", "******3210"),
        (PiType.UPI_VPA, "rahul@okaxis", "r****@okaxis"),
        (PiType.OTHER, "rahul@okaxis", "****axis"),
    ],
)
def test_masks_follow_the_pi_type(pi_type, value, masked):
    assert Masker.mask(value, pi_type) == masked


@pytest.mark.parametrize(("pi_type", "value"), [(PiType.PHONE, "3210"), (PiType.OTHER, "abcd")])
def test_masks_never_reveal_a_short_value_whole(pi_type, value):
    assert Masker.mask(value, pi_type) == "****"


@pytest.mark.parametrize("pi_type", list(PiType))
def test_mask_of_none_is_none(pi_type):
    assert Masker.mask(None, pi_type) is None


# --- BlindIndexer ---------------------------------------------------------------------------------------------


def test_digest_is_hex_hmac_sha256_of_the_normalised_email():
    assert BlindIndexer(KEY_1).digest("  Rahul@Gmail.COM ", PiType.EMAIL) == _hmac(KEY_1, "rahul@gmail.com")


@pytest.mark.parametrize("phone", ["9876543210", "+91 98765-43210", "0 98765 43210"])
def test_phone_digest_uses_only_the_last_ten_digits(phone):
    assert BlindIndexer(KEY_1).digest(phone, PiType.PHONE) == _hmac(KEY_1, "9876543210")


def test_other_types_are_only_stripped_before_hashing():
    assert BlindIndexer(KEY_1).digest(" Rahul@OkAxis ", PiType.UPI_VPA) == _hmac(KEY_1, "Rahul@OkAxis")


def test_digest_depends_on_the_key():
    assert BlindIndexer(KEY_1).digest("x", PiType.OTHER) != BlindIndexer(KEY_2).digest("x", PiType.OTHER)


def test_short_blind_index_key_is_refused():
    with pytest.raises(ValueError):
        BlindIndexer(b"sixteen byte key")


# --- EncryptedTextField ---------------------------------------------------------------------------------------


@pytest.fixture
def field_cipher(monkeypatch, cipher) -> PiiCipher:
    monkeypatch.setattr(model_fields, "get_pii_cipher", lambda: cipher)
    return cipher


def _encrypted_field(name: str, pi_type: PiType = PiType.EMAIL) -> EncryptedTextField:
    field = EncryptedTextField(pi_type=pi_type)
    field.set_attributes_from_name(name)
    return field


def test_field_writes_ciphertext_bound_to_its_pi_type_and_column(field_cipher):
    stored = _encrypted_field("guest_email").get_prep_value("rahul@example.com")

    assert stored.startswith("ENC_k1:")
    assert field_cipher.decrypt(stored, aad="EMAIL|guest_email") == "rahul@example.com"


def test_field_reads_back_the_plaintext(field_cipher):
    field = _encrypted_field("guest_email")

    assert field.from_db_value(field.get_prep_value("rahul@example.com"), None, None) == "rahul@example.com"


@pytest.mark.parametrize(
    ("name", "pi_type"),
    [("contact_email", PiType.EMAIL), ("guest_email", PiType.NAME)],
    ids=["other-column", "other-pi-type"],
)
def test_ciphertext_copied_into_another_column_does_not_decrypt(field_cipher, name, pi_type):
    stored = _encrypted_field("guest_email").get_prep_value("rahul@example.com")

    with pytest.raises(SecurityFailure):
        _encrypted_field(name, pi_type).from_db_value(stored, None, None)


def test_field_passes_real_ciphertext_through_unchanged(field_cipher):
    ciphertext = field_cipher.encrypt("rahul@example.com", aad="EMAIL|guest_email")

    assert _encrypted_field("guest_email").get_prep_value(ciphertext) == ciphertext


def test_field_encrypts_user_input_that_looks_like_ciphertext(field_cipher):
    field = _encrypted_field("guest_email")

    stored = field.get_prep_value("ENC_x:y")

    assert stored != "ENC_x:y"
    assert field.from_db_value(stored, None, None) == "ENC_x:y"


def test_field_keeps_null_as_null(field_cipher):
    field = _encrypted_field("guest_email")

    assert field.get_prep_value(None) is None
    assert field.from_db_value(None, None, None) is None


def test_field_reads_a_row_written_before_encryption_as_is(field_cipher):
    assert _encrypted_field("guest_email").from_db_value("legacy@example.com", None, None) == "legacy@example.com"


def test_field_to_python_never_decrypts(field_cipher):
    ciphertext = field_cipher.encrypt("rahul@example.com", aad="EMAIL|guest_email")

    assert _encrypted_field("guest_email").to_python(ciphertext) == ciphertext


def test_field_allows_only_isnull_lookups():
    field = _encrypted_field("guest_email")

    assert field.get_lookup("isnull") is not None
    with pytest.raises(FieldError, match="guest_email_hash"):
        field.get_lookup("exact")


def test_field_deconstruct_keeps_the_pi_type():
    _, _, args, kwargs = _encrypted_field("guest_phone", PiType.PHONE).deconstruct()

    assert kwargs["pi_type"] is PiType.PHONE
    assert EncryptedTextField(*args, **kwargs).pi_type is PiType.PHONE


# --- BlindIndexField ------------------------------------------------------------------------------------------


def test_blind_index_field_defaults_stay_out_of_migrations():
    field = BlindIndexField()

    assert (field.max_length, field.null, field.db_index, field.editable) == (64, True, True, False)
    assert field.deconstruct()[3] == {}


def test_blind_index_field_overrides_survive_deconstruction():
    _, _, args, kwargs = BlindIndexField(null=False, db_index=False).deconstruct()
    rebuilt = BlindIndexField(*args, **kwargs)

    assert (rebuilt.max_length, rebuilt.null, rebuilt.db_index, rebuilt.editable) == (64, False, False, False)
