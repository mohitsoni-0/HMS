"""ASGI entry point (callable ``application``) for ASGI servers. The compose stack serves WSGI through gunicorn."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel_booking_service.settings")

application = get_asgi_application()
