# Excel → config

```bash
facets-wrapper convert --excel MappingSheet.xlsx --project emblem [--overrides overrides.yaml] [--out configs] [--sheet "Mapping Sheet"] [--format yaml|json] [--strict]
```
Writes `configs/<project>/mapping.yaml`, `report.md`, `report.json`.
Exit code: `0` ok · `1` unreadable input/bad overrides · `2` with `--strict` if anything needs action or the config is not loadable.

> Close the workbook in Excel first (or convert a copy): Excel locks the file and the read fails.

## 1. Sheet layout the converter understands

Located **by header label**, not by column letter, so inserted/blank columns don't matter:

```
 row n-2   Claim Level Data          ← section banner (merged cells are fine)
 row n-1   Web Services / REST Services
 row n     Field Name | Data Type | Default Value | Facets Table | Facets Column | Field Name | API Field name | URL | Comments
 row n+1…  data rows
```

| Column (header label) | Used for |
|---|---|
| left `Field Name` | fallback business name |
| `Data Type` | `Alphanumeric/String→string`, `Numeric→decimal` (see ID rule), `Date→date`, … (`settings.type_map`) |
| `Default Value` | **`Mandatory` ⇒ `required: true`**, anything else ⇒ optional. (Despite its name this column holds requiredness, not a default.) |
| `Facets Table`, `Facets Column` | informational (`facets:` block in the config); **not used to call anything** |
| right `Field Name` | business field name → slug (`Claim Id` → `claim_id`) |
| `API Field name` | the JSON key → JSONPath via `settings.path_template` (default `$.{api_field}`) |
| `URL` (text or hyperlink target) | which endpoint → a *source* |
| `Comments` | copied to the field's `notes` |

Any number of sections; each banner becomes a **group** (`Claim Level Data → claim`, `Line Level Data → line`, `MatchUM → matchum`, …; rename via `settings.group_aliases`).

## 2. What the converter does to each row

1. Skips (and **reports**) rows with no field name, no/`NA` API field, no URL, or a URL that is not a REST endpoint. It never guesses.
2. **URL → source.** Host/port are discarded. Literal IDs after `Claims/`, `Lines/`, `Coverage/` become `{claim_id}`, `{line_no}`, `{subscriber_key}`. `//` is collapsed. A URL is "REST" only if its path matches `settings.rest_path_regex` (default `/RestServices/.+/api/`) and has no query string — so UI/search URLs are rejected.
   Source name = path without version/placeholders: `Claims/{claim_id}/Inquiry/Summary` → `claims_inquiry_summary` (rename with `source_aliases`).
3. **A cell with several URLs** (e.g. Phone: *"…Summary/ Get the subscriber key and then execute the below URL - …Family/Coverage/351550/Inquiry/"*): the **last** URL is the field's source; earlier ones are recorded as prerequisites in the source's `notes`.
4. **Several API fields** in one cell: `A & B` → `paths:[A,B]` + `join_space` (string); `A, B` → `list_non_null` (list).
5. **Type rule:** a `Numeric` field whose API name ends in `_ID/_NPI/_PHONE/_ZIP/_CODE/_NO/_NUM` (or whose label is Tax ID, NPI, Phone, Provider ID, Class, Frequency, POS, TOS, Rev, Type) becomes `string`, so leading zeros survive. Real amounts/units stay `decimal`.
6. Same business name twice in a group ⇒ second becomes `name_2` (reported).
7. Foreach: any source whose path has `{line_no}` gets `foreach` with `probe` discovery (reported as `DISCOVERY_DEFAULT`).

Output is **deterministic** (no timestamps; source-file SHA-256 recorded) so config diffs in git show exactly what changed.

## 3. The overrides file — where human decisions live

The sheet cannot say *which JSON key holds the subscriber key* or *how to find line numbers*. Those decisions go in `overrides.yaml`; re-running `convert` re-applies them, so regenerating from an updated sheet never loses work. Unknown keys / unknown targets are **errors** (a typo must not silently do nothing).

