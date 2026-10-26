#!/usr/bin/env bash
# Build and start the full local stack (postgres, redis, app, scheduler) in the background.
# --wait returns once every service is running and the app's health check passes, which happens after
# migrations and demo data are in, so the URLs printed below work straight away.
set -euo pipefail
cd "$(dirname "$0")"

docker compose up --build -d --wait

echo "Hotel booking service is up: http://localhost:8000/hotel/v1/docs"
echo "Mint a token: docker compose exec app python manage.py mint_token --sub guest-1 --roles GUEST"
echo "Follow logs:  docker compose logs -f app scheduler"
echo "Stop:         docker compose down"
