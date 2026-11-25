"""Turn a request (which groups/fields) into the minimal ordered set of upstream calls."""

from __future__ import annotations

from dataclasses import dataclass

from ..config.models import FieldConfig, ProjectConfig
from ..config.validate import source_dependencies


class SelectionError(Exception):
    def __init__(self, unknown_groups: list[str], unknown_fields: list[str]):
        self.unknown_groups = unknown_groups
        self.unknown_fields = unknown_fields
        super().__init__("invalid selection")


@dataclass
class Plan:
    groups: list[str]
    fields: list[FieldConfig]
    levels: list[list[str]]  # sources grouped by dependency depth; each level runs in parallel

    @property
    def sources(self) -> list[str]:
        return [s for lvl in self.levels for s in lvl]


def select_fields(cfg: ProjectConfig, groups: list[str] | None, fields: list[str] | None) -> list[FieldConfig]:
    """Resolve the selection. No selection -> everything. Both -> union.

    ``fields`` entries are ``group.field`` (exact) or a bare ``field`` (matches in every group).
    """
    if not groups and not fields:
        return list(cfg.fields)
    known_groups = set(cfg.groups())
    unknown_groups = sorted(set(groups or []) - known_groups)
    chosen: set[tuple[str, str]] = set()
    unknown_fields: list[str] = []
    for g in groups or []:
        if g in known_groups:
            chosen |= {(f.group, f.name) for f in cfg.fields if f.group == g}
    for ref in fields or []:
        if "." in ref:
            g, n = ref.split(".", 1)
            hits = {(f.group, f.name) for f in cfg.fields if f.group == g and f.name == n}
        else:
            hits = {(f.group, f.name) for f in cfg.fields if f.name == ref}
        if not hits:
            unknown_fields.append(ref)
        chosen |= hits
    if unknown_groups or unknown_fields:
        raise SelectionError(unknown_groups, unknown_fields)
    return [f for f in cfg.fields if (f.group, f.name) in chosen]  # keep config order


def build_plan(cfg: ProjectConfig, groups: list[str] | None = None, fields: list[str] | None = None) -> Plan:
    selected = select_fields(cfg, groups, fields)

    needed: set[str] = set()
    stack = [f.source for f in selected]
    while stack:
        s = stack.pop()
        if s in needed:
            continue
        needed.add(s)
        stack.extend(source_dependencies(cfg.sources[s]))

    # Depth = longest dependency chain beneath a source (validated acyclic at load time).
    depth: dict[str, int] = {}

    def d(s: str) -> int:
        if s not in depth:
            deps = source_dependencies(cfg.sources[s]) & needed
            depth[s] = 0 if not deps else 1 + max(d(x) for x in deps)
        return depth[s]

    for s in needed:
        d(s)
    levels: list[list[str]] = []
    for s in sorted(needed, key=lambda x: (depth[x], list(cfg.sources).index(x))):
        while len(levels) <= depth[s]:
            levels.append([])
        levels[depth[s]].append(s)
    present_groups = []
    for f in selected:
        if f.group not in present_groups:
            present_groups.append(f.group)
    return Plan(groups=present_groups, fields=selected, levels=levels)
