"""Offline contract tests for the reference event consumer.

Run from the repository root:

    python3 -m unittest discover -s examples/event-consumer -p 'test_*.py' -v
"""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from consumer import (Consumer, ConsumerError, IdempotencyConflict,
                      NotAuthorized, Store, TransportTimeout)
from fake_service import AckResult, FakeEventService, LEASE_SECONDS

TOPIC = "demo.orders"
SECRET_PAYLOAD = {"secret_field": "do-not-log-me"}


class ConsumerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xelab-consumer-test-")
        self.addCleanup(self.directory.cleanup)
        self.service = FakeEventService()
        self.event = self.service.publish(TOPIC, SECRET_PAYLOAD)
        self.subscription = self.service.create_subscription(TOPIC)
        self.store_path = os.path.join(self.directory.name, "consumer.sqlite")
        self.store = Store(self.store_path)
        self.addCleanup(self.store.close)
        self.consumer = Consumer(
            self.store, self.service, self.subscription, [TOPIC])

    def deliver(self):
        delivery = self.service.deliver(self.subscription)
        self.assertIsNotNone(delivery)
        return delivery


class HappyPathTests(ConsumerTestCase):
    def test_first_delivery_commits_then_confirms(self):
        report = self.consumer.handle(self.deliver())
        self.assertEqual(report["state"], "confirmed")
        self.assertFalse(report["deduplicated"])
        summary = self.store.report()
        self.assertEqual(summary["side_effects"], 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["ack_states"], {"confirmed": 1})
        self.assertEqual(self.store.processed_result(self.event["id"]),
                         self.consumer.business_result(self.event))
        self.assertIsNone(self.service.deliver(self.subscription))

    def test_ack_response_lost_retries_same_key(self):
        self.service.fail_next_ack("after-apply")
        report = self.consumer.handle(self.deliver())
        self.assertEqual(report["state"], "confirmed")
        self.assertEqual(self.store.report()["side_effects"], 1)
        self.assertEqual(self.store.report()["ack_states"], {"confirmed": 1})
        self.assertEqual(self.service.ack_calls, 2)

    def test_ack_request_lost_retries_same_key(self):
        self.service.fail_next_ack("before-apply")
        report = self.consumer.handle(self.deliver())
        self.assertEqual(report["state"], "confirmed")
        self.assertEqual(self.store.report()["side_effects"], 1)

    def test_both_ack_attempts_lost_leaves_prepared(self):
        with mock.patch.object(self.service, "ack",
                               side_effect=TransportTimeout("lost")):
            report = self.consumer.handle(self.deliver())
        self.assertEqual(report["state"], "prepared")
        summary = self.store.report()
        self.assertEqual(summary["side_effects"], 1)
        self.assertEqual(summary["ack_states"], {"prepared": 1})


class RedeliveryTests(ConsumerTestCase):
    def test_kernel_state_conflict_requires_subscription_recheck(self):
        delivery = self.deliver()
        # The kernel uses STATE_CONFLICT both for a stale lease and a disabled
        # subscription; the response alone does not distinguish them.
        with mock.patch.object(self.service, "ack", return_value=AckResult(409, "STATE_CONFLICT")) as ack:
            report = self.consumer.handle(delivery)
        self.assertEqual(report["state"], "state_conflict")
        self.assertEqual(ack.call_count, 1)
        self.assertEqual(self.store.report()["ack_states"], {"state_conflict": 1})

    def test_duplicate_event_redelivery_has_no_second_side_effect(self):
        with mock.patch.object(self.service, "ack",
                               side_effect=TransportTimeout("lost")):
            self.consumer.handle(self.deliver())
        self.service.advance(LEASE_SECONDS + 1)
        redelivery = self.deliver()
        self.assertEqual(redelivery["attempt"], 2)
        self.assertEqual(redelivery["event"]["id"], self.event["id"])
        report = self.consumer.handle(redelivery)
        self.assertEqual(report["state"], "confirmed")
        self.assertTrue(report["deduplicated"])
        self.assertEqual(self.store.report()["side_effects"], 1)

    def test_restart_replays_no_prepared_ack_and_dedupes_redelivery(self):
        with mock.patch.object(self.service, "ack",
                               side_effect=TransportTimeout("lost")):
            self.consumer.handle(self.deliver())
        self.store.close()
        reopened = Store(self.store_path)
        self.addCleanup(reopened.close)
        consumer = Consumer(reopened, self.service, self.subscription, [TOPIC])
        calls_before = self.service.ack_calls
        self.service.advance(LEASE_SECONDS + 1)
        report = consumer.handle(self.deliver())
        self.assertEqual(report["state"], "confirmed")
        self.assertTrue(report["deduplicated"])
        self.assertEqual(self.service.ack_calls, calls_before + 1)
        summary = reopened.report()
        self.assertEqual(summary["side_effects"], 1)
        self.assertEqual(summary["ack_states"], {"prepared": 1, "confirmed": 1})

    def test_stale_lease_ack_requires_recheck_before_redelivery(self):
        old = self.deliver()
        self.service.advance(LEASE_SECONDS + 1)
        fresh = self.deliver()
        self.assertNotEqual(fresh["delivery_id"], old["delivery_id"])
        self.assertNotEqual(fresh["lease_token"], old["lease_token"])
        stale_report = self.consumer.handle(old)
        self.assertEqual(stale_report["state"], "state_conflict")
        fresh_report = self.consumer.handle(fresh)
        self.assertEqual(fresh_report["state"], "confirmed")
        self.assertTrue(fresh_report["deduplicated"])
        self.assertEqual(self.store.report()["side_effects"], 1)

    def test_expired_lease_rejected_even_without_takeover(self):
        delivery = self.deliver()
        self.service.advance(LEASE_SECONDS + 1)
        result = self.service.ack(
            subscription_id=self.subscription,
            delivery_id=delivery["delivery_id"],
            lease_token=delivery["lease_token"],
            outcome="ack", idempotency_key="key-expired")
        self.assertEqual((result.status, result.code), (409, "STATE_CONFLICT"))


