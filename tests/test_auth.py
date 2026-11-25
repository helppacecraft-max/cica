"""How the wrapper authenticates TO the upstream: modes, extra headers, validation, secrecy, probe."""
from __future__ import annotations

import asyncio
import base64
import logging

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from api_wrapper.api.app import create_app
from api_wrapper.cli import main
from api_wrapper.config.loader import parse_config
from api_wrapper.engine.runtime import ProjectRuntime
from api_wrapper.probe import outline, run_probe
from api_wrapper.settings import load_project_env

CFG = {"project": "t", "sources": {"s": {"path": "/c/{claim_id}", "query": {"fixed": "1"}}},
       "fields": [{"name": "a", "group": "g", "source": "s", "path": "$.A"}]}
SECRET = "S3cr3t-VALUE-xyz"


def env(**kw):
    e = {"UPSTREAM_T_BASE_URL": "http://up"}
    e.update({f"UPSTREAM_T_{k}": v for k, v in kw.items()})
    return e


def call(environ):
    """Run one enrich through the runtime; return (request seen by the upstream, result)."""
    seen = {}

    def h(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"A": "x"})

    rt = ProjectRuntime(parse_config(CFG), load_project_env("t", environ), httpx.MockTransport(h))
    out = asyncio.run(rt.enrich({"claim_id": "ABC123"}))
    return seen["req"], out


# ---- each mode sends the right thing ---------------------------------------------------
def test_none_sends_no_credentials():
    r, out = call(env())
    assert out["complete"] and "authorization" not in r.headers and "x-api-key" not in r.headers


def test_basic():
    r, _ = call(env(AUTH="basic", USERNAME="u", PASSWORD="p:w"))
    assert r.headers["authorization"] == "Basic " + base64.b64encode(b"u:p:w").decode()


def test_bearer():
    r, _ = call(env(AUTH="bearer", TOKEN=SECRET))
    assert r.headers["authorization"] == f"Bearer {SECRET}"


def test_api_key_header_default_name():
    r, _ = call(env(AUTH="header", AUTH_HEADER_VALUE=SECRET))
    assert r.headers["x-api-key"] == SECRET and "authorization" not in r.headers


def test_api_key_header_custom_name_and_prefix():
    r, _ = call(env(AUTH="header", AUTH_HEADER_NAME="Authorization", AUTH_HEADER_PREFIX="ApiKey ", AUTH_HEADER_VALUE=SECRET))
    assert r.headers["authorization"] == f"ApiKey {SECRET}"
    r, _ = call(env(AUTH="header", AUTH_HEADER_NAME="X-Upstream-Token", AUTH_HEADER_VALUE=SECRET))
    assert r.headers["x-upstream-token"] == SECRET


def test_api_key_in_query_merges_with_source_query():
    r, _ = call(env(AUTH="query", AUTH_QUERY_NAME="apikey", AUTH_QUERY_VALUE=SECRET))
    assert dict(r.url.params) == {"apikey": SECRET, "fixed": "1"}


def test_extra_headers_work_with_any_mode_and_with_none():
    r, _ = call(env(HEADERS='{"X-Tenant": "acme", "X-Client-Id": "wrapper"}'))
    assert r.headers["x-tenant"] == "acme" and r.headers["x-client-id"] == "wrapper"
    r, _ = call(env(AUTH="bearer", TOKEN=SECRET, HEADERS='{"X-Tenant": "acme"}'))
    assert r.headers["authorization"] == f"Bearer {SECRET}" and r.headers["x-tenant"] == "acme"


def test_correlation_id_still_sent_alongside_credentials():
    r, out = call(env(AUTH="header", AUTH_HEADER_VALUE=SECRET))
    assert r.headers["x-correlation-id"] == out["meta"]["correlation_id"]


