"""Strict requests for cataloged official documentation sources."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .source_catalog import source_for


class OfficialPreflightRequest(BaseModel):
    """Request one exact source/version from the reviewed official catalog."""

    model_config = ConfigDict(strict=True, extra="forbid")

    source_id: str = Field(min_length=1, max_length=128)
    selection: Literal["requested"]
    requested_version: str = Field(min_length=1, max_length=128)
    query: str | None = Field(default=None, max_length=512)
    section_ids: list[str] = Field(default_factory=list, max_length=32)
    context_max_bytes: int = Field(default=12_000, ge=1, le=32_000)
    freshness_mode: Literal["require_check", "allow_cache", "allow_stale"]
    deadline_ms: int = Field(default=30_000, ge=1_000, le=45_000)

    @model_validator(mode="after")
    def validate_catalog_source(self):
        # source_for is the sole authorization boundary for official docs.
        source_for(self.source_id, self.requested_version)
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("duplicate_section_id")
        for section_id in self.section_ids:
            if not 1 <= len(section_id) <= 128:
                raise ValueError("invalid_section_id")
        return self

    @property
    def exact_version(self) -> str:
        return self.requested_version
