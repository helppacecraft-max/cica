from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnrichRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(..., description="Facets claim ID (validated against the project's claim_id_pattern)")
    groups: list[str] | None = Field(None, description="Return only these groups (e.g. ['claim','line'])")
    fields: list[str] | None = Field(None, description="Return only these fields: 'group.field' or bare 'field'")
    strict: bool = Field(False, description="If true, an incomplete result is returned with HTTP 502 instead of 200")
    include_provenance: bool = Field(False, description="Include source/path/type per field")


class EnrichResponse(BaseModel):
    claim_id: str
    complete: bool = Field(..., description="False if any source failed or any REQUIRED field is not 'ok'")
    data: dict[str, Any] = Field(..., description="group -> object, or list of row objects for per-line groups")
    field_status: dict[str, Any] = Field(..., description="same shape as data, values are status strings")
    required_failures: list[str]
    errors: list[dict[str, Any]]
    meta: dict[str, Any]
    provenance: dict[str, Any] | None = None
