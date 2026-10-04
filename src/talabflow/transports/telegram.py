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
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from .base import (
    InboundMessage,
    MessageTransport,
    OutboundMessage,
    PermanentTransportError,
    RateLimitedError,
    TransportAuthError,
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
        # Sends are not long polls, so they get the configured timeout without the long-poll
        # allowance. The outbox lease is validated against this value: a send that could run for
        # 30 seconds longer than the configuration says could outlive its own lease.
        self._send_timeout = timeout_seconds
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

    def _call(self, method: str, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        """Call a Bot API method and return its ``result``.

        ``timeout`` overrides the client default for this one call. ``None`` means "use the
        client default" -- it is deliberately not passed through to httpx, where ``None`` would
        mean "no timeout at all".

        Raises:
            TransportAuthError: when the bot token itself is rejected.
            PermanentTransportError: for errors that retrying cannot fix.
            RateLimitedError: when Telegram asks for a pause, with the pause it asked for.
            TransportError: for transient failures.
        """
        try:
            if timeout is None:
                response = self._client.post(f"/{method}", json=payload)
            else:
                response = self._client.post(f"/{method}", json=payload, timeout=timeout)
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
        if response.status_code in (401, 404):
            # 401 is a token Telegram does not know (mistyped, or revoked in @BotFather); 404 is
            # what it answers when the token does not even have the right shape, because the
            # token is part of the URL path. Neither becomes valid by retrying, and neither says
            # anything about the message being sent. The token is never put in the error text.
            raise TransportAuthError(f"Telegram rejected the bot token ({response.status_code})")
        if response.status_code == 403 or any(
            marker in description.lower() for marker in _PERMANENT_MARKERS
        ):
            raise PermanentTransportError(f"Telegram refused permanently: {description}")
        if response.status_code == 429:
            retry_after = 0.0
            parameters = body.get("parameters")
            if isinstance(parameters, dict):
                try:
                    retry_after = float(parameters.get("retry_after") or 0)
                except (TypeError, ValueError):
                    # A malformed hint must not turn a rate limit into a crash.
                    retry_after = 0.0
            raise RateLimitedError(
                f"Telegram rate-limited the request; retry after {retry_after:g}s",
                retry_after=retry_after,
            )
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

        parsed: list[tuple[int, InboundMessage]] = []
        highest = self._offset
        for update in result:
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", 0))
            highest = max(highest, update_id + 1)
            message = _parse_message(update.get("message"))
            if message is not None:
                parsed.append((update_id, message))

        if not self.manual_ack or not parsed:
            # Nothing the caller will acknowledge, so the whole batch is acknowledged here.
            if highest != self._offset:
                self._save_offset(highest)
            return [message for _, message in parsed]

        # Manual acknowledgement. Telegram forgets an update as soon as a higher offset is
        # requested, so the offset must not pass a message until the caller has handled it:
        # otherwise a crash in between loses that customer's message for good.
        #
        # Each message carries the offset to resume from once it is done -- the next message's
        # update id, or the end of the batch for the last one -- so unsupported updates that sit
        # between or after the messages are skipped by the same acknowledgement. Unsupported
        # updates *before* the first message have no one to acknowledge them and are skipped now.
        first_update_id = parsed[0][0]
        if first_update_id > self._offset:
            self._save_offset(first_update_id)
        messages: list[InboundMessage] = []
        for index, (_, message) in enumerate(parsed):
            resume_at = parsed[index + 1][0] if index + 1 < len(parsed) else highest
            messages.append(replace(message, ack_token=str(resume_at)))
        return messages

    def acknowledge(self, message: InboundMessage) -> None:
        try:
            resume_at = int(message.ack_token)
        except ValueError:
            return
        # Never move backwards: a late or repeated acknowledgement must not replay updates.
        if resume_at > self._offset:
            self._save_offset(resume_at)

    def send(self, message: OutboundMessage) -> str:
        last_id = ""
        parts = _split_text(message.text)
        markup = _reply_markup(message)
        for index, part in enumerate(parts):
            payload: dict[str, Any] = {
                "chat_id": message.chat_id,
                "text": part,
                # No parse_mode: customer-supplied text is echoed back in confirmations, and
                # Markdown/HTML parsing would let a stray character break delivery or let
                # crafted input inject formatting. Plain text is the safe default.
                "disable_web_page_preview": True,
            }
            # A keyboard belongs under the last part: that is the one the customer is looking
            # at when they answer.
            if markup is not None and index == len(parts) - 1:
                payload["reply_markup"] = markup
            result = self._call("sendMessage", payload, timeout=self._send_timeout)
            if isinstance(result, dict):
                last_id = str(result.get("message_id", ""))
        return last_id

    def close(self) -> None:
        self._client.close()


def _reply_markup(message: OutboundMessage) -> dict[str, Any] | None:
    """The Bot API ``reply_markup`` for a message, or ``None`` for a plain one.

    ``request_contact`` makes Telegram send the customer's *own* registered number when the
    button is tapped. It only works in private chats, which is the only kind this transport
    accepts.
    """
    if message.contact_button:
        return {
            "keyboard": [[{"text": message.contact_button, "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    if message.remove_keyboard:
        return {"remove_keyboard": True}
    return None


def _parse_message(raw: object) -> InboundMessage | None:
    """Convert a Telegram ``message`` object into an :class:`InboundMessage`.

    Returns ``None`` for anything without text -- a sticker, a photo, a join notification -- so
    unsupported update shapes are skipped rather than crashing the poll loop.

    A shared contact card is the one non-text message that is understood: its phone number
    becomes the text. It counts as the sender's own number only when Telegram reports the
    card's ``user_id`` as the sender's id, which is what the "share my phone number" button
    produces. A forwarded card for somebody else has a different id, or none.

    Also returns ``None`` for anything that is not a one-to-one chat. An order conversation echoes
    the customer's phone number and address back for confirmation, and later status notifications
    go to the last chat the customer wrote from. In a group that means reading a customer's
    address out to the whole group, and then sending their order updates there too. Telegram
    always includes ``chat.type``; only an explicit non-private type is refused, so a payload
    without the field is still treated as a direct message.
    """
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    chat = raw.get("chat")
    sender = raw.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None

    is_contact = False
    contact_is_sender = False
    contact = raw.get("contact")
    if not isinstance(text, str) and isinstance(contact, dict):
        number = contact.get("phone_number")
        if isinstance(number, str) and number:
            text = number
            is_contact = True
            contact_user = contact.get("user_id")
            sender_id = sender.get("id")
            contact_is_sender = (
                contact_user is not None
                and sender_id is not None
                and str(contact_user) == str(sender_id)
            )
    if not isinstance(text, str):
        return None
    if chat.get("type", "private") != "private":
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
        is_contact=is_contact,
        contact_is_sender=contact_is_sender,
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
