"""Checks a real response against the operation's documented OpenAPI response schema (test helper).

The document is drf-spectacular's own output. OpenAPI 3.0 ``nullable`` becomes ``anyOf [schema, null]`` and
``$ref`` is inlined, so ``jsonschema`` validates exactly what a strict generated client would expect.
"""

from functools import cache
from typing import Any

import jsonschema
from drf_spectacular.generators import SchemaGenerator

_REF_PREFIX = "#/components/schemas/"


@cache
def openapi_document() -> dict[str, Any]:
    return SchemaGenerator().get_schema(request=None, public=True)


def assert_matches_schema(response: Any, path: str, method: str) -> None:
    """``path`` is the templated OpenAPI path, e.g. ``/hotel/v1/catalog/properties/{property_id}``."""
    document = openapi_document()
    documented = document["paths"][path][method.lower()]["responses"][str(response.status_code)]
    schema = _json_schema(documented["content"]["application/json"]["schema"], document["components"]["schemas"])
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(response.json()), key=lambda e: e.json_path)
    assert not errors, [f"{error.json_path}: {error.message}" for error in errors]


def _json_schema(node: Any, components: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_json_schema(item, components) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return _json_schema(components[node["$ref"].removeprefix(_REF_PREFIX)], components)
    converted = {key: _json_schema(value, components) for key, value in node.items() if key != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [converted, {"type": "null"}]}
    return converted
