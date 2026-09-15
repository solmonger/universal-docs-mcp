"""SDK-independent result contracts; schemas describe actual domain data."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Count = Annotated[int, Field(ge=0)]
Ecosystem = Literal["python", "javascript", "rust"]


class ResultModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SectionInfo(ResultModel):
    slug: str
    title: str
    title_truncated: bool
    level: int = Field(ge=0, le=6)
    chars: Count
    tokens: Count


class ToolError(ResultModel):
    found: Literal[False] | None
    error: str
    retryable: bool = False
    message: str | None = None
    package: str | None = None
    ecosystem: Ecosystem | None = None
    version: str | None = None
    docs_url: str | None = None
    repository: str | None = None
    requested_section: str | None = None
    section_map: list[SectionInfo] | None = None


class CacheAvailable(ResultModel):
    available: Literal[True]
    total: Count
    valid: Count
    expired: Count


class CacheUnavailable(ResultModel):
    available: Literal[False]
    total: None
    valid: None
    expired: None


class PackageMetadata(ResultModel):
    name: str
    ecosystem: Ecosystem
    latest_stable: str | None
    description: str
    docs_url: str | None
    repository: str | None
    homepage: str | None
    license: str | None
    cached: bool = Field(default=False, alias="_cached")


class DocumentProvenance(ResultModel):
    found: Literal[True]
    package: str
    ecosystem: Ecosystem
    version: str
    source: str
    source_url: str
    fetched_at: float = Field(ge=0)
    metadata_refreshed: bool
    version_binding: Literal["registry_version", "unverified_git_ref"]
    content_trust: Literal["untrusted_upstream"]
    cached: bool


class DocumentResult(DocumentProvenance):
    content: str
    truncated: bool
    tokens_included: Count
    budget_tokens: int = Field(ge=200, le=6000)


class CompactDocument(DocumentResult):
    budget_scope: str
    sections_included: list[str]
    sections_omitted: list[str]
    sections_omitted_total: Count
    section_map: list[SectionInfo]
    section_map_total: Count
    section_map_truncated: bool


class DocumentSection(DocumentResult):
    section: SectionInfo


class DocumentOutline(DocumentProvenance):
    section_map: list[SectionInfo]
    section_map_total: Count
    next_offset: Count | None
    next: str


class DependencyPin(ResultModel):
    name: str
    ecosystem: Ecosystem
    spec: str
    pinned: str | None
    source: str
    spec_redacted: bool
    registry_lookup: bool
    extras: list[str]
    marker: str | None


class ProjectDependencies(ResultModel):
    found: Literal[True]
    manifest_kind: str
    dependencies: list[DependencyPin]
    usage: str


RESULT_TYPES = {
    "get_package_info": TypeAdapter(PackageMetadata | ToolError),
    "get_package_docs": TypeAdapter(CompactDocument | DocumentSection | ToolError),
    "get_docs_outline": TypeAdapter(DocumentOutline | ToolError),
    "get_project_dependencies": TypeAdapter(ProjectDependencies | ToolError),
    "cache_stats": TypeAdapter(CacheAvailable | CacheUnavailable | ToolError),
}


def output_schema(name: str) -> dict:
    # v1 clients require an object root; every branch remains a strict object.
    return {"type": "object", **RESULT_TYPES[name].json_schema()}
