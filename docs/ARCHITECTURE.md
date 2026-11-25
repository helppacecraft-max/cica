# Architecture

## 1. The idea in one picture

```
                         BUILD TIME (per project)                       RUN TIME
 ┌────────────┐  ┌──────────────┐   ┌────────────────────────┐   ┌───────────────────────────────┐
 │ Excel      │  │ overrides    │   │  mapping.yaml          │   │ FastAPI  POST /v1/claims/enrich│
 │ mapping    ├─▶│ .yaml (hand  ├──▶│  sources:  endpoints   ├──▶│  Planner → Executor → Extract  │
 │ sheet      │  │ decisions)   │   │  fields:   field↔path  │   │  → Coerce → Assemble           │
 └────────────┘  └──────────────┘   └────────────────────────┘   └───────────────┬───────────────┘
       converter (facets-wrapper convert)            + report.md                  │ GET (parallel)
                                                                                  ▼
                                                                      Facets REST endpoints
```

The code contains **no** Facets knowledge. Everything project-specific is in `mapping.yaml`.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **source** | One distinct Facets REST endpoint (URL template), e.g. `/Claims/{claim_id}/Inquiry/Summary` |
| **field** | One business field: which source, which JSONPath(s), type, required flag, optional transform |
| **group** | A section of the sheet (`claim`, `line`, `matchum`, …). Output is grouped the same way |
| **foreach source** | A source called once per item — e.g. once per claim line (`/Lines/{line_no}/Details`) |
| **project** | One customer/environment: its own sheet, its own `mapping.yaml`, its own base URL & credentials |

## 3. Request lifecycle

`POST /v1/claims/enrich` with `{"claim_id": "...", "groups": [...], "fields": [...], "strict": false}`

1. **Resolve project** – `X-Project` header (optional if only one project is loaded); check `X-API-Key` if one is configured.
2. **Validate input** – `claim_id` must match the project's `claim_id_pattern` (default `^[A-Za-z0-9]{6,20}$`). It is later URL-encoded again; two layers against path injection.
3. **Plan** (`engine/planner.py`) – select the requested fields (none given ⇒ all), find the *minimum* set of sources they need, add sources those depend on, and sort into **levels**. Asking for 3 fields never triggers calls for the other 30.
4. **Execute** (`engine/executor.py`, `client.py`) – run level by level; sources inside a level run in parallel (`asyncio.gather`, capped by a per-project semaphore).
   - *Bound params*: a URL placeholder like `{subscriber_key}` is filled from another source's response. The value must match `^[A-Za-z0-9_.\-]{1,64}$` or the source is **skipped** (`param_…_unresolved`) — upstream data can never add path segments or a query string.
   - *foreach*: discover the items (`probe` until the first 404 / `from_source` list or count / `fixed`), then fetch each (parallel except probe, which is sequential by nature).
   - *Retries*: timeouts, connection errors and 5xx are retried (default 2, exponential back-off). 4xx is not. 404 is a normal answer (“no such resource”), not an error.
5. **Extract & coerce** (`engine/extract.py`, `coerce.py`, `transforms.py`) – per field: JSONPath → (transform) → type coercion.
6. **Assemble** (`engine/assemble.py`) – build `data` and the parallel `field_status`, list errors and required failures, compute `complete`.
7. **Respond** – `200`; with `"strict": true` and `complete == false` ⇒ `502` with the same body.

```
levels for the sample config:   [claim_summary]  →  [family_coverage ‖ claim_cob ‖ claim_providers ‖ claim_lines(×N)]
                                  (needed for subscriber key + line count)       (all independent → parallel)
```
Latency ≈ two serial upstream hops + slowest call, however many sources are configured
(longer if lines must be *probed* — see §8).

## 4. Response contract

```jsonc
{
  "claim_id": "CLAIM000001",
  "complete": true,                      // false if ANY source failed/partial, or ANY required field is not "ok"
  "data": {
    "claim": { "provider_name": "…", "tax_id": "0123", "member_name": "Ann Lee" },   // object group
    "line":  [ { "line_no": 1, "charges": "125.00" }, { "line_no": 2, … } ]          // array group (foreach)
  },
  "field_status": {                      // SAME SHAPE as data; values are statuses
    "claim": { "provider_name": "ok", "tax_id": "null_in_source", "member_name": "ok" },
    "line":  [ { "line_no": 1, "charges": "ok" }, … ]
  },
  "required_failures": [ "claim.claim_id:source_not_found" ],
  "errors": [ { "source": "family_coverage", "code": "http_500", "http_status": 500, "state": "failed" },
              { "field": "line[line_no=2].charges", "code": "invalid_decimal" } ],
  "meta":   { "project": "demo", "config_version": 1, "correlation_id": "…", "latency_ms": 29,
              "sources": [ { "name": "claim_summary", "status": "ok", "http_status": 200, "attempts": 1, "latency_ms": 19, "items": null } ] },
  "provenance": { … }                    // only with include_provenance: source, paths, type, required per field
}
```

### Field statuses — a `null` is never ambiguous

| Status | Meaning |
|---|---|
| `ok` | value extracted and converted |
| `missing_in_source` | the path does not exist in the response *(suspect a wrong path or an API change)* |
| `null_in_source` | path exists but the value is `null` / empty string |
| `ambiguous_path` | a scalar field's path matched several values — refused rather than guessed |
| `coercion_error` | value could not be converted to the declared type (`errors[]` has the code, never the value) |
| `source_failed` | the upstream call failed or was skipped (see `errors[]`) |
| `source_not_found` | upstream answered 404 |

