from __future__ import annotations

import json

import openpyxl
import pytest

from facets_wrapper.config.loader import load_project_file
from facets_wrapper.converter.build import convert, write_outputs
from facets_wrapper.converter.excel_reader import ConversionError, read_sections
from facets_wrapper.converter.normalize import parse_api_field, parse_url, source_name
from facets_wrapper.converter.defaults import DEFAULT_SETTINGS


def codes(res):
    return {i.code for i in res.issues}


def field(res, group, name):
    return next(f for f in res.config["fields"] if f["group"] == group and f["name"] == name)


# ---- reading the sheet ---------------------------------------------------------------
def test_sections_and_banners_detected(sample_dir):
    _, secs = read_sections(sample_dir / "sample_mapping.xlsx")
    assert [s.title for s in secs] == ["Claim Level Data", "Line Level Data", "MatchUM", "Claim Inquery"]
    assert all(s.rows for s in secs)


def test_missing_header_is_a_clear_error(tmp_path):
    wb = openpyxl.Workbook()
    wb.active["A1"] = "nothing useful"
    wb.save(tmp_path / "x.xlsx")
    with pytest.raises(ConversionError, match="Facets Table"):
        read_sections(tmp_path / "x.xlsx")


def test_unknown_sheet_lists_available(sample_dir):
    with pytest.raises(ConversionError, match="Mapping Sheet"):
        read_sections(sample_dir / "sample_mapping.xlsx", "Nope")


def test_hyperlink_target_is_used_when_cell_text_has_no_url(tmp_path, sample_dir):
    wb = openpyxl.load_workbook(sample_dir / "sample_mapping.xlsx")
    ws = wb["Mapping Sheet"]
    # find the 'Provider Name' row, replace its URL text, keep a real hyperlink
    r = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 6).value == "Provider Name")
    ws.cell(r, 8).value = "click here"
    ws.cell(r, 8).hyperlink = "https://h.example:1/RestServices/facets/api/v1/Claims/ZZ1/Inquiry/Summary/"
    wb.save(tmp_path / "h.xlsx")
    res = convert(tmp_path / "h.xlsx", "demo")
    assert field(res, "claim", "provider_name")["source"] == "claims_inquiry_summary"


# ---- normalisation -------------------------------------------------------------------
RULES = DEFAULT_SETTINGS["path_param_rules"]
RX = DEFAULT_SETTINGS["rest_path_regex"]


def test_url_templating_and_host_dropped():
    u = parse_url("https://internal.corp:10408//RestServices/facets/api/v1/Claims/26A369350300/Inquiry/Lines/1/Details", RX, RULES)
    assert u.kind == "rest" and u.double_slash
    assert u.template == "/RestServices/facets/api/v1/Claims/{claim_id}/Inquiry/Lines/{line_no}/Details"
    assert "26A369350300" not in u.template and "internal.corp" not in u.template


def test_ui_url_and_bare_host_are_not_rest():
    assert parse_url("https://h:10408/?AdvancedSearch=N&ClaimId=1", RX, RULES).kind == "non_rest"
    assert parse_url("https://h:10418", RX, RULES).kind == "non_rest"


def test_bracket_placeholder_flagged():
    u = parse_url("https://h:1/RestServices/facets/api/v1/family/coverage/[clcl_id]/Inquiry", RX, RULES)
    assert u.bracket_placeholder == "[clcl_id]" and "{subscriber_key}" in u.template


def test_api_field_parsing():
    na = {"na", ""}
    assert parse_api_field("A & B", na).joiner == "&"
    assert parse_api_field("A, B", na).joiner == ","
    assert parse_api_field("NA", na).error == "not_applicable"
    assert parse_api_field("A & B, C", na).error == "mixed_separators"
    assert parse_api_field("not valid!", na).error == "invalid_name"


def test_source_name():
    assert source_name("/RestServices/facets/api/v1/Claims/{claim_id}/Inquiry/Summary") == "claims_inquiry_summary"


# ---- conversion ----------------------------------------------------------------------
def test_sample_converts_to_loadable_config(converted):
    res, _, path = converted
    assert res.loadable, res.load_errors
    assert load_project_file(path).project == "demo"


def test_expected_findings_are_reported(converted):
    res, *_ = converted
    for c in ("FILL_SERIES_URLS", "NON_REST_URL", "DUPLICATE_MAPPING", "MULTI_URL_CELL",
              "BRACKET_PLACEHOLDER", "DOUBLE_SLASH", "MIXED_CARDINALITY_GROUP", "TYPE_NUMERIC_ID_LIKE",
              "MULTI_API_FIELD", "LITERAL_IDS_REPLACED"):
        assert c in codes(res), c


def test_unmapped_mandatory_rows_are_counted(converted):
    res, *_ = converted
    assert res.stats["mandatory_rows_skipped"] == 1  # Claim Inquiry 'Claim Id' (UI URL)
    assert any(s["mandatory"] and s["field"] == "claim_id" for s in res.skipped)


