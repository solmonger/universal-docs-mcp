# Universal Docs MCP

A local, read-only MCP server for **version-aware package READMEs and registry descriptions** from PyPI, npm, and crates.io/GitHub. It helps coding agents use project-relevant documentation rather than assume their training data matches an installed dependency.

This source tree is **0.4.0.dev1**, an unaccepted local development candidate. The previously reviewed/deployed build remains **0.3.0rc2**. Do not assume the public repository or PyPI contains this revision. It is not a full API-documentation crawler or symbol-search service.

Cross-harness work is tracked in [the implementation spec](specs/cross-harness-fresh-docs/README.md). The freshness-aware `universal-docs-preflight` CLI is implemented as a source candidate; automatic per-harness context injection and July-2026 protocol compatibility are not yet claimed.

## What it does

- Resolves stable releases, excluding prereleases and known yanked releases. A package with no eligible stable release is reported explicitly.
- Fetches an exact version when supplied. Never silently replaces it with default-branch documentation.
- Caches raw documentation separately from compact output. Exact-version, TTL-valid cache hits do not require a registry connection.
- Returns source URL, acquisition timestamp, cache status, and a **version-binding caveat**. A GitHub ref is not proof of published-package identity.
- Provides paginated section outlines and bounded, code-preserving compact content.
- Optionally reads supported manifests inside one explicitly configured project root. URL/Git/path references are omitted wholesale instead of exposing embedded credentials.
- Bounds network bodies, cache storage, concurrent tool work, and tool response payloads; errors remain machine-readable.

## Install this checkout

Requires **Python 3.10 or newer**. Use a dedicated environment; no service or HTTP port is required.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

The installed entry point is `.venv/bin/universal-docs-mcp`. Use its **absolute path** in your MCP client configuration; it does not need the repository as its working directory.

For an audited/reproducible development installation, install the hash-locked dependencies before the local source:

```sh
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
```

The package supports MCP SDK `>=1.26,<2`. The SDK v2 API is not compatible with this server. Runtime dependencies have compatibility upper bounds; the standard installation still resolves within those ranges rather than reproducing the lock. The lockfile records the tested dependency set; it is not a promise that dependencies never need security updates.

## Connect a client

Use your client's documented MCP configuration location. A conventional stdio configuration is:

```json
{
  "mcpServers": {
    "universal-docs": {
      "command": "/absolute/path/to/.venv/bin/universal-docs-mcp"
    }
  }
}
```

Registry tools work without filesystem access. To enable manifest reading, add an explicitly trusted project directory to that server's environment:

```json
{
  "UNIVERSAL_DOCS_PROJECT_ROOT": "/absolute/path/to/project",
  "UNIVERSAL_DOCS_CACHE_DIR": "/absolute/path/to/cache"
}
```

Do not point the root at your home directory. Root-relative manifest paths are supported. On POSIX, descriptor-relative opens reject symlinks, escapes, directories, and special files. The manifest tool fails closed on platforms without the required secure open primitives; that currently excludes Windows. Registry tools do not require POSIX file access.

## Tools

| Tool | Purpose |
|---|---|
| `get_package_info` | Public registry metadata, including `latest_stable` or `null` if none |
| `get_docs_outline` | Versioned section map; `offset` and `limit` support pagination |
| `get_package_docs` | Compact documentation or one section; `max_tokens` defaults to 1500, range 200–6000 |
| `get_project_dependencies` | Supported manifest declarations, safe exact pins, extras/markers, and redaction/registry-lookup flags |
| `cache_stats` | Entry counts and `available`; counts are `null` when cache state is unknown |

### Fresh preflight CLI

The package also installs `.venv/bin/universal-docs-preflight`. It reads one
strict JSON request from stdin and writes exactly one JSON object to stdout; it
does not read a project manifest or execute a package manager. The canonical
request shape is:

```json
{
  "package": "requests",
  "ecosystem": "python",
  "selection": "requested",
  "requested_version": "2.32.3",
  "query": "timeouts and retries",
  "section_ids": ["usage"],
  "context_max_bytes": 12000,
  "freshness_mode": "require_check"
}
```

