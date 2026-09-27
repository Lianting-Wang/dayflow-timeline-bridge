#!/usr/bin/env python3
import secrets
from pathlib import Path

secret_dir = Path("secrets")
targets = [
    secret_dir / "dayflow_read_token",
    secret_dir / "dayflow_publish_token",
]

existing = [path for path in targets if path.exists()]
if existing:
    joined = ", ".join(str(path) for path in existing)
    raise SystemExit(
        f"Refusing to create tokens because target file(s) already exist: {joined}. "
        "Remove the existing token file(s) explicitly if you intend to rotate credentials."
    )

secret_dir.mkdir(parents=True, exist_ok=True)
secret_dir.chmod(0o700)

for path in targets:
    path.write_text(secrets.token_urlsafe(48) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"Wrote {path}")
