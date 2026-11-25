"""JSONPath extraction of one configured field from a source response."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonpath_ng.ext import parse as _parse

from ..config.models import FieldConfig
from .coerce import CoercionError, coerce
from .transforms import TRANSFORMS

# Field statuses -------------------------------------------------------------------
OK = "ok"
MISSING = "missing_in_source"  # the path does not exist in the response
NULL = "null_in_source"  # the path exists but holds null / empty string
AMBIGUOUS = "ambiguous_path"  # the path matched several values for a scalar field
COERCION = "coercion_error"  # value could not be converted to the declared type
SOURCE_FAILED = "source_failed"  # upstream call failed / was skipped
SOURCE_NOT_FOUND = "source_not_found"  # upstream returned 404 for this resource


@lru_cache(maxsize=8192)
def compile_path(expr: str):
    return _parse(expr)


def find(expr: str, data: Any) -> list[Any]:
    return [m.value for m in compile_path(expr).find(data)]


@dataclass
class FieldResult:
    value: Any = None
    status: str = OK
    code: str | None = None  # machine-readable detail; never contains upstream values


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def extract_field(field: FieldConfig, data: Any, null_values: frozenset[str] = frozenset()) -> FieldResult:
    """Extract, transform and coerce ``field`` from ``data`` (one source response / one item)."""
    multi = len(field.paths) > 1 or (
        field.transform is not None and TRANSFORMS[field.transform].multi
    )
    raw: list[Any] = []
    any_found = False
    for p in field.paths:
        matches = find(p, data)
        if not matches:
            raw.append(None)
            continue
        any_found = True
        if len(matches) > 1:
            if field.type == "list" and len(field.paths) == 1 and field.transform is None:
                return _finish(field, list(matches), null_values)
            return FieldResult(None, AMBIGUOUS, f"path_matched_{len(matches)}_values")
        raw.append(matches[0])

    if not any_found:
        return FieldResult(None, MISSING)

    if field.transform:
        t = TRANSFORMS[field.transform]
        value = t.fn(raw if t.multi else raw[0])
    else:
        value = raw[0]

    if _empty(value) or (isinstance(value, list) and not value and field.type != "list"):
        return FieldResult(None, NULL)
    return _finish(field, value, null_values)


def _finish(field: FieldConfig, value: Any, null_values: frozenset[str]) -> FieldResult:
    if isinstance(value, str) and value.strip() in null_values:
        return FieldResult(None, NULL)
    try:
        return FieldResult(coerce(value, field.type, field.date_formats), OK)
    except CoercionError as e:
        return FieldResult(None, COERCION, e.code)
