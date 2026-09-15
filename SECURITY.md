# Security policy and deployment boundary

Universal Docs MCP is a **single-user local stdio server**. Do not expose it directly as a public HTTP service or treat its tool annotations as authorization. There is no multi-user authentication or tenant isolation.

## Filesystem access

Manifest access is disabled unless the operator sets `UNIVERSAL_DOCS_PROJECT_ROOT`. Configure one trusted project root, not a home directory. The POSIX implementation opens path components relative to directory descriptors, refuses symlinks and special files, and limits bytes. The operator owns root configuration and must prevent hostile local users from replacing the trusted root or modifying its files. This is not an OS sandbox or protection from an attacker already controlling the account.

Only advertised manifest dependency sections are parsed. No dependency URL is fetched, no referenced file is followed, and no install/build hook from a manifest is executed. URL/Git/local references are redacted in full, including unsupported tags/aliases; URL/Git/path-like names fail validation rather than being echoed. Otherwise syntactically supported names, requirement markers and upstream prose are not a general-purpose data-loss-prevention boundary. Review sensitive manifests before granting access.

## Network and credentials

Only fixed PyPI, npm, crates.io and GitHub API hosts are fetched over HTTPS. Inputs cannot substitute a host, query, or path component. Redirects and inherited proxy settings are disabled. Response sizes and deadlines are bounded; cache failure is visible.

A configured `GITHUB_TOKEN` may be used only for GitHub repository-visibility and README requests. Authenticated fallback checks that the repository is public before reading its README. Do not configure a credential with unnecessary private-repository or write access. Do not include real tokens in examples, bug reports, manifests, or issue attachments. Synthetic canaries in regression tests are deliberately not credentials.

Invalid tool arguments and handled failures return safe codes rather than raw exception text, paths, or rejected argument values. The standalone process does not log document or manifest content. Dependency diagnostics (including SDK unknown-tool warnings and exception details) are replaced by `dependency_diagnostic_redacted`, retaining logger attribution and severity. This is an intentional privacy filter, not suppression of tool failures: structured errors and sanitized application failure classes remain visible. Importing the library does not reconfigure the host application’s logging. A surrounding client may log the arguments it supplies; server redaction cannot remove copies already present in client logs.

## Untrusted documentation

READMEs and registry data may contain prompt injection, malicious commands, stale instructions or vulnerable examples. Retrieval is not endorsement. Clients must treat content as data and retain their own approval, execution, identity and secret boundaries. No model is used to obey or execute fetched instructions.

## Reporting

Do not put live credentials or sensitive local data in public issues. Provide a minimal reproduction using synthetic values and identify the exact source commit/package version. Use the repository's private vulnerability-reporting feature if available; availability has not been assumed or enabled by this local work. Coordinate a private channel with the maintainer before sending sensitive details.

This release candidate has not been independently certified or approved for public deployment. Release requires the clean-install/protocol/privacy checks in `docs/HARDENING.md` and a separate publication decision.
