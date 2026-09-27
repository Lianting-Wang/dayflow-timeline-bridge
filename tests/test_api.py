from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_upload_mb: int | str = 64,
    day_boundary_hour: int | str = 4,
):
    data = tmp_path / "data"
    secrets = tmp_path / "secrets"
    data.mkdir()
    secrets.mkdir()
    (secrets / "read").write_text("read-secret\n", encoding="utf-8")
    (secrets / "publish").write_text("publish-secret\n", encoding="utf-8")

    env = {
        "DAYFLOW_DB": str(data / "timeline.sqlite"),
        "DAYFLOW_HASH": str(data / "timeline.sha256"),
        "DAYFLOW_LAST_SYNC": str(data / ".last-sync"),
        "DAYFLOW_READ_TOKEN_FILE": str(secrets / "read"),
        "DAYFLOW_PUBLISH_TOKEN_FILE": str(secrets / "publish"),
        "DAYFLOW_TZ": "America/New_York",
        "DAYFLOW_DAY_BOUNDARY_HOUR": str(day_boundary_hour),
        "DAYFLOW_MAX_UPLOAD_MB": str(max_upload_mb),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    module_name = f"dayflow_bridge_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / "app/main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, TestClient(module.app), data


def _make_db(path: Path, rows: list[tuple], *, extra_table: bool = False, bad_schema: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        if bad_schema:
            conn.execute(
                "CREATE TABLE timeline_cards (id INTEGER PRIMARY KEY, start_ts INTEGER, end_ts INTEGER)"
            )
        else:
            conn.execute(
                """
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
                )
                """
            )
            conn.executemany(
                "INSERT INTO timeline_cards VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.execute("CREATE INDEX idx_timeline_start ON timeline_cards(start_ts)")
            conn.execute("CREATE INDEX idx_timeline_end ON timeline_cards(end_ts)")
        if extra_table:
            conn.execute("CREATE TABLE extra(x INTEGER)")


def _logical_hash(path: Path) -> str:
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
    with sqlite3.connect(path) as conn:
        for (serialized,) in conn.execute(query):
            digest.update(serialized.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _rows(*, multiplier: int = 1) -> list[tuple]:
    tz = ZoneInfo("America/New_York")

    def ts(hour: int, minute: int = 0) -> int:
        value = int(datetime.datetime(2026, 9, 26, hour, minute, tzinfo=tz).timestamp())
        return value * multiplier

    return [
        (
            1,
            ts(3, 50),
            ts(4, 10),
            "Cross boundary",
            "summary one",
            "detail one",
            "Research",
            "Coding",
            json.dumps(
                {
                    "appSites": {"primary": "VS Code", "secondary": "ChatGPT, Gmail"},
                    "distractions": [{"id": "x"}],
                }
            ),
            0,
        ),
        (
            2,
            ts(5, 0),
            ts(5, 30),
            "Alpha hurricane",
            "summary two",
            "detail searchable",
            "Research",
            "Analysis",
            json.dumps({"appSites": {"primary": "Python"}}),
            0,
        ),
        (
            3,
            ts(5, 20),
            ts(5, 40),
            "Overlap",
            "summary three",
            None,
            "Admin",
            None,
            json.dumps({"distraction_count": 2}),
            0,
        ),
    ]


def _publish_headers(timeline_hash: str, token: str = "publish-secret") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Dayflow-Timeline-Hash": timeline_hash,
        "Content-Type": "application/octet-stream",
    }


@pytest.fixture
def bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module, client, data = _load_app(tmp_path, monkeypatch)
    source = tmp_path / "good.sqlite"
    _make_db(source, _rows())
    timeline_hash = _logical_hash(source)
    return module, client, data, source, timeline_hash


def test_auth_publish_read_and_idempotency(bridge):
    _, client, data, source, timeline_hash = bridge
    read_headers = {"Authorization": "Bearer read-secret"}

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["mirror_present"] is False

    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers={"Authorization": "Bearer publish-secret"}).status_code == 401
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers(timeline_hash, "read-secret"),
            content=source.read_bytes(),
        ).status_code
        == 401
    )

    response = client.put(
        "/v1/publish",
        headers=_publish_headers(timeline_hash),
        content=source.read_bytes(),
    )
    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert (data / "timeline.sha256").read_text().strip() == timeline_hash
    assert (data / ".last-sync").read_text().strip()

    response = client.put(
        "/v1/publish",
        headers=_publish_headers(timeline_hash),
        content=source.read_bytes(),
    )
    assert response.status_code == 200
    assert response.json()["changed"] is False

    status = client.get("/v1/status", headers=read_headers).json()
    assert status["card_count"] == 3
    assert status["timeline_hash"] == timeline_hash
    assert status["api_version"] == "0.3.5"


