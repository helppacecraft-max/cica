"""Excel mapping sheet  ->  project config (YAML/JSON)  +  conversion report.

Principles
* Never guess: a row that cannot be mapped with certainty is *skipped and reported*.
* Never copy environment data: hosts, ports and literal claim IDs from the sheet are discarded.
* Deterministic: same workbook + same overrides => byte-identical output (no timestamps).
* Hand decisions (subscriber-key binding, line discovery, renames, type fixes) live in an
  ``overrides.yml`` so re-running the conversion never loses them.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import __version__
from ..config.models import ProjectConfig
from ..config.validate import placeholders, validate_project
from .defaults import DEFAULT_SETTINGS
from .excel_reader import ConversionError, RawRow, read_sections
from .normalize import (
    group_name,
    map_type,
    parse_api_field,
    parse_url,
    path_for,
    slugify,
    source_name,
)

ALLOWED_OVERRIDE_KEYS = {"settings", "project", "source_aliases", "sources", "fields", "add_fields"}
ALLOWED_PROJECT_KEYS = {"version", "claim_id_pattern", "null_values"}
SEVERITIES = ("action", "error", "warn", "info")


@dataclass
class Issue:
    code: str
    severity: str  # action | error | warn | info
    message: str
    row: int | None = None
    section: str | None = None


@dataclass
class ConversionResult:
    project: str
    config: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    loadable: bool = False
    load_errors: list[str] = field(default_factory=list)

    @property
    def needs_action(self) -> bool:
        return any(i.severity in ("action", "error") for i in self.issues) or not self.loadable


# ------------------------------------------------------------------------------------------
def load_overrides(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise ConversionError(f"overrides file not found: {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ConversionError("overrides file must be a mapping")
    unknown = set(doc) - ALLOWED_OVERRIDE_KEYS
    if unknown:
        raise ConversionError(f"unknown top-level overrides key(s): {sorted(unknown)} (allowed: {sorted(ALLOWED_OVERRIDE_KEYS)})")
    bad = set(doc.get("settings", {})) - set(DEFAULT_SETTINGS)
    if bad:
        raise ConversionError(f"unknown settings key(s): {sorted(bad)} (allowed: {sorted(DEFAULT_SETTINGS)})")
    badp = set(doc.get("project", {})) - ALLOWED_PROJECT_KEYS
    if badp:
        raise ConversionError(f"unknown project key(s): {sorted(badp)} (allowed: {sorted(ALLOWED_PROJECT_KEYS)})")
    return doc


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def _sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------
def convert(excel_path: str | Path, project: str, overrides_path: str | Path | None = None, sheet: str | None = None) -> ConversionResult:
    ov = load_overrides(overrides_path)
    S = {**DEFAULT_SETTINGS, **ov.get("settings", {})}
    na_values = {v.lower() for v in S["not_applicable_values"]}
    required_values = {v.lower() for v in S["required_values"]}
    id_patterns = [re.compile(p, re.I) for p in S["string_api_field_patterns"]]
    id_names = {n.lower() for n in S["string_business_names"]}

    sheet_name, sections = read_sections(excel_path, sheet or S["sheet"])
    issues: list[Issue] = []
    skipped: list[dict[str, Any]] = []

    srcs: dict[str, dict[str, Any]] = {}  # lower-case template -> entry
    fields: list[dict[str, Any]] = []
    spellings: dict[str, set[str]] = {}
    endpoints: set[str] = set()
    double_slash_rows: list[int] = []
    literal_ids: set[str] = set()
    rows_read = 0
    nonrest_seq: list[tuple[int, str]] = []

    def skip(row: RawRow, sec: str, fname: str, code: str, reason: str, sev: str = "warn") -> None:
        mandatory = row.requirement.lower() in required_values
        skipped.append({"row": row.row, "section": sec, "field": fname or row.rest_field or row.web_field,
                        "code": code, "reason": reason, "mandatory": mandatory})
        issues.append(Issue(code, "warn" if (mandatory and sev == "info") else sev,
                            f"{fname or row.rest_field or row.web_field!r} skipped: {reason}"
                            + (" [MANDATORY field is unmapped]" if mandatory else ""), row.row, sec))

    for sec in sections:
        grp = group_name(sec.title, S["group_aliases"])
        used_names: dict[str, int] = {}
        for row in sec.rows:
            rows_read += 1
            label = row.rest_field or row.web_field
            fname = slugify(label)
            if not fname:
                skip(row, sec.title, "", "NO_FIELD_NAME", "no usable field name")
                continue

            api = parse_api_field(row.api_field, na_values)
            if api.error == "not_applicable":
                skip(row, sec.title, fname, "NO_API_FIELD", "API field is empty / NA", "info")
                continue
            if api.error:
                skip(row, sec.title, fname, "INVALID_API_FIELD", f"API field {row.api_field!r} not understood ({api.error})")
                continue

            if not row.urls:
                skip(row, sec.title, fname, "NO_URL", f"no URL (cell says {row.url_text!r})" if row.url_text else "no URL", "info")
                continue

            infos = [parse_url(u, S["rest_path_regex"], S["path_param_rules"]) for u in row.urls]
            rest = [i for i in infos if i.kind == "rest"]
            for u, i in zip(row.urls, infos):
                if i.kind == "non_rest":
                    nonrest_seq.append((row.row, u))
            if not rest:
                skip(row, sec.title, fname, "NON_REST_URL", f"URL is not a REST endpoint: {infos[0].reason}")
                continue
            for i in rest:
                endpoints.add(i.endpoint)
                if i.double_slash:
                    double_slash_rows.append(row.row)
                if "claim_id" in i.literal_params:
                    literal_ids.add(i.literal_params["claim_id"])
                if i.bracket_placeholder:
                    issues.append(Issue("BRACKET_PLACEHOLDER", "warn",
                        f"URL uses placeholder {i.bracket_placeholder} where the sheet elsewhere says the key is the "
                        "subscriber key; mapped to {subscriber_key} -- confirm which ID the endpoint really takes",
                        row.row, sec.title))
            primary = rest[-1]
            prereqs = [i.template for i in rest[:-1] if i.template != primary.template]
            if len(rest) > 1:
                issues.append(Issue("MULTI_URL_CELL", "info",
                    f"cell lists {len(rest)} REST URLs; the LAST is used as the source of this field, the earlier "
                    "ones are recorded as prerequisites (bind them in overrides if a placeholder needs their data)",
                    row.row, sec.title))

            key = primary.template.lower()
            spellings.setdefault(key, set()).add(primary.template)
            entry = srcs.setdefault(key, {"template": primary.template, "rows": [], "prereq": []})
            entry["rows"].append(row.row)
            for pt in prereqs:
                if pt.lower() not in entry["prereq"]:
                    entry["prereq"].append(pt.lower())

            # type / required ---------------------------------------------------------------
            ftype, known = map_type(row.data_type, S["type_map"])
            if not known:
                issues.append(Issue("TYPE_UNKNOWN", "warn", f"data type {row.data_type!r} unknown; using string", row.row, sec.title))
            if ftype in ("decimal", "integer") and (
                any(p.search(t) for p in id_patterns for t in api.tokens) or label.lower() in id_names
            ):
                issues.append(Issue("TYPE_NUMERIC_ID_LIKE", "info",
                    f"{fname!r} is Numeric in the sheet but looks like an identifier; using string (keeps leading zeros)",
                    row.row, sec.title))
                ftype = "string"
            if not row.data_type:
                issues.append(Issue("MISSING_TYPE_INFO", "info", f"{fname!r}: no Data Type in sheet; using string, optional", row.row, sec.title))

            fd: dict[str, Any] = {"name": fname, "group": grp, "_src": key,
                                  "paths": [path_for(S["path_template"], t) for t in api.tokens]}
            if len(api.tokens) > 1:
                fd["transform"] = "join_space" if api.joiner == "&" else "list_non_null"
                ftype = "string" if api.joiner == "&" else "list"
                issues.append(Issue("MULTI_API_FIELD", "info",
                    f"{fname!r} combines {len(api.tokens)} API fields with {api.joiner!r}; transform={fd['transform']}, type={ftype}",
                    row.row, sec.title))
            fd["type"] = ftype
            fd["required"] = row.requirement.lower() in required_values
            if label:
                fd["label"] = label
            facets = {k: v for k, v in (("table", row.facets_table), ("column", row.facets_column)) if v and v.lower() not in na_values}
            if facets:
                fd["facets"] = facets
            leftover = re.sub(r"\s+", " ", re.sub(r"https?://\S+", " ", row.url_text)).strip(" -:")
            note = " | ".join(x for x in (row.comment, leftover if len(leftover) > 3 else "") if x)
            if note:
                fd["notes"] = note
            fd["origin"] = {"sheet_row": row.row, "section": sec.title}

            n = used_names.get(fname, 0) + 1
            used_names[fname] = n
            if n > 1:
                issues.append(Issue("DUPLICATE_FIELD_NAME", "warn", f"field name {fname!r} repeats in group {grp!r}; renamed {fname}_{n}", row.row, sec.title))
                fd["name"] = f"{fname}_{n}"
            fields.append(fd)

    # ---- cross-row findings ---------------------------------------------------------------
    for run in _fill_series(nonrest_seq):
        issues.append(Issue("FILL_SERIES_URLS", "warn",
            f"URLs on sheet rows {run[0]}-{run[-1]} differ only by an incrementing number -- looks like an Excel "
            "fill-down accident, not real endpoints", run[0]))
    if double_slash_rows:
        issues.append(Issue("DOUBLE_SLASH", "info",
            f"{len(double_slash_rows)} URL(s) contain '//' inside the path; collapsed to '/'"))
    if literal_ids:
        issues.append(Issue("LITERAL_IDS_REPLACED", "info",
            f"URLs contained {len(literal_ids)} distinct hard-coded claim ID(s); replaced with {{claim_id}} and NOT stored"))
    if len(endpoints) > 1:
        issues.append(Issue("MULTIPLE_ENDPOINTS", "warn",
            f"REST URLs point at {len(endpoints)} different host:port combinations ({', '.join(sorted(endpoints))}); "
            "the base URL is taken from the environment, so only one is used"))
    for key, sp in spellings.items():
        if len(sp) > 1:
            issues.append(Issue("CASE_VARIANT_PATHS", "info", f"path written in several spellings {sorted(sp)}; first one kept"))

    # ---- source names -------------------------------------------------------------------------
    names: dict[str, str] = {}
    taken: set[str] = set()
    for key, e in srcs.items():
        base = source_name(e["template"])
        n, i = base, 2
        while n in taken:
            n, i = f"{base}_{i}", i + 1
        taken.add(n)
        names[key] = n

    sources_cfg: dict[str, Any] = {}
    for key, e in srcs.items():
        sc: dict[str, Any] = {"path": e["template"], "origin": {"sheet_rows": ",".join(map(str, e["rows"][:12])) + ("..." if len(e["rows"]) > 12 else ""), "row_count": len(e["rows"])}}
        if e["prereq"]:
            sc["notes"] = "the sheet says to call these endpoints first: " + ", ".join(srcs[p]["template"] for p in e["prereq"] if p in srcs)
        if "{line_no}" in e["template"]:
            sc["foreach"] = {"param": "line_no", "discovery": {"mode": "probe", "start": 1, "max": 50}}
        sources_cfg[names[key]] = sc
    fields_cfg = []
    for f in fields:
        f = dict(f)
        f["source"] = names[f.pop("_src")]
        fields_cfg.append(f)

    config: dict[str, Any] = {
        "schema_version": 1,
        "project": project,
        "version": 1,
        "claim_id_pattern": r"^[A-Za-z0-9]{6,20}$",
        "meta": {
            "generated_from": Path(excel_path).name,
            "sheet": sheet_name,
            "source_sha256": _sha256(excel_path),
            "tool_version": __version__,
            **({"overrides_sha256": _sha256(overrides_path)} if overrides_path else {}),
        },
        "sources": sources_cfg,
        "fields": fields_cfg,
    }

    _apply_overrides(config, ov, issues)

    # ---- post-override findings -------------------------------------------------------------
    sources_cfg = config["sources"]
    ovs = ov.get("sources", {})
    for name, sc in sources_cfg.items():
        fe = sc.get("foreach")
        if fe and "{line_no}" in sc["path"] and "foreach" not in ovs.get(name, {}):
            issues.append(Issue("DISCOVERY_DEFAULT", "warn",
                f"source {name!r}: line numbers are discovered by probing 1,2,3... until the first 404 (max 50). "
                "If the claim response exposes the lines, set sources." + name + ".foreach.discovery in overrides."))
        bound = {"claim_id"} | set(sc.get("params", {})) | ({fe["param"]} if fe else set())
        for ph in placeholders(sc["path"]):
            if ph not in bound:
                prereq = next((names[p] for p in srcs.get(sc["path"].lower(), {}).get("prereq", []) if p in names), None)
                src_guess = prereq or "<source_that_returns_it>"
                issues.append(Issue("UNBOUND_PARAM", "action",
                    f"source {name!r} needs {{{ph}}} from another response. Add to overrides:\n"
                    f"    sources:\n      {name}:\n        params:\n          {ph}: {{from_source: {src_guess}, path: \"$.<JSONPath>\"}}"))

    by_group: dict[str, list[dict]] = {}
    for f in config["fields"]:
        by_group.setdefault(f["group"], []).append(f)
    seen_map: dict[tuple, str] = {}
    for f in config["fields"]:
        k = (f["group"], f["source"], tuple(f.get("paths", [])))
        if k in seen_map:
            issues.append(Issue("DUPLICATE_MAPPING", "warn",
                f"{f['group']}.{f['name']} maps to the same API field as {seen_map[k]} -- probably a copy/paste error in the sheet",
                f.get("origin", {}).get("sheet_row"), f.get("origin", {}).get("section")))
        else:
            seen_map[k] = f"{f['group']}.{f['name']}"
    for g, fl in by_group.items():
        fe = [bool(sources_cfg[f["source"]].get("foreach")) for f in fl]
        if any(fe) and not all(fe):
            issues.append(Issue("MIXED_CARDINALITY_GROUP", "warn",
                f"group {g!r} mixes per-line and claim-level fields; claim-level values are repeated on every row"))

    # ---- validate the result exactly as the service will ------------------------------------
    load_errors: list[str] = []
    try:
        cfg = ProjectConfig.model_validate(_for_model(config))
        load_errors = validate_project(cfg)
    except Exception as e:  # pydantic ValidationError
        load_errors = [str(e)]
    for m in load_errors:
        issues.append(Issue("CONFIG_INVALID", "error", m))

    issues.sort(key=lambda i: (SEVERITIES.index(i.severity), i.row or 0))
    res = ConversionResult(project, config, issues, skipped, loadable=not load_errors, load_errors=load_errors)
    res.stats = {
        "sheet": sheet_name,
        "sections": len(sections),
        "rows_read": rows_read,
        "fields_mapped": len(config["fields"]),
        "rows_skipped": len(skipped),
        "mandatory_rows_skipped": sum(1 for s in skipped if s["mandatory"]),
        "sources": len(config["sources"]),
        "groups": len(by_group),
    }
    return res


# ------------------------------------------------------------------------------------------
def _fill_series(entries: list[tuple[int, str]]) -> list[list[int]]:
    runs: list[list[int]] = []
    cur: list[int] = []
    prev: tuple[str, int] | None = None
    for row, url in entries:
        m = re.search(r"(\d+)(?!.*\d)", url)
        if not m:
            prev, cur = None, []
            continue
        stem, num = url[: m.start()] + "#" + url[m.end():], int(m.group(1))
        if prev and prev[0] == stem and num == prev[1] + 1:
            cur = cur or [prev_row]
            cur.append(row)
        else:
            if len(cur) >= 3:
                runs.append(cur)
            cur = []
        prev, prev_row = (stem, num), row
    if len(cur) >= 3:
        runs.append(cur)
    return runs


def _apply_overrides(config: dict, ov: dict, issues: list[Issue]) -> None:
    errors: list[str] = []
    for k, v in ov.get("project", {}).items():
        config[k] = v

    for old, new in (ov.get("source_aliases") or {}).items():
        if old not in config["sources"]:
            errors.append(f"source_aliases: unknown generated source {old!r} (known: {sorted(config['sources'])})")
            continue
        if new in config["sources"]:
            errors.append(f"source_aliases: target name {new!r} already exists")
            continue
        config["sources"] = {(new if n == old else n): s for n, s in config["sources"].items()}
        for f in config["fields"]:
            if f["source"] == old:
                f["source"] = new
        # rewrite references held by other overrides / bindings
        for s in config["sources"].values():
            for b in s.get("params", {}).values():
                if b.get("from_source") == old:
                    b["from_source"] = new

    for name, patch in (ov.get("sources") or {}).items():
        if name not in config["sources"]:
            errors.append(f"sources: unknown source {name!r} (known: {sorted(config['sources'])})")
            continue
        _deep_merge(config["sources"][name], patch or {})

    for ref, patch in (ov.get("fields") or {}).items():
        g, _, n = ref.partition(".")
        target = next((f for f in config["fields"] if f["group"] == g and f["name"] == n), None)
        if not target:
            errors.append(f"fields: unknown field {ref!r} (use <group>.<field>)")
            continue
        patch = dict(patch or {})
        if patch.pop("skip", False):
            config["fields"].remove(target)
            continue
        if "path" in patch:
            patch["paths"] = [patch.pop("path")]
        target.update(patch)

    for nf in ov.get("add_fields") or []:
        nf = dict(nf)
        if "path" in nf:
            nf["paths"] = [nf.pop("path")]
        config["fields"].append(nf)

    if errors:
        raise ConversionError("overrides could not be applied:\n  - " + "\n  - ".join(errors))


def _for_model(config: dict) -> dict:
    return copy.deepcopy(config)


def _compact(config: dict) -> dict:
    """Readable on-disk form: single-element ``paths`` -> ``path``; stable key order."""
    out = copy.deepcopy(config)
    order = ["name", "group", "source", "path", "paths", "type", "required", "transform", "date_formats", "label", "facets", "notes", "origin"]
    new_fields = []
    for f in out["fields"]:
        if len(f.get("paths", [])) == 1:
            f["path"] = f.pop("paths")[0]
        new_fields.append({k: f[k] for k in order if k in f} | {k: v for k, v in f.items() if k not in order})
    out["fields"] = new_fields
    return out


# ------------------------------------------------------------------------------------------
def write_outputs(res: ConversionResult, out_dir: str | Path) -> dict[str, Path]:
    d = Path(out_dir) / res.project
    d.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {"config": d / "mapping.yml", "report_yml": d / "report.yml", "report_md": d / "report.md"}
    header = (
        "# GENERATED by `facets-wrapper convert` -- do not hand-edit.\n"
        "# Put manual decisions in overrides.yml and re-run the conversion.\n"
    )
    paths["config"].write_text(
        header + yaml.safe_dump(_compact(res.config), sort_keys=False, allow_unicode=True, width=120), encoding="utf-8"
    )
    paths["report_yml"].write_text(
        yaml.safe_dump(
            {"project": res.project, "loadable": res.loadable, "stats": res.stats,
             "issues": [asdict(i) for i in res.issues], "skipped": res.skipped},
            sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    paths["report_md"].write_text(render_report(res), encoding="utf-8")
    return paths


def render_report(res: ConversionResult) -> str:
    L: list[str] = [f"# Conversion report: {res.project}", ""]
    s = res.stats
    L += [
        f"- Sheet: `{s['sheet']}`  |  sections: {s['sections']}  |  rows read: {s['rows_read']}",
        f"- Fields mapped: **{s['fields_mapped']}**  |  rows skipped: **{s['rows_skipped']}** "
        f"(mandatory & unmapped: **{s['mandatory_rows_skipped']}**)",
        f"- Sources (distinct REST endpoints): **{s['sources']}**  |  groups: {s['groups']}",
        f"- Config loadable by the service: **{'YES' if res.loadable else 'NO -- fix the items under ACTION REQUIRED'}**",
        "",
        "> Field paths are generated from the sheet's *API field names* using `settings.path_template`. "
        "They are **unverified** until checked against real responses: run `facets-wrapper validate --samples <dir>`.",
        "",
    ]
    for sev, title in (("error", "ERRORS"), ("action", "ACTION REQUIRED"), ("warn", "WARNINGS"), ("info", "INFO")):
        items = [i for i in res.issues if i.severity == sev]
        if not items:
            continue
        L += [f"## {title} ({len(items)})", ""]
        for i in items:
            where = f" (row {i.row}{', ' + i.section if i.section else ''})" if i.row else ""
            L.append(f"- **{i.code}**{where}: {i.message}")
        L.append("")
    if res.skipped:
        L += ["## Skipped rows", "", "| Row | Section | Field | Code | Mandatory | Reason |", "|---|---|---|---|---|---|"]
        for k in res.skipped:
            L.append(f"| {k['row']} | {k['section']} | {k['field']} | {k['code']} | {'YES' if k['mandatory'] else ''} | {k['reason']} |")
        L.append("")
    L += ["## Sources", "", "| Source | Path | Fields | Foreach |", "|---|---|---|---|"]
    for n, sc in res.config["sources"].items():
        cnt = sum(1 for f in res.config["fields"] if f["source"] == n)
        L.append(f"| `{n}` | `{sc['path']}` | {cnt} | {'yes' if sc.get('foreach') else ''} |")
    L.append("")
    return "\n".join(L)
