"""FastAPI application: loads every project config in a directory and serves the wrapper API."""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config.loader import ConfigError, load_config_dir, load_plugins
from ..config.validate import effective_pattern
from ..engine.planner import MissingInputsError, SelectionError
from ..engine.runtime import ProjectRuntime
from ..settings import load_project_env
from .schemas import EnrichRequest, EnrichResponse

log = logging.getLogger("api_wrapper.api")


def _build_registry(config_dir: str, environ: dict[str, str], transport) -> dict[str, ProjectRuntime]:
    cfgs = load_config_dir(config_dir)  # raises ConfigError -> service refuses to start / reload
    reg: dict[str, ProjectRuntime] = {}
    for name, cfg in cfgs.items():
        rt = ProjectRuntime(cfg, load_project_env(name, environ), transport)
        if not rt.ready:
            log.error("project %s is NOT READY: %s", name, "; ".join(rt.not_ready_reasons))
        if rt.env.api_key is None:
            log.warning("project %s has no WRAPPER_API_KEY_%s set: its API is unauthenticated", name, name.upper())
        reg[name] = rt
    return reg


def create_app(
    config_dir: str | None = None,
    *,
    environ: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    env = dict(os.environ if environ is None else environ)
    cdir = config_dir or env.get("WRAPPER_CONFIG_DIR", "configs")
    load_plugins(env.get("WRAPPER_PLUGINS"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.registry = _build_registry(cdir, env, transport)
        log.info("loaded projects: %s", ", ".join(app.state.registry) or "(none)")
        yield
        for rt in app.state.registry.values():
            await rt.aclose()

    app = FastAPI(
        title="API wrapper",
        version=__version__,
        description="Config-driven aggregation of upstream REST services. See docs/ARCHITECTURE.md.",
        lifespan=lifespan,
    )

    # ---- helpers -----------------------------------------------------------------------
    def resolve(request: Request, project: str | None, api_key: str | None) -> ProjectRuntime:
        reg: dict[str, ProjectRuntime] = request.app.state.registry
        if project is None:
            if len(reg) == 1:
                project = next(iter(reg))
            else:
                raise HTTPException(400, {"error": "project_required", "detail": "send the X-Project header", "projects": sorted(reg)})
        rt = reg.get(project)
        if rt is None:
            raise HTTPException(404, {"error": "unknown_project", "projects": sorted(reg)})
        if rt.env.api_key is not None and not (api_key and hmac.compare_digest(api_key, rt.env.api_key)):
            raise HTTPException(401, {"error": "invalid_api_key"})
        return rt

    # ---- routes ------------------------------------------------------------------------
    @app.get("/healthz", tags=["ops"])
    async def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request):
        reg = request.app.state.registry
        state = {n: {"ready": rt.ready, "problems": rt.not_ready_reasons} for n, rt in reg.items()}
        ok = bool(reg) and all(rt.ready for rt in reg.values())
        return JSONResponse({"ready": ok, "projects": state}, status_code=200 if ok else 503)

    @app.get("/v1/projects", tags=["catalog"])
    async def projects(request: Request):
        return {
            n: {"version": rt.cfg.version, "ready": rt.ready, "groups": rt.cfg.groups(), "fields": len(rt.cfg.fields), "sources": len(rt.cfg.sources)}
            for n, rt in request.app.state.registry.items()
        }

    @app.get("/v1/catalog", tags=["catalog"])
    async def catalog(
        request: Request,
        x_project: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        rt = resolve(request, x_project, x_api_key)
        cfg = rt.cfg
        return {
            "project": cfg.project,
            "config_version": cfg.version,
            "request": {
                "inputs": [
                    {"name": i.name, "required": i.required, "label": i.label, "pattern": effective_pattern(cfg, i)}
                    for i in cfg.request.inputs
                ],
                "at_least_one_of": cfg.request.at_least_one_of,
                "controls": ["groups", "fields", "strict", "include_provenance"],
            },
            "groups": {
                g: {
                    "cardinality": "array" if cfg.group_is_array(g) else "object",
                    "fields": [
                        {"name": f.name, "type": f.type, "required": f.required, "label": f.label, "source": f.source}
                        for f in cfg.fields if f.group == g
                    ],
                }
                for g in cfg.groups()
            },
            "sources": {n: {"path": s.path, "foreach": bool(s.foreach)} for n, s in cfg.sources.items()},
        }

    @app.post("/v1/claims/enrich", response_model=EnrichResponse, response_model_exclude_none=True, tags=["claims"])
    async def enrich(
        body: EnrichRequest,
        request: Request,
        x_project: str | None = Header(None),
        x_api_key: str | None = Header(None),
        x_correlation_id: str | None = Header(None),
    ):
        rt = resolve(request, x_project, x_api_key)
        if not rt.ready:
            raise HTTPException(503, {"error": "project_not_ready", "problems": rt.not_ready_reasons})
        inputs, problems = rt.check_inputs(body.model_extra or {})
        if problems:  # names only -- values are never echoed
            raise HTTPException(422, {"error": "invalid_inputs", **problems, "declared_inputs": rt.cfg.input_names()})
        try:
            out: dict[str, Any] = await rt.enrich(
                inputs, body.groups, body.fields, body.include_provenance, x_correlation_id
            )
        except SelectionError as e:
            raise HTTPException(422, {"error": "invalid_selection", "unknown_groups": e.unknown_groups, "unknown_fields": e.unknown_fields}) from None
        except MissingInputsError as e:
            raise HTTPException(422, {"error": "inputs_missing_for_fields", "fields": e.fields}) from None
        log.info(
            "enrich project=%s cid=%s complete=%s latency_ms=%s sources=%s",
            rt.cfg.project, out["meta"]["correlation_id"], out["complete"], out["meta"]["latency_ms"],
            ",".join(f"{s['name']}:{s['status']}" for s in out["meta"]["sources"]),
        )
        # strict -> an incomplete answer is an error, not a 200 someone might trust
        return JSONResponse(out, status_code=502 if (body.strict and not out["complete"]) else 200)

    @app.post("/admin/reload", tags=["ops"])
    async def reload(request: Request, x_admin_key: str | None = Header(None)):
        key = env.get("WRAPPER_ADMIN_KEY")
        if not key:
            raise HTTPException(404, "reload disabled (WRAPPER_ADMIN_KEY not set)")
        if not (x_admin_key and hmac.compare_digest(x_admin_key, key)):
            raise HTTPException(401, {"error": "invalid_admin_key"})
        try:
            new = _build_registry(cdir, env, transport)
        except ConfigError as e:  # keep serving the old, known-good configs
            raise HTTPException(422, {"error": "invalid_config", "problems": e.errors}) from None
        old = request.app.state.registry
        request.app.state.registry = new
        for rt in old.values():
            await rt.aclose()
        return {"reloaded": sorted(new)}

    return app
