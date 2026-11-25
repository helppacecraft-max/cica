"""ProjectRuntime: one loaded project config + its upstream client."""

from __future__ import annotations

import re
import time
import uuid

import httpx

from ..config.models import ProjectConfig
from ..config.validate import effective_pattern
from ..settings import ProjectEnv
from .assemble import assemble
from .auth import build_auth
from .client import UpstreamClient
from .executor import execute_plan
from .planner import build_plan


class ProjectRuntime:
    def __init__(self, cfg: ProjectConfig, env: ProjectEnv, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.env = env
        self.specs = {i.name: i for i in cfg.request.inputs}
        self.input_re = {n: re.compile(effective_pattern(cfg, s)) for n, s in self.specs.items()}
        self.client: UpstreamClient | None = None
        self.not_ready_reasons = env.problems() if transport is None else [
            p for p in env.problems() if p != "base URL not configured"
        ]
        if not self.not_ready_reasons:
            auth, headers, params = build_auth(env)
            self.client = UpstreamClient(
                env.base_url or "http://upstream.invalid",
                auth=auth,
                headers=headers,
                params=params,
                timeout_s=env.timeout_s,
                retries=env.retries,
                backoff_s=env.backoff_s,
                verify=env.verify,
                max_concurrency=env.max_concurrency,
                transport=transport,
            )

    @property
    def ready(self) -> bool:
        return self.client is not None

    async def aclose(self) -> None:
        if self.client:
            await self.client.aclose()

    def check_inputs(self, raw: dict) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Validate caller-supplied inputs. Returns (clean inputs, problems). Values are never echoed."""
        clean: dict[str, str] = {}
        problems: dict[str, list[str]] = {"unknown": [], "invalid": [], "missing": []}
        for k, v in raw.items():
            if k not in self.specs:
                problems["unknown"].append(k)
            elif v is None:
                continue  # null == not supplied
            elif isinstance(v, bool) or not isinstance(v, (str, int)):
                problems["invalid"].append(k)
            elif not self.input_re[k].fullmatch(str(v)):  # fullmatch: '$' must not let a trailing newline through
                problems["invalid"].append(k)
            else:
                clean[k] = str(v)
        for n, s in self.specs.items():
            if s.required and n not in clean and n not in problems["invalid"] and n not in problems["unknown"]:
                if not (self.cfg.request.at_least_one_of and n in self.cfg.request.at_least_one_of):
                    problems["missing"].append(n)
        any_of = self.cfg.request.at_least_one_of
        if any_of and not any(n in clean for n in any_of) and not any(n in problems["invalid"] for n in any_of):
            problems["missing"].append("at least one of: " + ", ".join(any_of))
        return clean, {k: sorted(v) if k != "missing" else v for k, v in problems.items() if v}

    async def enrich(
        self,
        inputs: dict[str, str],
        groups: list[str] | None = None,
        fields: list[str] | None = None,
        include_provenance: bool = False,
        correlation_id: str | None = None,
    ) -> dict:
        assert self.client is not None
        cid = correlation_id or uuid.uuid4().hex
        t0 = time.perf_counter()
        plan = build_plan(self.cfg, groups, fields, set(inputs))  # may raise SelectionError / MissingInputsError
        results = await execute_plan(self.cfg, plan, self.client, inputs, cid)
        out = assemble(self.cfg, plan, results, include_provenance)
        out["meta"] = {
            "project": self.cfg.project,
            "config_version": self.cfg.version,
            "correlation_id": cid,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "sources": out.pop("meta_sources"),
        }
        if plan.skipped:
            out["meta"]["skipped_fields"] = plan.skipped  # selected broadly but need inputs that were not supplied
        out["inputs"] = dict(inputs)
        return out
