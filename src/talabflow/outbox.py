"""The notification worker: claims queued messages, then delivers them.

Delivery guarantees, stated precisely
-------------------------------------
This is **at-least-once delivery**. It is *not* exactly-once, and it cannot be: Telegram's
``sendMessage`` offers no client-supplied idempotency key, so no client can make a redelivery a
no-op on the provider's side.

What the design does guarantee:

* The notification row is written in the **same transaction** as the status change, so neither
  can exist without the other.
* ``idempotency_key`` is unique and derived from the audit event's id, so one logical change can
  never produce two rows.
* A worker **claims** a row -- committing ``status = processing`` with a lease -- *before* making
  any outbound call. Two workers therefore never hold the same row at the same time.
* A batch is claimed at once but sent one message at a time, so the worker **renews the lease
  immediately before each send** and skips any row it no longer holds. A lease therefore only has
  to outlast one send, not the whole batch.
* A row is marked ``sent`` immediately after the transport confirms.

The window that remains, stated plainly: if a worker crashes **after** the provider accepted the
message but **before** the ``sent`` commit, the lease eventually expires and the message is sent a
second time. That is inherent to at-least-once over a provider without idempotency keys.

Failures are separated by kind, because each one calls for something different:

* a **transient** error backs off exponentially and retries, within an attempt budget;
* a **permanent** one (the customer blocked the bot) goes straight to ``dead`` rather than burning
  that budget;
* a **rate limit** is not the message's fault at all. The message and the rest of the batch are
  put back untouched, to be tried again once the pause the provider asked for has passed, and no
  attempt is counted;
* **rejected credentials** stop the worker. Nothing is dead-lettered: the queue is exactly as
  deliverable as it was, once the configuration is fixed.

A worker only claims messages for its own transport's channel.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from . import repository
from .config import Settings
from .models import OutboxMessage, OutboxStatus, utcnow
from .transports.base import (
    MessageTransport,
    OutboundMessage,
    PermanentTransportError,
    RateLimitedError,
    TransportAuthError,
    TransportError,
)

logger = logging.getLogger(__name__)

#: Bounds on the pause taken after a rate limit. At least a second, so a provider that names no
#: delay is not asked again at once; at most an hour, so a nonsense value cannot park the queue.
MIN_RATE_LIMIT_WAIT_SECONDS: Final = 1.0
MAX_RATE_LIMIT_WAIT_SECONDS: Final = 3600.0


def backoff_delay(attempts: int, *, base_seconds: int, cap_seconds: int = 3600) -> timedelta:
    """Exponential backoff: ``base * 2**(attempts-1)``, capped.

    >>> backoff_delay(1, base_seconds=30).total_seconds()
    30.0
    >>> backoff_delay(3, base_seconds=30).total_seconds()
    120.0
    """
    if attempts < 1:
        attempts = 1
    seconds = min(base_seconds * (2 ** (attempts - 1)), cap_seconds)
    return timedelta(seconds=seconds)


class OutboxWorker:
    """Drains the outbox through a transport."""

    def __init__(
        self,
        *,
        settings: Settings,
        transport: MessageTransport,
        session_factory: sessionmaker[Session],
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.session_factory = session_factory
        #: Identifies this worker in the claims it takes. Readable when inspecting the table.
        self.worker_id = settings.worker_id
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    # -- one row -----------------------------------------------------------------

    def _deliver(self, session: Session, message: OutboxMessage) -> bool:
        """Attempt one delivery, updating the row. Returns True when it was sent."""
        message.attempts += 1
        try:
            provider_id = self.transport.send(
                OutboundMessage(chat_id=message.chat_id, text=message.body)
            )
        except (RateLimitedError, TransportAuthError):
            # Neither is an outcome for *this message*; the batch loop deals with both. This
            # clause must come first: rejected credentials are a kind of permanent error, and
            # falling through would dead-letter the message for a fault in the configuration.
            raise
        except PermanentTransportError as exc:
            # Retrying cannot help: the chat is gone or the bot is blocked.
            message.status = OutboxStatus.DEAD
            message.last_error = str(exc)[:500]
            repository.release_claim(message)
            logger.warning(
                "outbox message dead-lettered",
                extra={"outbox_id": message.id, "reason": str(exc)[:200]},
            )
            return False
        except TransportError as exc:
            self._record_failure(message, str(exc))
            return False

        message.status = OutboxStatus.SENT
        message.sent_at = utcnow()
        message.last_error = None
        repository.release_claim(message)
        logger.info(
            "outbox message sent",
            extra={"outbox_id": message.id, "provider_message_id": provider_id},
        )
        return True

    def _record_failure(self, message: OutboxMessage, error: str) -> None:
        """Apply the outcome of a failed attempt that has already been counted.

        Backs off for a retry, or dead-letters once the attempt budget is spent.
        """
        message.last_error = error[:500]
        if message.attempts >= self.settings.outbox_max_attempts:
            message.status = OutboxStatus.DEAD
            repository.release_claim(message)
            logger.error(
                "outbox message exhausted its attempts",
                extra={"outbox_id": message.id, "attempts": message.attempts},
            )
            return
        message.status = OutboxStatus.FAILED
        message.next_attempt_at = utcnow() + backoff_delay(
            message.attempts, base_seconds=self.settings.outbox_backoff_base_seconds
        )
        # Release the lease so the retry is claimable by whichever worker gets there first,
        # rather than reserved for this one.
        repository.release_claim(message)
        logger.info(
            "outbox delivery failed; will retry",
            extra={
                "outbox_id": message.id,
                "attempts": message.attempts,
                "next_attempt_at": message.next_attempt_at.isoformat(),
            },
        )

    def _record_unexpected_failure(
        self, session: Session, message: OutboxMessage, error: Exception
    ) -> None:
        """Count an attempt that ended in an exception the transport contract does not name.

        The rollback that follows such an exception also discards the attempt counter that
        ``_deliver`` had just incremented. Left like that, a message that reliably crashes the
        send -- a bug in a transport, a payload the provider's client chokes on -- would never use
        up its budget: its lease would expire, it would be claimed again, and it would fail again,
        for ever, without being dead-lettered.

        So the attempt is re-applied here in its own transaction, and treated exactly like a
        transient failure. If even that cannot be written, the lease is left to expire, which is
        the old behaviour and the safe fallback.
        """
        try:
            session.refresh(message)
            if (
                message.status is not OutboxStatus.PROCESSING
                or message.claimed_by != self.worker_id
            ):
                return
            message.attempts += 1
            self._record_failure(message, f"unexpected {type(error).__name__}: {error}")
            session.commit()
        except Exception:
            session.rollback()
            logger.exception(
                "could not record a failed attempt; the lease will expire and the message will "
                "be retried",
                extra={"outbox_id": message.id, "worker": self.worker_id},
            )

    def _hand_back(
        self,
        session: Session,
        messages: Sequence[OutboxMessage],
        *,
        until: datetime | None,
        note: str | None,
    ) -> None:
        """Return claimed messages to the queue unsent, without counting an attempt.

        Used when the reason for not sending has nothing to do with the messages themselves.
        ``until`` postpones them; ``None`` leaves them due.

        Each row is re-read first and only touched if this worker still holds it. If the write
        fails, the leases are left to expire, which returns the messages to the queue anyway --
        just later.
        """
        try:
            for message in messages:
                session.refresh(message)
                if (
                    message.status is not OutboxStatus.PROCESSING
                    or message.claimed_by != self.worker_id
                ):
                    continue
                # Back to the state it was claimed from: "failed" still means "has failed
                # before and is waiting for a retry".
                message.status = (
                    OutboxStatus.FAILED if message.attempts > 0 else OutboxStatus.PENDING
                )
                if until is not None:
                    message.next_attempt_at = until
                if note is not None:
                    message.last_error = note[:500]
                repository.release_claim(message)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception(
                "could not hand messages back; their leases will expire instead",
                extra={"worker": self.worker_id},
            )

    # -- batches -----------------------------------------------------------------

    def process_batch(self) -> tuple[int, int]:
        """Claim a batch, then deliver it.

        The claim is committed before any send, and each outcome is committed per message rather
        than per batch. Committing per message matters: with one commit at the end of the batch, a
        crash halfway through would roll back the ``sent`` marks of messages that had already been
        delivered, and they would all go out again.

        The lease is renewed right before each send. Without that, a slow batch outlives the lease
        taken at claim time: another worker reclaims the messages still waiting their turn, sends
        them, and this worker would then send them a second time.

        Returns:
            ``(sent, failed)`` counts for this batch. Messages postponed by a rate limit are
            neither.

        Raises:
            TransportAuthError: when the provider rejects the credentials. The claimed messages
                are returned to the queue first.
        """
        sent = 0
        failed = 0
        session = self.session_factory()
        try:
            claimed = repository.claim_outbox_batch(
                session,
                worker_id=self.worker_id,
                limit=self.settings.outbox_batch_size,
                lease_seconds=self.settings.outbox_lease_seconds,
                channel=self.transport.name,
            )
            for index, message in enumerate(claimed):
                try:
                    if not repository.renew_claim(
                        session,
                        message,
                        worker_id=self.worker_id,
                        lease_seconds=self.settings.outbox_lease_seconds,
                    ):
                        # Not a failure: another worker took the row over after its lease ran
                        # out, and is (or was) responsible for delivering it.
                        logger.warning(
                            "lease lost before sending; leaving the message to its new owner",
                            extra={"outbox_id": message.id, "worker": self.worker_id},
                        )
                        continue
                    if self._deliver(session, message):
                        sent += 1
                    else:
                        failed += 1
                    session.commit()
                except RateLimitedError as exc:
                    # The rollback discards the attempt ``_deliver`` had counted. Everything not
                    # yet sent goes back, this message included: any further send inside the
                    # pause would be refused as well, and tends to lengthen it.
                    session.rollback()
                    wait = min(
                        max(exc.retry_after, MIN_RATE_LIMIT_WAIT_SECONDS),
                        MAX_RATE_LIMIT_WAIT_SECONDS,
                    )
                    unsent = claimed[index:]
                    self._hand_back(
                        session,
                        unsent,
                        until=utcnow() + timedelta(seconds=wait),
                        note=f"rate limited by the provider; retry after {wait:g}s",
                    )
                    logger.warning(
                        "rate limited; postponing the rest of the batch",
                        extra={"worker": self.worker_id, "wait": wait, "postponed": len(unsent)},
                    )
                    break
                except TransportAuthError:
                    session.rollback()
                    self._hand_back(session, claimed[index:], until=None, note=None)
                    self._stopping = True
                    logger.error(
                        "the messaging provider rejected the credentials; stopping the worker. "
                        "No notification was discarded",
                        extra={"worker": self.worker_id},
                    )
                    raise
                except Exception as exc:
                    session.rollback()
                    logger.exception(
                        "unexpected error delivering an outbox message",
                        extra={"outbox_id": message.id, "worker": self.worker_id},
                    )
                    self._record_unexpected_failure(session, message, exc)
                    failed += 1
        finally:
            session.close()
        return sent, failed

    def run_forever(self, *, max_iterations: int | None = None) -> tuple[int, int]:
        """Process batches until stopped. Returns cumulative ``(sent, failed)``."""
        total_sent = 0
        total_failed = 0
        iterations = 0
        logger.info(
            "outbox worker started",
            extra={"transport": self.transport.name, "worker": self.worker_id},
        )
        while not self._stopping:
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            sent, failed = self.process_batch()
            total_sent += sent
            total_failed += failed
            if sent == 0 and failed == 0 and not self._stopping:
                time.sleep(self.settings.outbox_poll_interval_seconds)
        logger.info("outbox worker stopped", extra={"sent": total_sent, "failed": total_failed})
        return total_sent, total_failed
