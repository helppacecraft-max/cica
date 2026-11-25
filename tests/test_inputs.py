"""Configurable request inputs (claim_id, associate_id, ...)."""
from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from api_wrapper.api.app import create_app
from api_wrapper.config.loader import ConfigError, parse_config
from api_wrapper.engine.planner import MissingInputsError, build_plan
from api_wrapper.engine.runtime import ProjectRuntime
from api_wrapper.settings import ProjectEnv

CFG = {
    "project": "t",
    "request": {"inputs": [
        {"name": "claim_id"},
        {"name": "associate_id", "pattern": "^[0-9]{1,10}$", "required": False, "label": "Associate ID"},
    ]},
    "sources": {
        "claim": {"path": "/c/{claim_id}"},
        "assoc": {"path": "/a/{associate_id}/profile"},
        "both": {"path": "/a/{associate_id}/claims", "query": {"claim": "{claim_id}"}},
    },
    "fields": [
        {"name": "f_claim", "group": "g", "source": "claim", "path": "$.A"},
        {"name": "f_assoc", "group": "g", "source": "assoc", "path": "$.B"},
        {"name": "f_both", "group": "g", "source": "both", "path": "$.C"},
    ],
}


def cfg(**over):
    d = copy.deepcopy(CFG)
    d.update(over)
    return d


def errs(doc):
    with pytest.raises(ConfigError) as e:
        parse_config(doc)
    return "\n".join(e.value.errors)


# ---- config validation ---------------------------------------------------------------
def test_default_request_is_claim_id_only():
    c = parse_config({"project": "t", "sources": {"s": {"path": "/x/{claim_id}"}},
                      "fields": [{"name": "a", "group": "g", "source": "s", "path": "$.A"}]})
    assert c.input_names() == ["claim_id"] and c.request.inputs[0].required is True


def test_valid_multi_input_config():
    assert parse_config(CFG).input_names() == ["claim_id", "associate_id"]


def test_undeclared_placeholder_suggests_both_fixes():
    d = cfg(request={"inputs": [{"name": "claim_id"}]})
    m = errs(d)
    assert "associate_id" in m and "request input" in m and "params" in m


def test_unused_duplicate_reserved_and_too_many_inputs():
    assert "declared but no source" in errs(cfg(request={"inputs": [{"name": "claim_id"}, {"name": "associate_id"}, {"name": "ghost"}]}))
    assert "duplicate input" in errs(cfg(request={"inputs": [{"name": "claim_id"}, {"name": "claim_id"}, {"name": "associate_id"}]}))
    assert "reserved" in errs(cfg(request={"inputs": [{"name": "claim_id"}, {"name": "associate_id"}, {"name": "strict"}]}))
    many = [{"name": "claim_id"}, {"name": "associate_id"}] + [{"name": f"x{i}"} for i in range(20)]
    assert "maximum is 20" in errs(cfg(request={"inputs": many}))


def test_bad_pattern_and_bad_at_least_one_of():
    assert "not a valid regex" in errs(cfg(request={"inputs": [{"name": "claim_id", "pattern": "("}, {"name": "associate_id"}]}))
    assert "not a declared input" in errs(cfg(request={**CFG["request"], "at_least_one_of": ["nope"]}))


def test_param_cannot_shadow_an_input():
    d = cfg()
    d["sources"]["assoc"]["params"] = {"associate_id": {"from_source": "claim", "path": "$.X"}}
    assert "same name as a request input" in errs(d)


def test_query_placeholders_must_be_bound():
    d = cfg()
    d["sources"]["both"]["query"] = {"k": "{nope}"}
    assert "nope" in errs(d)


# ---- input checking ------------------------------------------------------------------
def rt(c=None, handler=None):
    return ProjectRuntime(parse_config(c or CFG), ProjectEnv(base_url="http://u", backoff_s=0, retries=0),
                          httpx.MockTransport(handler or (lambda r: httpx.Response(200, json={"A": "a", "B": "b", "C": "c"}))))


