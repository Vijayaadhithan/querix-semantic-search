import logging
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any


LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_ASSIGNED_SECRET = re.compile(
    r"(?i)(\b(?:x-api-key|x-admin-key|api[_-]?key|password|passwd|"
    r"token|secret|key)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_AUTHORIZATION_SECRET = re.compile(
    r"(?i)(\bauthorization\b\s*[:=]\s*)(?:bearer\s+)?"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_URL_CREDENTIALS = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s]+(@)"
)
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")


def sanitize_log_message(message: str, *, max_chars: int = 2000) -> str:
    sanitized = message.replace("\r", " ").replace("\n", " ")
    sanitized = _CONTROL_CHARACTERS.sub("", sanitized)
    sanitized = _URL_CREDENTIALS.sub(r"\1[REDACTED]\2", sanitized)
    sanitized = _AUTHORIZATION_SECRET.sub(r"\1[REDACTED]", sanitized)
    sanitized = _ASSIGNED_SECRET.sub(r"\1[REDACTED]", sanitized)
    sanitized = _BEARER_SECRET.sub(r"\1[REDACTED]", sanitized)
    if len(sanitized) > max_chars:
        return f"{sanitized[:max_chars]}...[truncated]"
    return sanitized


class AdminLogBuffer(logging.Handler):
    """Bounded, sanitized application-log feed for authenticated admins."""

    def __init__(self, capacity: int = 1000):
        if capacity < 1:
            raise ValueError("Admin log capacity must be at least one.")
        super().__init__(level=logging.NOTSET)
        self.capacity = capacity
        self.stream_id = uuid.uuid4().hex
        self._events: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._next_id = 1
        self._events_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = sanitize_log_message(record.getMessage())
            if record.name == "uvicorn.access":
                if "/api/v1/admin/logs" in message:
                    return
                successful_health_check = (
                    (
                        "GET /api/v1/ready" in message
                        or "GET /api/v1/live" in message
                    )
                    and message.endswith(" 200")
                )
                if successful_health_check:
                    return
            event = {
                "id": 0,
                "timestamp_utc": datetime.fromtimestamp(
                    record.created,
                    timezone.utc,
                ).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
            with self._events_lock:
                event["id"] = self._next_id
                self._next_id += 1
                self._events.append(event)
        except Exception:
            self.handleError(record)

    def snapshot(
        self,
        *,
        limit: int,
        minimum_level: str,
        after_id: int | None = None,
    ) -> dict[str, Any]:
        normalized_level = minimum_level.upper()
        if normalized_level not in LOG_LEVELS:
            raise ValueError(f"Unsupported log level: {minimum_level}")
        threshold = LOG_LEVELS[normalized_level]
        with self._events_lock:
            events = list(self._events)

        eligible = [
            event
            for event in events
            if LOG_LEVELS.get(event["level"], logging.INFO) >= threshold
            and (after_id is None or event["id"] > after_id)
        ]
        if after_id is None:
            selected = eligible[-limit:]
        else:
            selected = eligible[:limit]
        latest_id = events[-1]["id"] if events else 0
        oldest_id = events[0]["id"] if events else 0
        next_after_id = (
            selected[-1]["id"]
            if selected
            else (after_id if after_id is not None else latest_id)
        )
        return {
            "stream_id": self.stream_id,
            "capacity": self.capacity,
            "retained": len(events),
            "oldest_id": oldest_id,
            "latest_id": latest_id,
            "next_after_id": next_after_id,
            "returned": len(selected),
            "has_more": len(eligible) > len(selected),
            "events": selected,
        }
