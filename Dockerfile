# API image for the hotel booking service. The compose scheduler runs the same image.
# It holds code and dependencies only: configuration and keys are injected at runtime (compose env_file,
# a read-only secrets mount), and .dockerignore keeps env files and secrets/ out of every layer.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=hotel_booking_service.settings

# Unprivileged runtime user; the numeric id lets Kubernetes verify runAsNonRoot.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependencies first, so code changes reuse this layer. requirements.txt installs the shared library
# with "-e ./libs/hotel-commons", so libs/ must be in place before pip runs.
COPY requirements.txt ./
COPY libs/ ./libs/
RUN pip install -r requirements.txt

COPY . .

USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
