"""Pure helpers: slugs, API-field parsing, type mapping and URL -> source-template normalisation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def slugify(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", s)  # drop parentheticals, e.g. 'Revenue Code (x has "Rev")'
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if s and s[0].isdigit():
        s = "f_" + s
    return s


def group_name(title: str, aliases: dict[str, str]) -> str:
    key = re.sub(r"\s+", " ", title).strip().lower()
    return aliases.get(key) or slugify(key) or "group"


@dataclass
class ApiField:
    tokens: list[str] = field(default_factory=list)
    joiner: str | None = None  # "&" -> join_space, "," -> list_non_null
    error: str | None = None


def parse_api_field(text: str, na_values: set[str]) -> ApiField:
    t = text.strip()
    if t.lower() in na_values:
        return ApiField(error="not_applicable")
    has_amp, has_comma = "&" in t, "," in t
    if has_amp and has_comma:
        return ApiField(error="mixed_separators")
    sep = "&" if has_amp else ("," if has_comma else None)
    tokens = [x.strip() for x in (t.split(sep) if sep else [t])]
    if not all(_TOKEN_RE.match(x) for x in tokens):
        return ApiField(error="invalid_name")
    return ApiField(tokens=tokens, joiner=sep)


def path_for(template: str, token: str) -> str:
    camel = re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), token.lower())
    return template.format(api_field=token, api_field_lower=token.lower(), api_field_camel=camel)


def map_type(sheet_type: str, type_map: dict[str, str]) -> tuple[str, bool]:
    """-> (config type, recognised?)"""
    key = sheet_type.strip().lower()
    if not key:
        return "string", True
    if key in type_map:
        return type_map[key], True
    return "string", False


@dataclass
class UrlInfo:
    kind: str  # "rest" | "non_rest"
    reason: str | None = None
    endpoint: str = ""  # scheme://host:port (kept only for diagnostics, never written to config)
    template: str = ""  # normalised path with {placeholders}
    double_slash: bool = False
    bracket_placeholder: str | None = None  # e.g. "[clcl_id]" seen where a literal ID was expected
    literal_params: dict[str, str] = field(default_factory=dict)  # discarded, used for diagnostics
    port: int | None = None


def parse_url(url: str, rest_path_regex: str, rules: list[dict]) -> UrlInfo:
    parts = urlsplit(url)
    endpoint = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or ""
    double = "//" in path
    segs = [s for s in path.split("/") if s]
    info = UrlInfo(kind="non_rest", endpoint=endpoint, double_slash=double, port=parts.port)
    if parts.query:
        info.reason = "has a query string (looks like a UI/search URL, not a REST service)"
        return info
    if not segs:
        info.reason = "no path (bare host)"
        return info
    norm = "/" + "/".join(segs) + "/"
    if not re.search(rest_path_regex, norm, re.I):
        info.reason = f"path does not match REST pattern {rest_path_regex!r}"
        return info

    out: list[str] = []
    i = 0
    while i < len(segs):
        out.append(segs[i])
        rule = next((r for r in rules if segs[i].lower() == r["after"].lower()), None)
        if rule and i + 1 < len(segs):
            lit = segs[i + 1]
            if lit.startswith("[") or lit.startswith("{"):
                info.bracket_placeholder = lit
            else:
                info.literal_params[rule["param"]] = lit
            out.append("{" + rule["param"] + "}")
            i += 2
            continue
        i += 1
    info.kind = "rest"
    info.template = "/" + "/".join(out)
    return info


def source_name(template: str) -> str:
    segs = [s for s in template.split("/") if s]
    if "api" in [s.lower() for s in segs]:
        segs = segs[[s.lower() for s in segs].index("api") + 1 :]
    segs = [s for s in segs if not re.match(r"^v\d+$", s, re.I) and not s.startswith("{")]
    return slugify("_".join(segs)) or "source"
