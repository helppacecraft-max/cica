"""Named, tested value transforms referenced from the config.

Config never contains code: it names a transform. Projects needing something special register
their own with ``@register_transform("name", multi=...)`` in a plugin module listed in the
``FACETS_WRAPPER_PLUGINS`` environment variable.

* ``multi=False`` transforms receive one scalar (field has exactly one path).
* ``multi=True``  transforms receive a list with one entry per path (``None`` where missing).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Transform:
    name: str
    fn: Callable[[Any], Any]
    multi: bool


TRANSFORMS: dict[str, Transform] = {}


def register_transform(name: str, *, multi: bool = False):
    def deco(fn: Callable[[Any], Any]):
        if name in TRANSFORMS:
            raise ValueError(f"transform {name!r} already registered")
        TRANSFORMS[name] = Transform(name, fn, multi)
        return fn

    return deco


def _present(v: Any) -> bool:
    return v is not None and not (isinstance(v, str) and v.strip() == "")


@register_transform("identity")
def _identity(v):
    return v


@register_transform("strip")
def _strip(v):
    return v.strip() if isinstance(v, str) else v


@register_transform("upper")
def _upper(v):
    return v.upper() if isinstance(v, str) else v


@register_transform("lower")
def _lower(v):
    return v.lower() if isinstance(v, str) else v


@register_transform("join_space", multi=True)
def _join_space(values):
    parts = [str(v).strip() for v in values if _present(v)]
    return " ".join(parts) if parts else None


@register_transform("join_comma", multi=True)
def _join_comma(values):
    parts = [str(v).strip() for v in values if _present(v)]
    return ", ".join(parts) if parts else None


@register_transform("first_non_null", multi=True)
def _first_non_null(values):
    for v in values:
        if _present(v):
            return v
    return None


@register_transform("list_non_null", multi=True)
def _list_non_null(values):
    out = [v for v in values if _present(v)]
    return out or None
