"""api-wrapper command line.

  convert        Excel mapping sheet -> <out>/<project>/mapping.yml (+ report.md / report.yml)
  validate       check a config; optionally check every field path against real sample responses
  serve          run the wrapper API
  contract-test  replay recorded real responses, compare with human-verified expected output
  probe          one real call to the upstream: test credentials, see the JSON structure (no values printed)
  mock-upstream  run a fake upstream generated from a config (for local end-to-end testing)
  sample         write a sample workbook + overrides to try the workflow
  selftest       sample -> convert -> validate -> mock upstream -> wrapper -> assertions, in one command
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .config.loader import ConfigError, load_config_dir, load_plugins, load_project_file
from .converter.build import convert, write_outputs
from .converter.excel_reader import ConversionError


def _p(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------------------
def cmd_convert(a) -> int:
    try:
        res = convert(a.excel, a.project, a.overrides, a.sheet)
    except ConversionError as e:
        _p(f"ERROR: {e}")
        return 1
    paths = write_outputs(res, a.out)
    s = res.stats
    _p(f"Converted '{a.excel}' -> {paths['config']}")
    _p(f"  fields mapped: {s['fields_mapped']}   rows skipped: {s['rows_skipped']} "
       f"(mandatory & unmapped: {s['mandatory_rows_skipped']})   sources: {s['sources']}   groups: {s['groups']}")
    counts = {sev: sum(1 for i in res.issues if i.severity == sev) for sev in ("error", "action", "warn", "info")}
    _p(f"  issues: {counts['error']} error, {counts['action']} action-required, {counts['warn']} warnings, {counts['info']} info")
    _p(f"  report: {paths['report_md']}")
    if not res.loadable:
        _p("  config is NOT loadable yet -- see ACTION REQUIRED / ERRORS in the report")
    return 2 if (res.needs_action and a.strict) else 0


def cmd_validate(a) -> int:
    load_plugins(a.plugins)
    try:
        cfg = load_project_file(a.config)
    except ConfigError as e:
        _p(f"INVALID: {e}")
        return 1
    _p(f"OK: project '{cfg.project}' v{cfg.version}: {len(cfg.fields)} fields, {len(cfg.sources)} sources, groups={cfg.groups()}")
    if not a.samples:
        _p("(no --samples given: structure checked only; field paths NOT checked against real responses)")
        return 0
    return _check_samples(cfg, Path(a.samples), a.strict)


def _check_samples(cfg, sdir: Path, strict: bool) -> int:
    from .engine.extract import extract_field

    samples: dict[str, list] = {}
    for f in sorted(sdir.glob("*.json")):
        stem = f.stem  # <source>  or  <source>.<n>
        name = stem.split(".")[0]
        if name not in cfg.sources:
            _p(f"  note: {f.name} does not match any source; ignored")
            continue
        root = cfg.sources[name].response_root
        doc = json.loads(f.read_text(encoding="utf-8"))
        if root:
            from .engine.extract import find
            m = find(root, doc)
            if len(m) != 1:
                _p(f"  ERROR: {f.name}: response_root {root!r} not found")
                continue
            doc = m[0]
        samples.setdefault(name, []).append(doc)
    bad_required = bad_optional = no_sample = ok = 0
    nulls = frozenset(cfg.null_values)
    _p(f"\n{'field':40} {'source':22} result")
    for f in cfg.fields:
        docs = samples.get(f.source)
        if not docs:
            no_sample += 1
            _p(f"{f.group + '.' + f.name:40} {f.source:22} NO SAMPLE")
            continue
        statuses = [extract_field(f, d, nulls).status for d in docs]
        good = any(s == "ok" for s in statuses)
        # null_in_source is acceptable evidence that the path exists (value just empty in this sample)
        exists = good or any(s == "null_in_source" for s in statuses)
        if exists:
            ok += 1
            _p(f"{f.group + '.' + f.name:40} {f.source:22} ok" + ("" if good else "  (path exists, value empty in sample)"))
        else:
            tag = "REQUIRED" if f.required else "optional"
            bad_required += f.required
            bad_optional += not f.required
            _p(f"{f.group + '.' + f.name:40} {f.source:22} FAIL {statuses[0]} [{tag}]  paths={f.paths}")
    _p(f"\nsummary: {ok} verified, {bad_required} required FAIL, {bad_optional} optional FAIL, {no_sample} without sample")
    if bad_required or (strict and (bad_optional or no_sample)):
        return 1
    return 0


def cmd_serve(a) -> int:
    import logging
    import os

    import uvicorn

    logging.basicConfig(level=os.environ.get("WRAPPER_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from .api.app import create_app

    try:
        app = create_app(a.config_dir)
        # fail fast: surface config errors before binding the port
        load_config_dir(a.config_dir or "configs")
    except ConfigError as e:
        _p(f"Refusing to start: {e}")
        return 1
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


def cmd_contract(a) -> int:
    from .contract import run_cases

    load_plugins(a.plugins)
    try:
        cfg = load_project_file(a.config)
    except ConfigError as e:
        _p(f"INVALID: {e}")
        return 1
    results = run_cases(cfg, a.cases, a.update)
    if not results:
        _p(f"no cases found under {a.cases} (each case needs a case.json)")
        return 1
    for r in results:
        _p(f"[{'UPDATED' if r.updated else ('PASS' if r.passed else 'FAIL')}] {r.name}")
        for pr in r.problems[:20]:
            _p(f"      {pr}")
    if a.update:
        _p("expected.json files were overwritten with CURRENT output -- verify them against the source system before trusting them")
        return 0
    bad = sum(not r.passed for r in results)
    _p(f"\n{len(results) - bad}/{len(results)} cases passed")
    return 1 if bad else 0


def cmd_probe(a) -> int:
    import os

    from .probe import run_probe

    return run_probe(a.project, a.path, dict(os.environ), a.save)


def cmd_mock(a) -> int:
    import uvicorn

    from .mock_upstream import create_mock_app

    from .mock_upstream import parse_requirements

    cfg = load_project_file(a.config)
    try:
        req_h, req_q = parse_requirements(a.require_header, a.require_query, a.require_basic, a.require_bearer)
    except ValueError as e:
        _p(f"ERROR: {e}")
        return 1
    app = create_mock_app(cfg, a.lines, req_h, req_q)
    _p(f"Mock upstream for '{cfg.project}' on http://{a.host}:{a.port}  (lines per claim: {a.lines})")
    if req_h or req_q:
        _p(f"Requires credentials: headers {sorted(req_h)} query {sorted(req_q)} (values not shown) -> otherwise 401")
    _p("Magic claim IDs: NOTFOUND* -> 404, ERROR500* -> 500, BADJSON* -> non-JSON, NOLINES* -> no line items")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_sample(a) -> int:
    from .sample.make_workbook import write_sample

    x, o = write_sample(a.dir)
    _p(f"wrote {x}\nwrote {o}")
    _p(f"try:  api-wrapper convert --excel {x} --project demo --overrides {o} --out {Path(a.dir) / 'configs'}")
    return 0


# ---------------------------------------------------------------------------------------
def cmd_selftest(a) -> int:
    import httpx
    from fastapi.testclient import TestClient  # noqa: F401  (ensures test deps present for a clear error)

    from .api.app import create_app
    from .mock_upstream import create_mock_app
    from .sample.make_workbook import write_sample

    ok = True

    def check(label: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        ok &= bool(cond)
        _p(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' -- ' + extra) if extra and not cond else ''}")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _p("1. generate sample workbook + overrides")
        x, o = write_sample(td)
        _p("2. convert Excel -> YAML")
        res = convert(x, "demo", o)
        paths = write_outputs(res, td / "configs")
        check("conversion produced a loadable config", res.loadable, "; ".join(res.load_errors))
        codes = {i.code for i in res.issues}
        for want in ("FILL_SERIES_URLS", "NON_REST_URL", "DUPLICATE_MAPPING", "MULTI_URL_CELL", "BRACKET_PLACEHOLDER", "DOUBLE_SLASH"):
            check(f"report flags {want}", want in codes)
        _p("3. validate generated YAML")
        cfg = load_project_file(paths["config"])
        check("YAML re-loads and validates", True)
        _p("4. wrapper -> mock upstream, end to end")
        mock = create_mock_app(cfg, lines=3)
        transport = httpx.ASGITransport(app=mock)
        app = create_app(str(td / "configs"), environ={"UPSTREAM_DEMO_BASE_URL": "http://mock"}, transport=transport)
        with TestClient(app) as c:
            r = c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001", "include_provenance": True})
            body = r.json()
            check("HTTP 200", r.status_code == 200, str(r.status_code))
            check("complete == true", body.get("complete") is True, json.dumps(body.get("errors"))[:300])
            check("per-line group has 3 rows", len(body["data"]["line"]) == 3)
            check("decimal returned as exact string", body["data"]["line"][0]["charges"] == "125.00")
            check("two-field member_name joined", " " in (body["data"]["claim"]["member_name"] or ""))
            check("subscriber-key dependency resolved (family_coverage called)",
                  body["data"]["claim"]["member_zip"] is not None and body["data"]["claim"]["phone"] is not None)
            r2 = c.post("/v1/claims/enrich", json={"claim_id": "ERROR500XX", "strict": True})
            check("strict + upstream 500 -> HTTP 502", r2.status_code == 502, str(r2.status_code))
            r3 = c.post("/v1/claims/enrich", json={"claim_id": "bad id!"})
            check("invalid claim id rejected with 422", r3.status_code == 422)
            r4 = c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001", "fields": ["claim.provider_name"]})
            b4 = r4.json()
            check("field selection calls only the needed source", [s["name"] for s in b4["meta"]["sources"]] == ["claim_summary"],
                  str([s["name"] for s in b4["meta"]["sources"]]))
        _p("\nSELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="api-wrapper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="Excel -> YAML config")
    c.add_argument("--excel", required=True)
    c.add_argument("--project", required=True, help="project name (letters, digits, _ -)")
    c.add_argument("--overrides", help="overrides.yml with hand decisions")
    c.add_argument("--out", default="configs", help="output root; writes <out>/<project>/mapping.yml")
    c.add_argument("--sheet", help="worksheet name (default: 'Mapping Sheet' or the active sheet)")
    c.add_argument("--strict", action="store_true", help="exit 2 if anything needs action / config not loadable")
    c.set_defaults(fn=cmd_convert)

    v = sub.add_parser("validate", help="validate a config (+ check paths against sample responses)")
    v.add_argument("--config", required=True)
    v.add_argument("--samples", help="dir of real responses: <source>.json or <source>.<n>.json")
    v.add_argument("--strict", action="store_true", help="also fail on optional-field failures / missing samples")
    v.add_argument("--plugins", help="comma-separated plugin modules")
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("serve", help="run the wrapper API")
    s.add_argument("--config-dir", help="default: $WRAPPER_CONFIG_DIR or ./configs")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    ct = sub.add_parser("contract-test", help="replay recorded responses and compare with expected.json")
    ct.add_argument("--config", required=True)
    ct.add_argument("--cases", required=True, help="dir with one sub-directory per case")
    ct.add_argument("--update", action="store_true", help="snapshot current output into expected.json (verify by hand!)")
    ct.add_argument("--plugins")
    ct.set_defaults(fn=cmd_contract)

    pr = sub.add_parser("probe", help="ONE real call to the upstream: tests credentials, shows JSON structure (no values)")
    pr.add_argument("--project", required=True, help="project name (selects the UPSTREAM_<PROJECT>_* env vars)")
    pr.add_argument("--path", required=True, help="e.g. /RestServices/core/api/v1/Claims/<ID>/Inquiry/Summary")
    pr.add_argument("--save", help="also save the RAW response here (real data!) for validate --samples / contract-test")
    pr.set_defaults(fn=cmd_probe)

    m = sub.add_parser("mock-upstream", help="fake upstream generated from a config")
    m.add_argument("--config", required=True)
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=9000)
    m.add_argument("--lines", type=int, default=3)
    m.add_argument("--require-header", action="append", default=[], metavar="NAME=VALUE", help="answer 401 unless this header is sent (repeatable)")
    m.add_argument("--require-query", action="append", default=[], metavar="NAME=VALUE", help="answer 401 unless this query param is sent (repeatable)")
    m.add_argument("--require-basic", metavar="USER:PASS", help="require HTTP Basic auth")
    m.add_argument("--require-bearer", metavar="TOKEN", help="require 'Authorization: Bearer TOKEN'")
    m.set_defaults(fn=cmd_mock)

    sm = sub.add_parser("sample", help="write a sample workbook + overrides")
    sm.add_argument("--dir", default="sample_out")
    sm.set_defaults(fn=cmd_sample)

    t = sub.add_parser("selftest", help="end-to-end self test on built-in sample data")
    t.set_defaults(fn=cmd_selftest)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
