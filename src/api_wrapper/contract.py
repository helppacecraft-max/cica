"""Contract tests: replay RECORDED upstream responses through the real engine and compare the
result with a human-verified ``expected.json``. This is the regression gate for mapping accuracy.

Case layout (one directory per claim scenario)::

    cases/<case_name>/
        case.json              {"inputs": {"claim_id": "CLAIM000001"}, "groups": [...]?, "fields": [...]?}
                               (legacy {"claim_id": "..."} is also accepted)
        responses/
            <source>.json          response of a non-foreach source
            <source>.<n>.json      response of a foreach source for item n (e.g. claim_lines.1.json)
        expected.json          {"complete": true, "data": {...}, "field_status": {...}}

A source with no response file is served as HTTP 404 (so "this claim has no COB" is testable).
Record responses from the real upstream (Postman/curl), SANITISE any PHI, and have a person verify
``expected.json`` against the source system's UI the first time -- ``--update`` only snapshots current output.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import httpx

from .config.models import ProjectConfig
from .config.validate import PLACEHOLDER
from .engine.runtime import ProjectRuntime
from .settings import ProjectEnv


@dataclass
class CaseResult:
    name: str
    passed: bool
    problems: list[str] = field(default_factory=list)
    updated: bool = False


def _route_table(cfg: ProjectConfig):
    table = []
    for name, src in cfg.sources.items():
        rx, pos = "", 0
        for m in PLACEHOLDER.finditer(src.path):
            rx += re.escape(src.path[pos : m.start()]) + f"(?P<{m.group(1)}>[^/]+)"
            pos = m.end()
        rx += re.escape(src.path[pos:])
        table.append((name, re.compile("^" + rx + "$"), src.foreach.param if src.foreach else None))
    return table


def _transport(cfg: ProjectConfig, responses: Path) -> httpx.MockTransport:
    table = _route_table(cfg)

    def handler(req: httpx.Request) -> httpx.Response:
        path = unquote(req.url.path)
        for name, rx, fe_param in table:
            m = rx.match(path)
            if not m:
                continue
            f = responses / (f"{name}.{m.group(fe_param)}.json" if fe_param else f"{name}.json")
            if f.is_file():
                return httpx.Response(200, json=json.loads(f.read_text(encoding="utf-8")))
            return httpx.Response(404)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _diff(exp, got, path="") -> list[str]:
    if isinstance(exp, dict) and isinstance(got, dict):
        out = []
        for k in sorted(set(exp) | set(got)):
            if k not in got:
                out.append(f"{path}.{k}: missing in result (expected {exp[k]!r})")
            elif k not in exp:
                out.append(f"{path}.{k}: unexpected in result ({got[k]!r})")
            else:
                out += _diff(exp[k], got[k], f"{path}.{k}")
        return out
    if isinstance(exp, list) and isinstance(got, list):
        if len(exp) != len(got):
            return [f"{path}: expected {len(exp)} item(s), got {len(got)}"]
        return [d for i, (e, g) in enumerate(zip(exp, got)) for d in _diff(e, g, f"{path}[{i}]")]
    return [] if (exp == got and type(exp) is type(got)) else [f"{path}: expected {exp!r}, got {got!r}"]


def run_cases(cfg: ProjectConfig, cases_dir: str | Path, update: bool = False) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in sorted(p for p in Path(cases_dir).iterdir() if p.is_dir()):
        spec_file = case / "case.json"
        if not spec_file.is_file():
            continue
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        rt = ProjectRuntime(cfg, ProjectEnv(base_url="http://contract.invalid", retries=0, backoff_s=0), _transport(cfg, case / "responses"))
        inputs = spec["inputs"] if "inputs" in spec else {"claim_id": spec["claim_id"]}
        clean, problems = rt.check_inputs(inputs)
        if problems:
            results.append(CaseResult(case.name, False, [f"case.json inputs invalid: {problems}"]))
            continue
        out = asyncio.run(rt.enrich(clean, spec.get("groups"), spec.get("fields")))
        asyncio.run(rt.aclose())
        actual = {"complete": out["complete"], "data": out["data"], "field_status": out["field_status"]}
        exp_file = case / "expected.json"
        if update:
            exp_file.write_text(json.dumps(actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            results.append(CaseResult(case.name, True, updated=True))
            continue
        if not exp_file.is_file():
            results.append(CaseResult(case.name, False, ["expected.json missing (create it with --update, then verify it by hand)"]))
            continue
        problems = _diff(json.loads(exp_file.read_text(encoding="utf-8")), actual)
        results.append(CaseResult(case.name, not problems, problems))
    return results
