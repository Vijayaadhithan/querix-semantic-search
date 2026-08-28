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
MYSQL_DEFAULT_USERS = {
    "MYSQL_SEARCH_USER": "querix_search",
    "MYSQL_INGEST_USER": "querix_ingest",
    "MYSQL_TELEMETRY_USER": "querix_telemetry",
    "MYSQL_ANALYTICS_USER": "querix_analytics",
}
PGVECTOR_DEFAULT_USERS = {
    "PGVECTOR_SEARCH_USER": "querix_search",
    "PGVECTOR_INGEST_USER": "querix_ingest",
}
MYSQL_WORKLOAD_CREDENTIAL_MODES = {"dedicated", "shared"}


def _configured(raw_value: str) -> bool:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return bool(value.strip())


def _configured_value(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value.strip()


def _setting_from_files(root: Path, name: str) -> str | None:
    configured = None
    for path in (root / ".env", root / ".env.keys"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = ASSIGNMENT_RE.match(line.strip())
            if match and match.group(1) == name:
                configured = _configured_value(match.group(2)) or None
    return configured


def ensure_credentials(
    path: Path,
    *,
    mysql_mode: str = "dedicated",
    persist_mysql_mode: bool = False,
    service_api_keys: tuple[str, ...] = (),
) -> list[str]:
    mysql_mode = mysql_mode.casefold()
    if mysql_mode not in MYSQL_WORKLOAD_CREDENTIAL_MODES:
        expected = ", ".join(sorted(MYSQL_WORKLOAD_CREDENTIAL_MODES))
        raise RuntimeError(f"MySQL credential mode must be one of: {expected}")
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

    defaults = dict(PGVECTOR_DEFAULT_USERS)
    if mysql_mode == "dedicated":
        defaults.update(MYSQL_DEFAULT_USERS)
    password_keys = tuple(name.replace("_USER", "_PASSWORD") for name in defaults)

    generated: dict[str, str] = {}
    for name in service_api_keys:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*_ANALYTICS_API_KEY", name):
            raise RuntimeError(f"Unsafe analytics API key field: {name}")
        if not _configured(raw_values.get(name, "")):
            generated[name] = secrets.token_urlsafe(36)
    if (
        persist_mysql_mode
        and _configured_value(
            raw_values.get("MYSQL_WORKLOAD_CREDENTIAL_MODE", "")
        ).casefold()
        != mysql_mode
    ):
        generated["MYSQL_WORKLOAD_CREDENTIAL_MODE"] = mysql_mode
    for name, default in defaults.items():
        if not _configured(raw_values.get(name, "")):
            generated[name] = default
    for name in password_keys:
        if not _configured(raw_values.get(name, "")):
            generated[name] = secrets.token_urlsafe(36)
    if mysql_mode == "dedicated" and not _configured(
        raw_values.get("MYSQL_ROLE_HOST", "")
    ):
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


def _service_api_keys_from_example(root: Path) -> tuple[str, ...]:
    path = root / ".env.keys.example"
    if not path.is_file():
        return ()
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ASSIGNMENT_RE.match(line.strip())
        if match and match.group(1).endswith("_ANALYTICS_API_KEY"):
            names.append(match.group(1))
    return tuple(sorted(set(names)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--mysql-mode",
        choices=sorted(MYSQL_WORKLOAD_CREDENTIAL_MODES),
        help="Persist an explicit MySQL workload credential mode in .env.keys.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    mysql_mode = args.mysql_mode or _setting_from_files(
        root,
        "MYSQL_WORKLOAD_CREDENTIAL_MODE",
    )
    generated = ensure_credentials(
        root / ".env.keys",
        mysql_mode=mysql_mode or "dedicated",
        persist_mysql_mode=args.mysql_mode is not None,
        service_api_keys=_service_api_keys_from_example(root),
    )
    if generated:
        print("Created missing workload credential fields: " + ", ".join(generated))
    else:
        print("Workload credential fields are already configured.")


if __name__ == "__main__":
    main()
