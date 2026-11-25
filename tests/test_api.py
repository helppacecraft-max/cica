from __future__ import annotations

import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from facets_wrapper.api.app import create_app
from facets_wrapper.config.loader import ConfigError
from facets_wrapper.mock_upstream import create_mock_app


def post(c, **body):
    body.setdefault("claim_id", "CLAIM000001")
    return c.post("/v1/claims/enrich", json=body)


def test_enrich_full_claim(client):
    r = post(client, include_provenance=True)
    assert r.status_code == 200
    b = r.json()
    assert b["complete"] is True and b["claim_id"] == "CLAIM000001"
    # claim_inquiry survives only with 'paid_amount' (the other rows pointed at a UI URL and were skipped)
    assert set(b["data"]) == {"claim", "line", "matchum", "claim_inquiry"}
    assert list(b["data"]["claim_inquiry"]) == ["paid_amount"]
    assert len(b["data"]["line"]) == 3
    assert b["data"]["line"][0]["charges"] == "125.00"
    assert b["data"]["claim"]["member_name"].count(" ") == 1
    assert b["provenance"]["claim"]["tax_id"]["type"] == "string"
    assert b["meta"]["project"] == "demo" and b["meta"]["correlation_id"]


def test_every_requested_field_has_a_status(client):
    b = post(client).json()
    for g, obj in b["data"].items():
        st = b["field_status"][g]
        if isinstance(obj, list):
            assert [set(a) for a in obj] == [set(s) for s in st]
        else:
            assert set(obj) == set(st)


def test_selection_by_groups_and_fields(client):
    b = post(client, groups=["claim"]).json()
    assert set(b["data"]) == {"claim"}
    b = post(client, fields=["claim.provider_name", "line.charges"]).json()
    assert set(b["data"]["claim"]) == {"provider_name"} and set(b["data"]["line"][0]) == {"line_no", "charges"}


def test_invalid_selection_and_claim_id(client):
    r = post(client, groups=["nope"], fields=["claim.zzz"])
    assert r.status_code == 422 and r.json()["detail"]["unknown_groups"] == ["nope"]
    for bad in ("bad id!", "A/../B", "", "x" * 40, "../../etc/passwd"):
        assert post(client, claim_id=bad).status_code == 422, bad
    assert client.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001", "surprise": 1}).status_code == 422


def test_strict_mode_turns_incomplete_into_502(client):
    assert post(client, claim_id="ERROR500XX").status_code == 200          # non-strict: report, don't fail
    assert post(client, claim_id="ERROR500XX").json()["complete"] is False
    r = post(client, claim_id="ERROR500XX", strict=True)
    assert r.status_code == 502 and r.json()["complete"] is False


def test_not_found_claim_reports_required_failures(client):
    b = post(client, claim_id="NOTFOUND01").json()
    assert b["complete"] is False and any("claim_id" in x for x in b["required_failures"])


def test_catalog_and_projects(client):
    c = client.get("/v1/catalog").json()
    assert c["groups"]["line"]["cardinality"] == "array" and c["groups"]["claim"]["cardinality"] == "object"
    assert {"name": "charges", "type": "decimal", "required": True, "label": "Charges", "source": "claim_lines"} in c["groups"]["line"]["fields"]
    assert client.get("/v1/projects").json()["demo"]["ready"] is True
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").status_code == 200


# ---- multi-project, auth, readiness, reload -----------------------------------------
@pytest.fixture()
def two_projects(converted, tmp_path, demo_cfg):
    cdir = tmp_path / "configs"
    shutil.copytree(converted[1], cdir)
    # a second project: same mapping, other name
    txt = (cdir / "demo" / "mapping.yaml").read_text().replace("project: demo", "project: other", 1)
    (cdir / "other").mkdir()
    (cdir / "other" / "mapping.yaml").write_text(txt)
    return cdir


def make_app(cdir, demo_cfg, **env):
    transport = httpx.ASGITransport(app=create_mock_app(demo_cfg, 2))
    return create_app(str(cdir), environ={"FACETS_DEMO_BASE_URL": "http://m", "FACETS_OTHER_BASE_URL": "http://m", **env}, transport=transport)


def test_project_header_required_with_several_projects(two_projects, demo_cfg):
    with TestClient(make_app(two_projects, demo_cfg)) as c:
        assert post(c).status_code == 400
        assert post(c, ).status_code == 400
        r = c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={"X-Project": "other"})
        assert r.status_code == 200 and r.json()["meta"]["project"] == "other"
        assert c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={"X-Project": "zzz"}).status_code == 404


def test_api_key_per_project(two_projects, demo_cfg):
    with TestClient(make_app(two_projects, demo_cfg, FACETS_WRAPPER_API_KEY_DEMO="s3cret")) as c:
        h = {"X-Project": "demo"}
        assert c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers=h).status_code == 401
        assert c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={**h, "X-API-Key": "wrong"}).status_code == 401
        assert c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={**h, "X-API-Key": "s3cret"}).status_code == 200
        # other project has no key configured -> open (and logs a warning)
        assert c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={"X-Project": "other"}).status_code == 200
        assert c.get("/v1/catalog", headers=h).status_code == 401


def test_project_without_base_url_is_not_ready_but_others_serve(two_projects):
    app = create_app(str(two_projects), environ={"FACETS_OTHER_BASE_URL": "http://m"})   # no transport, demo has no base URL
    with TestClient(app) as c:
        r = c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={"X-Project": "demo"})
        assert r.status_code == 503 and "base URL" in str(r.json())
        ready = c.get("/readyz")
        assert ready.status_code == 503 and ready.json()["projects"]["demo"]["ready"] is False


def test_invalid_config_refuses_to_start(two_projects):
    p = two_projects / "other" / "mapping.yaml"
    p.write_text(p.read_text().replace("source: claim_summary", "source: nonexistent", 1))
    with pytest.raises(ConfigError, match="unknown source"):
        with TestClient(create_app(str(two_projects), environ={})):
            pass


def test_reload_requires_key_and_keeps_old_config_when_new_is_invalid(two_projects, demo_cfg):
    with TestClient(make_app(two_projects, demo_cfg)) as c:
        assert c.post("/admin/reload").status_code == 404   # disabled without FACETS_WRAPPER_ADMIN_KEY
    with TestClient(make_app(two_projects, demo_cfg, FACETS_WRAPPER_ADMIN_KEY="adm")) as c:
        assert c.post("/admin/reload", headers={"X-Admin-Key": "bad"}).status_code == 401
        assert c.post("/admin/reload", headers={"X-Admin-Key": "adm"}).json()["reloaded"] == ["demo", "other"]
        (two_projects / "other" / "mapping.yaml").write_text("project: other\nsources: {}\nfields: []\n")
        r = c.post("/admin/reload", headers={"X-Admin-Key": "adm"})
        assert r.status_code == 422
        # still serving the previous, known-good config
        assert c.post("/v1/claims/enrich", json={"claim_id": "CLAIM000001"}, headers={"X-Project": "demo"}).status_code == 200
