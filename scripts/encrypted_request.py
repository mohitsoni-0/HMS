"""Call the API with an encrypted payload (LLD §3.7, wire format v1) and print the decrypted response.

The script fetches the server's public key from ``security/public-key``, encrypts the JSON body with a
fresh AES-256 key wrapped by that key, sends the request with the v1 headers, and opens the response with
the same AES key. Rejections by the encryption middleware come back in plaintext and are printed as is.

Examples (server from ``docker compose up``)::

    python scripts/encrypted_request.py GET /hotel/v1/catalog/amenities
    python scripts/encrypted_request.py POST /hotel/v1/bookings/<booking_id>/cancel \\
        --data '{"reason": "plans changed"}' --token "$TOKEN" --header "Idempotency-Key: demo-cancel-0001"

``$TOKEN`` is a guest JWT, e.g. from ``python manage.py mint_token --sub guest-1 --roles GUEST``.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

_LIBRARY_PATH = str(Path(__file__).resolve().parents[1] / "libs" / "hotel-commons")
if _LIBRARY_PATH not in sys.path:
    sys.path.insert(0, _LIBRARY_PATH)  # run from a checkout without installing hotel-commons

from hotel_commons.consts import PAYLOAD_ENCRYPTION_HEADER  # noqa: E402
from hotel_commons.security.api_crypto import WIRE_VERSION, HybridPayloadClient  # noqa: E402

TIMEOUT_SECONDS = 10


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    base_url = args.base_url.rstrip("/")
    public_key_pem = _fetch_public_key(f"{base_url}/hotel/{args.api_version}/security/public-key")
    client = HybridPayloadClient(public_key_pem.encode())

    body = json.loads(args.data) if args.data is not None else None
    encrypted = client.encrypt_request(args.method, args.path, body)
    headers = {**_extra_headers(args.header), **encrypted.headers}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    status, response_headers, response_body = _send(args.method, f"{base_url}{args.path}", headers, encrypted.body)
    document = json.loads(response_body) if response_body else None
    if document is not None and response_headers.get(PAYLOAD_ENCRYPTION_HEADER) == WIRE_VERSION:
        document = client.decrypt_response(encrypted, document)
    sys.stdout.write(f"HTTP {status}\n{json.dumps(document, indent=2, ensure_ascii=False)}\n")
    return 0 if status < 400 else 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("method", help="HTTP method, e.g. GET or POST")
    parser.add_argument("path", help="URL path with optional query string, e.g. /hotel/v1/bookings")
    parser.add_argument("--data", help="JSON request body (omit for an empty body)")
    parser.add_argument("--token", help="bearer JWT, e.g. from `manage.py mint_token`")
    parser.add_argument("--header", action="append", default=[], help="extra header 'Name: value' (repeatable)")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-version", default="v1")
    return parser.parse_args(argv)


def _extra_headers(raw_headers: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        name, separator, value = raw.partition(":")
        if not separator or not name.strip():
            raise SystemExit(f"--header must look like 'Name: value', got {raw!r}")
        headers[name.strip()] = value.strip()
    return headers


def _fetch_public_key(url: str) -> str:
    status, _, body = _send("GET", url, {}, None)
    if status != 200:
        raise SystemExit(f"Could not fetch the public key from {url}: HTTP {status}")
    return json.loads(body)["data"]["public_key_pem"]


def _send(
    method: str, url: str, headers: Mapping[str, str], body: Mapping[str, Any] | None
) -> tuple[int, HTTPMessage, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    request_headers = {"Accept": "application/json", **headers}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method.upper(), headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


if __name__ == "__main__":
    sys.exit(main())