class AuthorizationTests(ConsumerTestCase):
    def test_revoked_credentials_stop_consumer(self):
        delivery = self.deliver()
        self.service.authorized = False
        with self.assertRaises(NotAuthorized):
            self.consumer.handle(delivery)
        self.assertEqual(self.store.report()["ack_states"], {"rejected": 1})

    def test_revoked_credentials_block_delivery(self):
        self.service.authorized = False
        with self.assertRaises(NotAuthorized):
            self.service.deliver(self.subscription)

    def test_disallowed_topic_is_dead_lettered(self):
        store = Store(os.path.join(self.directory.name, "other.sqlite"))
        self.addCleanup(store.close)
        service = FakeEventService()
        service.publish("demo.unexpected", SECRET_PAYLOAD)
        subscription = service.create_subscription("demo.unexpected")
        consumer = Consumer(store, service, subscription, [TOPIC])
        report = consumer.handle(service.deliver(subscription))
        self.assertEqual(report["outcome"], "dead_letter")
        self.assertEqual(report["state"], "confirmed")
        self.assertEqual(len(service.dead_letters), 1)
        self.assertEqual(store.report()["side_effects"], 0)


class ServiceContractTests(ConsumerTestCase):
    def test_retry_backoff_gates_redelivery(self):
        delivery = self.deliver()
        for attempt, delay in ((1, 1), (2, 2)):
            result = self.service.ack(
                subscription_id=self.subscription,
                delivery_id=delivery["delivery_id"],
                lease_token=delivery["lease_token"],
                outcome="retry", idempotency_key=f"key-retry-{attempt}")
            self.assertEqual(result.status, 204)
            self.assertIsNone(self.service.deliver(self.subscription))
            self.service.advance(delay - 0.01)
            self.assertIsNone(self.service.deliver(self.subscription))
            self.service.advance(0.01)
            delivery = self.deliver()
            self.assertEqual(delivery["attempt"], attempt + 1)
        self.assertEqual(self.service._backoff(4), 8)
        self.assertEqual(self.service._backoff(5), 16)
        self.assertEqual(self.service._backoff(6), 16)

    def test_maximum_attempts_dead_letters_and_advances(self):
        service = FakeEventService(maximum_attempts=3)
        service.publish(TOPIC, SECRET_PAYLOAD)
        subscription = service.create_subscription(TOPIC)
        for attempt in (1, 2, 3):
            delivery = service.deliver(subscription)
            self.assertEqual(delivery["attempt"], attempt)
            result = service.ack(
                subscription_id=subscription,
                delivery_id=delivery["delivery_id"],
                lease_token=delivery["lease_token"],
                outcome="retry", idempotency_key=f"key-max-{attempt}")
            self.assertEqual(result.status, 204)
            service.advance(60)
        self.assertEqual(len(service.dead_letters), 1)
        self.assertEqual(service.dead_letters[0][2], "maximum_attempts")
        self.assertIsNone(service.deliver(subscription))

    def test_reack_same_lease_succeeds_and_key_replay_is_recorded(self):
        delivery = self.deliver()
        request = dict(subscription_id=self.subscription,
                       delivery_id=delivery["delivery_id"],
                       lease_token=delivery["lease_token"], outcome="ack")
        first = self.service.ack(idempotency_key="key-a", **request)
        replay = self.service.ack(idempotency_key="key-a", **request)
        reack = self.service.ack(idempotency_key="key-b", **request)
        self.assertEqual((first.status, replay.status, reack.status), (204, 204, 204))
        conflict = self.service.ack(idempotency_key="key-a",
                                    **{**request, "outcome": "retry"})
        self.assertEqual((conflict.status, conflict.code),
                         (409, "IDEMPOTENCY_CONFLICT"))

    def test_consumer_surfaces_idempotency_conflict_without_replay(self):
        delivery = self.deliver()
        key = self.store.prepare_ack(
            self.subscription, delivery["delivery_id"], self.event["id"], "ack")
        self.service.idempotency[key] = ("other-fingerprint", AckResult(204))
        with self.assertRaises(IdempotencyConflict):
            self.consumer._perform_ack(key, delivery, "ack", deduplicated=False)
        self.assertEqual(self.store.report()["ack_states"], {"conflict": 1})
        self.assertEqual(self.service.ack_calls, 1)


class SafetyTests(ConsumerTestCase):
    def test_store_file_is_owner_only(self):
        mode = stat.S_IMODE(os.stat(self.store_path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_no_payload_or_lease_token_in_store_or_report(self):
        delivery = self.deliver()
        report = self.consumer.handle(delivery)
        self.store.close()
        with open(self.store_path, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"do-not-log-me", raw)
        self.assertNotIn(delivery["lease_token"].encode("utf-8"), raw)
        self.assertNotIn("do-not-log-me", repr(report))
        self.assertNotIn(delivery["lease_token"], repr(report))
        self.assertNotIn("payload", report)

    def test_malformed_delivery_rejected(self):
        delivery = self.deliver()
        token = delivery["lease_token"]
        del delivery["lease_token"]
        with self.assertRaises(ConsumerError):
            self.consumer.handle(delivery)
        other = {**delivery, "lease_token": token,
                 "subscription_id": "not-my-subscription"}
        with self.assertRaises(ConsumerError):
            self.consumer.handle(other)
        self.assertEqual(self.store.report()["side_effects"], 0)


if __name__ == "__main__":
    unittest.main()
