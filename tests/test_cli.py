from __future__ import annotations

import json

import yaml

from api_wrapper.cli import main


def test_selftest_passes(capsys):
    assert main(["selftest"]) == 0
    out = capsys.readouterr().out
    assert "SELFTEST PASSED" in out and "FAIL]" not in out


def test_convert_cli_writes_outputs_and_strict_flag(sample_dir, tmp_path, capsys):
    args = ["convert", "--excel", str(sample_dir / "sample_mapping.xlsx"), "--project", "demo", "--out", str(tmp_path)]
    assert main(args + ["--overrides", str(sample_dir / "overrides.yml")]) == 0
    assert (tmp_path / "demo" / "mapping.yml").is_file() and (tmp_path / "demo" / "report.md").is_file()
    assert yaml.safe_load((tmp_path / "demo" / "report.yml").read_text())["loadable"] is True
    # without overrides the config is not loadable -> --strict exits 2
    assert main(args + ["--strict"]) == 2
    assert "NOT loadable" in capsys.readouterr().out


def test_convert_cli_bad_input_exits_1(tmp_path, capsys):
    assert main(["convert", "--excel", str(tmp_path / "missing.xlsx"), "--project", "x", "--out", str(tmp_path)]) == 1
    assert "ERROR" in capsys.readouterr().out


def test_validate_structure_only(converted, capsys):
    assert main(["validate", "--config", str(converted[2])]) == 0
    assert "NOT checked against real responses" in capsys.readouterr().out


def test_validate_invalid_config(tmp_path, capsys):
    p = tmp_path / "m.yml"
    p.write_text("project: x\nsources: {}\nfields: []\n")
    assert main(["validate", "--config", str(p)]) == 1


def _samples(tmp_path, summary):
    d = tmp_path / "samples"
    d.mkdir()
    (d / "claim_summary.json").write_text(json.dumps(summary))
    (d / "claim_lines.1.json").write_text(json.dumps({
        "CLCL_ID": "x", "CDML_FROM_DT": "2025-01-01", "CDML_TO_DT": "2025-01-02", "RCRC_ID": "0450", "IPCD_ID": "99213",
        "CDML_UNITS": 1, "CDML_CHG_AMT": "10.00", "CDML_IPCD_MOD2": "25",
    }))
    return d


GOOD_SUMMARY = {
    "CLCL_ID": "x", "PRPR_NAME": "P", "MCTN_ID": "0123", "PRPR_NPI": "1", "PRPR_ID": "p1", "PRPR_ADDR1": "a",
    "CIV8_CLCL_NTWK_IND_DE": "IN", "MEME_FIRST_NAME": "A", "MEME_LAST_NAME": "B", "CLCL_RECD_DT": "2025-01-01",
    "CDML_FROM_DT": "2025-01-01", "CLCL_TOT_PAYABLE": "1.00", "CDML_UNITS": 1, "SBSB_ID": "S", "LINE_COUNT": 1,
}


def test_validate_samples_passes_when_paths_resolve(converted, tmp_path, capsys):
    d = _samples(tmp_path, GOOD_SUMMARY)
    assert main(["validate", "--config", str(converted[2]), "--samples", str(d)]) == 0
    out = capsys.readouterr().out
    assert "0 required FAIL" in out and "NO SAMPLE" in out   # family/cob/providers had no sample


def test_validate_samples_fails_on_wrong_path_for_required_field(converted, tmp_path, capsys):
    bad = dict(GOOD_SUMMARY)
    del bad["PRPR_NAME"]                     # the real response does not have this key
    bad["providerName"] = "P"                # ...it has a different spelling
    d = _samples(tmp_path, bad)
    assert main(["validate", "--config", str(converted[2]), "--samples", str(d)]) == 1
    out = capsys.readouterr().out
    assert "claim.provider_name" in out and "FAIL" in out and "REQUIRED" in out


def test_validate_strict_fails_on_missing_samples(converted, tmp_path):
    d = _samples(tmp_path, GOOD_SUMMARY)
    assert main(["validate", "--config", str(converted[2]), "--samples", str(d), "--strict"]) == 1


def test_sample_command(tmp_path, capsys):
    assert main(["sample", "--dir", str(tmp_path / "s")]) == 0
    assert (tmp_path / "s" / "sample_mapping.xlsx").is_file()
