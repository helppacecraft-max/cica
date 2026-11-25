"""Assemble the response: apply field mappings to source results and report per-field status.

Accuracy contract: every requested field appears in ``field_status`` with an explicit status.
A null value is never ambiguous -- the status says *why* it is null.
"""

from __future__ import annotations

from typing import Any

from ..config.models import FieldConfig, ProjectConfig
from .executor import ItemResult, SourceResult
from .extract import (
    AMBIGUOUS,
    COERCION,
    OK,
    SOURCE_FAILED,
    SOURCE_NOT_FOUND,
    FieldResult,
    extract_field,
)
from .planner import Plan


def _from_item(f: FieldConfig, sr: SourceResult, item: ItemResult | None, nulls: frozenset[str]) -> FieldResult:
    if sr.status in ("failed", "skipped") and item is None:
        return FieldResult(None, SOURCE_FAILED, sr.error_code)
    if item is None:
        return FieldResult(None, SOURCE_NOT_FOUND)
    if item.status == "not_found":
        return FieldResult(None, SOURCE_NOT_FOUND)
    if item.status == "failed":
        return FieldResult(None, SOURCE_FAILED, item.error_code)
    return extract_field(f, item.data, nulls)


def assemble(cfg: ProjectConfig, plan: Plan, results: dict[str, SourceResult], include_provenance: bool = False) -> dict:
    nulls = frozenset(cfg.null_values)
    data: dict[str, Any] = {}
    status: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    required_failures: list[str] = []
    provenance: dict[str, Any] = {}

    for g in plan.groups:
        gfields = [f for f in plan.fields if f.group == g]
        foreach_params = {cfg.sources[f.source].foreach.param for f in gfields if cfg.sources[f.source].foreach}
        if include_provenance:
            provenance[g] = {
                f.name: {"source": f.source, "paths": f.paths, "type": f.type, "required": f.required}
                for f in gfields
            }

        def run_fields(key: int | None) -> tuple[dict, dict]:
            vals: dict[str, Any] = {}
            stats: dict[str, str] = {}
            for f in gfields:
                src = cfg.sources[f.source]
                sr = results[f.source]
                # fields from non-foreach sources are broadcast onto every row of an array group
                item = sr.item(key if src.foreach else None)
                fr = _from_item(f, sr, item, nulls)
                vals[f.name], stats[f.name] = fr.value, fr.status
                label = f"{g}.{f.name}" if key is None else f"{g}[{next(iter(foreach_params))}={key}].{f.name}"
                if fr.status in (AMBIGUOUS, COERCION):
                    errors.append({"field": label, "code": fr.code or fr.status})
                if f.required and fr.status != OK:
                    required_failures.append(f"{label}:{fr.status}")
            return vals, stats

        if not foreach_params:
            data[g], status[g] = run_fields(None)
            continue

        row_key = next(iter(foreach_params))
        keys: set[int] = set()
        for f in gfields:
            if cfg.sources[f.source].foreach:
                keys |= {i.key for i in results[f.source].items if i.key is not None}
        rows, srows = [], []
        for k in sorted(keys):
            vals, stats = run_fields(k)
            rows.append({row_key: k, **vals})
            srows.append({row_key: k, **stats})
        data[g], status[g] = rows, srows

    source_errors = []
    for name in plan.sources:
        sr = results[name]
        if sr.status in ("failed", "skipped", "partial"):
            source_errors.append(
                {"source": name, "code": sr.error_code or sr.status, "http_status": sr.http_status, "state": sr.status}
            )
    errors = source_errors + errors

    out: dict[str, Any] = {
        "complete": not source_errors and not required_failures,
        "data": data,
        "field_status": status,
        "required_failures": required_failures,
        "errors": errors,
        "meta_sources": [
            {
                "name": n,
                "status": results[n].status,
                "http_status": results[n].http_status,
                "attempts": results[n].attempts,
                "latency_ms": results[n].latency_ms,
                "items": len(results[n].items) if results[n].foreach else None,
            }
            for n in plan.sources
        ],
    }
    if include_provenance:
        out["provenance"] = provenance
    return out
