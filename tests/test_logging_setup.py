"""Tests for log formatting and the reserved-field guard."""

from __future__ import annotations

import json
import logging

import pytest

from talabflow.logging_setup import JsonFormatter, configure_logging, safe_extra


def _record(message: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_a_record_renders_as_one_line_of_json() -> None:
    line = JsonFormatter().format(_record("something happened"))
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["message"] == "something happened"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["ts"]


def test_extra_fields_are_included() -> None:
    payload = json.loads(JsonFormatter().format(_record(reference="TF-20260928-K7M2", count=3)))
    assert payload["reference"] == "TF-20260928-K7M2"
    assert payload["count"] == 3


def test_arabic_is_not_escaped_into_unreadable_json() -> None:
    payload = JsonFormatter().format(_record("طلب جديد"))
    assert "طلب جديد" in payload


def test_non_serialisable_values_do_not_break_logging() -> None:
    """A logging call must never be the thing that raises."""
    payload = json.loads(JsonFormatter().format(_record(obj=object())))
    assert "obj" in payload


def test_safe_extra_renames_reserved_field_names() -> None:
    """Regression guard for a real 500.

    ``logger.info(..., extra={"created": username})`` raises
    ``KeyError: Attempt to overwrite 'created' in LogRecord``, because ``created`` is the
    record's own timestamp. The staff-creation endpoint hit exactly this and returned a 500
    *after* successfully creating the user -- the worst kind of failure, since the side effect
    had already happened.
    """
    result = safe_extra(created="a-user", message="x", name="y", reference="TF-1")
    assert result == {
        "created_": "a-user",
        "message_": "x",
        "name_": "y",
        "reference": "TF-1",
    }


@pytest.fixture
def enabled_logger() -> logging.Logger:
    """A logger guaranteed to actually build records.

    Without an explicit level these tests would pass vacuously: the root logger defaults to
    WARNING, so ``logger.info(...)`` short-circuits before ``makeRecord`` is ever called and no
    ``KeyError`` could be raised either way.
    """
    logger = logging.getLogger("talabflow.test.reserved")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.NullHandler())
    return logger


@pytest.mark.parametrize(
    "reserved",
    ["created", "message", "name", "module", "args", "levelname", "process", "filename", "lineno"],
)
def test_logging_with_a_reserved_key_through_safe_extra_does_not_raise(
    reserved: str, enabled_logger: logging.Logger
) -> None:
    enabled_logger.info("event", extra=safe_extra(**{reserved: "value"}))


def test_logging_with_a_reserved_key_directly_still_raises(
    enabled_logger: logging.Logger,
) -> None:
    """Documents *why* safe_extra exists: stdlib logging really does reject this."""
    with pytest.raises(KeyError, match="created"):
        enabled_logger.info("event", extra={"created": "value"})


def test_configure_logging_installs_exactly_one_handler_and_is_idempotent() -> None:
    configure_logging("INFO")
    first = len(logging.getLogger().handlers)
    configure_logging("DEBUG")
    assert len(logging.getLogger().handlers) == first == 1
    assert logging.getLogger().level == logging.DEBUG
    configure_logging("INFO", json_output=False)


def test_the_bot_token_never_reaches_the_log(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression guard for a credential leak.

    ``httpx`` logs every request line at INFO, URL included, and the Telegram Bot API carries the
    bot token *in the URL path*. With the root logger at INFO -- the default -- the token was
    written to the log on every poll and every send, so anyone who could read the logs could take
    over the bot.
    """
    import httpx

    configure_logging("DEBUG")
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True, "result": []})
        ),
        base_url="https://api.telegram.org/bot12345:SECRETTOKEN",
    )
    client.post("/getUpdates", json={})
    client.close()

    assert "SECRETTOKEN" not in capsys.readouterr().err
    configure_logging("INFO", json_output=False)


def test_exceptions_are_rendered_into_the_payload() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]
