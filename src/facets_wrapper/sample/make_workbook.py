"""Generate a SAMPLE mapping workbook with the same layout as the real sheet.

Everything here is fictional (host ``facets.example.com``, claim IDs ``00A0000001xx``). It also
deliberately reproduces the *kinds* of defects found in the real sheet, so the converter's
warnings can be seen working: fill-down URLs, a UI URL, an NA row, a duplicate mapping, a
multi-URL cell, a bracket placeholder, Numeric IDs, a mixed per-line/claim-level group, ...
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.worksheet.hyperlink import Hyperlink

H = "https://facets.example.com:10408"
API = "/RestServices/facets/api/v1"
SUMMARY = f"{H}/{API.lstrip('/')}/Claims/00A000000100/Inquiry/Summary/"
SUMMARY_DS = f"{H}//{API.lstrip('/')}/Claims/00A000000100/Inquiry/Summary/"  # double slash, as in the real sheet
LINES = f"{H}{API}/Claims/00A000000200/Inquiry/Lines/1/Details"
COB = f"{H}{API}/Claims/00A000000300/Inquiry/COB"
PROVIDERS = f"{H}{API}/Claims/00A000000200/Inquiry/Providers"
FAMILY_FIXED = f"{H}{API}/Family/Coverage/351550/Inquiry/"
FAMILY_BRACKET = f"{H}//{API.lstrip('/')}/family/coverage/[clcl_id]/Inquiry"
UI = f"{H}/?AdvancedSearch=N&ClaimType=M&ClaimId=00A0000002"

HEADER = ["Field Name", "Data Type", "Default Value", "Facets Table", "Facets Column",
          "Field Name", "API Field name", "URL", "Comments"]

# (web name, type, default, facets table, facets column, rest name, api field, url cell)
CLAIM = [
    ("Claim Id", "Alphanumeric", "Mandatory", "CMC_CLCL_CLAIM", "CLCL_ID", "Claim Id", "CLCL_ID", SUMMARY_DS),
    ("Provider Name", "String", "Mandatory", "CMC_PRPR_PROV", "PRPR_NAME", "Provider Name", "PRPR_NAME", SUMMARY_DS),
    ("Tax ID", "Numeric", "Mandatory", "CMC_PRPR_PROV", "MCTN_ID", "Tax ID", "MCTN_ID", SUMMARY_DS),
    ("NPI", "Numeric", "Blank", "CMC_PRPR_PROV", "PRPR_NPI", "NPI", "PRPR_NPI", SUMMARY_DS),
    ("Type", "String", "Mandatory", "CMC_PRPR_PROV", "CLCL_CL_TYPE", "Type", "CLCL_CL_TYPE", SUMMARY_DS),
    ("Provider ID", "Numeric", "Mandatory", "CMC_CLCL_CLAIM", "PRPR_ID", "Provider ID", "PRPR_ID", SUMMARY_DS),
    ("Phone", "String", "Blank", "CMC_PRAD_ADDRESS", "PRAD_PHONE", "Phone", "PRAD_PHONE",
     f"{SUMMARY_DS} Get the subscriber key and then execute the below URL - {FAMILY_FIXED}"),
    ("Network Indicator", "String", "Mandatory", "CMC_CLCL_CLAIM", "CIV8_CLCL_NTWK_IND_DE", "Network Indicator", "CIV8_CLCL_NTWK_IND_DE", SUMMARY_DS),
    ("Member Name", "String", "Mandatory", "CMC_MEME_MEMBER", "MEME_FIRST_NAME & MEME_LAST_NAME", "Member Name", "MEME_FIRST_NAME & MEME_LAST_NAME", SUMMARY_DS),
    ("Received Date", "Date", "Mandatory", "CMC_CLCL_CLAIM", "CLCL_RECD_DT", "Received Date", "CLCL_RECD_DT", SUMMARY_DS),
    ("Statement From Date", "Date", "Blank", "CMC_CLHP_HOSP", "CDML_FROM_DT", "Statement From Date", "CDML_FROM_DT", SUMMARY_DS),
    ("", "", "", "", "", "Member Zip", "SBAD_ZIP", f"{SUMMARY_DS} and select the below url - {FAMILY_BRACKET}"),
    ("", "", "", "", "", "Remit Payee Name", "CI10_CLRC_PAYER_REMIT_NAME", COB),
    ("", "", "", "", "", "PCP NPI", "CI11_PCP_PRPR_NPI", PROVIDERS),
]
LINE = [
    ("Claim Id", "Alphanumeric", "Mandatory", "CMC_CDML_CL_LINE", "CLCL_ID", "Claim Id", "CLCL_ID", LINES),
    ("From Date", "Date", "Mandatory", "CMC_CDML_CL_LINE", "CDML_FROM_DT", "From Date", "CDML_FROM_DT", LINES),
    ("To Date", "Date", "Mandatory", "CMC_CDML_CL_LINE", "CDML_TO_DT", "To Date", "CDML_TO_DT", LINES),
    ("Revenue Code", "Numeric", "Blank", "CMC_CDML_CL_LINE", "RCRC_ID", "Rev", "RCRC_ID", LINES),
    ("Proc", "Alphanumeric", "Blank", "CMC_CDML_CL_LINE", "IPCD_ID", "Proc", "IPCD_ID", LINES),
    ("Units", "Numeric", "Mandatory", "CMC_CDML_CL_LINE", "CDML_UNITS", "Units", "CDML_UNITS", LINES),
    ("Charges", "Numeric", "Mandatory", "CMC_CDML_CL_LINE", "CDML_CHG_AMT", "Charges", "CDML_CHG_AMT", LINES),
    ("Additional Modifiers", "String", "Blank", "CMC_CDML_CL_LINE", "CDML_IPCD_MOD2, CDML_IPCD_MOD3", "Additional Modifiers", "CDML_IPCD_MOD2, CDML_IPCD_MOD3", LINES),
    ("Modifiers", "String", "Blank", "NA", "NA", "Modifiers", "IPCD_ID", LINES),  # duplicate of Proc (sheet defect)
]
MATCHUM = [
    ("Claim Id", "Alphanumeric", "Mandatory", "CMC_CLCL_CLAIM", "CLCL_ID", "Claim Id", "CLCL_ID", LINES),
    ("Review", "String", "Blank", "No direct match", "", "Review", "NA", "Blank"),
    ("Ref. Number", "Alphanumeric", "Blank", "CMC_UMUM_UTIL_MGT", "UMUM_REF_ID", "Ref. Number", "CDML_REFSV_SEQ_NO", LINES),
    ("Units", "Numeric", "Blank", "CMC_UMSV_SERVICES", "UMSV_UNITS_AUTH", "Units", "CDML_UNITS", SUMMARY),  # claim-level in a per-line group
    ("", "", "", "", "", "UM Svc", "UM_SVC", f"{H.replace('10408', '10418')}"),  # bare host, other port
]
INQUIRY = [
    ("Claim Id", "Alphanumeric", "Mandatory", "CMC_CLCL_CLAIM", "CLCL_ID", "Claim Id", "CLCL_ID", f"{UI}5400"),
    ("HistoryClaim", "Alphanumeric", "Blank", "CMC_CLCL_CLAIM", "CLCL_ID_ADJ_FROM", "HistoryClaim", "CLCL_ID_ADJ_FROM", f"{UI}5401"),
    ("Member", "String", "Blank", "CMC_MEME_MEMBER", "MEME_FIRST_NAME", "Member", "MEME_FIRST_NAME", f"{UI}5402"),
    ("Provider", "String", "Blank", "CMC_CLCL_CLAIM", "PRPR_ID", "Provider", "PRPR_ID", f"{UI}5403"),
    ("Paid Amount", "Numeric", "Blank", "CMC_CLCL_CLAIM", "CLCL_TOT_PAYABLE", "Paid Amount", "CLCL_TOT_PAYABLE", SUMMARY_DS),
]

BLOCKS = [("Claim Level Data", CLAIM), ("Line Level Data", LINE), ("MatchUM", MATCHUM), ("Claim Inquery", INQUIRY)]


def build_workbook(path: str | Path) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mapping Sheet"
    r = 1
    for title, rows in BLOCKS:
        ws.cell(r, 1, title)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
        ws.cell(r, 6, title)
        ws.merge_cells(start_row=r, start_column=6, end_row=r, end_column=9)
        ws.cell(r + 1, 1, "Web Services")
        ws.cell(r + 1, 6, "REST Services")
        r += 2
        for c, h in enumerate(HEADER, 1):
            ws.cell(r, c, h)
        for row in rows:
            r += 1
            for c, v in enumerate(row, 1):
                ws.cell(r, c, v or None)
        r += 3  # blank spacer rows between blocks
    wb.save(path)
    return Path(path)


# Hand decisions for the sample: names, how to get the subscriber key, how to find line numbers.
SAMPLE_OVERRIDES = """\
# Hand-maintained decisions. Re-running `convert` re-applies these; nothing here is lost.
project:
  version: 1

source_aliases:
  claims_inquiry_summary: claim_summary
  claims_inquiry_lines_details: claim_lines
  family_coverage_inquiry: family_coverage
  claims_inquiry_cob: claim_cob
  claims_inquiry_providers: claim_providers

sources:
  family_coverage:
    params:
      subscriber_key: {from_source: claim_summary, path: "$.SBSB_ID"}
  claim_lines:
    foreach:
      discovery: {mode: from_source, source: claim_summary, path: "$.LINE_COUNT", kind: count, max: 50}

fields:
  claim.type:
    skip: true        # sheet maps it to the wrong table; excluded until confirmed
  # line.modifiers:   # the report flags it as a duplicate of line.proc (copy/paste error in the sheet);
  #   skip: true      # uncomment to drop it
"""


def write_sample(dir_: str | Path) -> tuple[Path, Path]:
    d = Path(dir_)
    d.mkdir(parents=True, exist_ok=True)
    x = build_workbook(d / "sample_mapping.xlsx")
    o = d / "overrides.yml"
    o.write_text(SAMPLE_OVERRIDES, encoding="utf-8")
    return x, o
