# Execution testing guide

Four levels, cheapest first. **Levels 1–3 prove the software works. Only level 4 proves the
*mapping* is right** — and that is what "100% accuracy" actually depends on.

| Level | Needs Facets? | Proves | Time |
|---|---|---|---|
| 1. Automated suite | no | engine, converter, API behave as designed (116 tests) | 10 s |
| 2. `selftest` | no | whole pipeline works on sample data | 5 s |
| 3. Manual run vs mock Facets | no | you can operate it; failure behaviour visible | 15 min |
| 4. Verify against real Facets | **yes** | **field paths and values are correct** | your sign-off |

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

All green ⇒ exit code 0 and `116 passed` (the number grows as tests are added).

## 2. `selftest`

```bash
facets-wrapper selftest
```
Generates a sample workbook, converts it, loads the YAML, runs the wrapper against an in-process fake
Facets and asserts ~17 properties. Expect every line `[PASS]` and `SELFTEST PASSED` (exit 0).

## 3. Manual run against the mock Facets

Two terminals (or two PowerShell windows), venv active in both.

```bash
facets-wrapper sample --dir work
facets-wrapper convert --excel work/sample_mapping.xlsx --project demo --overrides work/overrides.yaml --out work/configs
```
Open `work/configs/demo/report.md`: you should see 4 sections, 26 mapped fields, 6 skipped rows (1 mandatory), and warnings such as
`FILL_SERIES_URLS`, `NON_REST_URL`, `DUPLICATE_MAPPING`, `BRACKET_PLACEHOLDER` — the sample deliberately contains the defect types found in the real sheet.

**Terminal 1 — fake Facets**
```bash
facets-wrapper mock-upstream --config work/configs/demo/mapping.yaml --port 9000
```
**Terminal 2 — the wrapper**

bash:
```bash
FACETS_DEMO_BASE_URL=http://127.0.0.1:9000 facets-wrapper serve --config-dir work/configs --port 8000
```
PowerShell:
```powershell
$env:FACETS_DEMO_BASE_URL = "http://127.0.0.1:9000"
facets-wrapper serve --config-dir work/configs --port 8000
```
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
| `bad id!` / `../etc` | — | HTTP **422** `invalid_claim_id`, no upstream call made |
| `"groups":["nope"]` | — | HTTP 422 `invalid_selection` naming the unknown group |

Also try: stop the mock, call again → `errors[]` `connect_error:…` after retries; set
`FACETS_WRAPPER_API_KEY_DEMO=abc` and restart → calls without `X-API-Key: abc` get 401.
The wrapper prints one audit line per request (correlation ID, project, per-source states, latency — **no payloads**).

## 4. Verify against real Facets (the accuracy gate)

Do this per project, with a person who knows the claim data. Use **non-production or sanitised** claims where you can.

### 4.1 Convert your real sheet
```bash
facets-wrapper convert --excel MappingSheet_EH.xlsx --project emblem --out configs
```
Read `configs/emblem/report.md` top to bottom. Resolve **ACTION REQUIRED**; decide each **WARNING** (see [EXCEL_TO_CONFIG.md](EXCEL_TO_CONFIG.md)). Repeat until “Config loadable: YES”. Check the *mandatory & unmapped* count is acceptable (ideally 0).

### 4.2 Capture one real response per source
For each source in the report’s *Sources* table, call the real endpoint for a known claim and save the JSON as `<source>.json`
(foreach sources: `<source>.<n>.json`, e.g. `claim_lines.1.json`). Sanitise PHI before sharing/committing.

bash:
```bash
mkdir -p samples/emblem
curl -s -u "$USER:$PASS" "https://<facets-host>:10408/RestServices/facets/api/v1/Claims/<CLAIM>/Inquiry/Summary" -o samples/emblem/claims_inquiry_summary.json
```
PowerShell:
```powershell
$cred = Get-Credential
Invoke-RestMethod -Uri "https://<facets-host>:10408/RestServices/facets/api/v1/Claims/<CLAIM>/Inquiry/Summary" -Credential $cred |
  ConvertTo-Json -Depth 30 | Out-File -Encoding utf8 samples\emblem\claims_inquiry_summary.json
```
(Postman/browser “Save response” works too. Use the *source names* from your `mapping.yaml`.)

