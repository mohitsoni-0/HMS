"""Module boundaries of the modular monolith (HLD §3.4, §3.5; LLD §2, §3.1), checked on the source's AST.

Rules, each over every import statement of the shipped code (function-level and ``TYPE_CHECKING`` imports
included; ``tests/`` folders excluded):
- an app imports another app only through ``<app>.public`` (migrations included);
- ``hotel_commons`` imports no app;
- no ``domain/`` or ``interfaces/`` package imports Django, DRF or drf-spectacular;
- ``services/`` never import another app (they reach it through their own ports and adapters);
- the app dependency graph is acyclic and has only the edges of the HLD §3.4 context map.

Nothing is imported or executed, so the test needs neither Django nor a database.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMONS = ROOT / "libs" / "hotel-commons" / "hotel_commons"
APPS = ("catalog", "inventory", "pricing", "discovery", "booking", "payment")
FRAMEWORKS = ("django", "rest_framework", "drf_spectacular")
FRAMEWORK_FREE_PACKAGES = ("domain", "interfaces")

# HLD §3.4: importing app -> apps whose public package it uses.
CONTEXT_MAP = {
    "booking": {"catalog", "pricing", "inventory", "payment"},
    "discovery": {"catalog", "pricing", "inventory"},
    "catalog": {"inventory"},
    "pricing": {"inventory", "catalog"},
    "inventory": set(),
    "payment": set(),
}


@dataclass(frozen=True, slots=True)
class Import:
    path: Path  # relative to the repository root
    line: int
    module: str  # absolute dotted name: "catalog.public.api", or "catalog.public.api.CatalogAPI" for from-imports

    @property
    def top(self) -> str:
        return self.module.split(".")[0]

    def __str__(self) -> str:
        return f"{self.path}:{self.line} imports {self.module}"


def imports_of(path: Path, source: str) -> Iterator[Import]:
    """Every absolute import in ``source``; relative imports stay inside their own package and are skipped."""
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield Import(path, node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                yield Import(path, node.lineno, f"{node.module}.{alias.name}")


def _shipped_files(package_dir: Path) -> Iterator[Path]:
    for path in sorted(package_dir.rglob("*.py")):
        parts = path.relative_to(package_dir).parts
        if "tests" not in parts and "__pycache__" not in parts:
            yield path


def _imports_under(package_dir: Path) -> list[Import]:
    return [
        found
        for path in _shipped_files(package_dir)
        for found in imports_of(path.relative_to(ROOT), path.read_text(encoding="utf-8"))
    ]


def _app_of(found: Import) -> str:
    return found.path.parts[0]


def bypasses_public(found: Import) -> bool:
    """True for an import of another app that does not go through its ``public`` package."""
    own_app = _app_of(found)
    parts = found.module.split(".")
    return parts[0] in APPS and parts[0] != own_app and parts[1:2] != ["public"]


def imports_framework(found: Import) -> bool:
    return found.top in FRAMEWORKS


def in_package(found: Import, package: str) -> bool:
    return package in found.path.parts[:-1]


def find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """A dependency cycle as a path [a, b, ..., a], or None when the graph is acyclic."""
    visiting: list[str] = []
    done: set[str] = set()

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            return [*visiting[visiting.index(node) :], node]
        if node in done:
            return None
        visiting.append(node)
        for target in sorted(edges.get(node, ())):
            if cycle := visit(target):
                return cycle
        visiting.pop()
        done.add(node)
        return None

    for node in sorted(edges):
        if cycle := visit(node):
            return cycle
    return None


@pytest.fixture(scope="module")
def app_imports() -> list[Import]:
    found = [imported for app in APPS for imported in _imports_under(ROOT / app)]
    assert found, "no app source found"
    return found


@pytest.fixture(scope="module")
def app_graph(app_imports: list[Import]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {app: set() for app in APPS}
    for found in app_imports:
        if found.top in APPS and found.top != _app_of(found):
            graph[_app_of(found)].add(found.top)
    return graph


def test_cross_app_imports_go_through_public(app_imports: list[Import]) -> None:
    violations = [str(found) for found in app_imports if bypasses_public(found)]

    assert violations == []


def test_shared_library_imports_no_app() -> None:
    violations = [str(found) for found in _imports_under(COMMONS) if found.top in APPS]

    assert violations == []


@pytest.mark.parametrize("package", FRAMEWORK_FREE_PACKAGES)
def test_domain_and_interfaces_are_framework_free(app_imports: list[Import], package: str) -> None:
    library = [found for found in _imports_under(COMMONS) if in_package(found, package)]
    violations = [str(found) for found in [*app_imports, *library] if in_package(found, package) and imports_framework(found)]

    assert violations == []


def test_services_import_no_other_app(app_imports: list[Import]) -> None:
    violations = [
        str(found)
        for found in app_imports
        if in_package(found, "services") and found.top in APPS and found.top != _app_of(found)
    ]

    assert violations == []


def test_app_graph_is_acyclic_and_matches_the_context_map(app_graph: dict[str, set[str]]) -> None:
    assert find_cycle(app_graph) is None
    undocumented = {app: targets - CONTEXT_MAP[app] for app, targets in app_graph.items() if targets - CONTEXT_MAP[app]}
    assert undocumented == {}, "new app dependency: update HLD §3.4 and CONTEXT_MAP, keeping the graph acyclic"


# --- The checks themselves (negative controls on synthetic sources) -----------------------------------------


def _parse(path: str, source: str) -> list[Import]:
    return list(imports_of(Path(path), source))


def test_checker_flags_imports_that_bypass_public() -> None:
    source = "from catalog.models.catalog_models import Property\nimport payment.services.charge_service\n"
    flagged = [found.module for found in _parse("booking/adapters/x.py", source) if bypasses_public(found)]

    assert flagged == ["catalog.models.catalog_models.Property", "payment.services.charge_service"]


def test_checker_allows_public_own_app_and_relative_imports() -> None:
    source = (
        "from catalog.public.api import get_catalog_api\nfrom booking.models import booking_models\n"
        "from . import mappers\n"
        "def lazy():\n    from inventory.public.enums import HoldStatus\n"
    )

    assert [found for found in _parse("booking/adapters/x.py", source) if bypasses_public(found)] == []


def test_checker_finds_function_level_and_type_checking_imports() -> None:
    source = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from django.db import models\n"
        "def f():\n    import rest_framework.serializers\n"
    )
    found = _parse("booking/domain/x.py", source)

    assert [item.module for item in found if imports_framework(item)] == ["django.db.models", "rest_framework.serializers"]
    assert all(in_package(item, "domain") for item in found)


def test_checker_finds_cycles() -> None:
    assert find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == ["a", "b", "c", "a"]
    assert find_cycle({"a": {"b", "c"}, "b": {"c"}, "c": set()}) is None
