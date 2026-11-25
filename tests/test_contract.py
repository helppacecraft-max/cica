from __future__ import annotations

import json

import pytest

from api_wrapper.cli import main
from api_wrapper.contract import run_cases
from api_wrapper.mock_upstream import build_document


@pytest.fixture()
def cases(tmp_path, demo_cfg):
    """One case whose recorded responses are synthesised from the config (stand-in for real captures)."""
    case = tmp_path / "cases" / "happy"
    (case / "responses").mkdir(parents=True)
    (case / "case.json").write_text(json.dumps({"claim_id": "CLAIM000001"}))
    for name, src in demo_cfg.sources.items():
        if src.foreach:
            for k in (1, 2):
                doc, _ = build_document(demo_cfg, name, "CLAIM000001", 2, k)
                (case / "responses" / f"{name}.{k}.json").write_text(json.dumps(doc))
        else:
            doc, _ = build_document(demo_cfg, name, "CLAIM000001", 2, None)
            (case / "responses" / f"{name}.json").write_text(json.dumps(doc))
    return tmp_path / "cases"


def test_update_then_pass_then_detect_drift(cases, demo_cfg):
    assert all(r.updated for r in run_cases(demo_cfg, cases, update=True))
    assert all(r.passed for r in run_cases(demo_cfg, cases))

    exp = cases / "happy" / "expected.json"
    data = json.loads(exp.read_text())
    data["data"]["claim"]["provider_name"] = "SOMETHING ELSE"
    exp.write_text(json.dumps(data))
    res = run_cases(demo_cfg, cases)
    assert not res[0].passed and "provider_name" in res[0].problems[0]


def test_changed_upstream_response_is_detected(cases, demo_cfg):
    run_cases(demo_cfg, cases, update=True)
    f = cases / "happy" / "responses" / "claim_summary.json"
    doc = json.loads(f.read_text())
    doc["PRPR_NAME"] = "RENAMED"                 # upstream value changed
    del doc["MCTN_ID"]                           # upstream dropped a field
    f.write_text(json.dumps(doc))
    problems = "\n".join(run_cases(demo_cfg, cases)[0].problems)
    assert "provider_name" in problems and "tax_id" in problems


def test_missing_response_file_is_served_as_404(cases, demo_cfg):
    (cases / "happy" / "responses" / "claim_cob.json").unlink()
    run_cases(demo_cfg, cases, update=True)
    exp = json.loads((cases / "happy" / "expected.json").read_text())
    assert exp["field_status"]["claim"]["remit_payee_name"] == "source_not_found"


def test_type_difference_is_a_difference(cases, demo_cfg):
    run_cases(demo_cfg, cases, update=True)
    exp = cases / "happy" / "expected.json"
    d = json.loads(exp.read_text())
    d["data"]["line"][0]["charges"] = 125.0       # float instead of exact string
    exp.write_text(json.dumps(d))
    assert not run_cases(demo_cfg, cases)[0].passed


def test_cli_contract_exit_codes(cases, converted, capsys):
    cfg = str(converted[2])
    assert main(["contract-test", "--config", cfg, "--cases", str(cases)]) == 1       # no expected.json yet
    assert main(["contract-test", "--config", cfg, "--cases", str(cases), "--update"]) == 0
    assert main(["contract-test", "--config", cfg, "--cases", str(cases)]) == 0
    assert "1/1 cases passed" in capsys.readouterr().out


def test_case_json_accepts_inputs_form_and_rejects_invalid_inputs(cases, demo_cfg):
    case = cases / "happy" / "case.json"
    case.write_text(json.dumps({"inputs": {"claim_id": "CLAIM000001"}}))
    assert all(r.updated for r in run_cases(demo_cfg, cases, update=True))
    case.write_text(json.dumps({"inputs": {"claim_id": "bad id!"}}))
    res = run_cases(demo_cfg, cases)
    assert not res[0].passed and "invalid" in res[0].problems[0]
