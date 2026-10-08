from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pymysql
import pytest
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analytics_retry", ROOT / "scripts" / "analytics_retry.py"
)
retry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retry)


@pytest.mark.parametrize("code", [2002, 2003, 2006, 2013, 2055])
def test_mysql_transient_errors_wrapped_by_sqlalchemy(code):
    error = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(code, "test"))
    assert retry.is_transient_database_error(error)


@pytest.mark.parametrize("code", [1045, 1049, 1054, 1064, 1146])
def test_mysql_permanent_errors_do_not_retry(code):
    error = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(code, "test"))
    assert not retry.is_transient_database_error(error)


def test_exception_chain_cycles_and_non_database_timeouts():
    error = TimeoutError("not a database failure")
    error.__cause__ = error
    assert not retry.is_transient_database_error(error)


@pytest.mark.parametrize("code,expected", [(2003, 75), (1045, 1)])
def test_worker_returns_distinct_transient_exit_code(tmp_path, code, expected):
    worker = tmp_path / "worker.py"
    worker.write_text(
        f"import pymysql\nraise pymysql.err.OperationalError({code}, 'test')\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/analytics_retry.py"),
            "run-script",
            str(worker),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected


@pytest.fixture
def scheduled_runner(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "render_service_env.py").write_text("pass\n")
    shutil.copy(ROOT / "scripts/analytics_retry.py", scripts)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "flock").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "path = Path(os.environ['FAKE_DOCKER_STATE'])\n"
        "state = json.loads(path.read_text())\n"
        "assert sys.argv[1:] == ['reset-failed', 'semantic-search-analytics.service']\n"
        "state['calls'].append('clear-failed')\n"
        "path.write_text(json.dumps(state))\n"
    )
    (bin_dir / "docker").write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "path = Path(os.environ['FAKE_DOCKER_STATE'])\n"
        "state = json.loads(path.read_text())\n"
        "args = sys.argv[1:]\n"
        "stage = ('probe' if 'probe' in args else 'upload' if 'telemetry-uploader' "
        "in args else 'refresh' if 'analytics-refresh' in args else 'other')\n"
        "state['calls'].append(stage)\n"
        "codes = state['codes'].get(stage, [])\n"
        "code = codes.pop(0) if codes else 0\n"
        "path.write_text(json.dumps(state))\n"
        "sys.exit(code)\n"
    )
    for executable in bin_dir.iterdir():
        executable.chmod(0o755)
    state_path = tmp_path / "state.json"
    recovery = project / ".runtime/analytics-recovery/gainr"

    def run(codes, *, recovery_only=False, pending=None):
        state_path.write_text(json.dumps({"codes": codes, "calls": []}))
        if pending:
            recovery.mkdir(parents=True, exist_ok=True)
            (recovery / f"{pending}.pending").touch()
        environment = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PROJECT_DIR": str(project),
            "COMPANY_ID": "gainr",
            "LOCK_FILE": str(tmp_path / "lock"),
            "ANALYTICS_RETRY_DELAYS": "0 0 0",
            "ANALYTICS_RECOVERY_ONLY": str(int(recovery_only)),
            "FAKE_DOCKER_STATE": str(state_path),
        }
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/run_scheduled_analytics.sh")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, json.loads(state_path.read_text())["calls"], recovery

    return run


def test_upload_recovers_then_refreshes(scheduled_runner):
    result, calls, recovery = scheduled_runner({"upload": [75, 75, 0]})
    assert result.returncode == 0, result.stderr
    assert calls == ["upload", "upload", "upload", "refresh", "other"]
    assert not list(recovery.glob("*.pending"))


def test_exhausted_upload_preserves_catchup_and_skips_refresh(scheduled_runner):
    result, calls, recovery = scheduled_runner({"upload": [75] * 4})
    assert result.returncode == 75
    assert calls == ["upload"] * 4
    assert (recovery / "upload.pending").exists()


def test_permanent_failure_clears_old_catchup(scheduled_runner):
    result, calls, recovery = scheduled_runner({"upload": [1]}, pending="refresh")
    assert result.returncode == 1
    assert calls == ["upload"]
    assert not list(recovery.glob("*.pending"))


def test_exhausted_refresh_is_recorded(scheduled_runner):
    result, calls, recovery = scheduled_runner({"refresh": [75] * 4})
    assert result.returncode == 75
    assert calls == ["upload", *(["refresh"] * 4), "other"]
    assert (recovery / "refresh.pending").exists()


def test_recovery_does_nothing_without_pending_work(scheduled_runner):
    result, calls, _ = scheduled_runner({}, recovery_only=True)
    assert result.returncode == 0
    assert calls == []


def test_recovery_waits_until_connection_returns(scheduled_runner):
    result, calls, recovery = scheduled_runner(
        {"probe": [75]}, recovery_only=True, pending="upload"
    )
    assert result.returncode == 0
    assert calls == ["probe"]
    assert (recovery / "upload.pending").exists()
    result, calls, recovery = scheduled_runner({}, recovery_only=True)
    assert result.returncode == 0
    assert calls == ["probe", "upload", "refresh", "other", "clear-failed"]
    assert not list(recovery.glob("*.pending"))


def test_permanent_probe_stops_recovery_loop(scheduled_runner):
    result, calls, recovery = scheduled_runner(
        {"probe": [1]}, recovery_only=True, pending="refresh"
    )
    assert result.returncode == 1
    assert calls == ["probe"]
    assert not list(recovery.glob("*.pending"))