Use `selection: "latest"` without a version, and choose one explicit
`freshness_mode`: `require_check` bypasses document cache, `allow_cache` may
reuse a valid exact document (but latest still checks registry metadata), and
`allow_stale` may return a retained exact record after an upstream check
fails, explicitly labeled stale even when its cache TTL has not elapsed. Fallback
is bounded to seven days of source age; quotas/pruning mean retention is not
guaranteed. A response's `receipt` keeps `target_version`, `requested_version`,
`installed_version: null`/`installed_resolution: "unknown"`, and
`latest_observed` separate. It also reports `fetched_at`, successful
`checked_at`/`latest_checked_at`, cache/stale/unknown state, age, SHA-256,
source URL, and version binding. `context` is query/section-selected,
UTF-8-byte bounded, and labeled untrusted; `selection.no_match` is explicit
instead of silently dumping the document. Missing meaningful query/sections
returns an outline and no context. Exit status is 0 for usable context,
1 for a retrieval miss/unavailable response, and 2 for invalid input.

Package tools accept `ecosystem` aliases: Python (`python`, `pypi`, `pip`), JavaScript/TypeScript (`javascript`, `typescript`, `npm`, `js`, `ts`), Rust (`rust`, `cargo`, `crate`). Omit it to try applicable registries in that order; specify it when a package name exists in multiple ecosystems.

### Recommended workflow

1. Read the project's supported manifest, if that access is enabled.
2. Use a returned `pinned` value only when `registry_lookup` is true. **A range is not the installed version**, and latest may be outside that range. Consult the actual lockfile/package manager when no exact pin is available.
3. Call `get_docs_outline`, for example with `{"package":"requests","ecosystem":"python","version":"2.32.3"}`.
4. Follow `next_offset` for another outline page, or request a returned section slug using `get_package_docs` with the same package/version.
5. Inspect `version_binding`, `fetched_at`, and `truncated`. Never execute a clipped example as if it were complete.

### Manifest support and limits

- Python: PEP 508 requirement lines in `requirements*.txt` / `constraints*.txt`; `[project].dependencies` in `pyproject.toml`.
- JavaScript: `dependencies` and `devDependencies` in `package.json`.
- Rust: top-level `dependencies` and `dev-dependencies` in `Cargo.toml`; package records in `Cargo.lock`.
- This is **not** a package manager: no installation, resolution, recursive requirements includes, shell execution, npm lockfile support, Poetry groups, or workspace traversal. Unsupported requirements syntax returns an error instead of incomplete success. Unlisted manifest sections are not enumerated.
- Cargo bare versions are ranges. Git/path/workspace/private-registry declarations are not public-registry pins. Python extras and environment markers are preserved but not evaluated.
- Non-version references are returned as `[redacted]` with `spec_redacted: true` and `registry_lookup: false`. URL/Git/path-like dependency names are rejected rather than returned as identities. This is a supported-name syntax check, not general-purpose secret detection in otherwise valid names or prose; only grant access to trusted manifests.

## Versioned official-source candidate

The preflight CLI also accepts one fixed catalog entry: `mcp-tools` at
`2026-07-28`, fetched as Markdown from the canonical MCP specification site.
The official request is intentionally narrower than a package request:

```json
{
  "source_id": "mcp-tools",
  "selection": "requested",
  "requested_version": "2026-07-28",
  "query": "tools/list",
  "context_max_bytes": 12000,
  "freshness_mode": "require_check"
}
```