def test_types_required_and_transforms(converted):
    res, *_ = converted
    assert field(res, "claim", "tax_id")["type"] == "string"        # Numeric in sheet, id-like
    assert field(res, "claim", "npi")["type"] == "string"
    assert field(res, "line", "charges")["type"] == "decimal"        # real money stays decimal
    assert field(res, "line", "units")["type"] == "decimal"
    assert field(res, "claim", "received_date")["type"] == "date"
    assert field(res, "claim", "claim_id")["required"] is True
    assert field(res, "claim", "npi")["required"] is False
    m = field(res, "claim", "member_name")
    assert m["paths"] == ["$.MEME_FIRST_NAME", "$.MEME_LAST_NAME"] and m["transform"] == "join_space"
    am = field(res, "line", "additional_modifiers")
    assert am["type"] == "list" and am["transform"] == "list_non_null"


def test_last_url_in_multi_url_cell_is_the_source(converted):
    res, *_ = converted
    assert field(res, "claim", "phone")["source"] == "family_coverage"
    assert field(res, "claim", "member_zip")["source"] == "family_coverage"
    assert field(res, "claim", "provider_name")["source"] == "claim_summary"


def test_no_environment_data_leaks_into_config(converted):
    _, _, path = converted
    text = path.read_text()
    for needle in ("facets.example.com", "10408", "10418", "00A0000001", "00A0000002", "00A0000003", "351550"):
        assert needle not in text, needle


def test_output_is_deterministic(sample_dir, tmp_path):
    a = write_outputs(convert(sample_dir / "sample_mapping.xlsx", "demo", sample_dir / "overrides.yaml"), tmp_path / "a")
    b = write_outputs(convert(sample_dir / "sample_mapping.xlsx", "demo", sample_dir / "overrides.yaml"), tmp_path / "b")
    assert a["config"].read_bytes() == b["config"].read_bytes()


def test_json_output_loads(sample_dir, tmp_path):
    p = write_outputs(convert(sample_dir / "sample_mapping.xlsx", "demo", sample_dir / "overrides.yaml"), tmp_path, "json")["config"]
    assert json.loads(p.read_text())["project"] == "demo"
    assert load_project_file(p).project == "demo"


def test_without_overrides_unbound_param_is_an_action_with_snippet(sample_dir):
    res = convert(sample_dir / "sample_mapping.xlsx", "demo")
    assert not res.loadable and res.needs_action
    snippet = next(i for i in res.issues if i.code == "UNBOUND_PARAM")
    assert "subscriber_key" in snippet.message and "from_source" in snippet.message
    assert "DISCOVERY_DEFAULT" in codes(res)  # lines would be probed until 404


# ---- overrides -----------------------------------------------------------------------
def write_ov(tmp_path, text):
    p = tmp_path / "ov.yaml"
    p.write_text(text)
    return p


BIND = """
sources:
  claims_inquiry_summary: {}
  family_coverage_inquiry:
    params: {subscriber_key: {from_source: claims_inquiry_summary, path: "$.SBSB_ID"}}
"""


def test_override_field_patch_and_skip(sample_dir, tmp_path):
    ov = write_ov(tmp_path, BIND + """
fields:
  claim.npi: {type: integer, required: true, path: "$.provider.npi"}
  claim.type: {skip: true}
""")
    res = convert(sample_dir / "sample_mapping.xlsx", "demo", ov)
    npi = field(res, "claim", "npi")
    assert (npi["type"], npi["required"], npi["paths"]) == ("integer", True, ["$.provider.npi"])
    assert not any(f["name"] == "type" and f["group"] == "claim" for f in res.config["fields"])


def test_override_add_field(sample_dir, tmp_path):
    ov = write_ov(tmp_path, BIND + """
add_fields:
  - {name: extra, group: claim, source: claims_inquiry_summary, path: "$.EXTRA", type: string}
""")
    res = convert(sample_dir / "sample_mapping.xlsx", "demo", ov)
    assert field(res, "claim", "extra")["paths"] == ["$.EXTRA"]


@pytest.mark.parametrize("text, msg", [
    ("bogus: 1", "unknown top-level"),
    ("settings: {nope: 1}", "unknown settings"),
    ("project: {nope: 1}", "unknown project"),
    ("sources: {does_not_exist: {}}", "unknown source"),
    ("fields: {claim.nothing: {skip: true}}", "unknown field"),
    ("source_aliases: {nope: x}", "unknown generated source"),
])
def test_bad_overrides_fail_loudly(sample_dir, tmp_path, text, msg):
    with pytest.raises(ConversionError, match=msg):
        convert(sample_dir / "sample_mapping.xlsx", "demo", write_ov(tmp_path, text))


def test_settings_path_template_changes_all_paths(sample_dir, tmp_path):
    ov = write_ov(tmp_path, BIND + "settings:\n  path_template: '$.data.{api_field_lower}'\n")
    res = convert(sample_dir / "sample_mapping.xlsx", "demo", ov)
    assert field(res, "claim", "provider_name")["paths"] == ["$.data.prpr_name"]
