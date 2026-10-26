#!/usr/bin/env bash
# Run the test suite on the host against PostgreSQL from docker compose. The tests themselves use fakeredis.
# Extra arguments go to pytest, e.g. ./run-tests.sh -m "not integration"  or  ./run-tests.sh booking
set -euo pipefail
cd "$(dirname "$0")"

docker compose up -d --wait postgres redis

exec .venv/bin/pytest -q "$@"
