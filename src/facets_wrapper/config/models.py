"""Pydantic models for a project mapping config (the YAML/JSON the converter writes).

These models validate *structure*. Cross-reference checks (does the source exist, is every
URL placeholder bound, is the dependency graph acyclic, ...) live in ``config/validate.py``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FieldType = Literal["string", "integer", "decimal", "boolean", "date", "datetime", "list", "any"]


class _Strict(BaseModel):
    # Typos in a config must fail loudly instead of being silently ignored.
    model_config = ConfigDict(extra="forbid")


class ParamBinding(_Strict):
    """Fill a URL placeholder from another source's response."""

    from_source: str
    path: str


class Discovery(_Strict):
    """How to find out which items (e.g. claim lines) a ``foreach`` source must be called for."""

    mode: Literal["probe", "fixed", "from_source"] = "probe"
    start: int = 1
    max: int = Field(50, ge=1, le=1000, description="Hard safety cap on number of items")
    values: list[int] = Field(default_factory=list)  # mode=fixed
    source: str | None = None  # mode=from_source
    path: str | None = None  # mode=from_source
    kind: Literal["values", "count"] = "values"  # mode=from_source


class Foreach(_Strict):
    param: str = "line_no"
    discovery: Discovery = Field(default_factory=Discovery)


class SourceConfig(_Strict):
    method: Literal["GET"] = "GET"
    path: str
    params: dict[str, ParamBinding] = Field(default_factory=dict)
    query: dict[str, str] = Field(default_factory=dict)
    foreach: Foreach | None = None
    response_root: str | None = None
    timeout_s: float | None = None
    notes: str | None = None
    origin: dict[str, Any] = Field(default_factory=dict)  # informational only


class FieldConfig(_Strict):
    name: str
    group: str
    source: str
    paths: list[str]
    type: FieldType = "string"
    required: bool = False
    transform: str | None = None
    date_formats: list[str] = Field(default_factory=list)
    label: str | None = None
    facets: dict[str, str] | None = None
    notes: str | None = None
    origin: dict[str, Any] = Field(default_factory=dict)  # informational only

    @model_validator(mode="before")
    @classmethod
    def _path_shorthand(cls, data: Any) -> Any:
        # Allow ``path: "$.X"`` as shorthand for ``paths: ["$.X"]``.
        if isinstance(data, dict) and "path" in data:
            data = dict(data)
            if "paths" in data:
                raise ValueError("use either 'path' or 'paths', not both")
            p = data.pop("path")
            data["paths"] = [p] if isinstance(p, str) else p
        return data


class InputSpec(_Strict):
    """One identifier the caller may send in the request body (flat key), e.g. claim_id, associate_id."""

    name: str
    pattern: str | None = None  # regex the value must match; claim_id falls back to claim_id_pattern
    required: bool = True
    label: str | None = None


class RequestConfig(_Strict):
    inputs: list[InputSpec] = Field(default_factory=lambda: [InputSpec(name="claim_id")])
    at_least_one_of: list[str] = Field(
        default_factory=list, description="at least one of these inputs must be supplied (for 'either ID' lookups)"
    )


class ProjectConfig(_Strict):
    schema_version: int = 1
    project: str
    version: int | str = 1
    claim_id_pattern: str = r"^[A-Za-z0-9]{6,20}$"  # default pattern for the input named claim_id
    request: RequestConfig = Field(default_factory=RequestConfig)
    null_values: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    sources: dict[str, SourceConfig]
    fields: list[FieldConfig]

    # -- helpers -----------------------------------------------------------------
    def input_names(self) -> list[str]:
        return [i.name for i in self.request.inputs]

    def groups(self) -> list[str]:
        seen: list[str] = []
        for f in self.fields:
            if f.group not in seen:
                seen.append(f.group)
        return seen

    def group_is_array(self, group: str) -> bool:
        return any(self.sources[f.source].foreach for f in self.fields if f.group == group)
