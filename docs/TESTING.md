# Execution testing guide

Four levels, cheapest first. **Levels 1–3 prove the software works. Only level 4 proves the
*mapping* is right** — and that is what "100% accuracy" actually depends on.

| Level | Needs the upstream? | Proves | Time |
|---|---|---|---|
| 1. Automated suite | no | engine, converter, API behave as designed (192 tests) | 10 s |
| 2. `selftest` | no | whole pipeline works on sample data | 5 s |
| 3. Manual run vs mock upstream | no | you can operate it; failure behaviour visible | 15 min |
| 4. Verify against the real upstream | **yes** | **field paths and values are correct** | your sign-off |

> The mock (level 3) generates its JSON *from the config itself*, so it can never disagree with the
> config. It tests plumbing, not mapping. Don't let a green level 1–3 stand in for level 4.

## 0. Setup

**bash / macOS / Linux**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```
**Windows PowerShell**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process RemoteSigned
pip install -e ".[dev]"
```
Requires Python ≥ 3.10.

## 1. Automated suite

```bash
pytest                 # everything
pytest tests/test_converter.py -v        # just the Excel converter
pytest -k "retry or probe"               # by keyword
```
What is covered: Excel layouts (shifted columns, truncated headers, merged banners, hyperlinks), every
conversion warning, overrides errors, determinism, **no host/claim-ID leakage into config**; config
validation (cycles, unbound params, bad JSONPath…); coercion (exact decimals, dates, no value echo);
planner (minimum calls, ordering); executor (retries, 404 vs 5xx, partial line failure, probe limit,
path-injection attempts, response_root, discovery modes); API (strict mode, auth, multi-project,
readiness, reload keeps old config on bad new one); contract-test and `validate --samples`.

All green ⇒ exit code 0 and `192 passed` (the number grows as tests are added).

## 2. `selftest`

```bash
api-wrapper selftest
```
Generates a sample workbook, converts it, loads the YAML, runs the wrapper against an in-process fake
upstream and asserts ~17 properties. Expect every line `[PASS]` and `SELFTEST PASSED` (exit 0).

## 3. Manual run against the mock upstream

Two terminals (or two PowerShell windows), venv active in both.

```bash
api-wrapper sample --dir work
api-wrapper convert --excel work/sample_mapping.xlsx --project demo --overrides work/overrides.yml --out work/configs
```
Open `work/configs/demo/report.md`: you should see 4 sections, 26 mapped fields, 6 skipped rows (1 mandatory), and warnings such as
`FILL_SERIES_URLS`, `NON_REST_URL`, `DUPLICATE_MAPPING`, `BRACKET_PLACEHOLDER` — the sample deliberately contains the defect types found in the real sheet.

**Terminal 1 — fake upstream**
```bash
api-wrapper mock-upstream --config work/configs/demo/mapping.yml --port 9000
```
**Terminal 2 — the wrapper**

bash:
```bash
UPSTREAM_DEMO_BASE_URL=http://127.0.0.1:9000 api-wrapper serve --config-dir work/configs --port 8000
```
PowerShell:
```powershell
$env:UPSTREAM_DEMO_BASE_URL = "http://127.0.0.1:9000"
api-wrapper serve --config-dir work/configs --port 8000
```
Ask the service what a project accepts: `GET /v1/catalog` → `request.inputs` (name / required / pattern / label).
Check `http://127.0.0.1:8000/readyz` → `{"ready":true,...}` and browse `http://127.0.0.1:8000/docs`.

**Call it** — bash:
```bash
curl -s localhost:8000/v1/claims/enrich -H 'content-type: application/json' \
     -d '{"claim_id":"CLAIM000001","fields":["claim.provider_name","claim.member_zip","line.charges"]}'
```
PowerShell (use `Invoke-RestMethod`; plain `curl` is an alias with different syntax):
```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/claims/enrich -ContentType 'application/json' `
  -Body '{"claim_id":"CLAIM000001","fields":["claim.provider_name","claim.member_zip","line.charges"]}' |
  ConvertTo-Json -Depth 10
