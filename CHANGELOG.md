# Changelog

## 0.3.5

- Made `/v1/search` treat `%`, `_`, and backslash as literal characters instead of SQL `LIKE` wildcards.
- Added fail-fast startup validation: `DAYFLOW_DAY_BOUNDARY_HOUR` must be 0–23 and `DAYFLOW_MAX_UPLOAD_MB` must be an integer >= 1.
- Added a Docker Compose healthcheck using the unauthenticated `/healthz` endpoint.
- Added a CI container startup smoke test that launches the built image and verifies `/healthz` reports version 0.3.5.
- Added regression tests for literal search characters and invalid startup configuration.
- Updated English and Chinese documentation for the new validation and health behavior.

## 0.3.4

- Made the hash of the exact generated SQLite snapshot authoritative for publishing, eliminating a source-hash/snapshot race if Dayflow writes between change detection and snapshot creation.
- Re-evaluate per-destination publish state against the snapshot hash before sending.
- Strengthened the native macOS behavioral test to verify that the uploaded snapshot hash, `X-Dayflow-Timeline-Hash`, and local success-state hash are identical.
- Upgraded GitHub Actions to `actions/checkout@v7` and `actions/setup-python@v7`.
- Added a real Docker image build to CI in addition to Compose validation.
- Removed stale/duplicated license wording from the English and Chinese READMEs.

## 0.3.3

- Fixed the native macOS publisher behavioral test so its local HTTP server does not rely on `HTTPServer` reverse-DNS/FQDN lookup during bind.
- Pinned Python 3.13 in the macOS GitHub Actions job for deterministic CI behavior.
- Added captured server logs, early background-process failure detection, and a longer readiness timeout to the macOS test harness.
- No production API or publishing semantics changed from 0.3.2.

## 0.3.2

- Fixed a macOS publisher temporary-file leak by creating the SQLite snapshot inside a dedicated temporary directory and removing that directory during cleanup.
- Made token generation all-or-nothing: if either credential target already exists, no new token file is created.
- Added a regression test for partial token-file existence.
- Added a native macOS publisher behavioral regression test that performs a real local HTTP publish and verifies temporary-file cleanup.
- Added the MIT License.
- Updated README repository layouts and release/version references.

## 0.3.1

- Added a reproducible pytest integration suite for publish, authentication, validation, read APIs, timestamp handling, upload limits, and token generation.
- Added GitHub Actions for API tests, Compose validation, and native macOS `zsh` syntax checking.
- Added `TESTING.md` and `requirements-dev.txt`.
- Added non-interactive SSH backup behavior with `BatchMode` and configurable `DAYFLOW_SSH_CONNECT_TIMEOUT` (default 10 seconds).
- Strengthened upload validation to check exact column definitions, integer core fields, and non-negative activity time ranges.
- Fixed documentation/version consistency and a metadata-parser comment typo.

## 0.3.0

Initial public release.

- Timeline-only export from Dayflow's local SQLite database.
- Authenticated HTTPS publishing with logical SHA-256 verification.
- SQLite schema validation and atomic server-side replacement.
- Separate read and publish bearer credentials.
- Read-only status, timeline, detail, search, and time-breakdown endpoints.
- `metadata.appSites` parsing into compact `apps` output.
- Change-aware macOS publisher with launchd-friendly behavior.
- Optional SSH/rsync backup mirror with independent destination state.
- Docker hardening, bilingual documentation, security guidance, and Muse integration example.
