"""Telegram Bot API transport, implemented directly over ``httpx``.

Why not ``python-telegram-bot`` or ``aiogram``
----------------------------------------------
Both are good libraries, and both would own the architecture: they expect to run the event loop,
own the handler registry, and define what a "message" is. Since the point of
:mod:`.base` is that the *application* owns the conversation and the transport is replaceable,
the two surfaces this project needs -- ``getUpdates`` and ``sendMessage`` -- are cheaper to call
directly than to adapt. That also keeps the dependency list short and makes the offline
scripted transport a peer rather than a mock of a framework.

Long polling is used rather than webhooks so no public IP, TLS certificate, tunnel or hosting
bill is needed to run this.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from .base import (
    InboundMessage,
    MessageTransport,
    OutboundMessage,
    PermanentTransportError,
    TransportError,
)

logger = logging.getLogger(__name__)

CHANNEL = "telegram"

#: Telegram rejects ``sendMessage`` above this length; split rather than lose the tail.
MAX_TELEGRAM_MESSAGE = 4096

#: Bot API error descriptions that mean "never retry this".
_PERMANENT_MARKERS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bot can't initiate conversation",
)


class TelegramTransport(MessageTransport):
    """Talks to the real Bot API using long polling."""

    name = CHANNEL

    def __init__(
        self,
        *,
        bot_token: str,
        api_base: str = "https://api.telegram.org",
        timeout_seconds: float = 30.0,
        offset_path: Path | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("a Telegram bot token is required")
        self._token = bot_token
        self._client = httpx.Client(
            base_url=f"{api_base.rstrip('/')}/bot{bot_token}",
            # Must exceed the long-poll timeout or every poll would time out client-side.
            timeout=httpx.Timeout(timeout_seconds + 30.0),
            follow_redirects=False,
        )
        # getUpdates only acknowledges updates once a higher offset is requested, so the offset
        # is persisted: without it a restart replays the last batch and the customer is asked
        # the same question twice.
        self._offset_path = offset_path
        self._offset = self._load_offset()

    # -- offset persistence ------------------------------------------------------

    def _load_offset(self) -> int:
        if self._offset_path is None or not self._offset_path.is_file():
            return 0
        try:
            return int(self._offset_path.read_text(encoding="utf-8").strip() or 0)
        except (ValueError, OSError):
            logger.warning("could not read the update offset; starting from 0")
            return 0

    def _save_offset(self, offset: int) -> None:
        self._offset = offset
        if self._offset_path is None:
            return
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_path.write_text(str(offset), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not persist the update offset: %s", exc)

    # -- HTTP --------------------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        """Call a Bot API method and return its ``result``.

        Raises:
            PermanentTransportError: for errors that retrying cannot fix.
            TransportError: for transient failures.
        """
        try:
            response = self._client.post(f"/{method}", json=payload)
        except httpx.TimeoutException as exc:
            raise TransportError(f"{method} timed out") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"{method} failed: {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise TransportError(f"{method} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise TransportError(f"{method} returned an unexpected JSON shape")

        if body.get("ok"):
            return body.get("result")

        description = str(body.get("description", "unknown error"))
        if response.status_code == 401:
            # An invalid token will never become valid by retrying.
            raise PermanentTransportError("Telegram rejected the bot token (401)")
        if response.status_code == 403 or any(
            marker in description.lower() for marker in _PERMANENT_MARKERS
        ):
            raise PermanentTransportError(f"Telegram refused permanently: {description}")
        if response.status_code == 429:
            retry_after = 0
            parameters = body.get("parameters")
            if isinstance(parameters, dict):
                retry_after = int(parameters.get("retry_after", 0) or 0)
            raise TransportError(f"Telegram rate-limited the request; retry after {retry_after}s")
        raise TransportError(f"Telegram error on {method}: {description}")

    # -- transport interface -----------------------------------------------------

    def poll(self, *, timeout_seconds: float) -> list[InboundMessage]:
        result = self._call(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": int(timeout_seconds),
                # Only ask for what is handled. Anything else would be acknowledged and dropped.
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(result, list):
            raise TransportError("getUpdates returned an unexpected payload")

        messages: list[InboundMessage] = []
        highest = self._offset
        for update in result:
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", 0))
            highest = max(highest, update_id + 1)
            parsed = _parse_message(update.get("message"))
            if parsed is not None:
                messages.append(parsed)
        if highest != self._offset:
            self._save_offset(highest)
        return messages

    def send(self, message: OutboundMessage) -> str:
        last_id = ""
        for part in _split_text(message.text):
            result = self._call(
                "sendMessage",
                {
                    "chat_id": message.chat_id,
                    "text": part,
                    # No parse_mode: customer-supplied text is echoed back in confirmations, and
                    # Markdown/HTML parsing would let a stray character break delivery or let
                    # crafted input inject formatting. Plain text is the safe default.
                    "disable_web_page_preview": True,
                },
            )
            if isinstance(result, dict):
                last_id = str(result.get("message_id", ""))
        return last_id

    def close(self) -> None:
        self._client.close()


def _parse_message(raw: object) -> InboundMessage | None:
    """Convert a Telegram ``message`` object into an :class:`InboundMessage`.

    Returns ``None`` for anything without text -- a sticker, a photo, a join notification -- so
    unsupported update shapes are skipped rather than crashing the poll loop.
    """
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    chat = raw.get("chat")
    sender = raw.get("from")
    if not isinstance(text, str) or not isinstance(chat, dict) or not isinstance(sender, dict):
        return None

    first = str(sender.get("first_name", "") or "")
    last = str(sender.get("last_name", "") or "")
    username = sender.get("username")
    display = " ".join(part for part in (first, last) if part).strip()
    if not display and isinstance(username, str):
        display = username

    return InboundMessage(
        channel=CHANNEL,
        chat_id=str(chat.get("id", "")),
        user_id=str(sender.get("id", "")),
        text=text,
        message_id=str(raw.get("message_id", "")),
        display_name=display or None,
    )


def _split_text(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    """Split ``text`` into chunks Telegram will accept, preferring line boundaries."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts
