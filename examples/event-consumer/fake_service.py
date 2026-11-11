"""Offline simulation of the platform event service for the reference consumer.

This is NOT the kernel. It implements only the contract behaviors the example
depends on: one unacked 30s lease per subscription, redelivery with a new
delivery_id/lease_token, same-lease re-ack success, expired-lease 409,
Idempotency-Key replay/conflict, exponential backoff and dead letter at
maximum_attempts. The clock is injectable and jitter defaults to zero so runs
are deterministic; the real platform adds up to 25% jitter (cap 60s).
"""

import hashlib
import hmac
import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from consumer import NotAuthorized, TransportTimeout

LEASE_SECONDS = 30
BACKOFF_BASE_SECONDS = (1, 2, 4, 8, 16, 32, 64)
BACKOFF_CAP_SECONDS = 60


@dataclass
class AckResult:
    status: int
    code: str | None = None


class FakeEventService:
    def __init__(self, maximum_attempts=5, jitter_fraction=0.0, seed=0):
        self.maximum_attempts = maximum_attempts
        self.jitter_fraction = jitter_fraction
        self.rng = random.Random(seed)
        self.now = 0.0
        self.authorized = True
        self.events = []
        self.subscriptions = {}
        self.idempotency = {}
        self.dead_letters = []
        self.pending_failure = None
        self.ack_calls = 0

    def advance(self, seconds):
        self.now += seconds

    def fail_next_ack(self, mode):
        if mode not in ("before-apply", "after-apply"):
            raise ValueError("mode must be before-apply or after-apply")
        self.pending_failure = mode

    def publish(self, topic, payload, dedupe_key="dedupe-1", publisher_id=None):
        stamp = datetime.fromtimestamp(self.now, tz=timezone.utc).isoformat()
        event = {
            "id": str(uuid.uuid4()),
            "revision": 1,
            "created_at": stamp,
            "updated_at": stamp,
            "topic": topic,
            "version": "1.0.0",
            "publisher_id": publisher_id or str(uuid.uuid4()),
            "payload": payload,
            "dedupe_key": dedupe_key,
        }
        self.events.append(event)
        return event

    def create_subscription(self, topic):
        subscription_id = str(uuid.uuid4())
        self.subscriptions[subscription_id] = {
            "topic": topic,
            "cursor": 0,
            "attempt": 0,
            "delivery": None,
            "retry_at": 0.0,
            # The fake retains only token digests for completed ACK leases;
            # its event and idempotency data are already lifetime-scoped.
            "acked_leases": {},
        }
        return subscription_id

    def deliver(self, subscription_id):
        if not self.authorized:
            raise NotAuthorized("deliver rejected: credentials revoked")
        sub = self.subscriptions[subscription_id]
        delivery = sub["delivery"]
        if delivery is not None:
            if self.now < delivery["expires"]:
                return None
            if sub["attempt"] >= self.maximum_attempts:
                self._dead_letter(subscription_id, sub, "maximum_attempts")
                return None
            # Record the retry deadline once, measured from lease expiry even
            # if the first poll is late; this fake only keeps it in memory.
            sub["delivery"] = None
            sub["retry_at"] = delivery["expires"] + self._backoff(sub["attempt"])
        if self.now < sub["retry_at"]:
            return None
        topic_events = [e for e in self.events if e["topic"] == sub["topic"]]
        if sub["cursor"] >= len(topic_events):
            return None
        sub["attempt"] += 1
        if sub["attempt"] > self.maximum_attempts:
            self._dead_letter(subscription_id, sub, "maximum_attempts")
            return None
        event = topic_events[sub["cursor"]]
        delivery = {
            "delivery_id": str(uuid.uuid4()),
            "subscription_id": subscription_id,
            "event": event,
            "attempt": sub["attempt"],
            "lease_token": uuid.uuid4().hex + uuid.uuid4().hex,
            "lease_expires_at": datetime.fromtimestamp(
                self.now + LEASE_SECONDS, tz=timezone.utc).isoformat(),
            "expires": self.now + LEASE_SECONDS,
        }
        sub["delivery"] = delivery
        return {k: v for k, v in delivery.items() if k != "expires"}

    def ack(self, subscription_id, delivery_id, lease_token, outcome, idempotency_key):
        self.ack_calls += 1
        if not self.authorized:
            return AckResult(403, "FORBIDDEN")
        if outcome not in ("ack", "retry", "dead_letter"):
            return AckResult(400, "INVALID_OUTCOME")
        fingerprint = hashlib.sha256(json.dumps(
            [subscription_id, delivery_id, lease_token, outcome],
            separators=(",", ":")).encode("utf-8")).hexdigest()
        if idempotency_key in self.idempotency:
            seen_fingerprint, recorded = self.idempotency[idempotency_key]
            if seen_fingerprint != fingerprint:
                return AckResult(409, "IDEMPOTENCY_CONFLICT")
            return recorded
        if self.pending_failure == "before-apply":
            self.pending_failure = None
            raise TransportTimeout("simulated loss: request never arrived")
        result = self._apply_ack(subscription_id, delivery_id, lease_token, outcome)
        self.idempotency[idempotency_key] = (fingerprint, result)
        if self.pending_failure == "after-apply":
            self.pending_failure = None
            raise TransportTimeout("simulated loss: response lost after apply")
        return result

    def _apply_ack(self, subscription_id, delivery_id, lease_token, outcome):
        sub = self.subscriptions[subscription_id]
        delivery = sub["delivery"]
        previous = sub["acked_leases"].get(delivery_id)
        if previous is not None:
            valid_token = isinstance(lease_token, str) and hmac.compare_digest(
                previous, hashlib.sha256(lease_token.encode("utf-8", "surrogatepass")).digest())
            return AckResult(204) if valid_token and outcome == "ack" else AckResult(409, "STATE_CONFLICT")
        if delivery is None or delivery["delivery_id"] != delivery_id \
                or delivery["lease_token"] != lease_token:
            return AckResult(409, "STATE_CONFLICT")
        if self.now >= delivery["expires"]:
            return AckResult(409, "STATE_CONFLICT")
        if outcome == "ack":
            sub["acked_leases"][delivery_id] = hashlib.sha256(lease_token.encode("utf-8")).digest()
            self._terminate(sub)
        elif outcome == "dead_letter":
            self._dead_letter(subscription_id, sub, "consumer_dead_letter")
        elif sub["attempt"] >= self.maximum_attempts:
            self._dead_letter(subscription_id, sub, "maximum_attempts")
        else:
            delay = self._backoff(sub["attempt"])
            sub["delivery"] = None
            sub["retry_at"] = self.now + delay
        return AckResult(204)

    def _terminate(self, sub):
        sub["cursor"] += 1
        sub["attempt"] = 0
        sub["delivery"] = None
        sub["retry_at"] = 0.0

    def _dead_letter(self, subscription_id, sub, reason):
        topic_events = [e for e in self.events if e["topic"] == sub["topic"]]
        event = topic_events[sub["cursor"]]
        self.dead_letters.append((subscription_id, event["id"], reason))
        self._terminate(sub)

    def _backoff(self, attempt):
        base = BACKOFF_BASE_SECONDS[min(attempt - 1, len(BACKOFF_BASE_SECONDS) - 1)]
        delay = base * (1.0 + self.jitter_fraction * self.rng.random())
        return min(delay, BACKOFF_CAP_SECONDS)
