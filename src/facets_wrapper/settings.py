"""Deployment settings come from environment variables -- never from the mapping YAML.

Per project (``<P>`` = project name upper-cased, non-alphanumerics -> ``_``):

    FACETS_<P>_BASE_URL          required, e.g. https://facets-host:10408

  How the wrapper authenticates TO Facets (pick one mode):
    FACETS_<P>_AUTH              none (default) | basic | bearer | header | query
      basic   : FACETS_<P>_USERNAME, FACETS_<P>_PASSWORD
      bearer  : FACETS_<P>_TOKEN                          -> Authorization: Bearer <token>
      header  : FACETS_<P>_AUTH_HEADER_NAME  (default X-API-Key)
                FACETS_<P>_AUTH_HEADER_VALUE (secret)
                FACETS_<P>_AUTH_HEADER_PREFIX (optional, e.g. "Token " or "ApiKey ")
      query   : FACETS_<P>_AUTH_QUERY_NAME, FACETS_<P>_AUTH_QUERY_VALUE   -> ?name=value on every call
    FACETS_<P>_HEADERS           optional, ANY mode: JSON object of extra headers, e.g.
                                 {"X-Tenant": "acme", "X-Client-Id": "wrapper"}

  Connection:
    FACETS_<P>_VERIFY_TLS        true (default) | false
    FACETS_<P>_CA_BUNDLE         path to a CA bundle (overrides VERIFY_TLS)
    FACETS_<P>_TIMEOUT_S         default 15
    FACETS_<P>_RETRIES           default 2   (timeouts / connection errors / 5xx only)
    FACETS_<P>_MAX_CONCURRENCY   default 8   (parallel upstream calls per project)

  Protecting the WRAPPER's own API (inbound, unrelated to the above):
    FACETS_WRAPPER_API_KEY_<P>   if set, callers must send  X-API-Key

Global:  FACETS_WRAPPER_CONFIG_DIR, FACETS_WRAPPER_ADMIN_KEY, FACETS_WRAPPER_PLUGINS
Secrets are never logged and never appear in /readyz or error responses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


def bad_header(name: str, value: str) -> str | None:
    """Return a problem description (never containing the value) or None."""
    if not _HEADER_NAME.match(name or ""):
        return f"invalid header name {name!r}"
    if name.lower() in _FORBIDDEN_HEADERS:
        return f"header {name!r} may not be set"
    if any(c in value for c in "\r\n\x00"):
        return f"value of header {name!r} contains control characters"
    return None


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
    auth_header_name: str = "X-API-Key"
    auth_header_value: str | None = None
    auth_header_prefix: str = ""
    auth_query_name: str | None = None
    auth_query_value: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    config_errors: tuple[str, ...] = ()

    def problems(self) -> list[str]:
        """Why this project cannot call Facets yet. Never includes secret values."""
        out = list(self.config_errors)
        if not self.base_url:
            out.append("base URL not configured")
        if self.auth not in ("none", "basic", "bearer", "header", "query"):
            out.append(f"unknown auth type {self.auth!r} (use none, basic, bearer, header or query)")
        if self.auth == "basic" and not (self.username and self.password):
            out.append("basic auth needs username and password")
        if self.auth == "bearer" and not self.token:
            out.append("bearer auth needs a token")
        if self.auth == "header":
            if not self.auth_header_value:
                out.append("header auth needs FACETS_<P>_AUTH_HEADER_VALUE")
            elif (p := bad_header(self.auth_header_name, self.auth_header_prefix + self.auth_header_value)):
                out.append(p)
        if self.auth == "query" and not (self.auth_query_name and self.auth_query_value):
            out.append("query auth needs FACETS_<P>_AUTH_QUERY_NAME and FACETS_<P>_AUTH_QUERY_VALUE")
        if self.auth == "bearer" and self.token and (p := bad_header("Authorization", self.token)):
            out.append(p)
        reserved = {"authorization"} if self.auth in ("basic", "bearer") else set()
        if self.auth == "header":
            reserved.add(self.auth_header_name.lower())
        for n, v in self.extra_headers.items():
            if (p := bad_header(n, v)):
                out.append(p)
            elif n.lower() in reserved:
                out.append(f"extra header {n!r} conflicts with the {self.auth} auth header")
        return out


def load_project_env(project: str, environ: dict[str, str]) -> ProjectEnv:
    k = env_key(project)
    g = lambda suffix: environ.get(f"FACETS_{k}_{suffix}")  # noqa: E731
    errors: list[str] = []
    extra: dict[str, str] = {}
    if g("HEADERS"):
        try:
            parsed = json.loads(g("HEADERS"))  # type: ignore[arg-type]
            if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
                raise ValueError
            extra = parsed
        except ValueError:
            errors.append(f"FACETS_{k}_HEADERS must be a JSON object of string keys and string values")
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
        auth_header_name=g("AUTH_HEADER_NAME") or "X-API-Key",
        auth_header_value=g("AUTH_HEADER_VALUE"),
        auth_header_prefix=g("AUTH_HEADER_PREFIX") or "",
        auth_query_name=g("AUTH_QUERY_NAME"),
        auth_query_value=g("AUTH_QUERY_VALUE"),
        extra_headers=extra,
        config_errors=tuple(errors),
    )
