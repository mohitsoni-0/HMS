"""WSGI entry point. gunicorn serves ``hotel_booking_service.wsgi`` (callable ``application``)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel_booking_service.settings")

application = get_wsgi_application()
