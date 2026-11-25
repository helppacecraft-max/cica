# api-wrapper

A **config-driven FastAPI wrapper** over upstream REST services.

```
 Excel mapping sheet ──convert──▶ mapping.yml ──load──▶ wrapper API ──calls──▶ upstream REST endpoints
   (business ↔ API field)         (one per project)      POST /v1/claims/enrich
```

One request for a claim ID → the wrapper works out which upstream endpoints are needed, calls them
(in parallel where it can), extracts every mapped business field, and returns one structured
response in which **every field carries an explicit status**. Nothing about any particular upstream is hard-coded:
a new project = a new Excel sheet (+ a small overrides file), no code change.

> **Read this first — what is and is not proven.**
> The converter takes field names from your sheet's *API field name* column and assumes the JSON
> key has the same name (`$.PRPR_NAME`). That is a **hypothesis until checked against the real upstream
> responses**. Everything in this repo is built so a wrong mapping is *detected* (see
> [docs/TESTING.md](docs/TESTING.md), level 4) — but I could not call your upstream from here, so
> **that verification is still yours to run before anyone relies on the output.**

## Quick start (5 minutes, no upstream needed)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

api-wrapper selftest            # sample sheet -> YAML -> wrapper -> fake upstream, with assertions
pytest                             # full automated suite (192 tests)
```

Try the real workflow on the built-in sample sheet:

```bash
api-wrapper sample --dir work                                   # sample_mapping.xlsx + overrides.yml
api-wrapper convert --excel work/sample_mapping.xlsx --project demo \
                       --overrides work/overrides.yml --out work/configs
less work/configs/demo/report.md                                   # <- READ THE REPORT
api-wrapper mock-upstream --config work/configs/demo/mapping.yml --port 9000     # terminal 1
UPSTREAM_DEMO_BASE_URL=http://127.0.0.1:9000 \
api-wrapper serve --config-dir work/configs --port 8000                           # terminal 2
curl -s localhost:8000/v1/claims/enrich -H 'content-type: application/json' \
     -d '{"claim_id":"CLAIM000001","fields":["claim.provider_name","line.charges"]}'
```
Interactive API docs: <http://localhost:8000/docs>.

### Request format

Flat JSON. **Inputs** are whatever identifiers the project declares in `request.inputs` (default: just
`claim_id`; e.g. also `associate_id`, up to 20, each with its own pattern / required flag / "at least one of"
rule). Four **controls** are fixed:

```json
{ "claim_id": "26A369350300", "associate_id": "4471",
  "groups": ["claim", "line"], "fields": ["line.charges"], "strict": false, "include_provenance": false }
```
`GET /v1/catalog` tells a client exactly what a project accepts. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3, [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md).

## With your real sheet

```bash
api-wrapper convert --excel MappingSheet.xlsx --project myproject --out configs
#   -> configs/myproject/mapping.yml   (what the service loads)
#   -> configs/myproject/report.md      (what it could NOT map, and what you must decide)
```
Then follow [docs/EXCEL_TO_CONFIG.md](docs/EXCEL_TO_CONFIG.md) → [docs/TESTING.md](docs/TESTING.md).

## Commands

| Command | Purpose |
|---|---|
| `convert` | Excel → `mapping.yml` (+ `report.md`, `report.yml`) |
| `validate` | Check a config; with `--samples` check every field path against real responses |
| `contract-test` | Replay recorded real responses, compare with human-verified `expected.json` |
| `serve` | Run the wrapper API (refuses to start on an invalid config) |
| `probe` | ONE real call to the upstream with your configured credentials: tests auth, shows the JSON structure (names/types only) |
| `mock-upstream` | Fake upstream generated from a config, for local end-to-end runs (can demand credentials to rehearse auth) |
| `sample` / `selftest` | Sample inputs / one-command end-to-end check |

## Documentation

| Doc | Read it to… |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | understand the structure, request lifecycle, response contract, design decisions and **known limits** |
| [docs/EXCEL_TO_CONFIG.md](docs/EXCEL_TO_CONFIG.md) | know the sheet layout expected, how rows become config, the overrides file, every warning code |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | hand-write or review a `mapping.yml` |
| [docs/TESTING.md](docs/TESTING.md) | run the execution tests (levels 1–4, incl. PowerShell) and sign off accuracy |

## Layout

```
src/api_wrapper/
  converter/   excel_reader.py  normalize.py  build.py  defaults.py     Excel -> config + report
  config/      models.py  validate.py  loader.py                        schema + fail-closed validation
  engine/      planner.py  executor.py  client.py  extract.py  coerce.py  transforms.py  assemble.py  runtime.py
  api/         app.py  schemas.py                                       FastAPI app
  contract.py  mock_upstream.py  sample/  cli.py  settings.py
configs/<project>/mapping.yml                                          one directory per project
tests/                                                                   192 tests
```
