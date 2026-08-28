#!/usr/bin/env python3
"""Create missing workload credentials in the root-only local secret file."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
DEFAULT_USERS = {
    "MYSQL_SEARCH_USER": "querix_search",
    "MYSQL_INGEST_USER": "querix_ingest",
    "MYSQL_TELEMETRY_USER": "querix_telemetry",
    "MYSQL_ANALYTICS_USER": "querix_analytics",
    "PGVECTOR_SEARCH_USER": "querix_search",
    "PGVECTOR_INGEST_USER": "querix_ingest",
}
PASSWORD_KEYS = tuple(name.replace("_USER", "_PASSWORD") for name in DEFAULT_USERS)


def _configured(raw_value: str) -> bool:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return bool(value.strip())


def ensure_credentials(path: Path) -> list[str]:
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    if path.stat().st_mode & 0o077:
        raise RuntimeError(f"Secret file must be mode 0600: {path}")
    original_lines = path.read_text(encoding="utf-8").splitlines()
    positions: dict[str, int] = {}
    raw_values: dict[str, str] = {}
    for index, line in enumerate(original_lines):
        match = ASSIGNMENT_RE.match(line.strip())
        if match:
            positions[match.group(1)] = index
            raw_values[match.group(1)] = match.group(2)

    generated: dict[str, str] = {}
    for name, default in DEFAULT_USERS.items():
        if not _configured(raw_values.get(name, "")):
            generated[name] = default
    for name in PASSWORD_KEYS:
        if not _configured(raw_values.get(name, "")):
            generated[name] = secrets.token_urlsafe(36)
    if not _configured(raw_values.get("MYSQL_ROLE_HOST", "")):
        generated["MYSQL_ROLE_HOST"] = "%"
    if not generated:
        return []

    for name, value in generated.items():
        assignment = f"{name}={value}"
        if name in positions:
            original_lines[positions[name]] = assignment
        else:
            if original_lines and original_lines[-1].strip():
                original_lines.append("")
            original_lines.append(assignment)
    content = "\n".join(original_lines) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=".env.keys.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return sorted(generated)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    generated = ensure_credentials(args.root.resolve() / ".env.keys")
    if generated:
        print("Created missing workload credential fields: " + ", ".join(generated))
    else:
        print("Workload credential fields are already configured.")


if __name__ == "__main__":
    main()
