"""The local container stack's start-up and exposure rules (docker/entrypoint.sh, docker-compose.yml).

These read the shipped files rather than a running stack, so they need neither docker nor a database:
- gunicorn's access log never carries a query string (it bypasses the Django PII redaction filter);
- ``migrate`` runs with the request-path DB timeouts switched off, everything else keeps them;
- every port the compose file publishes is bound to the loopback interface.
"""

import os
import re
import shlex
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from gunicorn.config import Config
from gunicorn.glogging import Logger

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
COMPOSE = ROOT / "docker-compose.yml"

PII_QUERY = "guest_id=rahul.sharma%40gmail.com&city=9876543210"


def _commands() -> list[list[str]]:
    """The entrypoint's commands as argv lists, with backslash continuations joined and comments dropped."""
    script = ENTRYPOINT.read_text().replace("\\\n", " ")
    lines = (line.strip() for line in script.splitlines())
    return [shlex.split(line) for line in lines if line and not line.startswith("#")]


def _access_log_format() -> str:
    [assignment] = [argv for argv in _commands() if argv[0].startswith("ACCESS_LOG_FORMAT=")]
    return assignment[0].removeprefix("ACCESS_LOG_FORMAT=")


def _gunicorn_argv() -> list[str]:
    [argv] = [argv for argv in _commands() if argv[:2] == ["exec", "gunicorn"]]
    return argv


def _render_access_line(log_format: str, query_string: str) -> str:
    """One access-log line as gunicorn would write it for GET /hotel/v1/bookings?<query_string>."""
    config = Config()
    config.set("access_log_format", log_format)
    logger = Logger(config)
    environ = {
        "REQUEST_METHOD": "GET",
        "RAW_URI": f"/hotel/v1/bookings?{query_string}",
        "PATH_INFO": "/hotel/v1/bookings",
        "QUERY_STRING": query_string,
        "SERVER_PROTOCOL": "HTTP/1.1",
        "REMOTE_ADDR": "172.18.0.1",
        "HTTP_REFERER": f"http://localhost:8000/hotel/v1/docs?{query_string}",
    }
    response = SimpleNamespace(status="200 OK", sent=42, headers=[("X-Request-ID", "req-1")])
    request = SimpleNamespace(headers=[("REFERER", environ["HTTP_REFERER"])])
    atoms = logger.atoms_wrapper_class(logger.atoms(response, request, environ, timedelta(milliseconds=12)))
    return config.access_log_format % atoms


def test_gunicorn_access_log_omits_query_strings_and_referer() -> None:
    argv = _gunicorn_argv()
    assert argv[argv.index("--access-logformat") + 1] == "$ACCESS_LOG_FORMAT"
    log_format = _access_log_format()

    line = _render_access_line(log_format, PII_QUERY)

    assert '"GET /hotel/v1/bookings HTTP/1.1" 200 42' in line
    assert "request_id=req-1" in line
    for leak in ("guest_id", "gmail", "9876543210", "?"):
        assert leak not in line, line
    for atom in ("%(r)s", "%(q)s", "%(f)s"):  # request line with query, query, Referer
        assert atom not in log_format


def test_gunicorn_default_access_format_would_leak_the_query() -> None:
    """Negative control: the render helper does surface a query string when the format asks for one."""
    assert "gmail" in _render_access_line(Config().access_log_format, PII_QUERY)


def test_migrate_runs_without_request_timeouts_and_the_server_keeps_them() -> None:
    commands = _commands()
    [migrate] = [argv for argv in commands if "migrate" in argv]
    assert migrate[:2] == ["DB_LOCK_TIMEOUT_MS=0", "DB_STATEMENT_TIMEOUT_MS=0"]
    for argv in commands:
        if "migrate" not in argv:
            assert not any(word.startswith("DB_") for word in argv), argv  # seed and gunicorn keep the defaults


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"DB_LOCK_TIMEOUT_MS": "0", "DB_STATEMENT_TIMEOUT_MS": "0"}, set()),  # the entrypoint's migrate step
        ({}, {"lock_timeout=2000", "statement_timeout=5000"}),  # requests and jobs
    ],
)
def test_database_timeouts_follow_the_environment(overrides: dict[str, str], expected: set[str]) -> None:
    """Settings load in a fresh interpreter, so the environment is read as the container would read it."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("DB_")}
    environment.update(overrides, DJANGO_SETTINGS_MODULE="hotel_booking_service.test_settings")
    probe = (
        "import django; from django.conf import settings; django.setup(); "
        "print(settings.DATABASES['default'].get('OPTIONS', {}).get('options', ''))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env={**environment, "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'libs' / 'hotel-commons'}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert set(re.findall(r"(\w+_timeout=\d+)", result.stdout)) == expected, result.stdout


def test_compose_publishes_ports_on_loopback_only() -> None:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    published = {name: service.get("ports", []) for name, service in services.items()}

    assert published["app"] and published["postgres"] and published["redis"]
    for name, ports in published.items():
        for port in ports:
            assert isinstance(port, str) and port.startswith("127.0.0.1:"), f"{name}: {port}"
