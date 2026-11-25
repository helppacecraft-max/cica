"""A fake upstream that serves synthetic JSON generated FROM a project config.

Purpose: let anyone exercise the full wrapper (planning, parallel calls, line fan-out, error
handling) without access to a real upstream environment.

LIMITATION (important): because the fake responses are derived from the same config the wrapper
reads, this proves the *plumbing*, not the *mapping*. Whether the real upstream JSON contains
``$.PRPR_NAME`` can only be proven against real sample responses -- see ``api-wrapper validate``.

Magic claim IDs for failure testing:  ``NOTFOUND*`` -> 404 for every endpoint,
``ERROR500*`` -> HTTP 500, ``BADJSON*`` -> 200 with a non-JSON body, ``NOLINES*`` -> 404 for line 1.
"""

from __future__ import annotations

import base64
import hmac
import re
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .config.models import ProjectConfig, SourceConfig

_TOKEN = re.compile(r"\.([A-Za-z_]\w*)|\[(\*|\d+)\]")


def _tokens(path: str) -> list[str | int] | None:
    """Parse the simple JSONPath subset we can synthesise: $.a.b[*].c / $.a[0]. Else None."""
    if not path.startswith("$"):
        return None
    rest, out, pos = path[1:], [], 0
    while pos < len(rest):
        m = _TOKEN.match(rest, pos)
        if not m:
            return None
        out.append(m.group(1) if m.group(1) else (-1 if m.group(2) == "*" else int(m.group(2))))
        pos = m.end()
    return out or None


def _set(doc: Any, toks: list[str | int], value_fn, n_items: int) -> Any:
    """Insert value at path into doc (creating dicts/lists). value_fn(index) -> leaf value."""
    def rec(node, i, idx):
        t = toks[i]
        last = i == len(toks) - 1
        if isinstance(t, str):
            node = node if isinstance(node, dict) else {}
            node[t] = value_fn(idx) if last else rec(node.get(t), i + 1, idx)
            return node
        if t == -1:  # [*] -> n_items elements
            node = node if isinstance(node, list) else [None] * n_items
            for k in range(n_items):
                node[k] = value_fn(k) if last else rec(node[k], i + 1, k)
            return node
        node = node if isinstance(node, list) else [None] * (t + 1)
        while len(node) <= t:
            node.append(None)
        node[t] = value_fn(idx) if last else rec(node[t], i + 1, idx)
        return node

    return rec(doc, 0, 0)


def _leaf(field_name: str, type_: str, claim: str, idx: int):
    return {
        "decimal": "125.00", "integer": 7, "boolean": True,
        "date": "2025-01-15", "datetime": "2025-01-15T00:00:00", "list": ["A", "B"], "any": "x",
    }.get(type_, f"{field_name}-{claim}")


def build_document(cfg: ProjectConfig, source: str, claim: str, lines: int, key: int | None) -> tuple[Any, list[str]]:
    """Synthesise one response for ``source``. Returns (document, unmockable_paths)."""
    doc: Any = {}
    skipped: list[str] = []
    for f in cfg.fields:
        if f.source != source:
            continue
        for p in f.paths:
            toks = _tokens(p)
            if not toks:
                skipped.append(p)
                continue
            doc = _set(doc, toks, lambda i, f=f, t=toks: _leaf(str(t[-1]), f.type if len(f.paths) == 1 else "string", claim, i), lines)
    for other in cfg.sources.values():  # values other sources read from this one
        for b in other.params.values():
            if b.from_source == source and (toks := _tokens(b.path)):
                doc = _set(doc, toks, lambda i: "SUB000001", lines)
        d = other.foreach.discovery if other.foreach else None
        if d and d.mode == "from_source" and d.source == source and (toks := _tokens(d.path or "")):
            doc = _set(doc, toks, (lambda i: i + 1) if d.kind == "values" else (lambda i: lines), lines)
    root = cfg.sources[source].response_root
    if root and (rt := _tokens(root)):
        doc = _set({}, rt, lambda i: doc, 1)
    return doc, skipped


def create_mock_app(
    cfg: ProjectConfig,
    lines: int = 3,
    require_headers: dict[str, str] | None = None,
    require_query: dict[str, str] | None = None,
) -> FastAPI:
    """``require_headers`` / ``require_query``: the mock answers 401 unless ALL are present and exact --
    lets you test your auth configuration (basic = Authorization header with the encoded value)."""
    app = FastAPI(title=f"Mock upstream for {cfg.project}")
    app.state.unmockable = {}
    required_h = {k.lower(): v for k, v in (require_headers or {}).items()}

    if required_h or require_query:
        @app.middleware("http")
        async def _auth(request: Request, call_next):
            ok = all(hmac.compare_digest(request.headers.get(k, ""), v) for k, v in required_h.items()) and all(
                hmac.compare_digest(request.query_params.get(k, ""), v) for k, v in (require_query or {}).items()
            )
            if not ok:
                scheme = "Basic" if required_h.get("authorization", "").startswith("Basic ") else (
                    "Bearer" if required_h.get("authorization", "").startswith("Bearer ") else None)
                hdr = {"WWW-Authenticate": f'{scheme} realm="mock-upstream"'} if scheme else {}
                return JSONResponse({"error": "unauthorized"}, status_code=401, headers=hdr)
            return await call_next(request)

    def make(name: str, src: SourceConfig):
        fe = src.foreach

        async def handler(**kw):
            vals = [str(v) for k, v in kw.items() if not (fe and k == fe.param)]
            claim = next((v for v in vals if re.match(r"(?i)(NOTFOUND|ERROR500|BADJSON|NOLINES)", v)), None) \
                or str(kw.get("claim_id") or (vals[0] if vals else "CLAIM000001"))
            if claim.upper().startswith("NOTFOUND"):
                return JSONResponse({"error": "not found"}, status_code=404)
            if claim.upper().startswith("ERROR500"):
                return JSONResponse({"error": "boom"}, status_code=500)
            if claim.upper().startswith("BADJSON"):
                return Response("<html>not json</html>", media_type="text/html")
            key = None
            if fe:
                key = int(kw[fe.param])
                if key < 1 or key > lines or (claim.upper().startswith("NOLINES")):
                    return JSONResponse({"error": "no such line"}, status_code=404)
            doc, skipped = build_document(cfg, name, claim, lines, key)
            app.state.unmockable[name] = skipped
            return JSONResponse(doc)

        return handler

    def bind(h):
        async def endpoint(request: Request):
            return await h(**request.path_params)

        return endpoint

    for name, src in cfg.sources.items():
        app.add_api_route(src.path, bind(make(name, src)), methods=["GET"], name=name)
    return app


def parse_requirements(header: list[str], query: list[str], basic: str | None, bearer: str | None) -> tuple[dict[str, str], dict[str, str]]:
    """CLI helper: --require-header NAME=VALUE, --require-query NAME=VALUE, --require-basic user:pass, --require-bearer TOKEN."""
    h: dict[str, str] = {}
    q: dict[str, str] = {}
    for item, target in [(x, h) for x in header] + [(x, q) for x in query]:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ValueError(f"expected NAME=VALUE, got {item!r}")
        target[name] = value
    if basic:
        h["Authorization"] = "Basic " + base64.b64encode(basic.encode()).decode()
    if bearer:
        h["Authorization"] = f"Bearer {bearer}"
    return h, q