### 4.3 Check every field path against the real responses
```bash
facets-wrapper validate --config configs/emblem/mapping.yaml --samples samples/emblem
```
Per field: `ok` · `ok (path exists, value empty in sample)` · `FAIL missing_in_source [REQUIRED]` · `NO SAMPLE`.
Exit code 1 if any **required** field fails (`--strict`: also optional failures / missing samples).
**Typical findings:** real keys are camelCase (`prprName`) or wrapped (`{"data":{...}}`). Fix once, globally, in overrides:
`settings.path_template: "$.data.{api_field_camel}"` or `sources.<name>.response_root`, or per field via `fields.<g>.<f>.path`. Re-convert and re-validate until no required FAIL and no unexplained optional FAIL.

### 4.4 Lock in correctness with contract cases
`validate` proves paths exist; it does not prove **values are right**. Build golden cases:

```
cases/emblem/<case_name>/
    case.json          {"claim_id": "…"}                 (optional "groups"/"fields")
    responses/         the files from 4.2 (404 is simulated for sources without a file)
    expected.json      what the business says the answer is
```
```bash
facets-wrapper contract-test --config configs/emblem/mapping.yaml --cases cases/emblem --update   # snapshot once
# >>> now a PERSON compares expected.json with the Facets screen/claim for each field and corrects it <<<
facets-wrapper contract-test --config configs/emblem/mapping.yaml --cases cases/emblem            # from now on: regression gate
```
`--update` only records what the code currently produces — it is a starting point, **not** evidence of correctness until a person has checked it.
Good cases: a normal claim; one with several lines; one with a missing optional source (no COB); one with
empty/zero values; one with multiple modifiers; an institutional vs professional claim. Run in CI on every change to a config.

### 4.5 Live smoke test
```bash
export FACETS_EMBLEM_BASE_URL=https://<facets-host>:10408 FACETS_EMBLEM_AUTH=basic FACETS_EMBLEM_USERNAME=… FACETS_EMBLEM_PASSWORD=…
facets-wrapper serve --config-dir configs --port 8000
curl -s localhost:8000/v1/claims/enrich -H 'content-type: application/json' -d '{"claim_id":"<CLAIM>","include_provenance":true}'
```
(PowerShell: set `$env:FACETS_EMBLEM_BASE_URL = "…"` etc.) Then check:

- [ ] `complete` is `true` for several known-good claims
- [ ] every field in `field_status` is `ok` or an *explained* `null_in_source` (compare with Facets)
- [ ] `missing_in_source` appears **nowhere** unexpected (it means a wrong path)
- [ ] line count and per-line values match Facets for a multi-line claim
- [ ] amounts have the right scale (`"125.00"`), dates the right day, IDs keep leading zeros
- [ ] a claim ID that does not exist ⇒ `complete:false` with `source_not_found`, not a crash
- [ ] latency acceptable; if lines are probed and it is slow, switch to `discovery.mode: from_source`
- [ ] logs contain no PHI

### 4.6 Sign-off record
Keep per project: `mapping.yaml` + `overrides.yaml` (git), the `report.md`, the contract cases and the date of the
Facets version they were verified against. **Re-run 4.3–4.4 after any Facets upgrade or sheet change.**
The service itself helps: `missing_in_source` / `coercion_error` statuses in production are the signal that an upstream contract changed.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `convert`: `cannot open workbook … PermissionError` | Excel has the file open — close it or convert a copy |
| `no header row found` | the sheet needs a row labelled `Facets Table`, `Facets Column` and `URL`; pass `--sheet` if the data is not on “Mapping Sheet” |
| Report: *Config loadable: NO* | read ACTION REQUIRED / ERRORS; the usual cause is an unbound `{subscriber_key}` |
| `serve`: *Refusing to start* | config invalid — the message lists every problem |
| `503 project_not_ready` | `FACETS_<PROJECT>_BASE_URL` (or auth vars) missing; see `/readyz`. The name is the project **upper-cased** |
| `401 invalid_api_key` | `FACETS_WRAPPER_API_KEY_<P>` is set; send `X-API-Key` |
| `400 project_required` | several projects loaded: send `X-Project` |
| Everything `missing_in_source` | wrong `path_template` / `response_root`; inspect a real response and run `validate --samples` |
| `source_failed` + `param_…_unresolved` | the JSONPath bound in `params` finds nothing, or the value has characters outside `[A-Za-z0-9_.-]` |
| `probe_limit_reached` | claim has more lines than `discovery.max` (default 50) — raise it or use `from_source` |
| TLS errors | set `FACETS_<P>_CA_BUNDLE` to your CA file (avoid `VERIFY_TLS=false` outside a lab) |
