from __future__ import annotations

import pytest

from facets_wrapper.config.models import FieldConfig
from facets_wrapper.engine.coerce import CoercionError, coerce
from facets_wrapper.engine.extract import extract_field


def F(**kw):
    base = dict(name="x", group="g", source="s", paths=["$.A"], type="string")
    base.update(kw)
    return FieldConfig(**base)


# ---- coercion ------------------------------------------------------------------------
def test_decimal_is_exact_string_never_float():
    assert coerce("125.00", "decimal") == "125.00"
    assert coerce(0.1, "decimal") == "0.1"  # no 0.1000000000000000055...
    assert coerce(12, "decimal") == "12"


@pytest.mark.parametrize("bad", ["abc", "1,234.00", "NaN", "Infinity", True, [1]])
def test_decimal_rejects_garbage(bad):
    with pytest.raises(CoercionError):
        coerce(bad, "decimal")


def test_string_strips_padding_and_keeps_leading_zeros():
    assert coerce("  000123  ", "string") == "000123"
    assert coerce(123, "string") == "123"


def test_integer_and_boolean():
    assert coerce("42", "integer") == 42
    assert coerce(3.0, "integer") == 3
    with pytest.raises(CoercionError):
        coerce("4.5", "integer")
    with pytest.raises(CoercionError):
        coerce(True, "integer")
    assert coerce("Y", "boolean") is True and coerce("no", "boolean") is False
    with pytest.raises(CoercionError):
        coerce("maybe", "boolean")


@pytest.mark.parametrize("raw", ["2025-01-15", "2025-01-15T00:00:00", "01/15/2025", "20250115", "2025-01-15T00:00:00Z"])
def test_date_formats(raw):
    assert coerce(raw, "date") == "2025-01-15"


def test_custom_date_format_and_error():
    assert coerce("15.01.2025", "date", ["%d.%m.%Y"]) == "2025-01-15"
    with pytest.raises(CoercionError):
        coerce("not a date", "date")


def test_error_codes_do_not_echo_values():
    secret = "SSN-123-45-6789"
    with pytest.raises(CoercionError) as e:
        coerce(secret, "decimal")
    assert secret not in e.value.code and secret not in str(e.value)


# ---- extraction ----------------------------------------------------------------------
def test_ok_missing_null_and_empty_string():
    assert extract_field(F(), {"A": "v"}).status == "ok"
    assert extract_field(F(), {"B": 1}).status == "missing_in_source"
    assert extract_field(F(), {"A": None}).status == "null_in_source"
    assert extract_field(F(), {"A": "   "}).status == "null_in_source"


def test_missing_and_null_are_distinguishable():
    r_missing, r_null = extract_field(F(), {}), extract_field(F(), {"A": None})
    assert r_missing.value is None and r_null.value is None
    assert r_missing.status != r_null.status


def test_zero_is_a_value_not_null():
    r = extract_field(F(type="integer"), {"A": 0})
    assert (r.status, r.value) == ("ok", 0)


def test_ambiguous_scalar_path_is_an_error_not_a_guess():
    r = extract_field(F(paths=["$.items[*].id"]), {"items": [{"id": 1}, {"id": 2}]})
    assert r.status == "ambiguous_path"


def test_list_type_collects_matches():
    r = extract_field(F(paths=["$.items[*].id"], type="list"), {"items": [{"id": 1}, {"id": 2}]})
    assert (r.status, r.value) == ("ok", [1, 2])


def test_multi_path_transform_join():
    f = F(paths=["$.FN", "$.LN"], transform="join_space")
    assert extract_field(f, {"FN": "Ann", "LN": "Lee"}).value == "Ann Lee"
    assert extract_field(f, {"FN": "Ann"}).value == "Ann"  # one part missing is tolerated
    assert extract_field(f, {}).status == "missing_in_source"
    assert extract_field(f, {"FN": "", "LN": None}).status == "null_in_source"


def test_null_sentinel_values():
    r = extract_field(F(type="date"), {"A": "1753-01-01"}, frozenset({"1753-01-01"}))
    assert r.status == "null_in_source"


def test_coercion_failure_surfaces_as_status():
    r = extract_field(F(type="decimal"), {"A": "twelve"})
    assert (r.status, r.value, r.code) == ("coercion_error", None, "invalid_decimal")
