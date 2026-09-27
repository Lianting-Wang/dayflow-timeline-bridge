from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

APP_VERSION = "0.3.5"


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc

    if minimum is not None and value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be <= {maximum}")
    return value


DB_PATH = Path(os.environ.get("DAYFLOW_DB", "/dayflow/timeline.sqlite"))
HASH_PATH = Path(os.environ.get("DAYFLOW_HASH", "/dayflow/timeline.sha256"))
LAST_SYNC_PATH = Path(os.environ.get("DAYFLOW_LAST_SYNC", "/dayflow/.last-sync"))

READ_TOKEN_FILE = Path(
    os.environ.get("DAYFLOW_READ_TOKEN_FILE", "/run/secrets/dayflow_read_token")
)
PUBLISH_TOKEN_FILE = Path(
    os.environ.get("DAYFLOW_PUBLISH_TOKEN_FILE", "/run/secrets/dayflow_publish_token")
)

TZ_NAME = os.environ.get("DAYFLOW_TZ", "America/New_York")
DAY_BOUNDARY_HOUR = _env_int(
    "DAYFLOW_DAY_BOUNDARY_HOUR",
    4,
    minimum=0,
    maximum=23,
)
MAX_UPLOAD_MB = _env_int("DAYFLOW_MAX_UPLOAD_MB", 64, minimum=1)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

TZ = ZoneInfo(TZ_NAME)
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

EXPECTED_SCHEMA = [
    ("id", "INTEGER", 0, None, 1),
    ("start_ts", "INTEGER", 1, None, 0),
    ("end_ts", "INTEGER", 1, None, 0),
    ("title", "TEXT", 0, None, 0),
    ("summary", "TEXT", 0, None, 0),
    ("detailed_summary", "TEXT", 0, None, 0),
    ("category", "TEXT", 0, None, 0),
    ("subcategory", "TEXT", 0, None, 0),
    ("metadata", "TEXT", 0, None, 0),
    ("is_deleted", "INTEGER", 1, "0", 0),
]

app = FastAPI(
    title="Dayflow Timeline Bridge",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _read_token_file(path: Path, label: str) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"{label} token unavailable") from exc
    if not token:
        raise HTTPException(status_code=503, detail=f"{label} token unavailable")
    return token


def _supplied_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    return token


def require_read_auth(authorization: str | None = Header(default=None)) -> None:
    supplied = _supplied_bearer(authorization)
    expected = _read_token_file(READ_TOKEN_FILE, "read")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def require_publish_auth(authorization: str | None = Header(default=None)) -> None:
    supplied = _supplied_bearer(authorization)
    expected = _read_token_file(PUBLISH_TOKEN_FILE, "publish")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _connect_path(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise HTTPException(status_code=503, detail="Dayflow mirror unavailable")
    suffix = "?mode=ro"
    if immutable:
        suffix += "&immutable=1"
    uri = f"file:{path}{suffix}"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail="Could not open Dayflow mirror") from exc


def _connect() -> sqlite3.Connection:
    # Fresh read-only connection per request. The live DB is replaced atomically,
    # so this avoids a long-lived connection staying attached to the old inode.
    return _connect_path(DB_PATH)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Date must be YYYY-MM-DD") from exc


def _current_dayflow_day(now: datetime | None = None) -> date:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if now.hour < DAY_BOUNDARY_HOUR:
        return (now - timedelta(days=1)).date()
    return now.date()


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dt_time(hour=DAY_BOUNDARY_HOUR), TZ)
    return start, start + timedelta(days=1)


def _detect_epoch_divisor(sample: int | float | None) -> float:
    if sample is None:
        return 1.0
    value = abs(float(sample))
    now = datetime.now(timezone.utc).timestamp()
    choices = (1.0, 1_000.0, 1_000_000.0, 1_000_000_000.0)
    return min(choices, key=lambda d: abs((value / d) - now))


def _timestamp_divisor(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT MAX(end_ts) AS sample FROM timeline_cards WHERE is_deleted = 0"
    ).fetchone()
    return _detect_epoch_divisor(row["sample"] if row else None)


def _to_db_ts(dt: datetime, divisor: float) -> int:
    return int(round(dt.timestamp() * divisor))


def _from_db_ts(value: int | float, divisor: float) -> datetime:
    return datetime.fromtimestamp(float(value) / divisor, TZ)