# ---- validation: misconfiguration is caught before any call ---------------------------
@pytest.mark.parametrize("kw, fragment", [
    ({"AUTH": "magic"}, "unknown auth type"),
    ({"AUTH": "basic", "USERNAME": "u"}, "username and password"),
    ({"AUTH": "bearer"}, "needs a token"),
    ({"AUTH": "header"}, "AUTH_HEADER_VALUE"),
    ({"AUTH": "query", "AUTH_QUERY_NAME": "k"}, "AUTH_QUERY_NAME and"),
    ({"HEADERS": "not json"}, "JSON object"),
    ({"HEADERS": '["a"]'}, "JSON object"),
    ({"HEADERS": '{"a": 1}'}, "string keys and string values"),
    ({"HEADERS": '{"bad name": "x"}'}, "invalid header name"),
    ({"HEADERS": '{"Host": "evil"}'}, "may not be set"),
    ({"HEADERS": '{"X-A": "x\\r\\nInjected: 1"}'}, "control characters"),
    ({"AUTH": "bearer", "TOKEN": SECRET, "HEADERS": '{"authorization": "other"}'}, "conflicts"),
    ({"AUTH": "header", "AUTH_HEADER_NAME": "X-Key", "AUTH_HEADER_VALUE": SECRET, "HEADERS": '{"x-key": "other"}'}, "conflicts"),
    ({"AUTH": "header", "AUTH_HEADER_VALUE": "a\nb"}, "control characters"),
])
def test_misconfiguration_is_reported(kw, fragment):
    problems = load_project_env("t", env(**kw)).problems()
    assert any(fragment in p for p in problems), problems
    assert SECRET not in " ".join(problems)


def test_not_ready_project_never_calls_upstream_and_shows_no_secret(tmp_path, caplog):
    d = tmp_path / "configs" / "t"
    d.mkdir(parents=True)
    (d / "mapping.yml").write_text(yaml.safe_dump(CFG))
    calls = []
    app = create_app(str(tmp_path / "configs"), environ=env(AUTH="header", HEADERS='{"Bad Name": "' + SECRET + '"}', AUTH_HEADER_VALUE=SECRET),
                     transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200, json={})))
    with TestClient(app) as c:
        r = c.post("/v1/claims/enrich", json={"claim_id": "ABC123"})
        ready = c.get("/readyz")
    assert r.status_code == 503 and not calls
    assert SECRET not in r.text and SECRET not in ready.text


# ---- secrecy ------------------------------------------------------------------------------
def test_secret_never_appears_in_responses_or_logs_even_when_upstream_rejects_it(tmp_path, caplog):
    d = tmp_path / "configs" / "t"
    d.mkdir(parents=True)
    (d / "mapping.yml").write_text(yaml.safe_dump(CFG))
    environ = env(AUTH="bearer", TOKEN=SECRET, HEADERS='{"X-Tenant": "' + SECRET + '"}')
    app = create_app(str(tmp_path / "configs"), environ=environ, transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"echo": "denied"})))
    caplog.set_level(logging.DEBUG)
    with TestClient(app) as c:
        r = c.post("/v1/claims/enrich", json={"claim_id": "ABC123", "include_provenance": True})
        cat = c.get("/v1/catalog")
        ready = c.get("/readyz")
        proj = c.get("/v1/projects")
    assert r.json()["errors"][0]["code"] == "http_401"        # the operator can SEE that auth failed...
    for text in (r.text, cat.text, ready.text, proj.text, caplog.text):
        assert SECRET not in text                              # ...but never the secret


# ---- probe ------------------------------------------------------------------------------------
BODY = {"CLCL_ID": "SECRETCLAIM1", "PRPR_NAME": "Dr Who", "LINES": [{"AMT": "12.00", "CODE": "99213"}], "META": {"v": 1}}


def run(environ, handler, path="/RestServices/x", **kw):
    lines: list[str] = []
    rc = run_probe("t", path, environ, transport=httpx.MockTransport(handler), out=lines.append, **kw)
    return rc, "\n".join(lines)


def test_probe_success_prints_structure_but_no_values():
    rc, text = run(env(AUTH="bearer", TOKEN=SECRET), lambda r: httpx.Response(200, json=BODY))
    assert rc == 0 and "authenticated and JSON received" in text
    for key in ("CLCL_ID: str", "PRPR_NAME: str", "LINES: array (1 items)", "AMT: str", "META: object (1 keys)"):
        assert key in text, key
    for value in ("SECRETCLAIM1", "Dr Who", "12.00", "99213", SECRET):
        assert value not in text, value


def test_probe_uses_the_configured_credentials():
    seen = {}
    run(env(AUTH="header", AUTH_HEADER_NAME="X-Key", AUTH_HEADER_VALUE=SECRET, HEADERS='{"X-T": "1"}'),
        lambda r: (seen.update(h=dict(r.headers)), httpx.Response(200, json={}))[1])
    assert seen["h"]["x-key"] == SECRET and seen["h"]["x-t"] == "1"


