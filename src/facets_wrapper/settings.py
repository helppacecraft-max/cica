"""Deployment settings come from environment variables -- never from the mapping YAML.

Per project (``<P>`` = project name upper-cased, non-alphanumerics -> ``_``):

    FACETS_<P>_BASE_URL          required, e.g. https://facets-host:10408
    FACETS_<P>_AUTH              none (default) | basic | bearer
    FACETS_<P>_USERNAME / _PASSWORD   for basic
    FACETS_<P>_TOKEN             for bearer
    FACETS_<P>_VERIFY_TLS        true (default) | false
    FACETS_<P>_CA_BUNDLE         path to a CA bundle (overrides VERIFY_TLS)
    FACETS_<P>_TIMEOUT_S         default 15
    FACETS_<P>_RETRIES           default 2   (timeouts / connection errors / 5xx only)
    FACETS_<P>_MAX_CONCURRENCY   default 8   (parallel upstream calls per project)
    FACETS_WRAPPER_API_KEY_<P>   if set, callers must send  X-API-Key

Global:  FACETS_WRAPPER_CONFIG_DIR, FACETS_WRAPPER_ADMIN_KEY, FACETS_WRAPPER_PLUGINS
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def env_key(project: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", project).upper()


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class ProjectEnv:
    base_url: str | None
    auth: str = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    verify: bool | str = True
    timeout_s: float = 15.0
    retries: int = 2
    backoff_s: float = 0.2
    max_concurrency: int = 8
    api_key: str | None = None

    def problems(self) -> list[str]:
        out = []
        if not self.base_url:
            out.append("base URL not configured")
        if self.auth not in ("none", "basic", "bearer"):
            out.append(f"unknown auth type {self.auth!r}")
        if self.auth == "basic" and not (self.username and self.password):
            out.append("basic auth needs username and password")
        if self.auth == "bearer" and not self.token:
            out.append("bearer auth needs a token")
        return out


def load_project_env(project: str, environ: dict[str, str]) -> ProjectEnv:
    k = env_key(project)
    g = lambda suffix: environ.get(f"FACETS_{k}_{suffix}")  # noqa: E731
    verify: bool | str = _bool(g("VERIFY_TLS"), True)
    if g("CA_BUNDLE"):
        verify = g("CA_BUNDLE")  # type: ignore[assignment]
    return ProjectEnv(
        base_url=(g("BASE_URL") or "").rstrip("/") or None,
        auth=(g("AUTH") or "none").lower(),
        username=g("USERNAME"),
        password=g("PASSWORD"),
        token=g("TOKEN"),
        verify=verify,
        timeout_s=float(g("TIMEOUT_S") or 15),
        retries=int(g("RETRIES") or 2),
        max_concurrency=int(g("MAX_CONCURRENCY") or 8),
        api_key=environ.get(f"FACETS_WRAPPER_API_KEY_{k}") or None,
    )
