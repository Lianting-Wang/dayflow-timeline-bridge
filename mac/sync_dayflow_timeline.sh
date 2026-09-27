#!/bin/zsh
set -euo pipefail

# Dayflow Timeline Bridge publisher for macOS.
#
# Primary destination:
#   1. HTTP(S) PUT /v1/publish
#
# Optional backup destination:
#   2. SSH/rsync mirror
#
# HTTP and SSH maintain independent success state.

CONFIG_FILE="${DAYFLOW_CONFIG_FILE:-$HOME/.config/dayflow-timeline-bridge/config}"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
fi

SRC="${DAYFLOW_DB:-$HOME/Library/Application Support/Dayflow/chunks.sqlite}"
STATE_DIR="${DAYFLOW_STATE_DIR:-$HOME/.local/state/dayflow-timeline-bridge}"
LOCK_DIR="$STATE_DIR/sync.lock"

# HTTP(S) publishing is the primary path.
HTTP_URL="${DAYFLOW_HTTP_URL:-}"
HTTP_TOKEN_FILE="${DAYFLOW_HTTP_TOKEN_FILE:-$HOME/.config/dayflow-timeline-bridge/publish-token}"
HTTP_USER_AGENT="${DAYFLOW_HTTP_USER_AGENT:-Dayflow-Timeline-Bridge/0.3.5}"

# Optional SSH/rsync backup mirror. Disabled unless explicitly enabled.
SSH_ENABLED="${DAYFLOW_SSH_ENABLED:-0}"
SSH_REMOTE="${DAYFLOW_SSH_REMOTE:-}"
SSH_REMOTE_DIR="${DAYFLOW_SSH_REMOTE_DIR:-}"
SSH_CONNECT_TIMEOUT="${DAYFLOW_SSH_CONNECT_TIMEOUT:-10}"

HTTP_HASH_FILE="$STATE_DIR/last-http-uploaded.sha256"
SSH_HASH_FILE="$STATE_DIR/last-ssh-uploaded.sha256"

TMP=""
TMP_DIR=""
LOCK_HELD=0
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

timestamp() {
  /bin/date '+%Y-%m-%d %H:%M:%S %Z'
}

log() {
  echo "[$(timestamp)] $*"
}

cleanup() {
  local rc=$?

  if [[ -n "$TMP_DIR" ]]; then
    /bin/rm -rf "$TMP_DIR"
  elif [[ -n "$TMP" ]]; then
    /bin/rm -f "$TMP"
  fi

  if [[ $LOCK_HELD -eq 1 ]]; then
    /bin/rm -rf "$LOCK_DIR"
  fi

  if [[ $rc -ne 0 ]]; then
    echo "[$(timestamp)] ERROR: Dayflow timeline sync finished with errors (exit $rc)" >&2
  fi

  return $rc
}
trap cleanup EXIT

write_hash_state() {
  local path="$1"
  local tmp="${path}.tmp.$$"
  print -r -- "$CURRENT_HASH" > "$tmp"
  /bin/chmod 600 "$tmp"
  /bin/mv "$tmp" "$path"
}


/bin/mkdir -p "$STATE_DIR"
/bin/chmod 700 "$STATE_DIR"

# Prevent overlapping launchd/manual runs. Recover a stale lock if a previous
# process died without running the EXIT trap.
if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
  stale=1
  if [[ -f "$LOCK_DIR/pid" ]]; then
    old_pid="$(/bin/cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && /bin/kill -0 "$old_pid" 2>/dev/null; then
      stale=0
    fi
  fi

  if [[ $stale -eq 0 ]]; then
    exit 0
  fi

  /bin/rm -rf "$LOCK_DIR"
  /bin/mkdir "$LOCK_DIR"
fi
LOCK_HELD=1
print -r -- "$$" > "$LOCK_DIR/pid"

if [[ ! -f "$SRC" ]]; then
  echo "[$(timestamp)] ERROR: Dayflow DB not found: $SRC" >&2
  exit 1
fi

if [[ "$SSH_ENABLED" != "1" && -z "$HTTP_URL" ]]; then
  echo "[$(timestamp)] ERROR: No Dayflow sync destination is enabled." >&2
  echo "Set DAYFLOW_HTTP_URL, or enable DAYFLOW_SSH_ENABLED=1 and configure the SSH destination." >&2
  exit 1
fi

