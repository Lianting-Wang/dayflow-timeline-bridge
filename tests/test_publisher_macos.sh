#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d -t dayflow-publisher-test)"
SERVER_PID=""
SERVER_LOG="$WORK/server.log"

cleanup() {
  local rc=$?
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
  exit "$rc"
}
trap cleanup EXIT

SRC="$WORK/chunks.sqlite"
STATE_DIR="$WORK/state"
TOKEN_FILE="$WORK/publish-token"
PUBLISH_TMP="$WORK/publisher-tmp"
PORT_FILE="$WORK/port"
UPLOAD_FILE="$WORK/upload.sqlite"
HEADER_HASH_FILE="$WORK/header-hash"

mkdir -p "$STATE_DIR" "$PUBLISH_TMP"
printf '%s\n' 'publish-secret' > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

/usr/bin/sqlite3 "$SRC" <<'SQL'
CREATE TABLE timeline_cards (
  id INTEGER PRIMARY KEY,
  start_ts INTEGER NOT NULL,
  end_ts INTEGER NOT NULL,
  title TEXT,
  summary TEXT,
  detailed_summary TEXT,
  category TEXT,
  subcategory TEXT,
  metadata TEXT,
  is_deleted INTEGER NOT NULL DEFAULT 0
);
INSERT INTO timeline_cards
(id, start_ts, end_ts, title, summary, detailed_summary, category, subcategory, metadata, is_deleted)
VALUES
(1, 1700000000, 1700000600, 'Test', 'Summary', 'Detail', 'Work', 'Test', '{}', 0);
SQL

cat > "$WORK/server.py" <<'PY'
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer
import sys

port_file = Path(sys.argv[1])
upload_file = Path(sys.argv[2])
header_hash_file = Path(sys.argv[3])

class Handler(BaseHTTPRequestHandler):
    def do_PUT(self):
        if self.path != "/v1/publish":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        remaining = length
        with upload_file.open("wb") as handle:
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)

        header_hash_file.write_text(
            self.headers.get("X-Dayflow-Timeline-Hash", ""),
            encoding="utf-8",
        )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, _format, *_args):
        pass

class Server(TCPServer):
    allow_reuse_address = True

# Use TCPServer directly instead of HTTPServer. HTTPServer.server_bind()
# calls socket.getfqdn(host), which can block on reverse DNS in hosted CI.
with Server(("127.0.0.1", 0), Handler) as server:
    port_file.write_text(str(server.server_address[1]), encoding="utf-8")
    server.handle_request()
PY

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

"$PYTHON_BIN" "$WORK/server.py" "$PORT_FILE" "$UPLOAD_FILE" "$HEADER_HASH_FILE" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

# Wait up to ~30 seconds. Also fail immediately if the background server dies,
# printing its captured output to make CI failures diagnosable.
for _ in $(seq 1 600); do
  if [[ -s "$PORT_FILE" ]]; then
    break
  fi

  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    echo "HTTP test server exited before becoming ready." >&2
    cat "$SERVER_LOG" >&2 || true
    exit 1
  fi

  sleep 0.05
done

if [[ ! -s "$PORT_FILE" ]]; then
  echo "HTTP test server did not become ready within the timeout." >&2
  cat "$SERVER_LOG" >&2 || true
  exit 1
fi

PORT="$(cat "$PORT_FILE")"

TMPDIR="$PUBLISH_TMP" \
DAYFLOW_CONFIG_FILE="$WORK/no-config" \
DAYFLOW_DB="$SRC" \
DAYFLOW_STATE_DIR="$STATE_DIR" \
DAYFLOW_HTTP_URL="http://127.0.0.1:$PORT" \
DAYFLOW_HTTP_TOKEN_FILE="$TOKEN_FILE" \
DAYFLOW_SSH_ENABLED=0 \
/bin/zsh "$REPO_ROOT/mac/sync_dayflow_timeline.sh" --force

wait "$SERVER_PID"
SERVER_PID=""

test -s "$STATE_DIR/last-http-uploaded.sha256"
test -s "$UPLOAD_FILE"
test -s "$HEADER_HASH_FILE"

STATE_HASH="$(cat "$STATE_DIR/last-http-uploaded.sha256")"
HEADER_HASH="$(cat "$HEADER_HASH_FILE")"
UPLOAD_HASH="$(
  /usr/bin/sqlite3 -batch "$UPLOAD_FILE" <<'SQL' \
    | /usr/bin/shasum -a 256 \
    | /usr/bin/awk '{print $1}'
SELECT
    CAST(id AS TEXT) || '|' ||
    CAST(start_ts AS TEXT) || '|' ||
    CAST(end_ts AS TEXT) || '|' ||
    CASE WHEN title IS NULL THEN 'N' ELSE 'T' || hex(CAST(title AS BLOB)) END || '|' ||
    CASE WHEN summary IS NULL THEN 'N' ELSE 'T' || hex(CAST(summary AS BLOB)) END || '|' ||
    CASE WHEN detailed_summary IS NULL THEN 'N' ELSE 'T' || hex(CAST(detailed_summary AS BLOB)) END || '|' ||
    CASE WHEN category IS NULL THEN 'N' ELSE 'T' || hex(CAST(category AS BLOB)) END || '|' ||
    CASE WHEN subcategory IS NULL THEN 'N' ELSE 'T' || hex(CAST(subcategory AS BLOB)) END || '|' ||
    CASE WHEN metadata IS NULL THEN 'N' ELSE 'T' || hex(CAST(metadata AS BLOB)) END
FROM timeline_cards
WHERE is_deleted = 0
ORDER BY id;
SQL
)"

if [[ -z "$UPLOAD_HASH" || "$STATE_HASH" != "$UPLOAD_HASH" || "$HEADER_HASH" != "$UPLOAD_HASH" ]]; then
  echo "Publisher hash consistency check failed." >&2
  echo "state=$STATE_HASH" >&2
  echo "header=$HEADER_HASH" >&2
  echo "upload=$UPLOAD_HASH" >&2
  exit 1
fi

if find "$PUBLISH_TMP" -mindepth 1 -print -quit | grep -q .; then
  echo "Publisher left temporary files behind:" >&2
  find "$PUBLISH_TMP" -mindepth 1 -print >&2
  exit 1
fi

echo "macOS publisher behavioral test passed"
