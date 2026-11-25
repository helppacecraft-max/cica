"""Type coercion with strict, value-free error codes.

Design rules (accuracy):
* money/amounts are ``decimal`` and are returned as *strings* -- never floats;
* an unparsable value is an error, never silently turned into null or a guess;
* error codes never echo the upstream value (it may be PHI).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class CoercionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


DEFAULT_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S",
    "%m/%d/%Y",
    "%Y%m%d",
)
_INT_RE = re.compile(r"^[+-]?\d+$")
_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


def coerce(value: Any, type_: str, date_formats: list[str] | None = None) -> Any:
    fn = _COERCERS[type_]
    return fn(value, date_formats or [])


def _string(v, _f):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return format(Decimal(str(v)), "f")
    raise CoercionError("not_a_scalar")


def _integer(v, _f):
    if isinstance(v, bool):
        raise CoercionError("invalid_integer")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and _INT_RE.match(v.strip()):
        return int(v.strip())
    raise CoercionError("invalid_integer")


def _decimal(v, _f):
    if isinstance(v, bool) or isinstance(v, (list, dict)):
        raise CoercionError("invalid_decimal")
    try:
        d = Decimal(v.strip() if isinstance(v, str) else str(v))
    except (InvalidOperation, ValueError):
        raise CoercionError("invalid_decimal") from None
    if not d.is_finite():
        raise CoercionError("invalid_decimal")
    return format(d, "f")


def _boolean(v, _f):
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    raise CoercionError("invalid_boolean")


def _parse_datetime(v, formats) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if not isinstance(v, str):
        raise CoercionError("invalid_date")
    s = v.strip()
    for fmt in [*formats, *DEFAULT_DATE_FORMATS]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:  # ISO-8601 with offset etc.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise CoercionError("invalid_date") from None


def _date(v, formats):
    return _parse_datetime(v, formats).date().isoformat()


def _datetime(v, formats):
    return _parse_datetime(v, formats).isoformat()


def _list(v, _f):
    if isinstance(v, list):
        return v
    return [v]


def _any(v, _f):
    return v


_COERCERS = {
    "string": _string,
    "integer": _integer,
    "decimal": _decimal,
    "boolean": _boolean,
    "date": _date,
    "datetime": _datetime,
    "list": _list,
    "any": _any,
}