if [[ "$SSH_ENABLED" == "1" ]]; then
  if [[ -z "$SSH_REMOTE" || -z "$SSH_REMOTE_DIR" ]]; then
    echo "[$(timestamp)] ERROR: SSH publishing requires DAYFLOW_SSH_REMOTE and DAYFLOW_SSH_REMOTE_DIR." >&2
    exit 1
  fi
  if [[ ! "$SSH_CONNECT_TIMEOUT" =~ ^[0-9]+$ ]]; then
    echo "[$(timestamp)] ERROR: DAYFLOW_SSH_CONNECT_TIMEOUT must be a non-negative integer." >&2
    exit 1
  fi
fi

if [[ -n "$HTTP_URL" && ! -f "$HTTP_TOKEN_FILE" ]]; then
  echo "[$(timestamp)] ERROR: HTTP publish token file not found: $HTTP_TOKEN_FILE" >&2
  exit 1
fi

# Compute a deterministic hash of exactly the timeline data that is mirrored.
# chunks.sqlite uses WAL mode, so database mtime alone is not a reliable change
# detector.
SOURCE_HASH="$(
  /usr/bin/sqlite3 -batch "$SRC" <<'SQL' \
    | /usr/bin/shasum -a 256 \
    | /usr/bin/awk '{print $1}'
.timeout 5000
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

if [[ -z "$SOURCE_HASH" ]]; then
  echo "[$(timestamp)] ERROR: Failed to calculate Dayflow source timeline hash" >&2
  exit 1
fi

HTTP_LAST_HASH=""
if [[ -f "$HTTP_HASH_FILE" ]]; then
  HTTP_LAST_HASH="$(/bin/cat "$HTTP_HASH_FILE" 2>/dev/null || true)"
fi

SSH_LAST_HASH=""
if [[ -f "$SSH_HASH_FILE" ]]; then
  SSH_LAST_HASH="$(/bin/cat "$SSH_HASH_FILE" 2>/dev/null || true)"
fi

NEED_HTTP=0
NEED_SSH=0

if [[ -n "$HTTP_URL" ]]; then
  if [[ $FORCE -eq 1 || -z "$HTTP_LAST_HASH" || "$SOURCE_HASH" != "$HTTP_LAST_HASH" ]]; then
    NEED_HTTP=1
  fi
fi

if [[ "$SSH_ENABLED" == "1" ]]; then
  if [[ $FORCE -eq 1 || -z "$SSH_LAST_HASH" || "$SOURCE_HASH" != "$SSH_LAST_HASH" ]]; then
    NEED_SSH=1
  fi
fi

# If the live source hash matches every enabled destination, avoid building a
# snapshot at all. SOURCE_HASH is only a cheap change detector; the hash of the
# actual snapshot becomes authoritative before any publish occurs.
if [[ $NEED_HTTP -eq 0 && $NEED_SSH -eq 0 ]]; then
  exit 0
fi

# Build one consistent, timeline-only SQLite snapshot and reuse it for every
# enabled destination. No recordings, timelapses, screenshots, or raw capture
# tables are copied.
TMP_DIR="$(mktemp -d -t dayflow-timeline)"
TMP="$TMP_DIR/timeline.sqlite"

