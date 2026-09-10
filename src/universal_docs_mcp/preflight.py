"""Freshness-aware, harness-neutral documentation preflight.

The command accepts one bounded JSON request on stdin and writes one bounded JSON
object on stdout.  Retrieved documentation is data, never instructions: the
receipt labels its provenance and trust explicitly for downstream harnesses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cache import DocsCache
from .docs_fetcher import FetchedDocument, fetch_docs_content_with_provenance
from .network import network_lifespan
from .registries import PackageInfo, fetch_package
from .retrieval import load_stale_record, retrieve_document

SCHEMA = "universal-docs.preflight/v1"
MAX_INPUT_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
DEFAULT_CONTEXT_BYTES = 12_000
MAX_CONTEXT_BYTES = 32_000
DEFAULT_DEADLINE_MS = 30_000
MAX_DEADLINE_MS = 45_000
MAX_STALE_AGE_SECONDS = 7 * 24 * 60 * 60


class PreflightRequest(BaseModel):
    """Strict JSON request; project/manifest resolution is intentionally absent."""

    model_config = ConfigDict(strict=True, extra="forbid")

    package: str = Field(
        min_length=1,
        max_length=214,
        pattern=r"^(?:@[a-zA-Z0-9][a-zA-Z0-9._-]*/)?[a-zA-Z0-9][a-zA-Z0-9._-]*$",
    )
    ecosystem: Literal["python", "javascript", "rust"]
    selection: Literal["requested", "latest"]
    requested_version: str | None = Field(default=None, min_length=1, max_length=128)
    # ``version`` is a deliberately documented compatibility spelling for
    # callers that already use get_package_docs' field name.
    version: str | None = Field(default=None, min_length=1, max_length=128)
    query: str | None = Field(default=None, max_length=512)
    section_ids: list[str] = Field(default_factory=list, max_length=32)
    context_max_bytes: int = Field(
        default=DEFAULT_CONTEXT_BYTES, ge=1, le=MAX_CONTEXT_BYTES
    )
    freshness_mode: Literal["require_check", "allow_cache", "allow_stale"]
    deadline_ms: int = Field(default=DEFAULT_DEADLINE_MS, ge=1_000, le=MAX_DEADLINE_MS)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.requested_version and self.version:
            raise ValueError("duplicate_requested_version")
        selected = self.requested_version or self.version
        if self.selection == "requested" and selected is None:
            raise ValueError("requested_selection_needs_exact_version")
        if self.selection == "latest" and selected is not None:
            raise ValueError("latest_selection_cannot_include_version")
        if selected is not None:
            if self.ecosystem == "python":
                try:
                    Version(selected)
                except InvalidVersion:
                    raise ValueError("invalid_version") from None
            elif not re.fullmatch(
                r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
                r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
                selected,
            ):
                raise ValueError("invalid_version")
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("duplicate_section_id")
        for section_id in self.section_ids:
            if not 1 <= len(section_id) <= 128:
                raise ValueError("invalid_section_id")
        return self

    @property
    def exact_version(self) -> str | None:
        return self.requested_version or self.version


FetchPackage = Callable[..., Awaitable[PackageInfo | None]]
FetchDocument = Callable[..., Awaitable[FetchedDocument | None]]


def _empty_receipt() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "target": {
            "package": None,
            "ecosystem": None,
            "selection": None,
            "target_version": None,
            "requested_version": None,
            "installed_version": None,
            "installed_resolution": "unknown",
            "latest_observed": None,
        },
        "source": {
            "kind": None,
            "url": None,
            "version_binding": None,
            "content_sha256": None,
            "content_bytes": 0,
        },
        "freshness": {
            "policy": None,
            "state": "unknown",
            "fetched_at": None,
            "checked_at": None,
            "latest_checked_at": None,
            "age_seconds": None,
            "cached": False,
            "stale": False,
            "unknown": True,
            "stale_reason": None,
            "retryable": False,
        },
        "selection": {
            "query": None,
            "requested_sections": [],
            "matched_sections": [],
            "matches": [],
            "no_match": False,
            "sections_omitted": 0,
            "section_map": [],
            "section_map_total": 0,
            "section_map_truncated": False,
            "context_bytes": 0,
            "budget_bytes": None,
            "truncated": False,
        },
        "trust": {
            "content": "untrusted_upstream",
            "instructions_authoritative": False,
            "execution_performed": False,
        },
    }


def build_error_response(error: str, *, retryable: bool) -> dict[str, Any]:
    """Build a safe error without reflecting request or exception text."""
    response = {
        "schema": SCHEMA,
        "found": None,
        "context": "",
        "receipt": _empty_receipt(),
        "error": error,
        "retryable": retryable,
    }
    response["receipt"]["freshness"]["retryable"] = retryable
    return response


def _error_code(exc: BaseException) -> tuple[str, bool]:
    if isinstance(exc, asyncio.TimeoutError):
        return "upstream_timeout", True
    if isinstance(exc, httpx.HTTPError):
        return "upstream_unavailable", True
    if isinstance(exc, ValueError):
        if str(exc) == "no_stable_release":
            return "no_stable_release", False
        return "upstream_invalid", False
    return "internal_error", False


def _terms(query: str | None) -> list[str]:
    if not query:
        return []
    return re.findall(r"[a-z0-9][a-z0-9_-]{1,63}", query.lower())[:32]


def _score_section(section: Any, terms: list[str]) -> tuple[int, str]:
    title = section.title.lower()
    body = section.body.lower()
    title_hits = sum(title.count(term) for term in terms)
    body_hits = sum(body.count(term) for term in terms)
    score = title_hits * 4 + body_hits
    if title_hits:
        reason = "title and body term match" if body_hits else "title term match"
    elif body_hits:
        reason = "body term match"
    else:
        reason = ""
    return score, reason


def _truncate_utf8(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _select_context(
    raw: str,
    *,
    package: str,
    source: str,
    source_url: str,
    version: str,
    query: str | None,
    requested_sections: list[str],
    budget_bytes: int,
) -> tuple[str, dict[str, Any]]:
    # Imports stay local to keep the preflight's public surface wire-neutral.
    from .compaction import get_section, parse_sections, section_map

    sections = parse_sections(raw)
    terms = _terms(query)
    by_identity: dict[str, tuple[Any, int, str, int]] = {}
    candidates: list[tuple[Any, int, str, int]] = []

    for position, requested in enumerate(requested_sections):
        section = get_section(sections, requested)
        if section is not None and section.slug not in by_identity:
            item = (section, 10_000 - position, "explicit section", position)
            by_identity[section.slug] = item
            candidates.append(item)

    if terms:
        for position, section in enumerate(sections):
            score, reason = _score_section(section, terms)
            if score and section.slug not in by_identity:
                item = (section, score, reason, position)
                by_identity[section.slug] = item
                candidates.append(item)
    elif not requested_sections:
        # Without intent, provide only a small deterministic opening slice; a
        # caller wanting more must ask for a section or a query.
        for position, section in enumerate(sections[:8]):
            item = (section, 1, "default bounded opening", position)
            by_identity[section.slug] = item
            candidates.append(item)

    candidates.sort(key=lambda item: (-item[1], item[3]))
    prefix = (
        "UNTRUSTED DOCUMENTATION DATA\n"
        f"Package: {package} v{version}\n"
        f"Source: {source}\n"
        f"Source URL: {source_url}\n\n"
    )
    remaining = budget_bytes
    context_parts: list[str] = []
    if candidates and remaining:
        prefix = _truncate_utf8(prefix, remaining)
        context_parts.append(prefix)
        remaining -= len(prefix.encode("utf-8"))

    matched: list[str] = []
    matches: list[dict[str, Any]] = []
    truncated = False
    for section, score, reason, _position in candidates:
        rendered = f"## {section.title}\n\n{section.body}\n\n"
        if remaining <= 0:
            truncated = True
            continue
        clipped = _truncate_utf8(rendered, remaining)
        if not clipped:
            truncated = True
            continue
        context_parts.append(clipped)
        remaining -= len(clipped.encode("utf-8"))
        matched.append(section.slug)
        matches.append({"slug": section.slug, "score": score, "reason": reason})
        if clipped != rendered:
            truncated = True
            break

    context = "".join(context_parts)
    no_match = bool(sections) and not matched
    if not sections:
        no_match = True
    selection = {
        "query": query,
        "requested_sections": requested_sections,
        "matched_sections": matched,
        "matches": matches[:64],
        "no_match": no_match,
        "sections_omitted": max(0, len(sections) - len(matched)),
        "section_map": section_map(sections),
        "section_map_total": len(sections),
        "section_map_truncated": len(sections) > 100,
        "context_bytes": len(context.encode("utf-8")),
        "budget_bytes": budget_bytes,
        "truncated": truncated,
    }
    return context, selection


def _freshness(
    *,
    policy: str,
    state: str,
    fetched_at: float | None,
    checked_at: float | None,
    latest_checked_at: float | None,
    cached: bool,
    stale: bool,
    stale_reason: str | None,
    retryable: bool,
) -> dict[str, Any]:
    now = time.time()
    age = None if fetched_at is None else max(0, now - fetched_at)
    return {
        "policy": policy,
        "state": state,
        "fetched_at": fetched_at,
        "checked_at": checked_at,
        "latest_checked_at": latest_checked_at,
        "age_seconds": age,
        "cached": cached,
        "stale": stale,
        "unknown": state == "unknown",
        "stale_reason": stale_reason,
        "retryable": retryable,
    }


def _response_from_loaded(
    request: PreflightRequest,
    loaded: tuple[PackageInfo, str, str, bool, dict[str, Any]],
    *,
    state: str,
    stale: bool,
    stale_reason: str | None,
    retryable: bool,
    latest_checked_at: float | None,
    checked_at: float | None,
) -> dict[str, Any]:
    info, _header, raw, cached, provenance = loaded
    version = request.exact_version or info.latest_stable
    latest_observed = (
        info.latest_stable if request.selection == "latest" and not stale else None
    )
    if not raw or not version:
        response = build_error_response(
            "documentation_not_found" if raw == "" else "no_stable_release",
            retryable=False,
        )
        response["found"] = False
        response["receipt"]["target"].update(
            {
                "package": info.name,
                "ecosystem": info.ecosystem,
                "selection": request.selection,
                "target_version": version,
                "requested_version": request.exact_version,
                "latest_observed": latest_observed,
            }
        )
        return response

    context, selection = _select_context(
        raw,
        package=info.name,
        source=provenance.get("source") or "unknown",
        source_url=provenance.get("source_url") or "",
        version=version,
        query=request.query,
        requested_sections=request.section_ids,
        budget_bytes=request.context_max_bytes,
    )
    fetched_at = provenance.get("fetched_at")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    receipt = _empty_receipt()
    receipt["target"].update(
        {
            "package": info.name,
            "ecosystem": info.ecosystem,
            "selection": request.selection,
            "target_version": version,
            "requested_version": request.exact_version,
            "latest_observed": latest_observed,
        }
    )
    receipt["source"].update(
        {
            "kind": provenance.get("source"),
            "url": provenance.get("source_url"),
            "version_binding": provenance.get("version_binding"),
            "content_sha256": digest,
            "content_bytes": len(raw.encode("utf-8")),
        }
    )
    receipt["freshness"] = _freshness(
        policy=request.freshness_mode,
        state=state,
        fetched_at=fetched_at,
        checked_at=checked_at,
        latest_checked_at=latest_checked_at,
        cached=cached or stale,
        stale=stale,
        stale_reason=stale_reason,
        retryable=retryable,
    )
    receipt["selection"] = selection
    return {
        "schema": SCHEMA,
        "found": True,
        "context": context,
        "receipt": receipt,
        "retryable": retryable,
    }


async def run_preflight(
    request: PreflightRequest,
    *,
    cache: DocsCache,
    fetch_package_fn: FetchPackage = fetch_package,
    fetch_document_fn: FetchDocument = fetch_docs_content_with_provenance,
) -> dict[str, Any]:
    """Run one bounded preflight with explicit cache/freshness semantics."""
    args = {
        "package": request.package,
        "ecosystem": request.ecosystem,
        "version": request.exact_version,
        # require_check means both metadata and exact content bypass cache.
        "force_refresh": request.freshness_mode == "require_check",
    }
    latest_checked_at: float | None = None
    checked_at: float | None = None
    try:
        loaded = await retrieve_document(
            args,
            cache=cache,
            fetch_package_fn=fetch_package_fn,
            fetch_document_fn=fetch_document_fn,
        )
        if loaded is None:
            response = build_error_response("package_not_found", retryable=False)
            response["receipt"]["freshness"]["policy"] = request.freshness_mode
            response["receipt"]["target"].update(
                {
                    "package": request.package,
                    "ecosystem": request.ecosystem,
                    "selection": request.selection,
                    "requested_version": request.exact_version,
                }
            )
            return response
        info, _header, raw, cached, provenance = loaded
        if request.selection == "latest" and provenance.get("metadata_refreshed"):
            latest_checked_at = time.time()
        if provenance.get("metadata_refreshed") and not cached and raw:
            checked_at = max(
                time.time(), float(provenance.get("fetched_at") or 0) + 1e-6
            )
        state = "cache_hit" if cached else "upstream_checked"
        return _response_from_loaded(
            request,
            loaded,
            state=state,
            stale=False,
            stale_reason=None,
            retryable=False,
            latest_checked_at=latest_checked_at,
            checked_at=checked_at,
        )
    except Exception as exc:
        error, retryable = _error_code(exc)
        if request.freshness_mode == "allow_stale":
            fallback = load_stale_record(
                cache,
                request.package,
                request.ecosystem,
                request.exact_version,
                max_age=MAX_STALE_AGE_SECONDS,
            )
            if fallback is not None:
                record, info = fallback
                loaded = (
                    info,
                    "",
                    record["content"],
                    True,
                    {
                        "source": record.get("source"),
                        "source_url": record.get("source_url"),
                        "fetched_at": record.get("fetched_at"),
                        "metadata_refreshed": False,
                        "version_binding": (
                            "unverified_git_ref"
                            if record.get("source") == "github_readme"
                            else "registry_version"
                        ),
                    },
                )
                return _response_from_loaded(
                    request,
                    loaded,
                    state="stale_cache",
                    stale=True,
                    stale_reason=error,
                    retryable=True,
                    latest_checked_at=None,
                    checked_at=None,
                )
        response = build_error_response(error, retryable=retryable)
        response["receipt"]["freshness"]["policy"] = request.freshness_mode
        response["receipt"]["target"].update(
            {
                "package": request.package,
                "ecosystem": request.ecosystem,
                "selection": request.selection,
                "requested_version": request.exact_version,
            }
        )
        return response


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def parse_request(raw: bytes) -> PreflightRequest:
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input_too_large")
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_no_duplicate_keys,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if not isinstance(value, dict):
        raise ValueError("request_must_be_object")
    return PreflightRequest.model_validate(value)


def _bounded_json_bytes(value: dict[str, Any]) -> bytes:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return encoded
    return json.dumps(
        build_error_response("response_too_large", retryable=False),
        separators=(",", ":"),
    ).encode("utf-8")


async def _run_cli(request: PreflightRequest, cache: DocsCache) -> dict[str, Any]:
    async with network_lifespan():
        return await asyncio.wait_for(
            run_preflight(request, cache=cache), request.deadline_ms / 1000
        )


def main() -> int:
    """CLI entry point; stdout is exactly one JSON object."""
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        request = parse_request(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        sys.stdout.buffer.write(
            _bounded_json_bytes(
                build_error_response("invalid_request", retryable=False)
            )
        )
        sys.stdout.buffer.write(b"\n")
        return 2

    cache = DocsCache()
    try:
        try:
            response = asyncio.run(_run_cli(request, cache))
        except asyncio.TimeoutError:
            response = build_error_response("deadline_exceeded", retryable=True)
        except Exception as exc:
            error, retryable = _error_code(exc)
            response = build_error_response(error, retryable=retryable)
        sys.stdout.buffer.write(_bounded_json_bytes(response))
        sys.stdout.buffer.write(b"\n")
        return 0 if response.get("found") is True else 1
    finally:
        cache.close()


if __name__ == "__main__":
    raise SystemExit(main())