def test_timeline_activity_search_and_breakdown(bridge):
    _, client, _, source, timeline_hash = bridge
    client.put("/v1/publish", headers=_publish_headers(timeline_hash), content=source.read_bytes())
    read_headers = {"Authorization": "Bearer read-secret"}

    timeline = client.get("/v1/timeline?date=2026-09-26", headers=read_headers)
    assert timeline.status_code == 200
    activities = timeline.json()["activities"]
    assert len(activities) == 3
    assert activities[0]["start"] == "2026-09-26T03:50:00-04:00"
    assert activities[0]["apps"] == ["VS Code", "ChatGPT, Gmail"]
    assert activities[0]["distraction_count"] == 1

    detail = client.get("/v1/activity/2", headers=read_headers)
    assert detail.status_code == 200
    assert detail.json()["detailed_summary"] == "detail searchable"
    assert client.get("/v1/activity/999", headers=read_headers).status_code == 404

    search = client.get("/v1/search?q=searchable&limit=10", headers=read_headers)
    assert [item["record_id"] for item in search.json()["activities"]] == [2]
    search = client.get("/v1/search?q=hurricane&limit=10", headers=read_headers)
    assert search.json()["activities"][0]["record_id"] == 2

    breakdown = client.get(
        "/v1/time-breakdown?from=2026-09-26&to=2026-09-26",
        headers=read_headers,
    )
    assert breakdown.status_code == 200
    assert breakdown.json()["total_minutes"] == 60


def test_search_treats_sql_like_wildcards_as_literals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _, client, _ = _load_app(tmp_path, monkeypatch)
    source = tmp_path / "search-literals.sqlite"
    rows = _rows()

    first = list(rows[0])
    first[3] = "100% complete"
    first[4] = r"path\name"
    rows[0] = tuple(first)

    second = list(rows[1])
    second[3] = "under_score"
    rows[1] = tuple(second)

    _make_db(source, rows)
    timeline_hash = _logical_hash(source)
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers(timeline_hash),
            content=source.read_bytes(),
        ).status_code
        == 200
    )

    headers = {"Authorization": "Bearer read-secret"}

    percent = client.get("/v1/search", params={"q": "%"}, headers=headers).json()
    assert [item["record_id"] for item in percent["activities"]] == [1]

    underscore = client.get("/v1/search", params={"q": "_"}, headers=headers).json()
    assert [item["record_id"] for item in underscore["activities"]] == [2]

    backslash = client.get("/v1/search", params={"q": "\\"}, headers=headers).json()
    assert [item["record_id"] for item in backslash["activities"]] == [1]


