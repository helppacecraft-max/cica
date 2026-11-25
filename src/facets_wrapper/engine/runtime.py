"""ProjectRuntime: one loaded project config + its upstream client."""

from __future__ import annotations

import re
import time
import uuid

import httpx

from ..config.models import ProjectConfig
from ..settings import ProjectEnv
from .assemble import assemble
from .client import UpstreamClient
from .executor import execute_plan
from .planner import build_plan


class ProjectRuntime:
    def __init__(self, cfg: ProjectConfig, env: ProjectEnv, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.env = env
        self.claim_re = re.compile(cfg.claim_id_pattern)
        self.client: UpstreamClient | None = None
        self.not_ready_reasons = env.problems() if transport is None else [
            p for p in env.problems() if p != "base URL not configured"
        ]
        if not self.not_ready_reasons:
            auth = httpx.BasicAuth(env.username, env.password) if env.auth == "basic" else None
            headers = {"Authorization": f"Bearer {env.token}"} if env.auth == "bearer" else None
            self.client = UpstreamClient(
                env.base_url or "http://upstream.invalid",
                auth=auth,
                headers=headers,
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

    async def enrich(
        self,
        claim_id: str,
        groups: list[str] | None = None,
        fields: list[str] | None = None,
        include_provenance: bool = False,
        correlation_id: str | None = None,
    ) -> dict:
        assert self.client is not None
        cid = correlation_id or uuid.uuid4().hex
        t0 = time.perf_counter()
        plan = build_plan(self.cfg, groups, fields)  # may raise SelectionError
        results = await execute_plan(self.cfg, plan, self.client, claim_id, cid)
        out = assemble(self.cfg, plan, results, include_provenance)
        out["meta"] = {
            "project": self.cfg.project,
            "config_version": self.cfg.version,
            "correlation_id": cid,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "sources": out.pop("meta_sources"),
        }
        out["claim_id"] = claim_id
        return out
