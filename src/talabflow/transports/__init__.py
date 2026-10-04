"""Messaging transports and the factory that selects one from configuration."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .base import (
    InboundMessage,
    MessageTransport,
    OutboundMessage,
    PermanentTransportError,
    RateLimitedError,
    TransportAuthError,
    TransportError,
)
from .scripted import ScriptedTransport
from .telegram import TelegramTransport

__all__ = [
    "InboundMessage",
    "MessageTransport",
    "OutboundMessage",
    "PermanentTransportError",
    "RateLimitedError",
    "ScriptedTransport",
    "TelegramTransport",
    "TransportAuthError",
    "TransportError",
    "build_transport",
]


def build_transport(settings: Settings, *, offset_path: Path | None = None) -> MessageTransport:
    """Instantiate the transport named by ``settings.transport``.

    Configuration validity is already guaranteed by :class:`~talabflow.config.Settings`: the
    telegram transport without a bot token fails at settings construction, not here.
    """
    if settings.transport == "scripted":
        return ScriptedTransport()
    if not settings.telegram_bot_token:  # pragma: no cover - guarded by Settings validation
        raise ValueError("the telegram transport requires a bot token")
    return TelegramTransport(
        bot_token=settings.telegram_bot_token,
        api_base=settings.telegram_api_base,
        timeout_seconds=settings.http_timeout_seconds,
        offset_path=offset_path or Path("data/telegram-offset.txt"),
    )