def test_probe_explains_401_with_server_hint():
    rc, text = run(env(AUTH="bearer", TOKEN=SECRET), lambda r: httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="upstream"'}))
    assert rc == 1 and "credentials rejected" in text and 'Basic realm="upstream"' in text and "AUTH=basic" in text
    assert SECRET not in text


def test_probe_other_outcomes():
    assert "does not exist" in run(env(), lambda r: httpx.Response(404))[1]
    assert "not JSON" in run(env(), lambda r: httpx.Response(200, text="<html>login</html>"))[1]
    assert "unexpected status 500" in run(env(), lambda r: httpx.Response(500))[1]

    def boom(r):
        raise httpx.ConnectTimeout("t")
    assert "timeout" in run(env(), boom)[1]


def test_probe_refuses_bad_config_and_bad_path():
    rc, text = run(env(AUTH="bearer"), lambda r: httpx.Response(200, json={}))
    assert rc == 1 and "needs a token" in text
    rc, text = run(env(), lambda r: httpx.Response(200, json={}), path="https://evil.example/x")
    assert rc == 1 and "--path must be a path" in text


def test_probe_save_writes_raw_json_with_warning(tmp_path):
    f = tmp_path / "s" / "claim_summary.json"
    rc, text = run(env(), lambda r: httpx.Response(200, json=BODY), save=str(f))
    assert rc == 0 and "SECRETCLAIM1" in f.read_text() and "sanitise PHI" in text


def test_outline_is_depth_and_size_bounded():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert len(outline(deep)) <= 6
    wide = {f"k{i}": i for i in range(100)}
    assert outline(wide)[-1].startswith("... (40 more keys)")


def test_probe_cli_wiring(monkeypatch, capsys):
    monkeypatch.delenv("UPSTREAM_T_BASE_URL", raising=False)
    assert main(["probe", "--project", "t", "--path", "/x"]) == 1
    assert "base URL not configured" in capsys.readouterr().out


# ---- mock upstream can demand credentials, so the auth setup can be rehearsed locally ------------
def _mock_enrich(demo_cfg, environ, **require):
    from api_wrapper.mock_upstream import create_mock_app
    rt = ProjectRuntime(demo_cfg, load_project_env("demo", {"UPSTREAM_DEMO_BASE_URL": "http://m", **environ}),
                        httpx.ASGITransport(app=create_mock_app(demo_cfg, 2, **require)))
    return asyncio.run(rt.enrich({"claim_id": "CLAIM000001"}, fields=["claim.provider_name"]))


@pytest.mark.parametrize("require, good, bad", [
    ({"require_headers": {"X-API-Key": SECRET}}, {"AUTH": "header", "AUTH_HEADER_VALUE": SECRET}, {"AUTH": "header", "AUTH_HEADER_VALUE": "nope"}),
    ({"require_headers": {"Authorization": f"Bearer {SECRET}"}}, {"AUTH": "bearer", "TOKEN": SECRET}, {"AUTH": "none"}),
    ({"require_headers": {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}}, {"AUTH": "basic", "USERNAME": "u", "PASSWORD": "p"}, {"AUTH": "basic", "USERNAME": "u", "PASSWORD": "x"}),
    ({"require_query": {"apikey": SECRET}}, {"AUTH": "query", "AUTH_QUERY_NAME": "apikey", "AUTH_QUERY_VALUE": SECRET}, {"AUTH": "none"}),
    ({"require_headers": {"X-Tenant": "acme"}}, {"HEADERS": '{"X-Tenant": "acme"}'}, {"HEADERS": '{"X-Tenant": "other"}'}),
])
def test_mock_enforces_credentials(demo_cfg, require, good, bad):
    ok = _mock_enrich(demo_cfg, {f"UPSTREAM_DEMO_{k}": v for k, v in good.items()}, **require)
    assert ok["complete"] is True
    nope = _mock_enrich(demo_cfg, {f"UPSTREAM_DEMO_{k}": v for k, v in bad.items()}, **require)
    assert nope["complete"] is False and nope["errors"][0]["code"] == "http_401"


def test_parse_requirements():
    from api_wrapper.mock_upstream import parse_requirements
    h, q = parse_requirements(["X-A=1"], ["k=v"], "u:p", None)
    assert h["X-A"] == "1" and h["Authorization"].startswith("Basic ") and q == {"k": "v"}
    assert parse_requirements([], [], None, "tok")[0] == {"Authorization": "Bearer tok"}
    with pytest.raises(ValueError):
        parse_requirements(["novalue"], [], None, None)
