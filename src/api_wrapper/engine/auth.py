"""Build the credentials an upstream client sends, from a ProjectEnv. Shared by the runtime and `probe`."""

from __future__ import annotations

import httpx

from ..settings import ProjectEnv


def build_auth(env: ProjectEnv) -> tuple[httpx.Auth | None, dict[str, str], dict[str, str]]:
    """-> (httpx auth object, default headers, default query params)."""
    auth: httpx.Auth | None = None
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    if env.auth == "basic":
        auth = httpx.BasicAuth(env.username or "", env.password or "")
    elif env.auth == "bearer":
        headers["Authorization"] = f"Bearer {env.token}"
    elif env.auth == "header":
        headers[env.auth_header_name] = f"{env.auth_header_prefix}{env.auth_header_value}"
    elif env.auth == "query":
        params[env.auth_query_name or ""] = env.auth_query_value or ""
    headers.update(env.extra_headers)  # conflicts with the auth header were rejected by ProjectEnv.problems()
    return auth, headers, params
