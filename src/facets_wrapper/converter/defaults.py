"""Default converter settings. Every key can be overridden in the ``settings:`` block of the
overrides file, so a project whose sheet looks slightly different needs no code change."""

from __future__ import annotations

DEFAULT_SETTINGS: dict = {
    # Which worksheet to read. null -> "Mapping Sheet" if present, else the active sheet.
    "sheet": None,
    # How an API field name becomes a JSONPath. Placeholders: {api_field} {api_field_lower}
    # {api_field_camel}. If the real REST responses wrap or rename keys, change this once here.
    "path_template": "$.{api_field}",
    # A URL is a REST service URL only if its path matches this regex; anything else
    # (UI search URLs, bare hosts, ...) is reported and skipped, never guessed.
    "rest_path_regex": r"/RestServices/.+/api/",
    # Path segment AFTER one of these keywords is a literal ID in the sheet -> becomes a {param}.
    "path_param_rules": [
        {"after": "Claims", "param": "claim_id"},
        {"after": "Lines", "param": "line_no"},
        {"after": "Coverage", "param": "subscriber_key"},
    ],
    # ADDED to path_param_rules (additive, so the defaults above are never lost). Example for an
    # associate-based endpoint:  [{"after": "Associates", "param": "associate_id"}]
    "extra_path_param_rules": [],
    # Section banner (lower-cased) -> group name. Unlisted banners are slugified.
    "group_aliases": {
        "claim level data": "claim",
        "line level data": "line",
        "matchum": "matchum",
        "pre auth and referral": "pre_auth_referral",
        "claim inquery": "claim_inquiry",  # sic (typo exists in the source sheet)
        "claim inquiry": "claim_inquiry",
        "historyclaiminformation": "history_claim",
    },
    # Sheet "Data Type" -> config type
    "type_map": {
        "alphanumeric": "string",
        "string": "string",
        "numeric": "decimal",
        "number": "decimal",
        "integer": "integer",
        "date": "date",
        "datetime": "datetime",
        "boolean": "boolean",
    },
    # Numeric-typed fields that are really identifiers are forced to string (leading zeros!).
    "string_api_field_patterns": [r"_ID$", r"_NPI$", r"_PHONE$", r"_ZIP$", r"_CODE$", r"_NO$", r"_NUM$"],
    "string_business_names": ["tax id", "npi", "phone", "provider id", "class", "frequency", "pos", "tos", "rev", "type"],
    # API-field cell values meaning "nothing to map"
    "not_applicable_values": ["", "na", "n/a", "none", "-", "blank", "no direct match"],
    # Rows with these "Default Value" cell texts are required fields.
    "required_values": ["mandatory", "required"],
}
