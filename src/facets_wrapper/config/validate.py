"""Semantic validation of a ProjectConfig. Returns a list of human-readable problems.

The service refuses to start (and the converter reports "not loadable") if this list is
non-empty. Failing closed here is what prevents silently wrong data later.
"""

from __future__ import annotations

import re

from ..engine.extract import compile_path
from ..engine.transforms import TRANSFORMS
from .models import ProjectConfig

PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*)\}")
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
BUILTIN_PARAMS = {"claim_id"}


def placeholders(path: str) -> list[str]:
    return PLACEHOLDER.findall(path)


def source_dependencies(src) -> set[str]:
    deps = {b.from_source for b in src.params.values()}
    if src.foreach and src.foreach.discovery.mode == "from_source" and src.foreach.discovery.source:
        deps.add(src.foreach.discovery.source)
    return deps


def _check_path(expr: str, where: str, errors: list[str]) -> None:
    try:
        compile_path(expr)
    except Exception as e:  # jsonpath-ng raises assorted exception types
        errors.append(f"{where}: invalid JSONPath {expr!r} ({type(e).__name__})")


def validate_project(cfg: ProjectConfig) -> list[str]:
    errors: list[str] = []

    if not PROJECT_RE.match(cfg.project):
        errors.append(f"project name {cfg.project!r} must match {PROJECT_RE.pattern}")
    try:
        re.compile(cfg.claim_id_pattern)
    except re.error as e:
        errors.append(f"claim_id_pattern is not a valid regex: {e}")

    # ---- sources -------------------------------------------------------------------
    for name, src in cfg.sources.items():
        w = f"source {name!r}"
        if not IDENT.match(name):
            errors.append(f"{w}: name must match {IDENT.pattern}")
        if not src.path.startswith("/"):
            errors.append(f"{w}: path must start with '/' (base URL comes from the environment)")
        if "?" in src.path or "://" in src.path:
            errors.append(f"{w}: path must not contain a host or query string (use 'query:')")
        if src.response_root:
            _check_path(src.response_root, f"{w} response_root", errors)

        bound = set(BUILTIN_PARAMS) | set(src.params)
        if src.foreach:
            bound.add(src.foreach.param)
            if src.foreach.param in src.params or src.foreach.param in BUILTIN_PARAMS:
                errors.append(f"{w}: foreach param {src.foreach.param!r} is also bound elsewhere")
            d = src.foreach.discovery
            if d.mode == "fixed" and not d.values:
                errors.append(f"{w}: discovery mode 'fixed' needs 'values'")
            if d.mode == "from_source":
                if not d.source or not d.path:
                    errors.append(f"{w}: discovery mode 'from_source' needs 'source' and 'path'")
                else:
                    if d.source not in cfg.sources:
                        errors.append(f"{w}: discovery source {d.source!r} does not exist")
                    elif cfg.sources[d.source].foreach:
                        errors.append(f"{w}: discovery source {d.source!r} must not itself be foreach")
                    _check_path(d.path, f"{w} discovery path", errors)
        for ph in placeholders(src.path):
            if ph not in bound:
                errors.append(
                    f"{w}: URL placeholder {{{ph}}} is not bound -- add "
                    f"sources.{name}.params.{ph} {{from_source, path}} in the overrides file"
                )
        for pname, b in src.params.items():
            if pname not in placeholders(src.path):
                errors.append(f"{w}: param {pname!r} is not used in the path")
            if b.from_source not in cfg.sources:
                errors.append(f"{w}: param {pname!r} refers to unknown source {b.from_source!r}")
            elif b.from_source == name:
                errors.append(f"{w}: param {pname!r} cannot come from the source itself")
            elif cfg.sources[b.from_source].foreach:
                errors.append(f"{w}: param {pname!r} must not come from foreach source {b.from_source!r}")
            _check_path(b.path, f"{w} param {pname!r}", errors)

    # dependency cycles
    state: dict[str, int] = {}

    def visit(n: str, stack: list[str]) -> None:
        if state.get(n) == 2 or n not in cfg.sources:
            return
        if state.get(n) == 1:
            errors.append("dependency cycle: " + " -> ".join([*stack, n]))
            return
        state[n] = 1
        for d in sorted(source_dependencies(cfg.sources[n])):
            visit(d, [*stack, n])
        state[n] = 2

    for n in cfg.sources:
        visit(n, [])

    # ---- fields --------------------------------------------------------------------
    seen: set[tuple[str, str]] = set()
    group_foreach_params: dict[str, set[str]] = {}
    for f in cfg.fields:
        w = f"field {f.group}.{f.name}"
        if not IDENT.match(f.name):
            errors.append(f"{w}: field name must match {IDENT.pattern}")
        if not IDENT.match(f.group):
            errors.append(f"{w}: group name must match {IDENT.pattern}")
        if (f.group, f.name) in seen:
            errors.append(f"{w}: duplicate field")
        seen.add((f.group, f.name))
        if f.source not in cfg.sources:
            errors.append(f"{w}: unknown source {f.source!r}")
        else:
            fe = cfg.sources[f.source].foreach
            if fe:
                group_foreach_params.setdefault(f.group, set()).add(fe.param)
        if not f.paths:
            errors.append(f"{w}: needs at least one path")
        for p in f.paths:
            _check_path(p, w, errors)
        if f.transform:
            t = TRANSFORMS.get(f.transform)
            if t is None:
                errors.append(f"{w}: unknown transform {f.transform!r} (known: {', '.join(sorted(TRANSFORMS))})")
            elif not t.multi and len(f.paths) != 1:
                errors.append(f"{w}: transform {f.transform!r} takes one path but {len(f.paths)} given")
        elif len(f.paths) > 1:
            errors.append(f"{w}: several paths need a multi-value transform (e.g. join_space)")
        if f.date_formats and f.type not in ("date", "datetime"):
            errors.append(f"{w}: date_formats only valid for date/datetime fields")

    for g, params in group_foreach_params.items():
        if len(params) > 1:
            errors.append(f"group {g!r} mixes foreach sources with different params {sorted(params)}")
        row_key = next(iter(params))
        if any(f.group == g and f.name == row_key for f in cfg.fields):
            errors.append(f"group {g!r}: a field is named {row_key!r}, which is reserved for the row key")

    if not cfg.fields:
        errors.append("config has no fields")
    return errors
