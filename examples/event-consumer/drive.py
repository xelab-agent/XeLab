"""Offline demo driver for the reference event consumer.

Runs the contract scenarios against fake_service.FakeEventService and prints
a transcript. No network, no privileged dependencies. Run:

    python3 examples/event-consumer/drive.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from consumer import Consumer, NotAuthorized, Store, TransportTimeout
from fake_service import FakeEventService, LEASE_SECONDS

TOPIC = "demo.orders"
PAYLOAD = {"secret_field": "do-not-log-me"}


def make_consumer(directory, service, subscription_id, name="consumer.sqlite"):
    store = Store(os.path.join(directory, name))
    return Consumer(store, service, subscription_id, [TOPIC]), store


def scenario_happy(directory):
    service = FakeEventService()
    service.publish(TOPIC, PAYLOAD)
    sub = service.create_subscription(TOPIC)
    consumer, store = make_consumer(directory, service, sub)
    report = consumer.handle(service.deliver(sub))
    assert report["state"] == "confirmed" and not report["deduplicated"]
    assert store.report()["side_effects"] == 1
    assert service.deliver(sub) is None
    store.close()
    return "first delivery: side effect committed, ack confirmed"


def scenario_ack_response_lost(directory):
    service = FakeEventService()
    service.publish(TOPIC, PAYLOAD)
    sub = service.create_subscription(TOPIC)
    consumer, store = make_consumer(directory, service, sub)
    service.fail_next_ack("after-apply")
    report = consumer.handle(service.deliver(sub))
    assert report["state"] == "confirmed"
    assert store.report()["side_effects"] == 1
    assert store.report()["ack_states"] == {"confirmed": 1}
    store.close()
    return "ack response lost: same-key retry confirmed, one side effect"


def scenario_ack_request_lost_then_restart(directory):
    service = FakeEventService()
    service.publish(TOPIC, PAYLOAD)
    sub = service.create_subscription(TOPIC)
    consumer, store = make_consumer(directory, service, sub)
    # Simulate both the request and the same-key retry being lost, then a crash.
    original_ack = service.ack

    def losing_ack(**kwargs):
        raise TransportTimeout("simulated crash window")

    service.ack = losing_ack
    report = consumer.handle(service.deliver(sub))
    assert report["state"] == "prepared"
    store.close()  # crash: prepared intent survives, lease token does not

    service.ack = original_ack
    service.advance(LEASE_SECONDS + 1)
    consumer2, store2 = make_consumer(directory, service, sub)
    redelivery = service.deliver(sub)
    assert redelivery["attempt"] == 2
    report2 = consumer2.handle(redelivery)
    assert report2["state"] == "confirmed" and report2["deduplicated"]
    assert store2.report()["side_effects"] == 1
    assert store2.report()["ack_states"].get("prepared") == 1
    store2.close()
    return ("ack request lost + restart: redelivery deduplicated, "
            "prepared intent never auto-replayed")


def scenario_stale_lease(directory):
    service = FakeEventService()
    service.publish(TOPIC, PAYLOAD)
    sub = service.create_subscription(TOPIC)
    consumer, store = make_consumer(directory, service, sub)
    old = service.deliver(sub)
    service.advance(LEASE_SECONDS + 1)
    fresh = service.deliver(sub)
    assert fresh["delivery_id"] != old["delivery_id"]
    stale_report = consumer.handle(old)
    assert stale_report["state"] == "state_conflict"
    assert store.report()["side_effects"] == 1  # old handle committed before ack
    fresh_report = consumer.handle(fresh)
    assert fresh_report["state"] == "confirmed" and fresh_report["deduplicated"]
    assert store.report()["side_effects"] == 1
    store.close()
    return "stale lease ack: 409 state conflict, simulated redelivery deduplicated"


def scenario_revoked(directory):
    service = FakeEventService()
    service.publish(TOPIC, PAYLOAD)
    sub = service.create_subscription(TOPIC)
    consumer, store = make_consumer(directory, service, sub)
    delivery = service.deliver(sub)
    service.authorized = False
    try:
        consumer.handle(delivery)
    except NotAuthorized:
        store.close()
        return "revoked credentials: consumer stops with NotAuthorized"
    raise AssertionError("expected NotAuthorized")


def scenario_dead_letter(directory):
    service = FakeEventService()
    service.publish("demo.unexpected", PAYLOAD)
    sub = service.create_subscription("demo.unexpected")
    store = Store(os.path.join(directory, "consumer.sqlite"))
    consumer = Consumer(store, service, sub, [TOPIC])
    report = consumer.handle(service.deliver(sub))
    assert report["state"] == "confirmed" and report["outcome"] == "dead_letter"
    assert len(service.dead_letters) == 1
    assert store.report()["side_effects"] == 0
    store.close()
    return "disallowed topic: dead letter, no side effect"


SCENARIOS = (
    scenario_happy,
    scenario_ack_response_lost,
    scenario_ack_request_lost_then_restart,
    scenario_stale_lease,
    scenario_revoked,
    scenario_dead_letter,
)


def main():
    failed = 0
    for scenario in SCENARIOS:
        with tempfile.TemporaryDirectory(prefix="xelab-consumer-") as directory:
            try:
                message = scenario(directory)
            except Exception as error:
                failed += 1
                print(f"FAIL {scenario.__name__}: {type(error).__name__}: {error}")
            else:
                print(f"PASS {scenario.__name__}: {message}")
    print(f"{len(SCENARIOS) - failed}/{len(SCENARIOS)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
