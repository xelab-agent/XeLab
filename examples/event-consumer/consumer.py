"""Reference event consumer for the XeLab platform event contract.

Stdlib-only, offline example. It talks to any object implementing the
ack/deliver interface of fake_service.FakeEventService; it performs no
network I/O and never logs payloads or lease tokens.
"""

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timezone


class TransportTimeout(Exception):
    """The ack request's outcome is unknown (it may have been applied)."""


class ConsumerError(Exception):
    pass


class NotAuthorized(ConsumerError):
    """Credentials revoked or scope lost; the consumer must stop."""


class IdempotencyConflict(ConsumerError):
    """Same Idempotency-Key seen with different content; needs operator review."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    event_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS side_effects (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ack_journal (
    idempotency_key TEXT PRIMARY KEY,
    subscription_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    detail TEXT
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Local durability: business result, dedupe marker and ack intent commit
    in one SQLite transaction before any ack is sent."""

    def __init__(self, path):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)

    def processed_result(self, event_id):
        row = self.connection.execute(
            "SELECT result_digest FROM processed WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else row[0]

    def prepare_ack(self, subscription_id, delivery_id, event_id, outcome):
        key = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO ack_journal VALUES (?, ?, ?, ?, ?, 'prepared', ?, NULL)",
                (key, subscription_id, delivery_id, event_id, outcome, _now()),
            )
        return key

    def commit_processing(self, event_id, topic, result_digest,
                          subscription_id, delivery_id, outcome):
        key = uuid.uuid4().hex
        now = _now()
        with self.connection:
            self.connection.execute(
                "INSERT INTO side_effects (event_id, action, committed_at) "
                "VALUES (?, 'ledger-append', ?)", (event_id, now))
            self.connection.execute(
                "INSERT INTO processed VALUES (?, ?, ?, ?)",
                (event_id, topic, result_digest, now))
            self.connection.execute(
                "INSERT INTO ack_journal VALUES (?, ?, ?, ?, ?, 'prepared', ?, NULL)",
                (key, subscription_id, delivery_id, event_id, outcome, now),
            )
        return key

    def record_ack(self, key, state, detail=None):
        with self.connection:
            self.connection.execute(
                "UPDATE ack_journal SET state = ?, detail = ? WHERE idempotency_key = ?",
                (state, detail, key))

    def report(self):
        states = dict(self.connection.execute(
            "SELECT state, COUNT(*) FROM ack_journal GROUP BY state"))
        return {
            "processed": self.connection.execute(
                "SELECT COUNT(*) FROM processed").fetchone()[0],
            "side_effects": self.connection.execute(
                "SELECT COUNT(*) FROM side_effects").fetchone()[0],
            "ack_states": states,
        }

    def close(self):
        self.connection.close()


class Consumer:
    """At-least-once consumer: dedupe by event_id, commit before ack.

    Durability assumptions and the exact protocol subset are documented in
    README.md. This is a reference example, not an exactly-once guarantee.
    """

    def __init__(self, store, service, subscription_id, topics):
        self.store = store
        self.service = service
        self.subscription_id = subscription_id
        self.topics = frozenset(topics)

    def handle(self, delivery):
        self._validate(delivery)
        event = delivery["event"]
        event_id = event["id"]
        if event["topic"] not in self.topics:
            key = self.store.prepare_ack(
                self.subscription_id, delivery["delivery_id"], event_id, "dead_letter")
            return self._perform_ack(key, delivery, "dead_letter", deduplicated=False)
        deduplicated = self.store.processed_result(event_id) is not None
        if deduplicated:
            key = self.store.prepare_ack(
                self.subscription_id, delivery["delivery_id"], event_id, "ack")
        else:
            digest = self.business_result(event)
            key = self.store.commit_processing(
                event_id, event["topic"], digest,
                self.subscription_id, delivery["delivery_id"], "ack")
        return self._perform_ack(key, delivery, "ack", deduplicated=deduplicated)

    def business_result(self, event):
        """The example's side effect is the local ledger row; the digest binds
        the committed result to the event identity without storing payload."""
        return hashlib.sha256(
            f"{event['topic']}\n{event['id']}".encode("utf-8")).hexdigest()

    def _validate(self, delivery):
        for field in ("delivery_id", "subscription_id", "event", "attempt",
                      "lease_token", "lease_expires_at"):
            if field not in delivery:
                raise ConsumerError(f"delivery missing field: {field}")
        if delivery["subscription_id"] != self.subscription_id:
            raise ConsumerError("delivery belongs to a different subscription")
        for field in ("id", "topic"):
            if field not in delivery["event"]:
                raise ConsumerError(f"event missing field: {field}")

    def _perform_ack(self, key, delivery, outcome, deduplicated):
        report = {
            "event_id": delivery["event"]["id"],
            "delivery_id": delivery["delivery_id"],
            "outcome": outcome,
            "deduplicated": deduplicated,
        }
        try:
            result = self._ack_once(key, delivery, outcome)
        except TransportTimeout:
            # Unknown outcome: only a same-key retry is permitted. The lease
            # token stays in memory; it is never persisted or logged.
            try:
                result = self._ack_once(key, delivery, outcome)
            except TransportTimeout:
                report["state"] = "prepared"
                return report
        if result.status == 204:
            self.store.record_ack(key, "confirmed")
            report["state"] = "confirmed"
            return report
        if result.status == 409 and result.code == "STATE_CONFLICT":
            # This can mean a stale lease, but also a disabled subscription.
            # Never retry the old token; the caller must recheck state before
            # waiting for an active subscription's redelivery.
            self.store.record_ack(key, "state_conflict")
            report["state"] = "state_conflict"
            return report
        if result.status == 409 and result.code == "IDEMPOTENCY_CONFLICT":
            self.store.record_ack(key, "conflict", detail=result.code)
            raise IdempotencyConflict(
                f"idempotency conflict for event {delivery['event']['id']}")
        if result.status in (401, 403):
            self.store.record_ack(key, "rejected", detail=str(result.status))
            raise NotAuthorized(f"ack rejected with status {result.status}")
        self.store.record_ack(key, "rejected", detail=str(result.status))
        raise ConsumerError(f"unexpected ack status {result.status}")

    def _ack_once(self, key, delivery, outcome):
        return self.service.ack(
            subscription_id=self.subscription_id,
            delivery_id=delivery["delivery_id"],
            lease_token=delivery["lease_token"],
            outcome=outcome,
            idempotency_key=key,
        )
