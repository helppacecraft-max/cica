# `mapping.yaml` reference

Normally generated (see [EXCEL_TO_CONFIG.md](EXCEL_TO_CONFIG.md)); read this to review or hand-write one.
YAML and JSON are both accepted (`mapping.yaml` / `mapping.yml` / `mapping.json`). Unknown keys are rejected.

```yaml
schema_version: 1
project: emblem                       # [A-Za-z0-9_-]; selects the FACETS_<PROJECT>_* env vars
version: 3                            # echoed in every response as meta.config_version
claim_id_pattern: '^[A-Za-z0-9]{6,20}$'   # claim_id in requests must match
null_values: []                       # raw strings treated as null before coercion
meta: {...}                           # informational (generated_from, source_sha256, ...)

sources:
  claim_summary:                      # name: [a-z][a-z0-9_]*
    path: /RestServices/facets/api/v1/Claims/{claim_id}/Inquiry/Summary   # starts with '/', no host, no '?'
    method: GET                       # only GET
    query: {}                         # fixed query-string parameters
    params:                           # fill other placeholders from another source's response
      subscriber_key: {from_source: claim_summary, path: "$.SBSB_ID"}     # source must exist, not be foreach, no cycles
    foreach:                          # call once per item
      param: line_no                  # the placeholder to fill; also the row key in the output
      discovery:
        mode: probe                   # probe | fixed | from_source
        start: 1
        max: 50                       # hard safety cap (1..1000)
        # fixed:        values: [1, 2, 3]
        # from_source:  source: claim_summary, path: "$.lines[*].seq", kind: values   (or kind: count, path -> an integer)
    response_root: "$.data"           # JSONPath to unwrap the payload (must match exactly once)
    timeout_s: 15                     # per-source override
    notes: free text
    origin: {...}                     # informational

fields:
  - name: provider_name               # [a-z][a-z0-9_]*; unique within its group
    group: claim                      # output group name: [a-z][a-z0-9_]*
    source: claim_summary
    path: "$.PRPR_NAME"               # shorthand for paths: ["$.PRPR_NAME"]
    # paths: ["$.MEME_FIRST_NAME", "$.MEME_LAST_NAME"]   # several paths REQUIRE a multi-value transform
    type: string                      # string | integer | decimal | boolean | date | datetime | list | any
    required: true                    # not "ok" => listed in required_failures, complete=false
    transform: join_space             # optional, see below
    date_formats: ["%d.%m.%Y"]        # extra formats tried first (date/datetime only)
    label: Provider Name              # business label (informational; shown in /v1/catalog)
    facets: {table: CMC_PRPR_PROV, column: PRPR_NAME}   # informational
    notes: free text
```

JSONPath is `jsonpath-ng` (extended): `$.A.B`, `$.items[0].id`, `$.items[*].id`, `$.items[?(@.type=='X')].id`.
A scalar field whose path matches more than one value is an `ambiguous_path` error — use a more specific path or `type: list`.

## Type behaviour

| type | accepts | returns |
|---|---|---|
| `string` | str, int, float, bool | trimmed string (`true`/`false` for bool) |
| `integer` | int, integral float, digit string | int (bool rejected) |
| `decimal` | number or numeric string (no thousands separators, no NaN/Inf) | **exact string**, e.g. `"125.00"` |
| `boolean` | bool, 0/1, true/false/yes/no/y/n/t/f | bool |
| `date` | ISO, `YYYY-MM-DDTHH:MM:SS[Z]`, `MM/DD/YYYY`, `YYYYMMDD`, `date_formats` | `YYYY-MM-DD` (time part dropped) |
| `datetime` | same | ISO-8601 |
| `list` | list, or any scalar (wrapped) | list |
| `any` | anything | unchanged |

## Built-in transforms

| name | takes | does |
|---|---|---|
| `identity`, `strip`, `upper`, `lower` | 1 path | as named |
| `join_space`, `join_comma` | ≥1 paths | joins non-empty parts; null if none |
| `first_non_null` | ≥1 paths | first non-empty |
| `list_non_null` | ≥1 paths | list of non-empty (use `type: list`) |

Custom: `@register_transform("name", multi=False|True)` in a module listed in `FACETS_WRAPPER_PLUGINS`.

## Environment variables

| Variable | Default | |
|---|---|---|
| `FACETS_WRAPPER_CONFIG_DIR` | `configs` | where `<project>/mapping.*` live |
| `FACETS_<P>_BASE_URL` | — (**required**) | e.g. `https://facets-host:10408`; project is *not ready* (503) without it |
| `FACETS_<P>_AUTH` | `none` | `none` · `basic` (`_USERNAME`, `_PASSWORD`) · `bearer` (`_TOKEN`) |
| `FACETS_<P>_VERIFY_TLS` / `_CA_BUNDLE` | `true` / — | TLS verification / custom CA file |
| `FACETS_<P>_TIMEOUT_S` / `_RETRIES` / `_MAX_CONCURRENCY` | `15` / `2` / `8` | |
| `FACETS_WRAPPER_API_KEY_<P>` | unset = open | callers send `X-API-Key` |
| `FACETS_WRAPPER_ADMIN_KEY` | unset = reload disabled | `POST /admin/reload` with `X-Admin-Key` |
| `FACETS_WRAPPER_PLUGINS` | — | comma-separated module names |
| `FACETS_WRAPPER_LOG_LEVEL` | `INFO` | |

`<P>` = project name upper-cased, non-alphanumerics → `_` (`emblem-uat` → `EMBLEM_UAT`).

## HTTP API

| Endpoint | |
|---|---|
| `POST /v1/claims/enrich` | body: `claim_id`, `groups?`, `fields?`, `strict?`, `include_provenance?` · headers: `X-Project`, `X-API-Key`, `X-Correlation-ID` |
| `GET /v1/catalog` | groups, fields (type/required/label/source), sources for the project |
| `GET /v1/projects` | loaded projects |
| `GET /healthz` · `GET /readyz` | liveness · readiness (503 if any project lacks base URL/credentials) |
| `POST /admin/reload` | atomic config reload |
| `GET /docs` | OpenAPI UI |