Source states in `meta.sources`: `ok`, `partial` (some items failed / discovery incomplete), `not_found`, `failed`, `skipped`.
Error codes: `http_NNN`, `timeout`, `connect_error:<Type>`, `invalid_json`, `response_root_missing`,
`dependency_<src>_unavailable`, `param_<name>_unresolved`, `discovery_invalid`, `probe_limit_reached`,
`discovery_limit_reached`, `no_items`, `internal_error:<Type>`.

### HTTP codes

`200` normal (even if `complete:false`) · `502` strict & incomplete · `422` bad claim ID / selection / body · `400` project required · `404` unknown project · `401` bad API key · `503` project not ready (no base URL / credentials).

### Rules worth knowing

* **Money is a string** (`"125.00"`) via `Decimal`; never a float. IDs, Tax ID, NPI, phone, zip are strings (leading zeros survive). Strings are trimmed (Facets CHAR padding).
* **Empty string = null.** `0` and `false` are values.
* **Per-line groups**: one row per discovered item, in ascending order, row key = the foreach param (`line_no`). If a group mixes per-line and claim-level fields (the sample’s `matchum.units`), the claim-level value is **repeated on every row** — the converter flags this (`MIXED_CARDINALITY_GROUP`) because it may not be what the business means.
* **404 on an optional source** (a claim with no COB) is not an error; **404 on a source holding a required field** makes `complete:false` through `required_failures`.

## 5. Module map

| Module | Responsibility | Knows about Facets? |
|---|---|---|
| `converter/excel_reader.py` | find header rows/columns/banners by label; read rows + hyperlinks | sheet layout only |
| `converter/normalize.py` | URL → template, API-field parsing, type map, slugs | URL conventions (configurable) |
| `converter/build.py` | assemble config, apply overrides, all warnings, write YAML/JSON + report | no |
| `config/models.py` | Pydantic schema (`extra="forbid"`: typos fail) | no |
| `config/validate.py` | cross-reference checks: placeholders bound, no cycles, transforms exist, JSONPath compiles… | no |
| `config/loader.py` | read YAML/JSON, load a directory of projects, plugin import | no |
| `engine/*` | plan → execute → extract → coerce → assemble | **no** |
| `api/app.py` | routes, auth, project resolution, reload | no |
| `contract.py` | replay recorded responses vs `expected.json` | no |
| `mock_upstream.py` | fake Facets generated from a config | no |

## 6. Multi-project

`configs/<project>/mapping.yaml` — every directory is loaded at startup. Environment-specific
values (base URL, credentials, TLS, timeouts, API key) come from env vars per project
(`FACETS_<PROJECT>_BASE_URL`, … see `settings.py`), **never** from the YAML, so one config can run
in dev/UAT/prod. Hosts, ports and literal claim IDs found in the sheet are deliberately discarded
by the converter.

Startup is **fail-closed**: if any config is invalid the service refuses to start. `POST /admin/reload`
(needs `FACETS_WRAPPER_ADMIN_KEY`) re-reads everything atomically and keeps the old configs if the new ones are invalid.

## 7. Extending

* **New project** – new sheet → `convert` → review report → add overrides → commit `mapping.yaml`.
* **Different JSON shape** – change `settings.path_template` (e.g. `$.data.{api_field_lower}`) or set `sources.<name>.response_root`; no code.
* **Special value logic** – register a transform in a plugin module and list it in `FACETS_WRAPPER_PLUGINS`:
  ```python
  from facets_wrapper.engine.transforms import register_transform
  @register_transform("phone_digits")            # single-value; use multi=True for several paths
  def phone_digits(v): return "".join(c for c in str(v) if c.isdigit())
  ```
* **Deliberately not configurable**: arbitrary code/expressions in YAML. If config can run logic it becomes an unreviewable programming language.

## 8. Known limits (honest list)

1. **Field paths are unverified until you run level 4 of `docs/TESTING.md`.** The sheet gives API field *names*, not JSON paths.
2. **Line discovery defaults to probing** (`Lines/1`, `2`, … until 404, max 50), sequentially. Correct but slow for big claims, and it hits the endpoint once more than needed. If the claim Summary exposes a line count/list, configure `discovery.mode: from_source`.
3. **Only GET + JSON**; auth = none/basic/bearer. No OAuth flows, no client-certificate mTLS (server-side CA bundle is supported).
4. **No caching, no batch endpoint, no wrapper-side rate limiting / circuit breaker.** Add at the gateway if needed.
5. **Facets “null” sentinels** (e.g. a minimum SQL date such as `1753-01-01`) are *not* assumed. If the real data uses them, list them in `project.null_values` via overrides.
6. **Not yet built:** live-capture of Facets responses (save them yourself and sanitise PHI), metrics/OpenTelemetry, per-caller authorisation beyond one API key per project.
7. The Excel reader was developed against the layout visible in four phone photos of the real sheet, **not the file itself**; layout is detected by header labels, but run `convert` and read the report before trusting it.

## 9. Security & PHI

* No upstream payloads are logged — only correlation ID, project, source names/states, latency. Coercion errors never echo values.
* Wrapper API: set `FACETS_WRAPPER_API_KEY_<PROJECT>`; without it the project is open (a warning is logged). Put it behind your gateway/TLS regardless.
* Upstream TLS verification is on by default; `…_VERIFY_TLS=false` exists for lab use only.
* `.gitignore` excludes `*.xlsx`, `/out`, `/local`, `.env`. Recorded samples/contract cases contain data — **sanitise before committing**.