```

### Scenarios and expected outcomes

| Request `claim_id` | Mock behaviour | Expected result |
|---|---|---|
| `CLAIM000001` | healthy, 3 lines | `complete:true`, 3 rows in `data.line`, `charges:"125.00"` (string), all statuses `ok` |
| `CLAIM000001` + `"fields":["claim.provider_name"]` | | `meta.sources` lists **only** `claim_summary` — minimum calls |
| `NOTFOUND01` | every endpoint 404 | `complete:false`, `required_failures` non-empty, dependent sources `skipped`, statuses `source_not_found` |
| `ERROR500XX` | every endpoint 500 | `complete:false`, `errors[]` has `http_500` (after 3 attempts), dependents `dependency_…_unavailable`; add `"strict":true` ⇒ **HTTP 502** |
| `BADJSON01` | HTTP 200, HTML body | `errors[]` code `invalid_json` |
| `NOLINES01` | Summary says 3 lines, each line 404 | 3 rows with `source_not_found` statuses (explicit, not silently empty) |
| `bad id!` / `../etc` / `"ABC123\n"` | — | HTTP **422** `invalid_inputs` with `"invalid":["claim_id"]` (the value is never echoed), no upstream call made |
| `{"claim_id":"CLAIM000001","member_id":"1"}` | — | HTTP **422** `invalid_inputs` with `"unknown":["member_id"]` and the list of `declared_inputs` |
| `"groups":["nope"]` | — | HTTP 422 `invalid_selection` naming the unknown group |

Also try: stop the mock, call again → `errors[]` `connect_error:…` after retries; set
`WRAPPER_API_KEY_DEMO=abc` and restart → calls without `X-API-Key: abc` get 401.
The wrapper prints one audit line per request (correlation ID, project, per-source states, latency — **no payloads**).

## 4. Verify against the real upstream (the accuracy gate)

Do this per project, with a person who knows the claim data. Use **non-production or sanitised** claims where you can.

### 4.0 Find out how the upstream wants you to authenticate (do this first)

You don't need to know in advance. Set the base URL, make **one** call with `probe`, read what the server says, adjust, repeat.
`probe` prints status, the server's `WWW-Authenticate` hint, and the JSON **structure (key names and types — never values)**.

bash:
```bash
export UPSTREAM_MYPROJECT_BASE_URL=https://<upstream-host>:10408
api-wrapper probe --project myproject --path /RestServices/core/api/v1/Claims/<A-REAL-CLAIM-ID>/Inquiry/Summary
```
PowerShell:
```powershell
$env:UPSTREAM_MYPROJECT_BASE_URL = "https://<upstream-host>:10408"
api-wrapper probe --project myproject --path /RestServices/core/api/v1/Claims/<A-REAL-CLAIM-ID>/Inquiry/Summary
```
| You see | Meaning → do |
|---|---|
| `HTTP 200 … authenticated and JSON received` + structure | done. The key names printed are what `path_template` / `response_root` must match |
| `HTTP 401`, `www-authenticate: Basic …` | `UPSTREAM_MYPROJECT_AUTH=basic` + `_USERNAME` / `_PASSWORD` |
| `HTTP 401`, `www-authenticate: Bearer …` | `AUTH=bearer` + `_TOKEN` |
| `HTTP 401/403`, no hint | look at a request that works for you in Postman / browser dev tools → Headers tab. A header like `X-Api-Key: …` ⇒ `AUTH=header` with `_AUTH_HEADER_NAME=X-Api-Key` and `_AUTH_HEADER_VALUE`. Extra headers such as a tenant/app id ⇒ `_HEADERS='{"X-Tenant":"…"}'` |
| `HTTP 403` after trying the right style | account lacks permission for that endpoint, or a required extra header is missing |
| `HTTP 404` | credentials fine; path or claim ID wrong |
| `body is not JSON` | wrong URL or an HTML login page |
| `connection error` / `timeout` | host/port, VPN/firewall, or TLS (`_CA_BUNDLE`) |

Add `--save samples/myproject/claims_inquiry_summary.json` to keep the raw response as a sample (**real data on disk — sanitise PHI first**; this replaces hand-saving in 4.2).

**Rehearse locally first** — the mock can demand credentials, so you can watch a 401, fix your variables, and see it go green without touching the real upstream:
```bash
api-wrapper mock-upstream --config work/configs/demo/mapping.yml --port 9000 --require-header "X-Upstream-Key=demo-secret"
#   also: --require-basic user:pass | --require-bearer TOKEN | --require-query apikey=value  (repeatable)
UPSTREAM_DEMO_BASE_URL=http://127.0.0.1:9000 api-wrapper probe --project demo --path /RestServices/core/api/v1/Claims/CLAIM000001/Inquiry/Summary   # -> 401
UPSTREAM_DEMO_BASE_URL=http://127.0.0.1:9000 UPSTREAM_DEMO_AUTH=header UPSTREAM_DEMO_AUTH_HEADER_NAME=X-Upstream-Key UPSTREAM_DEMO_AUTH_HEADER_VALUE=demo-secret \
    api-wrapper probe --project demo --path /RestServices/core/api/v1/Claims/CLAIM000001/Inquiry/Summary                                   # -> 200