```yaml
settings:                       # change how the SHEET is interpreted (all optional; see converter/defaults.py)
  path_template: "$.{api_field}"          # e.g. "$.data.{api_field_lower}" if responses are camelCase/lowercase/wrapped
  rest_path_regex: "/RestServices/.+/api/"
  group_aliases: { "claim level data": "claim" }
  type_map: { numeric: decimal }
  string_api_field_patterns: ["_ID$", "_NPI$"]

project:                        # config-level values
  version: 2
  claim_id_pattern: "^[0-9]{2}[A-Z][0-9]{9}$"      # tighten to your real claim-ID format
  null_values: ["1753-01-01", "9999-12-31"]         # raw strings to treat as null (if Facets uses sentinels)

source_aliases:                 # rename generated sources (do this FIRST; later keys use the new names)
  claims_inquiry_summary: claim_summary
  family_coverage_inquiry: family_coverage

sources:                        # patch a source
  family_coverage:
    params:
      subscriber_key: {from_source: claim_summary, path: "$.SBSB_ID"}     # REQUIRED for any {placeholder} other than claim_id / line_no
  claims_inquiry_lines_details:
    foreach:
      discovery: {mode: from_source, source: claim_summary, path: "$.LINE_COUNT", kind: count, max: 200}
    response_root: "$.data"       # if the payload is wrapped
    timeout_s: 30

fields:                         # patch / drop a field:  <group>.<field>
  claim.npi:  {type: string, required: false, path: "$.provider.npi"}
  claim.type: {skip: true}

add_fields:                     # fields the sheet doesn't have
  - {name: extra, group: claim, source: claim_summary, path: "$.EXTRA", type: string}
```
Merge order: `project` → `source_aliases` → `sources` → `fields` → `add_fields`; objects are deep-merged, lists replaced.

## 4. The report — what to do with it

`report.md` sections: **ERRORS** (config invalid) → **ACTION REQUIRED** (you must decide) → **WARNINGS** (probably a sheet defect) → **INFO**, then *Skipped rows* and a *Sources* table. “Config loadable: NO” means the service will refuse to start until fixed — by design.

| Code | Severity | Meaning / what to do |
|---|---|---|
| `UNBOUND_PARAM` | action | A URL needs an ID from another response. The report prints the exact YAML to add to overrides; fill in the JSONPath. |
| `CONFIG_INVALID` | error | Semantic validation failed (message says why). |
| `NON_REST_URL` | warn | URL isn't a REST endpoint (UI URL, bare host, query string). Field skipped. **Mandatory fields skipped are flagged and counted.** Fix the URL in the sheet or add the field via `add_fields`. |
| `FILL_SERIES_URLS` | warn | Consecutive URLs differ only by an incrementing number — an Excel drag-fill accident. Fix the sheet. |
| `DUPLICATE_MAPPING` | warn | Two business fields map to the same API field in one group (e.g. *Modifiers* = *Proc*). Probably a copy-paste error. |
| `BRACKET_PLACEHOLDER` | warn | URL has `[clcl_id]` where the sheet elsewhere says the key is the subscriber key. Confirm which ID the endpoint takes. |
| `MIXED_CARDINALITY_GROUP` | warn | A group mixes per-line and claim-level fields; claim-level values repeat on each row. Confirm that's intended. |
| `DISCOVERY_DEFAULT` | warn | Lines will be probed until 404. Prefer `from_source` if Summary exposes lines. |
| `MULTIPLE_ENDPOINTS` | warn | REST URLs point at several host:port pairs; only the env base URL is used. |
| `INVALID_API_FIELD`, `TYPE_UNKNOWN`, `DUPLICATE_FIELD_NAME` | warn | Row skipped / type defaulted to string / name suffixed. |
| `NO_API_FIELD`, `NO_URL`, `NO_FIELD_NAME` | info (warn if mandatory) | Row skipped. |
| `MULTI_URL_CELL`, `MULTI_API_FIELD`, `TYPE_NUMERIC_ID_LIKE`, `MISSING_TYPE_INFO`, `DOUBLE_SLASH`, `LITERAL_IDS_REPLACED`, `CASE_VARIANT_PATHS` | info | Heuristics applied; skim to confirm. |

## 5. Workflow

1. `convert` without overrides → read `report.md`.
2. Create `overrides.yaml` from the ACTION REQUIRED snippets (aliases first, then params, discovery, field fixes).
3. `convert` again until *Config loadable: YES*.
4. **Fix defects in the sheet itself** where you own it (fill-series URLs, duplicates) — overrides are a patch, the sheet is the source of truth for the business.
5. Commit `mapping.yaml` + `overrides.yaml` (not the workbook: it may carry internal hosts/IDs).
6. Verify against real responses → [TESTING.md](TESTING.md) level 4.