def _iso(value: int | float, divisor: float) -> str:
    return _from_db_ts(value, divisor).isoformat(timespec="seconds")


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _find_key(obj: Any, names: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in names:
                return value
        for value in obj.values():
            found = _find_key(value, names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, names)
            if found is not None:
                return found
    return None


def _extract_apps(metadata: Any) -> list[str]:
    obj = _safe_json(metadata)

    # Current Dayflow metadata stores applications/sites under appSites.
    # Preserve primary and secondary as two display strings in
    # a compact primary/secondary display format, e.g.
    # ["VS Code", "ChatGPT, Gmail"].
    if isinstance(obj, dict):
        app_sites = obj.get("appSites")
        if isinstance(app_sites, dict):
            out: list[str] = []
            primary = app_sites.get("primary")
            secondary = app_sites.get("secondary")
            if isinstance(primary, str) and primary.strip():
                out.append(primary.strip())
            if isinstance(secondary, str) and secondary.strip():
                out.append(secondary.strip())
            if out:
                return out

    # Additional accepted metadata shapes.
    found = _find_key(obj, {"apps", "applications", "app_names", "appnames"})
    if found is None:
        return []
    if isinstance(found, str):
        return [found]
    if isinstance(found, list):
        out: list[str] = []
        for item in found:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                name = (
                    item.get("name")
                    or item.get("app")
                    or item.get("application")
                    or item.get("display_name")
                )
                if isinstance(name, str):
                    out.append(name)
        return list(dict.fromkeys(out))
    return []


def _extract_distraction_count(metadata: Any) -> int:
    obj = _safe_json(metadata)
    found = _find_key(
        obj,
        {"distraction_count", "distractions_count", "distractioncount"},
    )
    if isinstance(found, bool):
        return int(found)
    if isinstance(found, (int, float)):
        return max(0, int(found))
    distractions = _find_key(obj, {"distractions"})
    if isinstance(distractions, list):
        return len(distractions)
    return 0


def _activity_summary(row: sqlite3.Row, divisor: float) -> dict[str, Any]:
    start = _from_db_ts(row["start_ts"], divisor)
    end = _from_db_ts(row["end_ts"], divisor)
    duration = max(0, round((end - start).total_seconds() / 60))
    return {
        "record_id": row["id"],
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "duration_minutes": duration,
        "title": row["title"],
        "summary": row["summary"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "apps": _extract_apps(row["metadata"]),
        "distraction_count": _extract_distraction_count(row["metadata"]),
    }


def _read_text_file(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec="seconds")
    except OSError:
        return None


def _logical_timeline_hash(conn: sqlite3.Connection) -> str:
    """
    Reproduce the exact logical SHA-256 used by the Mac sync script:
    one deterministic serialized row per line, ordered by id.
    """
    query = """
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
    ORDER BY id
    """
    digest = hashlib.sha256()
    for row in conn.execute(query):
        digest.update(str(row[0]).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_uploaded_db(path: Path, expected_hash: str) -> dict[str, Any]:
    try:
        conn = _connect_path(path, immutable=True)
    except HTTPException as exc:
        raise HTTPException(status_code=422, detail="Uploaded file is not a readable SQLite database") from exc

    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise HTTPException(status_code=422, detail="SQLite quick_check failed")

        tables = [
            row[0]
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        if tables != ["timeline_cards"]:
            raise HTTPException(
                status_code=422,
                detail="Uploaded database must contain only timeline_cards",
            )

        schema = [
            (row["name"], row["type"].upper(), row["notnull"], row["dflt_value"], row["pk"])
            for row in conn.execute("PRAGMA table_info(timeline_cards)").fetchall()
        ]
        if schema != EXPECTED_SCHEMA:
            raise HTTPException(status_code=422, detail="Unexpected timeline_cards schema")

        invalid_core_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM timeline_cards
            WHERE typeof(id) != 'integer'
               OR typeof(start_ts) != 'integer'
               OR typeof(end_ts) != 'integer'
               OR typeof(is_deleted) != 'integer'
               OR end_ts < start_ts
            """
        ).fetchone()[0]
        if invalid_core_rows:
            raise HTTPException(
                status_code=422,
                detail="Uploaded mirror contains invalid core field types or time ranges",
            )

        deleted_count = conn.execute(
            "SELECT COUNT(*) FROM timeline_cards WHERE is_deleted != 0"
        ).fetchone()[0]
        if deleted_count:
            raise HTTPException(
                status_code=422,
                detail="Uploaded mirror contains deleted rows",
            )

        actual_hash = _logical_timeline_hash(conn)
        if not hmac.compare_digest(actual_hash.lower(), expected_hash.lower()):
            raise HTTPException(
                status_code=422,
                detail="Timeline hash does not match uploaded database",
            )

        divisor = _timestamp_divisor(conn)
        row = conn.execute(
            """
            SELECT COUNT(*) AS card_count, MAX(end_ts) AS latest_activity_end
            FROM timeline_cards
            WHERE is_deleted = 0
            """
        ).fetchone()

        return {
            "card_count": int(row["card_count"] if row else 0),
            "latest_activity_end": (
                _iso(row["latest_activity_end"], divisor)
                if row and row["latest_activity_end"] is not None
                else None
            ),
            "timeline_hash": actual_hash,
        }
    except sqlite3.Error as exc:
        raise HTTPException(status_code=422, detail="SQLite validation failed") from exc
    finally:
        conn.close()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@app.exception_handler(sqlite3.Error)
def sqlite_error_handler(_request, _exc: sqlite3.Error):
    return JSONResponse(status_code=503, content={"detail": "SQLite read failed"})


@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "mirror_present": DB_PATH.is_file(),
        "service": "dayflow-timeline-bridge",
        "version": APP_VERSION,
    }


@app.put("/v1/publish", dependencies=[Depends(require_publish_auth)])
async def publish(
    request: Request,
    x_dayflow_timeline_hash: str | None = Header(
        default=None, alias="X-Dayflow-Timeline-Hash"
    ),
):
    if not x_dayflow_timeline_hash or not HEX64_RE.fullmatch(x_dayflow_timeline_hash):
        raise HTTPException(
            status_code=400,
            detail="X-Dayflow-Timeline-Hash must be a 64-character SHA-256 hex string",
        )
    expected_hash = x_dayflow_timeline_hash.lower()

    # Idempotent fast path. Do not trust the sidecar hash alone: verify that the
    # currently published SQLite content still has the same logical hash.
    current_hash = _read_text_file(HASH_PATH)
    if current_hash and DB_PATH.is_file() and hmac.compare_digest(
        current_hash.lower(), expected_hash
    ):
        try:
            with _connect_path(DB_PATH, immutable=True) as current_conn:
                live_hash = _logical_timeline_hash(current_conn)
        except (HTTPException, sqlite3.Error):
            live_hash = None

        if live_hash and hmac.compare_digest(live_hash.lower(), expected_hash):
            return {
                "ok": True,
                "changed": False,
                "timeline_hash": expected_hash,
                "published_at": _read_text_file(LAST_SYNC_PATH),
            }

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if declared > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Upload too large")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DB_PATH.parent / f".timeline.upload.{uuid.uuid4().hex}.sqlite"

    received = 0
    try:
        with open(tmp_path, "xb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload too large")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

        if received == 0:
            raise HTTPException(status_code=400, detail="Empty upload")

        os.chmod(tmp_path, 0o600)
        validation = _validate_uploaded_db(tmp_path, expected_hash)

        # Publish DB atomically. Existing readers continue on the old inode;
        # future readers open the new mirror.
        os.replace(tmp_path, DB_PATH)
        os.chmod(DB_PATH, 0o600)

        published_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        _atomic_write_text(HASH_PATH, expected_hash + "\n")
        _atomic_write_text(LAST_SYNC_PATH, published_at + "\n")
        _fsync_directory(DB_PATH.parent)

        return {
            "ok": True,
            "changed": True,
            "timeline_hash": expected_hash,
            "published_at": published_at,
            "card_count": validation["card_count"],
            "latest_activity_end": validation["latest_activity_end"],
        }
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


@app.get("/v1/status", dependencies=[Depends(require_read_auth)])
def status():
    if not DB_PATH.is_file():
        return {
            "card_count": 0,
            "database_mtime": None,
            "latest_activity_end": None,
            "timeline_hash": _read_text_file(HASH_PATH),
            "last_sync": _read_text_file(LAST_SYNC_PATH),
            "time_zone": TZ_NAME,
            "day_boundary_hour": DAY_BOUNDARY_HOUR,
            "schema_version": 1,
            "api_version": APP_VERSION,
            "mirror_present": False,
        }

    with _connect() as conn:
        divisor = _timestamp_divisor(conn)
        row = conn.execute(
            """
            SELECT COUNT(*) AS card_count, MAX(end_ts) AS latest_activity_end
            FROM timeline_cards
            WHERE is_deleted = 0
            """
        ).fetchone()

    latest = None
    if row and row["latest_activity_end"] is not None:
        latest = _iso(row["latest_activity_end"], divisor)

    return {
        "card_count": int(row["card_count"] if row else 0),
        "database_mtime": _mtime_iso(DB_PATH),
        "latest_activity_end": latest,
        "timeline_hash": _read_text_file(HASH_PATH),
        "last_sync": _read_text_file(LAST_SYNC_PATH),
        "time_zone": TZ_NAME,
        "day_boundary_hour": DAY_BOUNDARY_HOUR,
        "schema_version": 1,
        "api_version": APP_VERSION,
        "mirror_present": True,
    }


@app.get("/v1/timeline", dependencies=[Depends(require_read_auth)])
def timeline(day: str | None = Query(default=None, alias="date")):
    target = _parse_date(day) if day else _current_dayflow_day()
    start, end = _day_bounds(target)

    with _connect() as conn:
        divisor = _timestamp_divisor(conn)
        start_ts = _to_db_ts(start, divisor)
        end_ts = _to_db_ts(end, divisor)
        rows = conn.execute(
            """
            SELECT id, start_ts, end_ts, title, summary, detailed_summary,
                   category, subcategory, metadata
            FROM timeline_cards
            WHERE is_deleted = 0
              AND end_ts > ?
              AND start_ts < ?
            ORDER BY start_ts ASC, id ASC
            """,
            (start_ts, end_ts),
        ).fetchall()

    return {
        "date": target.isoformat(),
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "activities": [_activity_summary(row, divisor) for row in rows],
        "schema_version": 1,
    }


@app.get("/v1/activity/{record_id}", dependencies=[Depends(require_read_auth)])
def activity_detail(record_id: int):
    with _connect() as conn:
        divisor = _timestamp_divisor(conn)
        row = conn.execute(
            """
            SELECT id, start_ts, end_ts, title, summary, detailed_summary,
                   category, subcategory, metadata
            FROM timeline_cards
            WHERE id = ? AND is_deleted = 0
            """,
            (record_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Activity not found")

    result = _activity_summary(row, divisor)
    result["detailed_summary"] = row["detailed_summary"]
    result["metadata"] = _safe_json(row["metadata"])
    return result


def _escape_like(value: str) -> str:
    # Treat %, _, and backslash literally in the public search API.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/v1/search", dependencies=[Depends(require_read_auth)])
def search(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
):
    pattern = f"%{_escape_like(q)}%"
    with _connect() as conn:
        divisor = _timestamp_divisor(conn)
        rows = conn.execute(
            """
            SELECT id, start_ts, end_ts, title, summary, detailed_summary,
                   category, subcategory, metadata
            FROM timeline_cards
            WHERE is_deleted = 0
              AND (
                    COALESCE(title, '') LIKE ? ESCAPE '\\' COLLATE NOCASE
                 OR COALESCE(summary, '') LIKE ? ESCAPE '\\' COLLATE NOCASE
                 OR COALESCE(detailed_summary, '') LIKE ? ESCAPE '\\' COLLATE NOCASE
              )
            ORDER BY end_ts DESC, id DESC
            LIMIT ?
            """,
            (pattern, pattern, pattern, limit),
        ).fetchall()

    return {
        "query": q,
        "activities": [_activity_summary(row, divisor) for row in rows],
        "schema_version": 1,
    }


@app.get("/v1/time-breakdown", dependencies=[Depends(require_read_auth)])
def time_breakdown(
    from_day: str = Query(alias="from"),
    to_day: str = Query(alias="to"),
):
    first = _parse_date(from_day)
    last = _parse_date(to_day)
    if last < first:
        raise HTTPException(status_code=400, detail="'to' must be >= 'from'")
    if (last - first).days > 366:
        raise HTTPException(status_code=400, detail="Date range too large")

    start, _ = _day_bounds(first)
    _, end = _day_bounds(last)

    with _connect() as conn:
        divisor = _timestamp_divisor(conn)
        start_ts = _to_db_ts(start, divisor)
        end_ts = _to_db_ts(end, divisor)
        rows = conn.execute(
            """
            SELECT id, start_ts, end_ts, category
            FROM timeline_cards
            WHERE is_deleted = 0
              AND end_ts > ?
              AND start_ts < ?
            ORDER BY start_ts ASC
            """,
            (start_ts, end_ts),
        ).fetchall()

    totals: dict[str, float] = {}
    total_minutes = 0.0
    for row in rows:
        row_start = max(_from_db_ts(row["start_ts"], divisor), start)
        row_end = min(_from_db_ts(row["end_ts"], divisor), end)
        minutes = max(0.0, (row_end - row_start).total_seconds() / 60.0)
        category = row["category"] or "Uncategorized"
        totals[category] = totals.get(category, 0.0) + minutes
        total_minutes += minutes

    breakdown = [
        {
            "category": category,
            "minutes": round(minutes),
            "hours": round(minutes / 60.0, 2),
            "share": round(minutes / total_minutes, 4) if total_minutes else 0.0,
        }
        for category, minutes in sorted(
            totals.items(), key=lambda item: item[1], reverse=True
        )
    ]

    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "total_minutes": round(total_minutes),
        "total_hours": round(total_minutes / 60.0, 2),
        "breakdown": breakdown,
        "schema_version": 1,
    }
