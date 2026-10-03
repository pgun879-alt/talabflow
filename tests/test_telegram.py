"""Tests for the Telegram transport.

Driven through an in-process ``httpx`` mock transport, so request shape, error mapping, offset
persistence and update parsing are genuinely exercised without a bot token or a network call.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from talabflow.transports.base import (
    OutboundMessage,
    PermanentTransportError,
    TransportError,
)
from talabflow.transports.telegram import (
    MAX_TELEGRAM_MESSAGE,
    TelegramTransport,
    _parse_message,
    _split_text,
)

BASE = "https://api.telegram.org/bot12345:TESTTOKEN"


def _transport(
    handler: httpx.MockTransport, *, offset_path: Path | None = None
) -> TelegramTransport:
    built = TelegramTransport(
        bot_token="12345:TESTTOKEN", timeout_seconds=5.0, offset_path=offset_path
    )
    built._client = httpx.Client(transport=handler, base_url=BASE, timeout=5.0)
    return built


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _error(status: int, description: str, **extra: object) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "description": description, **extra})


def _message(text: str = "hello", *, user_id: int = 42, chat_id: int = 42) -> dict:
    return {
        "message_id": 7,
        "text": text,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "first_name": "Amina", "last_name": "B"},
    }


# --------------------------------------------------------------------- construction


def test_an_empty_token_is_refused() -> None:
    with pytest.raises(ValueError, match="token is required"):
        TelegramTransport(bot_token="")


# --------------------------------------------------------------------- polling


def test_poll_parses_a_text_message() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _ok([{"update_id": 100, "message": _message("I need a repair")}])

    transport = _transport(httpx.MockTransport(handler))
    (message,) = transport.poll(timeout_seconds=25)
    assert message.text == "I need a repair"
    assert message.channel == "telegram"
    assert message.user_id == "42"
    assert message.chat_id == "42"
    assert message.display_name == "Amina B"
    assert captured["path"].endswith("/getUpdates")
    # Only the update type that is actually handled is requested; anything else would be
    # acknowledged by the offset and silently dropped.
    assert captured["body"]["allowed_updates"] == ["message"]
    transport.close()


def test_poll_advances_the_offset_past_processed_updates() -> None:
    """getUpdates only acknowledges an update once a higher offset is requested. Without this,
    a restart replays the last batch and the customer is asked the same question twice."""
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(json.loads(request.content)["offset"])
        if len(offsets) == 1:
            return _ok([{"update_id": 100, "message": _message()}])
        return _ok([])

    transport = _transport(httpx.MockTransport(handler))
    transport.poll(timeout_seconds=0)
    transport.poll(timeout_seconds=0)
    assert offsets == [0, 101]
    transport.close()


def test_the_offset_is_persisted_across_instances(tmp_path: Path) -> None:
    offset_file = tmp_path / "offset.txt"

    first = _transport(
        httpx.MockTransport(lambda r: _ok([{"update_id": 500, "message": _message()}])),
        offset_path=offset_file,
    )
    first.poll(timeout_seconds=0)
    first.close()
    assert offset_file.read_text().strip() == "501"

    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["offset"])
        return _ok([])

    second = _transport(httpx.MockTransport(handler), offset_path=offset_file)
    second.poll(timeout_seconds=0)
    assert seen == [501], "a restart must not replay already-handled updates"
    second.close()


def test_a_corrupt_offset_file_falls_back_to_zero(tmp_path: Path) -> None:
    offset_file = tmp_path / "offset.txt"
    offset_file.write_text("not-a-number")
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["offset"])
        return _ok([])

    transport = _transport(httpx.MockTransport(handler), offset_path=offset_file)
    transport.poll(timeout_seconds=0)
    assert seen == [0]
    transport.close()


def test_updates_without_text_are_skipped_but_still_acknowledged() -> None:
    """A sticker or a join notification must not crash the poll loop or be replayed forever."""
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offsets.append(json.loads(request.content)["offset"])
        if len(offsets) == 1:
            return _ok(
                [
                    {"update_id": 1, "message": {"message_id": 1, "sticker": {"id": "x"}}},
                    {"update_id": 2, "message": _message("real text")},
                    {"update_id": 3, "edited_message": _message("an edit")},
                ]
            )
        return _ok([])

    transport = _transport(httpx.MockTransport(handler))
    messages = transport.poll(timeout_seconds=0)
    assert [m.text for m in messages] == ["real text"]
    transport.poll(timeout_seconds=0)
    assert offsets[1] == 4, "the skipped updates must still be acknowledged"
    transport.close()


def test_an_empty_update_list_is_not_an_error() -> None:
    transport = _transport(httpx.MockTransport(lambda r: _ok([])))
    assert transport.poll(timeout_seconds=0) == []
    transport.close()


def test_an_unexpected_payload_shape_raises() -> None:
    transport = _transport(httpx.MockTransport(lambda r: _ok({"not": "a list"})))
    with pytest.raises(TransportError, match="unexpected payload"):
        transport.poll(timeout_seconds=0)
    transport.close()


# --------------------------------------------------------------------- sending


def test_send_posts_plain_text_without_a_parse_mode() -> None:
    """Customer text is echoed back in confirmations. With parse_mode set, a stray underscore
    or bracket would break delivery or inject formatting."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return _ok({"message_id": 99})

    transport = _transport(httpx.MockTransport(handler))
    assert transport.send(OutboundMessage(chat_id="42", text="Your order *is* _ready_")) == "99"
    assert captured["path"].endswith("/sendMessage")
    assert "parse_mode" not in captured["body"]
    assert captured["body"]["text"] == "Your order *is* _ready_"
    transport.close()


