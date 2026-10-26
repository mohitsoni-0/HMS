"""Django settings for the test suite (``pytest.ini`` sets ``DJANGO_SETTINGS_MODULE`` to this module).

The shared base settings validate keys and connection details when they are imported, so every value
they check is defaulted here first, before the project settings are imported. The keys are fixed
TEST-ONLY values (base64 of 32 random bytes, generated once), so runs are deterministic. They protect
nothing and are different from the dev keys in ``.env.example``. The values are only defaults, so CI
can still point the suite at another Postgres through the environment.
"""

import os
from pathlib import Path

_DEV_RSA_PRIVATE_KEY = Path(__file__).resolve().parent.parent / "secrets" / "dev" / "api_rsa_private.pem"

_TEST_ENVIRONMENT = {
    "ENVIRONMENT": "local",
    "DJANGO_SECRET_KEY": "D3QVjB9pvS51ARxUhDQxY6uhyL8ZooW0qwDK9Bde96PD-ilV_nQk32_Cwe5RkxMI",
    "JWT_SIGNING_KEY": "6XBs1Cf5Qm3ehYtmSERCzMEFvZLD71ZB2fr6Bx1UwGQ=",
    "PII_ENCRYPTION_KEYS": '{"t1": "7nZp6ebAVIJxCvrzgmrUx1TVwH1Y9SBBB6CJMS0zTSc="}',
    "PII_ACTIVE_KEY_ID": "t1",
    "PII_BLIND_INDEX_KEY": "QHuIwpjpqHejUKdrFrTP5SFCgGFbfEgRNoUtLeuPS3k=",
    "IDEMPOTENCY_FINGERPRINT_KEY": "z3UytTe56Eq5krbo49RFs48eFgFlaPA0L1TzZyE7wx4=",
    "API_ENCRYPTION_MODE": "optional",
    "API_RSA_PRIVATE_KEY_PATH": str(_DEV_RSA_PRIVATE_KEY),
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_NAME": "hotel",
    "POSTGRES_USER": "hotel",
    "POSTGRES_PASSWORD": "hotel",
}
for _name, _value in _TEST_ENVIRONMENT.items():
    os.environ.setdefault(_name, _value)

# Forced rather than defaulted: conftest.py flushes Redis before every test, so the suite must
# never reach a real server, whatever the shell exports (LLD §14: fakeredis in every test).
os.environ["REDIS_URL"] = "fakeredis://"

from hotel_booking_service.settings import *  # noqa: E402,F401,F403

RATE_LIMIT_ENABLED = True

# The API speaks JSON only, so the DRF test client sends JSON unless a test asks otherwise.
REST_FRAMEWORK = {**REST_FRAMEWORK, "TEST_REQUEST_DEFAULT_FORMAT": "json"}  # noqa: F405
