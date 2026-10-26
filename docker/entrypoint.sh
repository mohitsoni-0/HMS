#!/bin/sh
# API container start-up: apply migrations, load the idempotent demo data, then replace this shell
# with gunicorn, so gunicorn runs as PID 1 and receives the container's stop signal itself.
#
# Migrations run with the request-path lock_timeout/statement_timeout switched off (0 omits them): a long
# CREATE INDEX or a table rewrite must not be cancelled after 5 s and abort the start-up. Requests, the
# demo seed and the scheduler's jobs keep the defaults (2000 ms / 5000 ms, LLD §3.15).
#
# gunicorn's access log does not go through the Django logging config or its PII redaction filter, so it
# logs the path only (%(U)s), never the query string (%(q)s, %(r)s) or the Referer (%(f)s): query parameters
# such as guest_id may carry an e-mail address. X-Request-ID ties each line to the application's logs.
set -eu

ACCESS_LOG_FORMAT='%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(M)sms request_id=%({x-request-id}o)s'

DB_LOCK_TIMEOUT_MS=0 DB_STATEMENT_TIMEOUT_MS=0 python manage.py migrate --noinput
python manage.py seed_demo
exec gunicorn hotel_booking_service.wsgi -b 0.0.0.0:8000 -w 3 \
    --access-logfile - --access-logformat "$ACCESS_LOG_FORMAT"
