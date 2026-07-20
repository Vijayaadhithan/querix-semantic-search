import logging

from admin_logs import AdminLogBuffer, sanitize_log_message


def test_sanitize_log_message_redacts_common_credentials():
    message = sanitize_log_message(
        "Authorization: Bearer abc123 "
        "X-API-Key=customer-secret password='database-secret' "
        "mysql://user:plain-password@example.test/catalog"
    )

    assert "abc123" not in message
    assert "customer-secret" not in message
    assert "database-secret" not in message
    assert "plain-password" not in message
    assert message.count("[REDACTED]") == 4


def test_admin_log_buffer_is_bounded_and_supports_incremental_polling():
    buffer = AdminLogBuffer(capacity=2)
    logger = logging.getLogger("admin-log-buffer-test")
    original_level = logger.level
    original_propagate = logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(buffer)
    try:
        logger.info("first")
        logger.warning("second token=private-token")
        logger.error("third")

        latest = buffer.snapshot(limit=10, minimum_level="INFO")
        assert latest["retained"] == 2
        assert [event["message"] for event in latest["events"]] == [
            "second token=[REDACTED]",
            "third",
        ]
        assert latest["oldest_id"] == 2
        assert latest["latest_id"] == 3

        warnings = buffer.snapshot(
            limit=1,
            minimum_level="WARNING",
            after_id=1,
        )
        assert warnings["has_more"] is True
        assert warnings["next_after_id"] == 2
        assert warnings["events"][0]["level"] == "WARNING"

        remaining = buffer.snapshot(
            limit=10,
            minimum_level="WARNING",
            after_id=warnings["next_after_id"],
        )
        assert remaining["has_more"] is False
        assert [event["level"] for event in remaining["events"]] == [
            "ERROR"
        ]
    finally:
        logger.removeHandler(buffer)
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        buffer.close()
