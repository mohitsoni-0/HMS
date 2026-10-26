"""No test fixture or README example pays with a hard-coded card expiry year.

The payment container validates expiry against the real clock (``CardHandler(SystemClock())``), so a literal
year turns into a 400 "Invalid expiry_year" the January after it passes, with no code change. Fixtures derive
the year from today instead; unit tests that pin a ``FixedClock`` live in the apps and are not scanned here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LITERAL_EXPIRY_YEAR = re.compile(r"""["']?expiry_year["']?\s*[:=]\s*\d{4}\b""")


def _scanned_files() -> list[Path]:
    this_file = Path(__file__).resolve()  # its own negative control below holds literal years on purpose
    return [ROOT / "README.md", *(path for path in sorted((ROOT / "tests").rglob("*.py")) if path != this_file)]


def test_no_literal_card_expiry_year_in_root_tests_or_readme() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for path in _scanned_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if LITERAL_EXPIRY_YEAR.search(line)
    ]

    assert offenders == []


def test_pattern_catches_the_shapes_used_in_fixtures() -> None:
    for line in ('"expiry_year": 2030,', "expiry_year=2030", '{"expiry_year":2031}'):
        assert LITERAL_EXPIRY_YEAR.search(line), line
    assert not LITERAL_EXPIRY_YEAR.search('"expiry_year": SystemClock().today().year + 2,')