```

### 4.1 Convert your real sheet
```bash
api-wrapper convert --excel MappingSheet.xlsx --project myproject --out configs
```
Read `configs/myproject/report.md` top to bottom. Resolve **ACTION REQUIRED**; decide each **WARNING** (see [EXCEL_TO_CONFIG.md](EXCEL_TO_CONFIG.md)). Repeat until “Config loadable: YES”. Check the *mandatory & unmapped* count is acceptable (ideally 0).

### 4.2 Capture one real response per source
For each source in the report’s *Sources* table, call the real endpoint for a known claim and save the JSON as `<source>.json`
(foreach sources: `<source>.<n>.json`, e.g. `claim_lines.1.json`). Sanitise PHI before sharing/committing.

bash:
```bash
mkdir -p samples/myproject
curl -s -u "$USER:$PASS" "https://<upstream-host>:10408/RestServices/core/api/v1/Claims/<CLAIM>/Inquiry/Summary" -o samples/myproject/claims_inquiry_summary.json
```
PowerShell:
```powershell
$cred = Get-Credential
Invoke-RestMethod -Uri "https://<upstream-host>:10408/RestServices/core/api/v1/Claims/<CLAIM>/Inquiry/Summary" -Credential $cred |
  ConvertTo-Json -Depth 30 | Out-File -Encoding utf8 samples\myproject\claims_inquiry_summary.json
```
(Postman/browser “Save response” works too. Use the *source names* from your `mapping.yml`.)

### 4.3 Check every field path against the real responses
```bash
api-wrapper validate --config configs/myproject/mapping.yml --samples samples/myproject
```
Per field: `ok` · `ok (path exists, value empty in sample)` · `FAIL missing_in_source [REQUIRED]` · `NO SAMPLE`.
Exit code 1 if any **required** field fails (`--strict`: also optional failures / missing samples).
**Typical findings:** real keys are camelCase (`prprName`) or wrapped (`{"data":{...}}`). Fix once, globally, in overrides:
`settings.path_template: "$.data.{api_field_camel}"` or `sources.<name>.response_root`, or per field via `fields.<g>.<f>.path`. Re-convert and re-validate until no required FAIL and no unexplained optional FAIL.

### 4.4 Lock in correctness with contract cases
`validate` proves paths exist; it does not prove **values are right**. Build golden cases:

```
cases/myproject/<case_name>/
    case.json          {"inputs": {"claim_id": "…", "associate_id": "…"}}     (optional "groups"/"fields"; legacy {"claim_id": "…"} works)
    responses/         the files from 4.2 (404 is simulated for sources without a file)
    expected.json      what the business says the answer is