def test_validation_rejections_preserve_live_database(bridge, tmp_path: Path):
    _, client, _, source, timeline_hash = bridge
    read_headers = {"Authorization": "Bearer read-secret"}
    client.put("/v1/publish", headers=_publish_headers(timeline_hash), content=source.read_bytes())

    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers("0" * 64),
            content=source.read_bytes(),
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/v1/publish",
            headers={**_publish_headers(timeline_hash), "X-Dayflow-Timeline-Hash": "abc"},
            content=source.read_bytes(),
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers("1" * 64),
            content=b"not sqlite",
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers("2" * 64),
            content=b"",
        ).status_code
        == 400
    )

    bad_schema = tmp_path / "bad-schema.sqlite"
    _make_db(bad_schema, [], bad_schema=True)
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers("3" * 64),
            content=bad_schema.read_bytes(),
        ).status_code
        == 422
    )

    changed_rows = _rows()
    first = list(changed_rows[0])
    first[3] = "Different title"
    changed_rows[0] = tuple(first)
    extra_table = tmp_path / "extra-table.sqlite"
    _make_db(extra_table, changed_rows, extra_table=True)
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers(_logical_hash(extra_table)),
            content=extra_table.read_bytes(),
        ).status_code
        == 422
    )


    invalid_timestamp = tmp_path / "invalid-timestamp.sqlite"
    _make_db(invalid_timestamp, _rows())
    with sqlite3.connect(invalid_timestamp) as conn:
        conn.execute("UPDATE timeline_cards SET start_ts = 'not-a-timestamp' WHERE id = 1")
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers(_logical_hash(invalid_timestamp)),
            content=invalid_timestamp.read_bytes(),
        ).status_code
        == 422
    )

    negative_duration = tmp_path / "negative-duration.sqlite"
    _make_db(negative_duration, _rows())
    with sqlite3.connect(negative_duration) as conn:
        conn.execute("UPDATE timeline_cards SET end_ts = start_ts - 1 WHERE id = 1")
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers(_logical_hash(negative_duration)),
            content=negative_duration.read_bytes(),
        ).status_code
        == 422
    )

    deleted_row = list(_rows()[0])
    deleted_row[-1] = 1
    deleted_db = tmp_path / "deleted.sqlite"
    _make_db(deleted_db, [tuple(deleted_row)])
    assert (
        client.put(
            "/v1/publish",
            headers=_publish_headers(_logical_hash(deleted_db)),
            content=deleted_db.read_bytes(),
        ).status_code
        == 422
    )

    status = client.get("/v1/status", headers=read_headers).json()
    assert status["timeline_hash"] == timeline_hash
    assert status["card_count"] == 3


def test_input_validation(bridge):
    _, client, _, _, _ = bridge
    read_headers = {"Authorization": "Bearer read-secret"}
    assert client.get("/v1/timeline?date=bad", headers=read_headers).status_code == 400
    assert (
        client.get(
            "/v1/time-breakdown?from=2026-09-27&to=2026-09-26",
            headers=read_headers,
        ).status_code
        == 400
    )
    assert client.get("/v1/search?q=x&limit=201", headers=read_headers).status_code == 422


def test_millisecond_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, client, _, = _load_app(tmp_path, monkeypatch)
    source = tmp_path / "milliseconds.sqlite"
    _make_db(source, _rows(multiplier=1000))
    timeline_hash = _logical_hash(source)
    response = client.put(
        "/v1/publish",
        headers=_publish_headers(timeline_hash),
        content=source.read_bytes(),
    )
    assert response.status_code == 200

    timeline = client.get(
        "/v1/timeline?date=2026-09-26",
        headers={"Authorization": "Bearer read-secret"},
    ).json()
    assert timeline["activities"][1]["start"] == "2026-09-26T05:00:00-04:00"
    assert timeline["activities"][1]["duration_minutes"] == 30


def test_upload_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, client, _ = _load_app(tmp_path, monkeypatch, max_upload_mb=1)
    response = client.put(
        "/v1/publish",
        headers=_publish_headers("4" * 64),
        content=b"x" * (1024 * 1024 + 1),
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"day_boundary_hour": 24}, "DAYFLOW_DAY_BOUNDARY_HOUR must be <= 23"),
        ({"day_boundary_hour": -1}, "DAYFLOW_DAY_BOUNDARY_HOUR must be >= 0"),
        ({"day_boundary_hour": "bad"}, "DAYFLOW_DAY_BOUNDARY_HOUR must be an integer"),
        ({"max_upload_mb": 0}, "DAYFLOW_MAX_UPLOAD_MB must be >= 1"),
        ({"max_upload_mb": "bad"}, "DAYFLOW_MAX_UPLOAD_MB must be an integer"),
    ],
)
def test_invalid_startup_configuration_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict,
    message: str,
):
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _load_app(tmp_path, monkeypatch, **kwargs)
