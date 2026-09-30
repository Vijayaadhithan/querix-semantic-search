"""Optional low-overhead memory checkpoints for refresh diagnostics."""

from __future__ import annotations

import logging
import os
import resource
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def log_refresh_memory(phase: str) -> None:
    if os.getenv("ANALYTICS_REFRESH_MEMORY_TRACE") != "1":
        return
    status = Path("/proc/self/status")
    if not status.exists():
        return
    rss_kib = next(
        (
            int(line.split()[1])
            for line in status.read_text().splitlines()
            if line.startswith("VmRSS:")
        ),
        0,
    )
    cgroup = Path("/sys/fs/cgroup/memory.current")
    cgroup_mib = int(cgroup.read_text()) / 1024**2 if cgroup.exists() else None
    peak_rss_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    LOGGER.info(
        "Analytics memory phase=%s process_rss_mib=%.1f "
        "peak_process_rss_mib=%.1f cgroup_mib=%s",
        phase,
        rss_kib / 1024,
        peak_rss_mib,
        f"{cgroup_mib:.1f}" if cgroup_mib is not None else "unavailable",
    )
