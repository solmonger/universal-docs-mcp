# Shared Node host boundary (local candidate)

`prepareContext({executable, requestFile, cacheDir, timeoutMs})` invokes the fixed
`universal-docs-context --request-file ...` CLI. It returns the
`universal-docs.context/v1` frame, with `prepared`, `prepared_stale`, or
`unavailable` status. All three paths are explicit. The executable, request file
and cache directory must be absolute trusted settings, never taken from an event.

The Python delivery core owns receipt validation and packet formatting. This
module owns only bounded POSIX process execution and strict frame acceptance:
16 KiB stdout, 8 KiB context, no shell, filtered environment, explicit cache
scope, deadline and process-group cleanup. It is not an OS sandbox for hostile
executables. `HOME` remains available for Python startup; credentials and runtime
injection variables are not inherited. Windows is not supported by this bridge.

The OpenClaw and Pi entry files are independent in-progress consumers; their
manifest entries are not a claim of native-host verification. Do not install this
candidate into an active harness until its matching integration gate is accepted.

Run bridge checks with `node --test adapters/node/tests/context-bridge.test.mjs`.
