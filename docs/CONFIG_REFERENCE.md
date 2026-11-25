# `mapping.yml` reference

Normally generated (see [EXCEL_TO_CONFIG.md](EXCEL_TO_CONFIG.md)); read this to review or hand-write one.
Configs are **YAML**, named `mapping.yml` (`mapping.yaml` also loads). JSON config files are not supported. Unknown keys are rejected.

```yaml
schema_version: 1
project: emblem                       # [A-Za-z0-9_-]; selects the FACETS_<PROJECT>_* env vars
version: 3                            # echoed in every response as meta.config_version
claim_id_pattern: '^[A-Za-z0-9]{6,20}$'   # default pattern for the input named claim_id

request:                              # OPTIONAL. Omitted => a single required input: claim_id
  inputs:                             # identifiers the caller sends as flat keys in the body (max 20)
    - {name: claim_id}                          # required: true by default; pattern defaults to claim_id_pattern
    - {name: associate_id, pattern: '^[0-9]{1,10}$', required: false, label: Associate ID}
  at_least_one_of: []                 # e.g. [claim_id, associate_id] => "either ID is enough" (make both required: false)
null_values: []                       # raw strings treated as null before coercion
meta: {...}                           # informational (generated_from, source_sha256, ...)

sources:
  claim_summary:                      # name: [a-z][a-z0-9_]*
    path: /RestServices/facets/api/v1/Claims/{claim_id}/Inquiry/Summary   # starts with '/', no host, no '?'
    method: GET                       # only GET
    query: {}                         # query-string parameters; values may use {input} placeholders, e.g. {assoc: "{associate_id}"}
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

## Request inputs — rules

* A **source needs an input** if `{name}` appears in its path or query, directly or through a source it depends on. Every declared input must be used by at least one source (an unused one is rejected as a likely typo).
* Names: `[a-z][a-z0-9_]*`, unique, not `groups`/`fields`/`strict`/`include_provenance`, at most 20. A `params` binding or `foreach.param` cannot reuse an input name.
* Supplying fewer inputs than declared is fine when they are optional: fields that need a missing input are **dropped and listed in `meta.skipped_fields`** for broad selections, and are a **422** if you ask for them by name.
* Inputs are strings (JSON numbers are accepted and converted). `null` counts as "not supplied".

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

Secrets and environment-specific values live **only** here (never in `mapping.yml`, which is committed).
`<P>` = project name upper-cased, non-alphanumerics → `_` (`emblem-uat` → `EMBLEM_UAT`).

### Authenticating TO Facets (outbound)

Pick **one mode** with `FACETS_<P>_AUTH`. Any mode can also add extra headers.

| `AUTH` | What you set | What is sent |
|---|---|---|
| `none` (default) | — | no credentials |
| `basic` | `_USERNAME`, `_PASSWORD` | `Authorization: Basic base64(user:pass)` |
| `bearer` | `_TOKEN` | `Authorization: Bearer <token>` |
| `header` *(API key)* | `_AUTH_HEADER_VALUE` (secret), `_AUTH_HEADER_NAME` (default `X-API-Key`), `_AUTH_HEADER_PREFIX` (optional) | `<NAME>: <PREFIX><VALUE>` — e.g. `X-Facets-Key: abc`, or `Authorization: ApiKey abc` |
| `query` | `_AUTH_QUERY_NAME`, `_AUTH_QUERY_VALUE` | `?<name>=<value>` added to every call (API keys in URLs can end up in upstream/proxy logs — prefer a header) |
| *(any mode)* | `_HEADERS` = JSON object | extra fixed headers, e.g. `{"X-Tenant":"acme","X-Client-Id":"wrapper"}` |

```bash
# API key in a custom header
export FACETS_EMBLEM_AUTH=header FACETS_EMBLEM_AUTH_HEADER_NAME=X-Facets-Key FACETS_EMBLEM_AUTH_HEADER_VALUE='…'
# bearer token + a tenant header
export FACETS_EMBLEM_AUTH=bearer FACETS_EMBLEM_TOKEN='…' FACETS_EMBLEM_HEADERS='{"X-Tenant":"acme"}'
```
```powershell
$env:FACETS_EMBLEM_AUTH = "header"
$env:FACETS_EMBLEM_AUTH_HEADER_NAME = "X-Facets-Key"
$env:FACETS_EMBLEM_AUTH_HEADER_VALUE = "…"
$env:FACETS_EMBLEM_HEADERS = '{"X-Tenant":"acme"}'        # single quotes keep the JSON intact
```
Checked before any call (a problem makes the project *not ready*, 503, with the reason shown but never the secret): unknown mode,
missing values, header names with spaces, `Host`/`Content-Length`-style forbidden headers, CR/LF in values (header injection),
and an extra header that would collide with the auth header.

**Not supported (yet):** OAuth flows that first *fetch* a token (client-credentials / login → token → refresh), client-certificate mTLS.
If Facets needs a token from a login call, tell me the exact call and I'll add a mode.

**Don't know which style Facets uses?** Run `facets-wrapper probe` — see [TESTING.md](TESTING.md) §4.0.

### Connection and the wrapper's own API

| Variable | Default | |
|---|---|---|
| `FACETS_WRAPPER_CONFIG_DIR` | `configs` | where `<project>/mapping.*` live |
| `FACETS_<P>_BASE_URL` | — (**required**) | e.g. `https://facets-host:10408`; project is *not ready* (503) without it |
| `FACETS_<P>_VERIFY_TLS` / `_CA_BUNDLE` | `true` / — | TLS verification / custom CA file |
| `FACETS_<P>_TIMEOUT_S` / `_RETRIES` / `_MAX_CONCURRENCY` | `15` / `2` / `8` | |
| `FACETS_WRAPPER_API_KEY_<P>` | unset = open | **inbound**: callers of the wrapper send `X-API-Key` (unrelated to the outbound auth above) |
| `FACETS_WRAPPER_ADMIN_KEY` | unset = reload disabled | `POST /admin/reload` with `X-Admin-Key` |
| `FACETS_WRAPPER_PLUGINS` | — | comma-separated module names |
| `FACETS_WRAPPER_LOG_LEVEL` | `INFO` | |

## HTTP API

| Endpoint | |
|---|---|
| `POST /v1/claims/enrich` | body: the project's **inputs** (flat keys, e.g. `claim_id`, `associate_id`) + controls `groups?`, `fields?`, `strict?`, `include_provenance?` · headers: `X-Project`, `X-API-Key`, `X-Correlation-ID` |
| `GET /v1/catalog` | the project's request inputs (name/required/pattern/label, `at_least_one_of`), groups, fields (type/required/label/source), sources |
| `GET /v1/projects` | loaded projects |
| `GET /healthz` · `GET /readyz` | liveness · readiness (503 if any project lacks base URL/credentials) |
| `POST /admin/reload` | atomic config reload |
| `GET /docs` | OpenAPI UI |