def test_check_inputs_matrix():
    r = rt()
    ok, p = r.check_inputs({"claim_id": "ABC123", "associate_id": 42})
    assert ok == {"claim_id": "ABC123", "associate_id": "42"} and p == {}
    _, p = r.check_inputs({"associate_id": "1"})
    assert p == {"missing": ["claim_id"]}
    _, p = r.check_inputs({"claim_id": "ABC123", "associate_id": "12ab"})
    assert p == {"invalid": ["associate_id"]}
    _, p = r.check_inputs({"claim_id": "ABC123", "whatever": "x"})
    assert p == {"unknown": ["whatever"]}
    _, p = r.check_inputs({"claim_id": "ABC123", "associate_id": True})
    assert p == {"invalid": ["associate_id"]}
    ok, p = r.check_inputs({"claim_id": "ABC123", "associate_id": None})   # null == not supplied
    assert p == {} and "associate_id" not in ok


@pytest.mark.parametrize("bad", ["ABC123\n", "ABC 123", "AB", "A" * 21, "ABC/123", "ABC123\x00", ""])
def test_claim_id_pattern_is_fully_enforced(bad):
    _, p = rt().check_inputs({"claim_id": bad})
    assert p, bad       # incl. trailing newline, which a plain '$' anchor would let through


def test_problem_report_never_echoes_values():
    _, p = rt().check_inputs({"claim_id": "SECRET-VALUE!!", "associate_id": "9" * 30})
    assert "SECRET" not in str(p) and "999" not in str(p)


def test_at_least_one_of_rule():
    c = cfg(request={"inputs": [{"name": "claim_id", "required": False}, {"name": "associate_id", "required": False}],
                     "at_least_one_of": ["claim_id", "associate_id"]})
    r = rt(c)
    assert r.check_inputs({})[1] == {"missing": ["at least one of: claim_id, associate_id"]}
    assert r.check_inputs({"claim_id": "ABC123"})[1] == {}
    assert r.check_inputs({"associate_id": "5"})[1] == {}
    assert r.check_inputs({"associate_id": "x!"})[1] == {"invalid": ["associate_id"]}   # reported as invalid, not 'missing'


# ---- planning ------------------------------------------------------------------------
def test_broad_selection_drops_unservable_fields_and_reports_them():
    plan = build_plan(parse_config(CFG), available={"claim_id"})
    assert [f.name for f in plan.fields] == ["f_claim"]
    assert plan.skipped == {"g.f_assoc": ["associate_id"], "g.f_both": ["associate_id"]}
    assert plan.sources == ["claim"]


def test_explicit_field_needing_missing_input_is_an_error():
    with pytest.raises(MissingInputsError) as e:
        build_plan(parse_config(CFG), fields=["g.f_assoc"], available={"claim_id"})
    assert e.value.fields == {"g.f_assoc": ["associate_id"]}


def test_nothing_servable_is_an_error_not_an_empty_success():
    with pytest.raises(MissingInputsError):
        build_plan(parse_config(CFG), groups=["g"], available=set())


def test_all_inputs_supplied_plans_everything():
    plan = build_plan(parse_config(CFG), available={"claim_id", "associate_id"})
    assert len(plan.fields) == 3 and not plan.skipped


# ---- execution -----------------------------------------------------------------------
def test_inputs_fill_path_and_query_and_are_encoded():
    seen = []

    def h(req: httpx.Request):
        seen.append((req.url.raw_path.decode(), dict(req.url.params)))
        return httpx.Response(200, json={"A": "a", "B": "b", "C": "c"})

    c = cfg(request={"inputs": [{"name": "claim_id", "pattern": "^.+$"}, {"name": "associate_id", "pattern": "^.+$"}]})
    out = asyncio.run(rt(c, h).enrich({"claim_id": "a b&c=d", "associate_id": "x/y"}))
    assert out["complete"] and out["inputs"] == {"claim_id": "a b&c=d", "associate_id": "x/y"}
    paths = {p for p, _ in seen}
    assert "/c/a%20b%26c%3Dd" in paths and "/a/x%2Fy/profile" in paths      # path values encoded, no extra segments
    q = next(q for p, q in seen if p.startswith("/a/x%2Fy/claims"))
    assert q == {"claim": "a b&c=d"}                                         # query value survives intact