/usr/bin/sqlite3 "$SRC" <<SQL
.timeout 5000
BEGIN;
ATTACH DATABASE '$TMP' AS mirror;
CREATE TABLE mirror.timeline_cards (
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
INSERT INTO mirror.timeline_cards
  (id, start_ts, end_ts, title, summary, detailed_summary,
   category, subcategory, metadata, is_deleted)
SELECT
  id, start_ts, end_ts, title, summary, detailed_summary,
  category, subcategory, metadata, is_deleted
FROM main.timeline_cards
WHERE is_deleted = 0;
CREATE INDEX mirror.idx_timeline_start ON timeline_cards(start_ts);
CREATE INDEX mirror.idx_timeline_end ON timeline_cards(end_ts);
COMMIT;
SQL

# Recompute the logical hash from the exact SQLite snapshot that will be sent.
# Dayflow may write to chunks.sqlite between the source pre-check above and this
# transaction. Using the snapshot hash here guarantees that:
#   - X-Dayflow-Timeline-Hash matches the uploaded SQLite content;
#   - SSH timeline.sha256 matches timeline.sqlite;
#   - local success state records the content that was actually published.
CURRENT_HASH="$(
  /usr/bin/sqlite3 -batch "$TMP" <<'SQL' \
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

if [[ -z "$CURRENT_HASH" ]]; then
  echo "[$(timestamp)] ERROR: Failed to calculate Dayflow snapshot timeline hash" >&2
  exit 1
fi

# Re-evaluate destination state against the authoritative snapshot hash. The
# source may have changed (or even changed back) while the snapshot was built.
NEED_HTTP=0
NEED_SSH=0

if [[ -n "$HTTP_URL" ]]; then
  if [[ $FORCE -eq 1 || -z "$HTTP_LAST_HASH" || "$CURRENT_HASH" != "$HTTP_LAST_HASH" ]]; then
    NEED_HTTP=1
  fi
fi

if [[ "$SSH_ENABLED" == "1" ]]; then
  if [[ $FORCE -eq 1 || -z "$SSH_LAST_HASH" || "$CURRENT_HASH" != "$SSH_LAST_HASH" ]]; then
    NEED_SSH=1
  fi
fi

if [[ $NEED_HTTP -eq 0 && $NEED_SSH -eq 0 ]]; then
  exit 0
fi

if [[ $FORCE -eq 1 ]]; then
  log "Forced Dayflow timeline sync requested."
else
  log "Dayflow timeline needs publishing."
fi

sync_ssh() {
  local ssh_transport="/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=$SSH_CONNECT_TIMEOUT"

  /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" "$SSH_REMOTE" \
    "mkdir -p '$SSH_REMOTE_DIR' && chmod 700 '$SSH_REMOTE_DIR'" || return 1

  /usr/bin/rsync -az \
    -e "$ssh_transport" \
    "$TMP" \
    "$SSH_REMOTE:$SSH_REMOTE_DIR/timeline.sqlite.new" || return 1

  /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout="$SSH_CONNECT_TIMEOUT" "$SSH_REMOTE" \
    "set -e; \
     mv '$SSH_REMOTE_DIR/timeline.sqlite.new' '$SSH_REMOTE_DIR/timeline.sqlite'; \
     chmod 600 '$SSH_REMOTE_DIR/timeline.sqlite'; \
     printf '%s\\n' '$CURRENT_HASH' > '$SSH_REMOTE_DIR/timeline.sha256.new'; \
     mv '$SSH_REMOTE_DIR/timeline.sha256.new' '$SSH_REMOTE_DIR/timeline.sha256'; \
     chmod 600 '$SSH_REMOTE_DIR/timeline.sha256'; \
     date -u '+%Y-%m-%dT%H:%M:%SZ' > '$SSH_REMOTE_DIR/.last-sync.new'; \
     mv '$SSH_REMOTE_DIR/.last-sync.new' '$SSH_REMOTE_DIR/.last-sync'; \
     chmod 600 '$SSH_REMOTE_DIR/.last-sync'" || return 1
}

sync_http() {
  local token
  local url

  token="$(/bin/cat "$HTTP_TOKEN_FILE" 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    echo "[$(timestamp)] ERROR: HTTP publish token file is empty." >&2
    return 1
  fi

  url="${HTTP_URL%/}/v1/publish"

  # --config - keeps the bearer token out of curl's process arguments.
  {
    print -r -- "header = \"Authorization: Bearer $token\""
    print -r -- "header = \"X-Dayflow-Timeline-Hash: $CURRENT_HASH\""
    print -r -- 'header = "Content-Type: application/octet-stream"'
  } | /usr/bin/curl \
        --config - \
        --fail-with-body \
        --silent \
        --show-error \
        --retry 3 \
        --retry-delay 2 \
        --connect-timeout 10 \
        --max-time 120 \
        --user-agent "$HTTP_USER_AGENT" \
        --request PUT \
        --data-binary "@$TMP" \
        "$url" \
        >/dev/null || return 1
}

FAILED=0

if [[ $NEED_HTTP -eq 1 ]]; then
  if sync_http; then
    write_hash_state "$HTTP_HASH_FILE"
    log "Published Dayflow timeline to HTTP API (sha256=${CURRENT_HASH[1,12]}…)."
  else
    echo "[$(timestamp)] ERROR: HTTP Dayflow publish failed." >&2
    FAILED=1
  fi
fi

if [[ $NEED_SSH -eq 1 ]]; then
  if sync_ssh; then
    write_hash_state "$SSH_HASH_FILE"
    log "Published Dayflow timeline to SSH backup mirror (sha256=${CURRENT_HASH[1,12]}…)."
  else
    echo "[$(timestamp)] ERROR: SSH backup mirror publish failed." >&2
    FAILED=1
  fi
fi

exit $FAILED
