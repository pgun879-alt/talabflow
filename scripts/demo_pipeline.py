#!/usr/bin/env python3
"""Move an order through the status pipeline and deliver the customer notifications.

Demonstrates three properties that distinguish this from a chat log with extra steps:

1. Invalid transitions are refused, with the error naming what *is* allowed.
2. Each valid change writes an audit event and queues exactly one customer notification.
3. Running the worker twice does not message the customer twice.
"""

from __future__ import annotations

import sys

from talabflow.config import get_settings
from talabflow.db import build_engine, build_session_factory, session_scope
from talabflow.logging_setup import configure_logging
from talabflow.models import OrderStatus
from talabflow.outbox import OutboxWorker
from talabflow.repository import (
    InvalidTransitionError,
    change_order_status,
    get_order_by_reference,
    list_orders,
    load_order_events,
)
from talabflow.transports.scripted import ScriptedTransport

GREEN = "\033[0;32m"
RED = "\033[0;31m"
DIM = "\033[2m"
RESET = "\033[0m"


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    factory = build_session_factory(build_engine(settings))
    transport = ScriptedTransport()
    worker = OutboxWorker(settings=settings, transport=transport, session_factory=factory)

    with session_scope(factory) as session:
        orders = list_orders(session, limit=1).items
        if not orders:
            print("no orders found; run the conversation demo first", file=sys.stderr)
            return 1
        reference = orders[0].reference

    print(f"Working on order {reference}\n")

    # 1. An invalid jump is refused.
    with session_scope(factory) as session:
        order = get_order_by_reference(session, reference)
        assert order is not None
        try:
            change_order_status(
                session, order=order, to_status=OrderStatus.COMPLETED, actor="amina"
            )
            print(f"{RED}BUG: an invalid transition was allowed{RESET}")
            return 1
        except InvalidTransitionError as exc:
            print(f"{RED}refused:{RESET} {exc}\n")

    # 2. Walk the pipeline properly, notifying the customer at each step.
    steps = [
        (OrderStatus.CONFIRMED, "A technician has been assigned"),
        (OrderStatus.IN_PROGRESS, "The technician is on the way"),
        (OrderStatus.READY, "Work finished, awaiting your confirmation"),
        (OrderStatus.COMPLETED, "Thank you for your business"),
    ]
    for target, note in steps:
        with session_scope(factory) as session:
            order = get_order_by_reference(session, reference)
            assert order is not None
            change_order_status(session, order=order, to_status=target, actor="amina", note=note)
        transport.sent.clear()
        sent, failed = worker.process_batch()
        print(f"{GREEN}{target.value:<12}{RESET} notification sent={sent} failed={failed}")
        for text in transport.sent_texts():
            print(f"  {DIM}to customer:{RESET} {' / '.join(text.splitlines())}")

    # 3. Idempotency: a second pass must not re-message anyone.
    transport.sent.clear()
    sent, failed = worker.process_batch()
    print(f"\nre-running the worker: sent={sent} failed={failed} (expected 0 and 0)")
    if transport.sent:
        print(f"{RED}BUG: the customer was messaged twice{RESET}")
        return 1
    print(f"{GREEN}no duplicate messages{RESET}")

    # 4. The audit trail.
    with session_scope(factory) as session:
        order = get_order_by_reference(session, reference)
        assert order is not None
        print(f"\nAudit trail for {reference}:")
        for event in load_order_events(session, order.id):
            origin = event.from_status.value if event.from_status else "(created)"
            stamp = event.created_at.strftime("%H:%M:%S")
            print(f"  {stamp}  {origin:>12} -> {event.to_status.value:<12} by {event.actor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