def test_selection_runs_only_sources_for_supplied_inputs():
    calls = []
    r = rt(handler=lambda req: (calls.append(req.url.path), httpx.Response(200, json={"A": "a"}))[1])
    out = asyncio.run(r.enrich({"claim_id": "ABC123"}))
    assert calls == ["/c/ABC123"]
    assert out["meta"]["skipped_fields"] == {"g.f_assoc": ["associate_id"], "g.f_both": ["associate_id"]}
    assert set(out["data"]["g"]) == {"f_claim"}


# ---- HTTP API ------------------------------------------------------------------------
@pytest.fixture()
def api(tmp_path):
    d = tmp_path / "configs" / "t"
    d.mkdir(parents=True)
    (d / "mapping.yml").write_text(yaml.safe_dump(CFG))
    h = lambda req: httpx.Response(200, json={"A": "a", "B": "b", "C": "c"})  # noqa: E731
    app = create_app(str(tmp_path / "configs"), environ={"UPSTREAM_T_BASE_URL": "http://u"}, transport=httpx.MockTransport(h))
    with TestClient(app) as c:
        yield c


def test_api_two_inputs_flat_in_body(api):
    r = api.post("/v1/claims/enrich", json={"claim_id": "ABC123", "associate_id": "77"})
    b = r.json()
    assert r.status_code == 200 and b["inputs"] == {"claim_id": "ABC123", "associate_id": "77"}
    assert b["data"]["g"] == {"f_claim": "a", "f_assoc": "b", "f_both": "c"}


def test_api_error_shapes(api):
    r = api.post("/v1/claims/enrich", json={"associate_id": "77"})
    assert r.status_code == 422 and r.json()["detail"]["missing"] == ["claim_id"]
    assert r.json()["detail"]["declared_inputs"] == ["claim_id", "associate_id"]
    r = api.post("/v1/claims/enrich", json={"claim_id": "ABC123", "member_id": "1"})
    assert r.status_code == 422 and r.json()["detail"]["unknown"] == ["member_id"]
    r = api.post("/v1/claims/enrich", json={"claim_id": "ABC123\n"})
    assert r.status_code == 422 and r.json()["detail"]["invalid"] == ["claim_id"]
    r = api.post("/v1/claims/enrich", json={"claim_id": "ABC123", "fields": ["g.f_assoc"]})
    assert r.status_code == 422 and r.json()["detail"] == {"error": "inputs_missing_for_fields", "fields": {"g.f_assoc": ["associate_id"]}}
    assert api.post("/v1/claims/enrich", json={"claim_id": "ABC123", "strict": "maybe"}).status_code == 422


def test_api_partial_inputs_reports_skipped_fields(api):
    b = api.post("/v1/claims/enrich", json={"claim_id": "ABC123"}).json()
    assert b["complete"] is True and set(b["data"]["g"]) == {"f_claim"}
    assert b["meta"]["skipped_fields"]["g.f_assoc"] == ["associate_id"]


def test_catalog_describes_the_request(api):
    req = api.get("/v1/catalog").json()["request"]
    assert [i["name"] for i in req["inputs"]] == ["claim_id", "associate_id"]
    assert req["inputs"][1] == {"name": "associate_id", "required": False, "label": "Associate ID", "pattern": "^[0-9]{1,10}$"}
    assert req["controls"] == ["groups", "fields", "strict", "include_provenance"]
