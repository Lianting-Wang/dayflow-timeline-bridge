from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_generator(cwd: Path, *, check: bool = False):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/generate_tokens.py")],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def test_token_generation_and_overwrite_protection(tmp_path: Path):
    first = _run_generator(tmp_path, check=True)
    assert "dayflow_read_token" in first.stdout
    assert "dayflow_publish_token" in first.stdout

    for name in ("dayflow_read_token", "dayflow_publish_token"):
        path = tmp_path / "secrets" / name
        assert path.read_text(encoding="utf-8").strip()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    second = _run_generator(tmp_path)
    assert second.returncode != 0
    assert "Refusing to create tokens" in second.stderr


def test_token_generator_is_all_or_nothing_if_one_target_exists(tmp_path: Path):
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    existing = secret_dir / "dayflow_publish_token"
    existing.write_text("existing-token\n", encoding="utf-8")

    result = _run_generator(tmp_path)

    assert result.returncode != 0
    assert "Refusing to create tokens" in result.stderr
    assert existing.read_text(encoding="utf-8") == "existing-token\n"
    assert not (secret_dir / "dayflow_read_token").exists()
