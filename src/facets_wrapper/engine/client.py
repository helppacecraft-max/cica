"""Async HTTP client for the upstream Facets REST services (GET + JSON only)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("facets_wrapper.client")


@dataclass
class HttpResult:
    status: str  # "ok" | "not_found" | "failed"
    http_status: int | None = None
    data: Any = None
    attempts: int = 0
    latency_ms: int = 0
    error_code: str | None = None


class UpstreamClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth: httpx.Auth | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        timeout_s: float = 15.0,
        retries: int = 2,
        backoff_s: float = 0.2,
        verify: bool | str = True,
        max_concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            headers={"Accept": "application/json", **(headers or {})},
            params=params or None,
            timeout=timeout_s,
            verify=verify,
            transport=transport,
        )
        self._timeout_s = timeout_s
        self._retries = max(0, retries)
        self._backoff_s = backoff_s
        self._sem = asyncio.Semaphore(max(1, max_concurrency))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self,
        path: str,
        query: dict[str, str] | None = None,
        *,
        correlation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> HttpResult:
        """GET with bounded retries on timeouts / connection errors / 5xx. 404 is a normal outcome."""
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else {}
        t0 = time.perf_counter()
        attempts = 0
        last: HttpResult | None = None
        async with self._sem:
            while True:
                attempts += 1
                try:
                    resp = await self._client.get(
                        path, params=query or None, headers=headers, timeout=timeout_s or self._timeout_s
                    )
                except httpx.TimeoutException:
                    last = HttpResult("failed", None, error_code="timeout")
                except httpx.TransportError as e:
                    last = HttpResult("failed", None, error_code=f"connect_error:{type(e).__name__}")
                else:
                    sc = resp.status_code
                    if sc == 404:
                        return self._done(HttpResult("not_found", 404), attempts, t0)
                    if 200 <= sc < 300:
                        try:
                            data = resp.json()
                        except ValueError:
                            return self._done(HttpResult("failed", sc, error_code="invalid_json"), attempts, t0)
                        return self._done(HttpResult("ok", sc, data=data), attempts, t0)
                    last = HttpResult("failed", sc, error_code=f"http_{sc}")
                    if sc < 500:  # 4xx will not get better by retrying
                        return self._done(last, attempts, t0)
                if attempts > self._retries:
                    return self._done(last, attempts, t0)
                await asyncio.sleep(self._backoff_s * (2 ** (attempts - 1)))

    @staticmethod
    def _done(res: HttpResult, attempts: int, t0: float) -> HttpResult:
        res.attempts = attempts
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        return res
