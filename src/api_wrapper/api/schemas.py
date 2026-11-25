from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnrichRequest(BaseModel):
    """Control keys are fixed. Every OTHER top-level key is a request *input* (e.g. claim_id, associate_id):
    which ones exist, their patterns and whether they are required are defined per project in the
    config (``request.inputs``) -- see GET /v1/catalog. Unknown keys are rejected with 422."""

    model_config = ConfigDict(extra="allow")

    groups: list[str] | None = Field(None, description="Return only these groups (e.g. ['claim','line'])")
    fields: list[str] | None = Field(None, description="Return only these fields: 'group.field' or bare 'field'")
    strict: bool = Field(False, description="If true, an incomplete result is returned with HTTP 502 instead of 200")
    include_provenance: bool = Field(False, description="Include source/path/type per field")


class EnrichResponse(BaseModel):
    inputs: dict[str, str] = Field(..., description="the validated inputs that were used")
    complete: bool = Field(..., description="False if any source failed or any REQUIRED field is not 'ok'")
    data: dict[str, Any] = Field(..., description="group -> object, or list of row objects for per-line groups")
    field_status: dict[str, Any] = Field(..., description="same shape as data, values are status strings")
    required_failures: list[str]
    errors: list[dict[str, Any]]
    meta: dict[str, Any]
    provenance: dict[str, Any] | None = None
