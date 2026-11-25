# facets-wrapper

A **config-driven FastAPI wrapper** over Facets REST services.

```
 Excel mapping sheet ──convert──▶ mapping.yaml ──load──▶ wrapper API ──calls──▶ Facets REST endpoints
   (business ↔ API field)         (one per project)      POST /v1/claims/enrich
```

One request for a claim ID → the wrapper works out which Facets endpoints are needed, calls them
(in parallel where it can), extracts every mapped business field, and returns one structured
response in which **every field carries an explicit status**. Nothing about Facets is hard-coded:
a new project = a new Excel sheet (+ a small overrides file), no code change.

> **Read this first — what is and is not proven.**
> The converter takes field names from your sheet's *API field name* column and assumes the JSON
> key has the same name (`$.PRPR_NAME`). That is a **hypothesis until checked against real Facets
> responses**. Everything in this repo is built so a wrong mapping is *detected* (see
> [docs/TESTING.md](docs/TESTING.md), level 4) — but I could not call your Facets from here, so
> **that verification is still yours to run before anyone relies on the output.**

## Quick start (5 minutes, no Facets needed)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

facets-wrapper selftest            # sample sheet -> YAML -> wrapper -> fake Facets, with assertions
pytest                             # full automated suite (116 tests)
```

Try the real workflow on the built-in sample sheet:

```bash
facets-wrapper sample --dir work                                   # sample_mapping.xlsx + overrides.yaml
facets-wrapper convert --excel work/sample_mapping.xlsx --project demo \
                       --overrides work/overrides.yaml --out work/configs
less work/configs/demo/report.md                                   # <- READ THE REPORT
facets-wrapper mock-upstream --config work/configs/demo/mapping.yaml --port 9000     # terminal 1
FACETS_DEMO_BASE_URL=http://127.0.0.1:9000 \
facets-wrapper serve --config-dir work/configs --port 8000                           # terminal 2
curl -s localhost:8000/v1/claims/enrich -H 'content-type: application/json' \
     -d '{"claim_id":"CLAIM000001","fields":["claim.provider_name","line.charges"]}'
```
Interactive API docs: <http://localhost:8000/docs>.

## With your real sheet

```bash
facets-wrapper convert --excel MappingSheet_EH.xlsx --project emblem --out configs
#   -> configs/emblem/mapping.yaml   (what the service loads)
#   -> configs/emblem/report.md      (what it could NOT map, and what you must decide)
```
Then follow [docs/EXCEL_TO_CONFIG.md](docs/EXCEL_TO_CONFIG.md) → [docs/TESTING.md](docs/TESTING.md).

## Commands

| Command | Purpose |
|---|---|
| `convert` | Excel → `mapping.yaml` (+ `report.md`, `report.json`) |
| `validate` | Check a config; with `--samples` check every field path against real responses |
| `contract-test` | Replay recorded real responses, compare with human-verified `expected.json` |
| `serve` | Run the wrapper API (refuses to start on an invalid config) |
| `mock-upstream` | Fake Facets generated from a config, for local end-to-end runs |
| `sample` / `selftest` | Sample inputs / one-command end-to-end check |

## Documentation

| Doc | Read it to… |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | understand the structure, request lifecycle, response contract, design decisions and **known limits** |
| [docs/EXCEL_TO_CONFIG.md](docs/EXCEL_TO_CONFIG.md) | know the sheet layout expected, how rows become config, the overrides file, every warning code |
| [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) | hand-write or review a `mapping.yaml` |
| [docs/TESTING.md](docs/TESTING.md) | run the execution tests (levels 1–4, incl. PowerShell) and sign off accuracy |

## Layout

```
src/facets_wrapper/
  converter/   excel_reader.py  normalize.py  build.py  defaults.py     Excel -> config + report
  config/      models.py  validate.py  loader.py                        schema + fail-closed validation
  engine/      planner.py  executor.py  client.py  extract.py  coerce.py  transforms.py  assemble.py  runtime.py
  api/         app.py  schemas.py                                       FastAPI app
  contract.py  mock_upstream.py  sample/  cli.py  settings.py
configs/<project>/mapping.yaml                                          one directory per project
tests/                                                                  116 tests
```