```
```bash
api-wrapper contract-test --config configs/myproject/mapping.yml --cases cases/myproject --update   # snapshot once
# >>> now a PERSON compares expected.json with the source system's screen/claim for each field and corrects it <<<
api-wrapper contract-test --config configs/myproject/mapping.yml --cases cases/myproject            # from now on: regression gate
```
`--update` only records what the code currently produces — it is a starting point, **not** evidence of correctness until a person has checked it.
Good cases: a normal claim; one with several lines; one with a missing optional source (no COB); one with
empty/zero values; one with multiple modifiers; an institutional vs professional claim. Run in CI on every change to a config.

### 4.5 Live smoke test
```bash
export UPSTREAM_MYPROJECT_BASE_URL=https://<upstream-host>:10408 UPSTREAM_MYPROJECT_AUTH=basic UPSTREAM_MYPROJECT_USERNAME=… UPSTREAM_MYPROJECT_PASSWORD=…
api-wrapper serve --config-dir configs --port 8000
curl -s localhost:8000/v1/claims/enrich -H 'content-type: application/json' -d '{"claim_id":"<CLAIM>","include_provenance":true}'
```
(PowerShell: set `$env:UPSTREAM_MYPROJECT_BASE_URL = "…"` etc.) Then check:

- [ ] `complete` is `true` for several known-good claims
- [ ] every field in `field_status` is `ok` or an *explained* `null_in_source` (compare with the source system)
- [ ] `missing_in_source` appears **nowhere** unexpected (it means a wrong path)
- [ ] line count and per-line values match the source system for a multi-line claim
- [ ] amounts have the right scale (`"125.00"`), dates the right day, IDs keep leading zeros
- [ ] a claim ID that does not exist ⇒ `complete:false` with `source_not_found`, not a crash
- [ ] latency acceptable; if lines are probed and it is slow, switch to `discovery.mode: from_source`
- [ ] logs contain no PHI

### 4.6 Sign-off record
Keep per project: `mapping.yml` + `overrides.yml` (git), the `report.md`, the contract cases and the date of the
upstream version they were verified against. **Re-run 4.3–4.4 after any upstream upgrade or sheet change.**
The service itself helps: `missing_in_source` / `coercion_error` statuses in production are the signal that an upstream contract changed.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `convert`: `cannot open workbook … PermissionError` | Excel has the file open — close it or convert a copy |
| `no header row found` | the sheet needs a row with labels ending in `Table` and `Column`, plus `URL` and an `API Field…` column; pass `--sheet` if the data is not on “Mapping Sheet” |
| Report: *Config loadable: NO* | read ACTION REQUIRED / ERRORS; the usual cause is an unbound `{subscriber_key}` |
| `serve`: *Refusing to start* | config invalid — the message lists every problem |
| `503 project_not_ready` | `UPSTREAM_<PROJECT>_BASE_URL` (or auth vars) missing; see `/readyz`. The name is the project **upper-cased** |
| `401 invalid_api_key` | the *wrapper's own* key (`WRAPPER_API_KEY_<P>`) is set; send `X-API-Key` to the wrapper |
| `errors[]` shows `http_401` / `http_403` from a source | the upstream rejected the wrapper's credentials — run `probe`, see §4.0 |
| `503 project_not_ready` mentioning auth / header | a credential variable is missing or malformed; `/readyz` lists the problem (never the secret) |
| `400 project_required` | several projects loaded: send `X-Project` |
| Everything `missing_in_source` | wrong `path_template` / `response_root`; inspect a real response and run `validate --samples` |
| `source_failed` + `param_…_unresolved` | the JSONPath bound in `params` finds nothing, or the value has characters outside `[A-Za-z0-9_.-]` |
| `probe_limit_reached` | claim has more lines than `discovery.max` (default 50) — raise it or use `from_source` |
| TLS errors | set `UPSTREAM_<P>_CA_BUNDLE` to your CA file (avoid `VERIFY_TLS=false` outside a lab) |
