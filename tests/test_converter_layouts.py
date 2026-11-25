"""The converter must locate columns/sections from labels, not from fixed positions."""
from __future__ import annotations

import openpyxl

from facets_wrapper.converter.build import convert
from facets_wrapper.converter.excel_reader import read_sections

U = "https://h.example:1/RestServices/facets/api/v1/Claims/ABC123/Inquiry/Summary/"


def book(path, blocks, offset=0, header=None, spacer=True):
    """blocks: [(title_rows, [data rows as 8-tuples])]; offset shifts everything right."""
    header = header or ["Field Name", "Data Type", "Default Value", "Facets Table", "Facets Column",
                        "Field Name", "API Field name", "URL", "Comments"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Whatever"
    r = 1
    for titles, rows in blocks:
        for t in titles:
            ws.cell(r, 1 + offset, t)
            r += 1
        for c, h in enumerate(header):
            ws.cell(r, 1 + offset + c, h)
        for row in rows:
            r += 1
            for c, v in enumerate(row):
                ws.cell(r, 1 + offset + c, v or None)
        r += 2 if spacer else 1
    wb.save(path)
    return path


ROW = ("Claim Id", "Alphanumeric", "Mandatory", "CMC_CLCL_CLAIM", "CLCL_ID", "Claim Id", "CLCL_ID", U, "a comment")
ROW2 = ("Provider Name", "String", "Blank", "CMC_PRPR_PROV", "PRPR_NAME", "Provider Name", "PRPR_NAME", U, "")


def test_columns_shifted_right_by_two(tmp_path):
    p = book(tmp_path / "a.xlsx", [(["Claim Level Data", "REST Services"], [ROW, ROW2])], offset=2)
    res = convert(p, "p")
    assert [f["name"] for f in res.config["fields"]] == ["claim_id", "provider_name"]
    assert res.config["fields"][0]["notes"] == "a comment"


def test_truncated_filter_header_labels_as_in_matchum_block(tmp_path):
    hdr = ["Field Name", "Data Typ", "Default Va", "Facets Table", "Facets Column", "Field Name", "API Field", "URL", "Co"]
    p = book(tmp_path / "b.xlsx", [(["MatchUM", "REST Services"], [ROW, ROW2])], header=hdr)
    res = convert(p, "p")
    f = res.config["fields"]
    assert f[0]["required"] is True and f[1]["required"] is False
    assert res.config["fields"][0]["group"] == "matchum"


def test_single_banner_row_and_no_spacer_between_blocks(tmp_path):
    p = book(tmp_path / "c.xlsx", [(["Claim Level Data"], [ROW]), (["Line Level Data"], [ROW2])], spacer=False)
    _, secs = read_sections(p)
    assert [s.title for s in secs] == ["Claim Level Data", "Line Level Data"]
    assert [len(s.rows) for s in secs] == [1, 1]   # the banner of block 2 is not swallowed as a data row of block 1


def test_banner_with_only_service_words_gets_a_fallback_title(tmp_path):
    p = book(tmp_path / "d.xlsx", [(["REST Services"], [ROW])])
    res = convert(p, "p")
    assert res.config["fields"][0]["group"].startswith("section_row_")


def test_group_alias_override_in_settings(tmp_path):
    p = book(tmp_path / "e.xlsx", [(["Claim Level Data", "REST Services"], [ROW])])
    ov = tmp_path / "ov.yml"
    ov.write_text("settings:\n  group_aliases: {'claim level data': 'header'}\n")
    assert convert(p, "p", ov).config["fields"][0]["group"] == "header"


def test_repeated_field_name_in_group_is_renamed_and_reported(tmp_path):
    p = book(tmp_path / "f.xlsx", [(["Claim Level Data"], [ROW, ROW])])
    res = convert(p, "p")
    assert [f["name"] for f in res.config["fields"]] == ["claim_id", "claim_id_2"]
    assert any(i.code == "DUPLICATE_FIELD_NAME" for i in res.issues)
    assert any(i.code == "DUPLICATE_MAPPING" for i in res.issues)


def test_row_without_rest_name_falls_back_to_web_name(tmp_path):
    row = ("Web Only Name", "String", "Blank", "T", "C", "", "CLCL_X", U, "")
    res = convert(book(tmp_path / "g.xlsx", [(["Claim Level Data"], [row])]), "p")
    assert res.config["fields"][0]["name"] == "web_only_name"


def test_invalid_api_field_is_skipped_not_guessed(tmp_path):
    row = ("X", "String", "Mandatory", "T", "C", "X", "SOME FIELD (see note)", U, "")
    res = convert(book(tmp_path / "h.xlsx", [(["Claim Level Data"], [row])]), "p")
    assert res.config["fields"] == [] and res.skipped[0]["code"] == "INVALID_API_FIELD" and res.skipped[0]["mandatory"]
