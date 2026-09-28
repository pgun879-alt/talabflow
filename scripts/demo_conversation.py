#!/usr/bin/env python3
"""Drive two scripted customer conversations through the offline transport.

This is the part that would normally require a bot token, a webhook and a phone. Because the
messaging layer is an interface (see ``talabflow.transports.base``), the identical application
code runs against an in-memory transport and the whole flow is demonstrable on a laptop with no
network at all.
"""

from __future__ import annotations

import sys

from talabflow.bot import BotRunner
from talabflow.config import get_settings
from talabflow.db import build_engine, build_session_factory
from talabflow.logging_setup import configure_logging
from talabflow.transports.scripted import ScriptedTransport

RESET = "\033[0m"
CUSTOMER = "\033[1;33m"
BOT = "\033[0;32m"

#: (label, user_id, messages). The second customer deliberately makes mistakes.
CONVERSATIONS = [
    (
        "Amina — places an order cleanly",
        "5001",
        [
            "/start",
            "/new",
            "1",
            "The washing machine will not drain and there is water on the floor",
            "0555123456",
            "12 Rue Didouche Mourad, Algiers",
            "yes",
        ],
    ),
    (
        "Karim — mistypes, changes his mind, then succeeds",
        "5002",
        [
            "/new",
            "99",  # not a valid menu number
            "plumbing",  # not an offered service
            "2",  # Installation
            "ac",  # too short
            "Need a split air conditioner installed in the living room",
            "not telling you",  # not a phone number
            "+213 555 98 76 54",  # accepted, formatting and all
            "Cite 200 Logements, Bloc B, Oran",
            "wait",  # not a confirmation
            "yes",
        ],
    ),
]


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    factory = build_session_factory(build_engine(settings))

    for label, user_id, messages in CONVERSATIONS:
        print(f"\n\033[1m{label}\033[0m")
        transport = ScriptedTransport()
        runner = BotRunner(settings=settings, transport=transport, session_factory=factory)
        for message in messages:
            transport.sent.clear()
            transport.queue(message, user_id=user_id, display_name=label.split(" —")[0])
            runner.poll_once()
            print(f"  {CUSTOMER}customer >{RESET} {message}")
            for reply in transport.sent_texts():
                first, *rest = reply.splitlines() or [""]
                print(f"  {BOT}bot      <{RESET} {first}")
                for line in rest:
                    print(f"              {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
