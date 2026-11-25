"""Execute a Plan: call each source (in dependency order, parallel within a level)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from ..config.models import ProjectConfig, SourceConfig
from ..config.validate import PLACEHOLDER
from .client import HttpResult, UpstreamClient
from .extract import find
from .planner import Plan

log = logging.getLogger("api_wrapper.executor")

_SAFE_PARAM = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@dataclass
class ItemResult:
    key: int | None  # None for non-foreach sources
    status: str  # ok | not_found | failed
    data: Any = None
    http_status: int | None = None
    error_code: str | None = None


@dataclass
class SourceResult:
    name: str
    status: str  # ok | partial | not_found | failed | skipped
    foreach: bool = False
    items: list[ItemResult] = field(default_factory=list)
    http_status: int | None = None
    error_code: str | None = None
    attempts: int = 0
    latency_ms: int = 0

    def item(self, key: int | None) -> ItemResult | None:
        for it in self.items:
            if it.key == key:
                return it
        return None


def render_path(template: str, params: dict[str, Any]) -> str:
    # Every substituted value is URL-encoded: a value can never add path segments or a query.
    return PLACEHOLDER.sub(lambda m: quote(str(params[m.group(1)]), safe=""), template)


def _scalar(v: Any) -> str | None:
    if isinstance(v, bool) or v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return s if _SAFE_PARAM.match(s) else None


async def execute_plan(
    cfg: ProjectConfig,
    plan: Plan,
    client: UpstreamClient,
    inputs: dict[str, str],
    correlation_id: str | None = None,
) -> dict[str, SourceResult]:
    results: dict[str, SourceResult] = {}
    for level in plan.levels:
        outs = await asyncio.gather(
            *(_run_source(n, cfg, client, inputs, results, correlation_id) for n in level)
        )
        results.update({r.name: r for r in outs})
    return results


async def _run_source(
    name: str,
    cfg: ProjectConfig,
    client: UpstreamClient,
    inputs: dict[str, str],
    results: dict[str, SourceResult],
    cid: str | None,
) -> SourceResult:
    t0 = time.perf_counter()
    src = cfg.sources[name]
    try:
        params: dict[str, Any] = dict(inputs)
        for pname, b in src.params.items():
            dep = results[b.from_source]
            item = dep.item(None)
            if dep.status != "ok" or item is None:
                return SourceResult(name, "skipped", error_code=f"dependency_{b.from_source}_unavailable")
            matches = find(b.path, item.data)
            value = _scalar(matches[0]) if len(matches) == 1 else None
            if value is None:
                return SourceResult(name, "skipped", error_code=f"param_{pname}_unresolved")
            params[pname] = value
        res = await (_run_foreach(name, src, cfg, client, params, results, cid) if src.foreach
                     else _run_single(name, src, client, params, cid))
    except Exception as e:  # never let one source take the whole request down
        log.exception("source %s crashed", name)
        res = SourceResult(name, "failed", error_code=f"internal_error:{type(e).__name__}")
    res.latency_ms = int((time.perf_counter() - t0) * 1000)
    return res


async def _fetch(src: SourceConfig, params: dict[str, Any], client: UpstreamClient, cid: str | None) -> HttpResult:
    # query values may use {input} placeholders; httpx URL-encodes them
    query = {k: PLACEHOLDER.sub(lambda m: str(params[m.group(1)]), v) for k, v in src.query.items()}
    res = await client.get_json(render_path(src.path, params), query, correlation_id=cid, timeout_s=src.timeout_s)
    if res.status == "ok" and src.response_root:
        matches = find(src.response_root, res.data)
        if len(matches) != 1:
            return HttpResult("failed", res.http_status, attempts=res.attempts, error_code="response_root_missing")
        res.data = matches[0]
    return res


def _item(key: int | None, r: HttpResult) -> ItemResult:
    return ItemResult(key, r.status, r.data if r.status == "ok" else None, r.http_status, r.error_code)


async def _run_single(name, src, client, params, cid) -> SourceResult:
    r = await _fetch(src, params, client, cid)
    return SourceResult(
        name, r.status, items=[_item(None, r)], http_status=r.http_status, error_code=r.error_code,
        attempts=r.attempts,
    )


async def _run_foreach(name, src, cfg, client, params, results, cid) -> SourceResult:
    fe = src.foreach
    d = fe.discovery
    out = SourceResult(name, "ok", foreach=True)

    async def fetch_key(k: int) -> ItemResult:
        r = await _fetch(src, {**params, fe.param: k}, client, cid)
        out.attempts += r.attempts
        return _item(k, r)

    complete = True
    if d.mode == "probe":
        for k in range(d.start, d.start + d.max):
            it = await fetch_key(k)
            if it.status == "not_found":
                break
            out.items.append(it)
            if it.status == "failed":
                complete = False  # cannot know whether further items exist
                break
        else:
            complete = False
            out.error_code = "probe_limit_reached"
    else:
        if d.mode == "fixed":
            keys = list(d.values)
        else:
            dep = results[d.source]
            dep_item = dep.item(None)
            if dep.status != "ok" or dep_item is None:
                return SourceResult(name, "skipped", foreach=True, error_code=f"dependency_{d.source}_unavailable")
            matches = find(d.path, dep_item.data)
            try:
                if d.kind == "count":
                    if len(matches) != 1:
                        raise ValueError
                    n = int(matches[0])
                    keys = list(range(d.start, d.start + min(max(n, 0), d.max)))
                    if n > d.max:
                        complete = False
                        out.error_code = "discovery_limit_reached"
                else:
                    keys = [int(m) for m in matches]
                    if len(keys) > d.max:
                        keys, complete = keys[: d.max], False
                        out.error_code = "discovery_limit_reached"
            except (TypeError, ValueError):
                return SourceResult(name, "failed", foreach=True, error_code="discovery_invalid")
        out.items = list(await asyncio.gather(*(fetch_key(k) for k in keys)))

    failed = [i for i in out.items if i.status == "failed"]
    if failed:
        out.http_status, out.error_code = failed[0].http_status, out.error_code or failed[0].error_code
    if failed or not complete:
        out.status = "partial"
        out.error_code = out.error_code or "discovery_incomplete"
    if not any(i.status == "ok" for i in out.items):
        out.status = "failed" if failed else "not_found"
        out.error_code = out.error_code or ("no_items" if not failed else None)
    return out
