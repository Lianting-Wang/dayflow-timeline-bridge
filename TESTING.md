# Testing

The repository includes API/integrity tests and CI checks for the macOS publisher.

## Local test suite

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

The tests cover:

- unauthenticated `/healthz`;
- read/publish bearer-token separation;
- successful and idempotent publishing;
- logical SHA-256 sidecar updates;
- timeline overlap semantics around the 04:00 boundary;
- `metadata.appSites` and distraction parsing;
- activity detail and search, including literal SQL `LIKE` wildcard characters (`%`, `_`, and `\\`);
- time-breakdown clipping;
- invalid date/range/query validation;
- rejection of wrong hashes, corrupt SQLite, empty uploads, bad schema, extra tables, and deleted rows;
- preservation of the live mirror after rejected publishes;
- millisecond timestamp detection;
- upload-size enforcement;
- startup configuration validation for day-boundary and upload-size settings;
- token generation permissions and overwrite protection.

## Static checks

```bash
python -m py_compile app/main.py scripts/generate_tokens.py
/bin/zsh -n mac/sync_dayflow_timeline.sh
docker compose config -q
```

GitHub Actions runs the Python/API suite, Compose validation, a real Docker image build, and a container `/healthz` startup smoke test on Ubuntu, plus publisher syntax and behavioral checks on a native macOS runner.

- token generation is all-or-nothing when one credential file already exists.

## Native macOS publisher regression test

GitHub Actions also runs:

```bash
/bin/bash tests/test_publisher_macos.sh
```

on `macos-latest`. It creates a temporary Dayflow-style SQLite database, performs a real HTTP publish to a local test server, verifies that the uploaded SQLite logical hash, HTTP hash header, and destination hash state all match, and confirms that publisher temporary files are cleaned up.

### macOS CI server note

The behavioral test uses Python's `socketserver.TCPServer` directly rather than
`http.server.HTTPServer`. `HTTPServer.server_bind()` performs a
`socket.getfqdn(host)` lookup, which is unnecessary for this test and can make
hosted macOS CI startup dependent on reverse-DNS behavior.

The test captures server stdout/stderr and prints it if the background process
exits or fails to become ready.
