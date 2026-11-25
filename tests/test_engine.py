"""Planner + executor + assembler against scripted upstream responses (httpx.MockTransport)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from api_wrapper.config.loader import parse_config
from api_wrapper.engine.planner import SelectionError, build_plan
from api_wrapper.engine.runtime import ProjectRuntime
from api_wrapper.settings import ProjectEnv

CFG = {
    "project": "t",
    "sources": {
        "summary": {"path": "/c/{claim_id}/summary"},
        "lines": {
            "path": "/c/{claim_id}/lines/{line_no}",
            "foreach": {"param": "line_no", "discovery": {"mode": "probe", "max": 5}},
        },
        "family": {
            "path": "/fam/{subscriber_key}",
            "params": {"subscriber_key": {"from_source": "summary", "path": "$.SUB"}},
        },
    },
    "fields": [
        {"name": "claim_id", "group": "claim", "source": "summary", "path": "$.ID", "required": True},
        {"name": "sub_zip", "group": "claim", "source": "family", "path": "$.ZIP"},
        {"name": "charge", "group": "line", "source": "lines", "path": "$.AMT", "type": "decimal", "required": True},
        {"name": "units", "group": "line", "source": "summary", "path": "$.UNITS", "type": "integer"},
    ],
}


def runtime(handler, cfg=CFG, **env):
    c = parse_config(cfg)
    rt = ProjectRuntime(c, ProjectEnv(base_url="http://up", backoff_s=0, **env), httpx.MockTransport(handler))
    return rt


def run(rt, **kw):
    inputs = kw.pop("inputs", None) or {"claim_id": kw.pop("claim_id", "C1")}
    kw.pop("claim_id", None)
    return asyncio.run(rt.enrich(inputs, **kw))


def ok(body):
    return httpx.Response(200, json=body)


# ---- planner -------------------------------------------------------------------------
def test_plan_selects_minimum_sources_and_orders_dependencies():
    cfg = parse_config(CFG)
    assert build_plan(cfg, fields=["claim.claim_id"]).levels == [["summary"]]
    assert build_plan(cfg, fields=["claim.sub_zip"]).levels == [["summary"], ["family"]]  # dependency pulled in
    full = build_plan(cfg)
    assert full.levels[0] == ["summary", "lines"] or set(full.levels[0]) == {"summary", "lines"}
    assert full.levels[1] == ["family"]


def test_selection_by_group_bare_name_and_union():
    cfg = parse_config(CFG)
    assert [f.name for f in build_plan(cfg, groups=["line"]).fields] == ["charge", "units"]
    assert [f"{f.group}.{f.name}" for f in build_plan(cfg, fields=["charge"]).fields] == ["line.charge"]
    assert len(build_plan(cfg, groups=["line"], fields=["claim.claim_id"]).fields) == 3


def test_unknown_selection_reports_what_is_unknown():
    with pytest.raises(SelectionError) as e:
        build_plan(parse_config(CFG), groups=["nope"], fields=["claim.zzz"])
    assert e.value.unknown_groups == ["nope"] and e.value.unknown_fields == ["claim.zzz"]


# ---- happy path ----------------------------------------------------------------------
def happy(req: httpx.Request):
    p = req.url.path
    if p == "/c/C1/summary":
        return ok({"ID": "C1", "SUB": "S9", "UNITS": "4"})
    if p == "/fam/S9":
        return ok({"ZIP": "10001"})
    if p.startswith("/c/C1/lines/"):
        n = int(p.rsplit("/", 1)[1])
        return ok({"AMT": f"{n}0.50"}) if n <= 3 else httpx.Response(404)
    return httpx.Response(500)


def test_full_enrichment_and_per_line_rows():
    out = run(runtime(happy))
    assert out["complete"] is True and out["errors"] == [] and out["required_failures"] == []
    assert out["data"]["claim"] == {"claim_id": "C1", "sub_zip": "10001"}
    assert [r["charge"] for r in out["data"]["line"]] == ["10.50", "20.50", "30.50"]
    assert [r["line_no"] for r in out["data"]["line"]] == [1, 2, 3]
    # claim-level 'units' is broadcast onto every row (documented behaviour)
    assert [r["units"] for r in out["data"]["line"]] == [4, 4, 4]
    assert all(s == "ok" for r in out["field_status"]["line"] for k, s in r.items() if k != "line_no")


def test_only_needed_upstream_calls_are_made():
    seen = []
    def h(req):
        seen.append(req.url.path)
        return happy(req)
    run(runtime(h), fields=["claim.claim_id"])
    assert seen == ["/c/C1/summary"]


# ---- failure handling ----------------------------------------------------------------
def test_upstream_500_is_reported_never_silently_null():
    def h(req):
        return httpx.Response(500) if req.url.path.endswith("summary") else happy(req)
    out = run(runtime(h, retries=1))
    assert out["complete"] is False
    assert out["field_status"]["claim"]["claim_id"] == "source_failed"
    assert out["field_status"]["claim"]["sub_zip"] == "source_failed"  # dependency failed -> skipped
    assert any(e.get("source") == "summary" and e["code"] == "http_500" for e in out["errors"])
    assert any(e.get("source") == "family" for e in out["errors"])
    assert "claim.claim_id:source_failed" in out["required_failures"]


def test_404_on_required_source_is_a_required_failure_but_not_a_source_error():
    def h(req):
        return httpx.Response(404) if req.url.path.endswith("summary") else happy(req)
    out = run(runtime(h), fields=["claim.claim_id"])
    assert out["field_status"]["claim"]["claim_id"] == "source_not_found"
    assert out["complete"] is False  # required field not ok
    assert out["errors"] == []       # a 404 is a normal answer, not a technical failure


def test_retry_on_5xx_then_success_and_no_retry_on_4xx():
    calls = {"n": 0}
    def flaky(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else ok({"ID": "C1"})
    out = run(runtime(flaky, retries=2), fields=["claim.claim_id"])
    assert out["complete"] and calls["n"] == 3
    assert out["meta"]["sources"][0]["attempts"] == 3

    calls["n"] = 0
    def forbidden(req):
        calls["n"] += 1
        return httpx.Response(403)
    out = run(runtime(forbidden, retries=3), fields=["claim.claim_id"])
    assert calls["n"] == 1 and out["errors"][0]["code"] == "http_403"


def test_timeout_and_connect_error_are_retried_then_reported():
    def boom(req):
        raise httpx.ConnectTimeout("t")
    out = run(runtime(boom, retries=1), fields=["claim.claim_id"])
    assert out["errors"][0]["code"] == "timeout" and out["meta"]["sources"][0]["attempts"] == 2


def test_non_json_response_is_an_error():
    out = run(runtime(lambda r: httpx.Response(200, text="<html>")), fields=["claim.claim_id"])
    assert out["errors"][0]["code"] == "invalid_json"


def test_partial_line_failure_marks_only_that_row():
    def h(req):
        if req.url.path.endswith("/lines/2"):
            return httpx.Response(500)
        return happy(req)
    out = run(runtime(h, retries=0), groups=["line"])
    # probing stops at the failing item: we can NOT know whether more lines exist -> partial
    assert out["complete"] is False
    assert any(e.get("source") == "lines" and e["state"] == "partial" for e in out["errors"])
    rows = out["data"]["line"]
    assert rows[0]["charge"] == "10.50"
    assert rows[1]["charge"] is None and out["field_status"]["line"][1]["charge"] == "source_failed"


def test_probe_limit_is_reported_as_incomplete_not_silently_truncated():
    def h(req):
        return ok({"AMT": "1.00"}) if "/lines/" in req.url.path else happy(req)  # never 404s
    out = run(runtime(h), groups=["line"])
    assert len(out["data"]["line"]) == 5
    assert out["complete"] is False
    assert any(e.get("code") == "probe_limit_reached" for e in out["errors"])


def test_zero_lines_is_not_found_not_empty_success():
    def h(req):
        return httpx.Response(404) if "/lines/" in req.url.path else happy(req)
    out = run(runtime(h), groups=["line"])
    assert out["data"]["line"] == []
    lines = next(x for x in out["meta"]["sources"] if x["name"] == "lines")
    assert lines["status"] == "not_found"


# ---- security ------------------------------------------------------------------------
def test_param_from_upstream_cannot_inject_path_segments():
    seen = []
    def h(req):
        seen.append(str(req.url.path))
        return ok({"ID": "C1", "SUB": "../../admin?x=1"})
    out = run(runtime(h), fields=["claim.sub_zip"])
    assert out["field_status"]["claim"]["sub_zip"] == "source_failed"
    assert not any("admin" in p for p in seen)  # family endpoint never called
    assert any(e.get("code") == "param_subscriber_key_unresolved" for e in out["errors"])


def test_claim_id_is_url_encoded_in_path():
    seen = []
    def h(req):
        seen.append(req.url.raw_path.decode())
        return ok({"ID": "x"})
    run(runtime(h), claim_id="A/../B", fields=["claim.claim_id"])  # API layer rejects this; engine must still be safe
    assert seen == ["/c/A%2F..%2FB/summary"]


def test_correlation_id_forwarded_upstream():
    hdr = {}
    def h(req):
        hdr.update(req.headers)
        return ok({"ID": "x"})
    out = run(runtime(h), fields=["claim.claim_id"], correlation_id="corr-123")
    assert hdr["x-correlation-id"] == "corr-123" and out["meta"]["correlation_id"] == "corr-123"


# ---- discovery & response_root -------------------------------------------------------
def test_discovery_from_source_values_and_count():
    cfg = json.loads(json.dumps(CFG))
    cfg["sources"]["lines"]["foreach"]["discovery"] = {"mode": "from_source", "source": "summary", "path": "$.LINES[*].n", "kind": "values"}
    def h(req):
        if req.url.path.endswith("summary"):
            return ok({"ID": "C1", "SUB": "S", "UNITS": 1, "LINES": [{"n": 2}, {"n": 5}]})
        if "/lines/" in req.url.path:
            return ok({"AMT": "1.00"})
        return ok({})
    out = run(runtime(h, cfg), groups=["line"])
    assert [r["line_no"] for r in out["data"]["line"]] == [2, 5]

    cfg["sources"]["lines"]["foreach"]["discovery"] = {"mode": "from_source", "source": "summary", "path": "$.COUNT", "kind": "count"}
    def h2(req):
        return ok({"COUNT": 4, "UNITS": 1, "AMT": "1.00"})
    out = run(runtime(h2, cfg), groups=["line"])
    assert [r["line_no"] for r in out["data"]["line"]] == [1, 2, 3, 4]


def test_response_root_unwraps_and_reports_when_absent():
    cfg = json.loads(json.dumps(CFG))
    cfg["sources"]["summary"]["response_root"] = "$.data"
    out = run(runtime(lambda r: ok({"data": {"ID": "C1"}}), cfg), fields=["claim.claim_id"])
    assert out["data"]["claim"]["claim_id"] == "C1"
    out = run(runtime(lambda r: ok({"nope": 1}), cfg), fields=["claim.claim_id"])
    assert out["errors"][0]["code"] == "response_root_missing"


def test_provenance_included_on_request():
    out = run(runtime(happy), fields=["claim.claim_id"], include_provenance=True)
    assert out["provenance"]["claim"]["claim_id"] == {"source": "summary", "paths": ["$.ID"], "type": "string", "required": True}


def test_a_crashing_source_does_not_kill_the_request(monkeypatch):
    from api_wrapper.engine import executor
    async def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(executor, "_run_single", boom)
    out = run(runtime(happy), fields=["claim.claim_id"])
    assert out["errors"][0]["code"].startswith("internal_error")
