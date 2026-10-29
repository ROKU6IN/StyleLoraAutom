import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from stylelora.runtime.errors import ServiceError
from stylelora.runtime.events import EventBus


class EventTests(unittest.TestCase):
    def test_ordered_replay_filters_and_epoch_resync(self):
        bus = EventBus(str(uuid4()))
        start = f"{bus.epoch}:0"
        bus.publish("image.updated", {"image_ids": [1]}, project_id="p", resource={"kind": "image", "id": 1})
        bus.publish("source.started", {"source_id": 2}, project_id="p")
        sub, frames = bus.subscribe(after=start, topics=("image",), project_id="p", resource_id="1")
        self.assertEqual([f["type"] for f in frames], ["image.updated", "stream.ready"])
        self.assertTrue(frames[-1]["data"]["payload"]["replayed"])
        bus.unsubscribe(sub)
        sub, frames = bus.subscribe(after=f"{uuid4()}:1")
        self.assertEqual(frames[0]["data"]["payload"]["reason"], "epoch_changed")
        self.assertIn(b"event: stream.resync_required", bus.encode(frames[0]))

    def test_capacity_time_expiration_and_future_id(self):
        bus = EventBus(str(uuid4()), capacity=2)
        for i in range(3):
            bus.publish("image.updated", {"image_ids": [i]})
        for after in (f"{bus.epoch}:0", f"{bus.epoch}:999"):
            sub, frames = bus.subscribe(after=after)
            self.assertEqual(frames[0]["type"], "stream.resync_required")
            bus.unsubscribe(sub)
        with patch("stylelora.runtime.events.time.monotonic", return_value=time.monotonic()+601):
            sub, frames = bus.subscribe(after=f"{bus.epoch}:2")
        self.assertEqual(frames[0]["data"]["payload"]["reason"], "buffer_expired")

    def test_slow_consumers_have_bounded_count_and_bytes(self):
        for count, payload in ((2, {}), (256, {"text": "x"*50000})):
            bus = EventBus(str(uuid4()), queue_limit=count)
            sub, _ = bus.subscribe()
            for _ in range(25):
                bus.publish("image.updated", payload)
            self.assertTrue(sub.closed)
            self.assertEqual(sub.queue.qsize(), 1)
            self.assertLess(sub.queue.bytes, 1024*1024)
            self.assertEqual(sub.queue.get()["data"]["payload"]["reason"], "slow_consumer")

    def test_subscriber_limit_validation_and_project_close(self):
        bus = EventBus(str(uuid4()))
        for value in ("invalid", "123:1", f"{uuid4()}:-1"):
            with self.assertRaises(ServiceError):
                bus.subscribe(after=value)
        with self.assertRaises(ServiceError):
            bus.subscribe(topics=("arbitrary",))
        subs = [bus.subscribe(project_id="p")[0] for _ in range(4)]
        with self.assertRaises(ServiceError) as caught:
            bus.subscribe()
        self.assertEqual(caught.exception.status, 429)
        bus.close_project("p")
        self.assertTrue(all(sub.closed for sub in subs))
        for sub in subs:
            bus.unsubscribe(sub)
        bus.subscribe()