def test_a_long_message_is_split_rather_than_truncated() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return _ok({"message_id": len(sent)})

    transport = _transport(httpx.MockTransport(handler))
    transport.send(OutboundMessage(chat_id="42", text="x" * (MAX_TELEGRAM_MESSAGE + 500)))
    assert len(sent) == 2
    assert all(len(part) <= MAX_TELEGRAM_MESSAGE for part in sent)
    assert sum(len(part) for part in sent) == MAX_TELEGRAM_MESSAGE + 500
    transport.close()


# --------------------------------------------------------------------- error mapping


@pytest.mark.parametrize(
    ("status", "description"),
    [
        (401, "Unauthorized"),
        (403, "Forbidden: bot was blocked by the user"),
        (400, "Bad Request: chat not found"),
        (400, "Forbidden: user is deactivated"),
    ],
)
def test_unrecoverable_errors_are_permanent(status: int, description: str) -> None:
    """A permanent error must not burn through the outbox retry budget."""
    transport = _transport(httpx.MockTransport(lambda r: _error(status, description)))
    with pytest.raises(PermanentTransportError):
        transport.send(OutboundMessage(chat_id="42", text="hello"))
    transport.close()


def test_rate_limiting_is_transient_and_reports_the_retry_delay() -> None:
    transport = _transport(
        httpx.MockTransport(
            lambda r: _error(429, "Too Many Requests", parameters={"retry_after": 12})
        )
    )
    with pytest.raises(TransportError, match="retry after 12s") as info:
        transport.send(OutboundMessage(chat_id="42", text="hello"))
    assert not isinstance(info.value, PermanentTransportError)
    transport.close()


def test_a_server_error_is_transient() -> None:
    transport = _transport(httpx.MockTransport(lambda r: _error(500, "Internal Server Error")))
    with pytest.raises(TransportError) as info:
        transport.send(OutboundMessage(chat_id="42", text="hello"))
    assert not isinstance(info.value, PermanentTransportError)
    transport.close()


def test_a_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    transport = _transport(httpx.MockTransport(handler))
    with pytest.raises(TransportError, match="timed out"):
        transport.send(OutboundMessage(chat_id="42", text="hello"))
    transport.close()


def test_a_connection_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = _transport(httpx.MockTransport(handler))
    with pytest.raises(TransportError, match="failed"):
        transport.poll(timeout_seconds=0)
    transport.close()


def test_a_non_json_body_is_reported_clearly() -> None:
    transport = _transport(httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(TransportError, match="non-JSON"):
        transport.send(OutboundMessage(chat_id="42", text="hello"))
    transport.close()


# --------------------------------------------------------------------- parsing helpers


def test_parse_message_rejects_anything_without_text() -> None:
    assert _parse_message(None) is None
    assert _parse_message("a string") is None
    assert _parse_message({}) is None
    assert _parse_message({"text": "hi"}) is None  # no chat or sender
    assert _parse_message({"chat": {"id": 1}, "from": {"id": 2}}) is None  # no text


def test_parse_message_falls_back_to_the_username_for_a_display_name() -> None:
    parsed = _parse_message(
        {"message_id": 1, "text": "hi", "chat": {"id": 5}, "from": {"id": 6, "username": "amina_b"}}
    )
    assert parsed is not None
    assert parsed.display_name == "amina_b"


def test_parse_message_tolerates_a_missing_name_entirely() -> None:
    parsed = _parse_message({"message_id": 1, "text": "hi", "chat": {"id": 5}, "from": {"id": 6}})
    assert parsed is not None
    assert parsed.display_name is None


def test_split_text_prefers_line_boundaries() -> None:
    text = ("a" * 100 + "\n") * 60  # ~6060 chars, well over the limit
    parts = _split_text(text, limit=4096)
    assert len(parts) == 2
    assert all(len(part) <= 4096 for part in parts)
    assert not parts[0].endswith("a" * 100 + "b")


def test_split_text_leaves_a_short_message_alone() -> None:
    assert _split_text("short") == ["short"]


def test_split_text_falls_back_to_a_hard_cut_without_newlines() -> None:
    parts = _split_text("x" * 9000, limit=4096)
    assert [len(part) for part in parts] == [4096, 4096, 808]


def test_sends_use_the_configured_timeout_not_the_long_poll_allowance() -> None:
    """The outbox lease is validated against the HTTP timeout, so a send must really honour it.

    The client default carries an extra allowance so a long poll does not time out client-side.
    A send that inherited that allowance could run longer than the configuration claims -- and
    longer than the lease that is supposed to cover it.
    """
    seen: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        seen[method] = request.extensions["timeout"]["read"]
        return _ok([] if method == "getUpdates" else {"message_id": 1})

    transport = TelegramTransport(bot_token="12345:TESTTOKEN", timeout_seconds=5.0)
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=BASE, timeout=35.0
    )
    transport.poll(timeout_seconds=0)
    transport.send(OutboundMessage(chat_id="42", text="hello"))

    assert seen == {"getUpdates": 35.0, "sendMessage": 5.0}