`source_id` and the exact requested version are resolved through the immutable
catalog; arbitrary URLs, `latest`, and package/ecosystem aliases are rejected.
The request uses the shared selector and emits the same bounded JSON preflight
shape. Its receipt keeps `package` and `ecosystem` null, records `source_id`,
uses `installed_resolution: "not_applicable"`, and records the exact `.md` URL,
`official_markdown` source, `versioned_url` binding, full-source SHA-256, and
acquisition/freshness timestamps. `allow_cache` may reuse only the exact
snapshot; `allow_stale` may return a bounded seven-day snapshot after an
upstream failure, explicitly labeled stale. Missing or irrelevant source
content returns empty context and a nonzero CLI status; no version is guessed.
`fetch_official_source` remains a library-level catalog fetcher and never
expands the generic registry URL allowlist.


## Guarantees and limits

- **Documentation coverage:** READMEs/registry descriptions, not full API sites, symbol signatures, or all programming languages. Rust exact docs.rs retrieval is not implemented.
- **Version binding:** `registry_version` means a version-specific registry endpoint; `unverified_git_ref` means a GitHub README at that version string. Git refs can move, tags may be `v`-prefixed, and monorepos may not match. A miss does not trigger default-branch fallback.
- **Freshness:** `fetched_at` is local acquisition time, not upstream publication time. Cache TTL is 24 hours. `metadata_refreshed: false` means an exact cache hit did not contact the registry. A previous latest lookup also establishes an exact-version alias. Auto-detection reuses only an established namespace association; it never guesses a registry from unrelated cached packages. Omitted versions are resolved anew before raw-cache lookup. `force_refresh: true` bypasses the relevant cache; it cannot certify upstream accuracy.
- **Budgets:** `max_tokens` is a four-characters-per-token content estimate, not model-token accounting. Outlines have at most 100 entries per page; compact maps/omitted-section lists expose totals when capped. Long display headings are marked truncated. The serialized MCP `CallToolResult` has a separate 128 KiB cap, including both text and structured representations and JSON escaping; the transport envelope/request ID is outside that cap. Oversized results return `response_too_large`; request fewer outline entries or a smaller content budget.
- **Resource limits:** four simultaneous tools, 45-second tool deadline, 20-second per-request deadline, 8 MiB upstream body limit, 1 MiB document/manifest limits, at most 1,000 heading sections per document. Network redirects, inherited proxies, and unsolicited compressed responses are refused. Package tools fetch only the fixed public registry/GitHub API hosts; the separate official-source catalog permits only its exact reviewed URL.
- **Cache limits:** 256 entries, 2 MiB per encoded value, 32 MiB total logical value bytes. Writes prune expired/oldest entries. These are logical quotas, not a byte-perfect SQLite-file/RSS guarantee; existing database pages can be reused without shrinking the file. Cache failure degrades to uncached retrieval, not false success.
- **Trust:** fetched content is untrusted data, not agent instructions. Tool availability does not guarantee an agent uses it. This is a single-user local stdio service, not an authenticated multi-tenant network service.

Each tool advertises a strict `outputSchema` for successful results and errors. Outbound payloads are validated before SDK handling; invalid results become `invalid_tool_result` without reflecting their contents.

Tool failures set MCP `isError: true` and return the same JSON object in text and structured content. Examples include `invalid_arguments`, `manifest_access_disabled`, `manifest_error`, `package_not_found`, `documentation_not_found`, `no_stable_release`, `upstream_unavailable`, `upstream_invalid`, `server_busy`, and `tool_timeout`. `found: null` denotes unknown/unavailable, not proven absence. Repeatedly retrying a permanent validation error will not help.

## Development and verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m build --no-isolation
```

The default suite blocks network sockets and includes a real subprocess/stdio client test. Public-registry tests are explicitly opt-in:

```sh
UNIVERSAL_DOCS_LIVE_TESTS=1 .venv/bin/python -m pytest -m live -q
.venv/bin/python scripts/smoke.py --live --output smoke.json
```

Run the smoke script with a **fresh wheel-installed interpreter from outside the repository** before release. CI builds and installs a wheel and runs the offline suite across Python 3.10–3.14; remote CI results are not implied by a local pass. See [hardening acceptance checks](docs/HARDENING.md), [security boundaries](SECURITY.md), and [changelog](CHANGELOG.md).

## License

MIT.
