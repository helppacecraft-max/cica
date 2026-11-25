"""`facets-wrapper probe`: make ONE real GET to Facets with the project's configured credentials and
report what came back -- without ever printing a value.

Use it to (1) find out which authentication Facets accepts, (2) see the JSON structure (key names,
types) so you can set ``path_template`` / ``response_root`` correctly, (3) optionally save the raw
response as a sample for ``validate --samples`` / ``contract-test`` (``--save`` writes REAL DATA to disk).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .engine.auth import build_auth
from .settings import ProjectEnv, load_project_env


def outline(v: Any, depth: int = 0, max_depth: int = 4, max_keys: int = 60, indent: str = "") -> list[str]:
    """Key names and value TYPES only -- never values."""
    lines: list[str] = []
    if isinstance(v, dict):
        for i, (k, x) in enumerate(v.items()):
            if i >= max_keys:
                lines.append(f"{indent}... ({len(v) - max_keys} more keys)")
                break
            if isinstance(x, (dict, list)) and depth < max_depth:
                kind = f"object ({len(x)} keys)" if isinstance(x, dict) else f"array ({len(x)} items)"
                lines.append(f"{indent}{k}: {kind}")
                lines += outline(x if isinstance(x, dict) else (x[0] if x else None), depth + 1, max_depth, max_keys, indent + "    ")
            else:
                lines.append(f"{indent}{k}: {type(x).__name__}")
    elif isinstance(v, list) and v:
        lines += outline(v[0], depth + 1, max_depth, max_keys, indent)
    return lines


def run_probe(
    project: str,
    path: str,
    environ: dict[str, str],
    save: str | None = None,
    transport: httpx.BaseTransport | None = None,
    out: Callable[[str], None] = print,
) -> int:
    env: ProjectEnv = load_project_env(project, environ)
    probs = [p for p in env.problems() if not (transport is not None and p == "base URL not configured")]
    if probs:
        out("Cannot probe -- fix the configuration first:")
        for p in probs:
            out(f"  - {p}")
        return 1
    if not path.startswith("/") or "://" in path:
        out("--path must be a path starting with '/', e.g. /RestServices/facets/api/v1/Claims/<ID>/Inquiry/Summary")
        return 1

    auth, headers, params = build_auth(env)
    out(f"Probing project '{project}'  auth={env.auth}  extra_headers={sorted(env.extra_headers) or 'none'}")
    t0 = time.perf_counter()
    try:
        with httpx.Client(
            base_url=env.base_url or "http://upstream.invalid", auth=auth, headers={"Accept": "application/json", **headers},
            params=params or None, timeout=env.timeout_s, verify=env.verify, transport=transport,
        ) as c:
            r = c.get(path)
    except httpx.TimeoutException:
        out("RESULT: timeout -- host reachable? firewall / VPN / wrong port?")
        return 1
    except httpx.TransportError as e:
        out(f"RESULT: connection error ({type(e).__name__}) -- wrong host/port, TLS problem (see FACETS_<P>_CA_BUNDLE), or VPN?")
        return 1
    ms = int((time.perf_counter() - t0) * 1000)
    out(f"HTTP {r.status_code} in {ms} ms   content-type: {r.headers.get('content-type', '-')}")

    if r.status_code in (401, 403):
        wa = r.headers.get("www-authenticate")
        out(f"RESULT: credentials rejected.{'  Server says www-authenticate: ' + wa if wa else ''}")
        out("  Hints: 'Basic' -> AUTH=basic | 'Bearer' -> AUTH=bearer | an 'X-...-Key' header name in the docs/Postman -> AUTH=header "
            "with FACETS_<P>_AUTH_HEADER_NAME. Compare with a request that works in Postman/browser dev tools (Headers tab).")
        out("  403 with valid credentials can also mean the account lacks permission for this endpoint, or a required extra header "
            "(e.g. tenant/application id) is missing -> FACETS_<P>_HEADERS.")
        return 1
    if r.status_code == 404:
        out("RESULT: authenticated OK (or auth not enforced) but this path/ID does not exist -- check the path and claim ID.")
        return 1
    if not (200 <= r.status_code < 300):
        out(f"RESULT: unexpected status {r.status_code}.")
        return 1
    try:
        body = r.json()
    except ValueError:
        out(f"RESULT: HTTP OK but the body is not JSON ({len(r.content)} bytes) -- wrong URL or an HTML login page?")
        return 1
    out("RESULT: OK -- authenticated and JSON received.")
    out("Structure (names and types only, no values):")
    for line in outline(body) or ["(empty or scalar JSON)"]:
        out("  " + line)
    if save:
        p = Path(save)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        out(f"Saved the RAW response to {p}  <-- contains real data: sanitise PHI before sharing or committing.")
    return 0
