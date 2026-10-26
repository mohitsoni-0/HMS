#!/bin/sh
# API container start-up: apply migrations, load the idempotent demo data, then replace this shell
# with gunicorn, so gunicorn runs as PID 1 and receives the container's stop signal itself.
set -eu

python manage.py migrate --noinput
python manage.py seed_demo
exec gunicorn hotel_booking_service.wsgi -b 0.0.0.0:8000 -w 3 --access-logfile -
