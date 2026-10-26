#!/bin/sh
# Background jobs for the local stack (HLD §6.6). Every 60 s it expires unpaid booking holds and keeps
# the inventory horizon provisioned. A failed run is reported and retried on the next tick, so only a
# stop signal ends the loop. Sleeping in the background and waiting on it lets that signal end the
# loop at once instead of after the current sleep.
set -u

trap 'exit 0' INT TERM

while true; do
    python manage.py expire_pending_bookings || echo "scheduler: expire_pending_bookings failed" >&2
    python manage.py extend_inventory_horizon || echo "scheduler: extend_inventory_horizon failed" >&2
    sleep 60 &
    wait $!
done
