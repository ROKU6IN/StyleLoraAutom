import json
import queue
import threading
import time
from collections import deque
from uuid import UUID, uuid4

from ..project_db.validation import utc_now
from .errors import ServiceError


class EventQueue(queue.Queue):
    def __init__(self, limit, max_bytes=1024*1024):
        super().__init__(limit)
        self.byte_limit, self.bytes = max_bytes, 0

    def _put(self, item):
        size = len(json.dumps(item, ensure_ascii=False).encode())
        if self.bytes + size > self.byte_limit:
            raise queue.Full
        self.bytes += size
        super()._put(item)

    def _get(self):
        item = super()._get()
        self.bytes -= len(json.dumps(item, ensure_ascii=False).encode())
        return item


class Subscription:
    def __init__(self, topics, project_id, resource_id, limit):
        self.topics, self.project_id, self.resource_id = topics, project_id, resource_id
        self.queue = EventQueue(limit)
        self.closed = False

    def matches(self, event):
        return ((not self.topics or event["type"].split(".")[0] in self.topics)
                and (self.project_id is None or event["data"]["project_id"] == self.project_id)
                and (self.resource_id is None or str(event["data"]["resource"]["id"]) == self.resource_id))


class EventBus:
    def __init__(self, instance_id, *, capacity=10000, max_bytes=8*1024*1024, queue_limit=256):
        self.instance_id, self.epoch = instance_id, str(uuid4())
        self.capacity, self.max_bytes, self.queue_limit = capacity, max_bytes, queue_limit
        self.sequence, self.bytes = 0, 0
        self._events, self._subscribers = deque(), set()
        self._lock = threading.RLock()

    def _frame(self, kind, payload, project_id=None, resource=None, version=None):
        data = {"schema_version": 1, "epoch": self.epoch, "sequence": self.sequence, "occurred_at": utc_now(),
                "project_id": project_id, "resource": resource or {"kind": "operation", "id": None},
                "version": version, "payload": payload}
        return {"id": f"{self.epoch}:{self.sequence}", "type": kind, "data": data}

    def resync(self, reason):
        return self._frame("stream.resync_required", {"reason": reason, "snapshot_urls": ["/api/session", "/api/projects/current"], "current_sequence": self.sequence})

    def _expire(self):
        now = time.monotonic()
        while self._events and (len(self._events) > self.capacity or self.bytes > self.max_bytes or now-self._events[0][0] > 600):
            self.bytes -= self._events.popleft()[1]

    def publish(self, kind, payload, *, project_id=None, resource=None, version=None):
        with self._lock:
            self.sequence += 1
            frame = self._frame(kind, payload, project_id, resource, version)
            size = len(json.dumps(frame, ensure_ascii=False).encode())
            if size > 65536:
                raise ValueError("event exceeds frame budget")
            now = time.monotonic()
            self._events.append((now, size, frame))
            self.bytes += size
            self._expire()
            for sub in tuple(self._subscribers):
                if sub.closed or not sub.matches(frame):
                    continue
                try:
                    sub.queue.put_nowait(frame)
                except queue.Full:
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    sub.queue.put_nowait(self.resync("slow_consumer"))
                    sub.closed = True

    def subscribe(self, *, after=None, topics=(), project_id=None, resource_id=None):
        if set(topics) - {"project", "source", "image", "annotations", "export", "operation", "server", "catalog", "scheme", "backend", "job", "tagger", "llm"}:
            raise ServiceError(422, "event:invalid_topics")
        with self._lock:
            self._expire()
            if len(self._subscribers) >= 4:
                raise ServiceError(429, "event:subscription_limit")
            replay, replayed = [], False
            sub = Subscription(tuple(topics), project_id, resource_id, self.queue_limit)
            if after:
                try:
                    epoch, sequence = after.split(":")
                    if str(UUID(epoch)) != epoch or not sequence.isdecimal():
                        raise ValueError()
                    sequence = int(sequence)
                except (ValueError, TypeError) as exc:
                    raise ServiceError(400, "event:invalid_id") from exc
                first = self._events[0][2]["data"]["sequence"] if self._events else self.sequence + 1
                if epoch != self.epoch:
                    replay.append(self.resync("epoch_changed"))
                elif sequence < first-1 or sequence > self.sequence:
                    replay.append(self.resync("buffer_expired"))
                else:
                    replay = [frame for _, _, frame in self._events if frame["data"]["sequence"] > sequence and sub.matches(frame)]
                    replayed = True
            ready = self._frame("stream.ready", {"instance_id": self.instance_id, "current_sequence": self.sequence, "replayed": replayed})
            self._subscribers.add(sub)
            return sub, [*replay, ready]

    def unsubscribe(self, sub):
        with self._lock:
            sub.closed = True
            self._subscribers.discard(sub)

    def close_project(self, project_id):
        with self._lock:
            for sub in self._subscribers:
                if sub.project_id == project_id:
                    sub.closed = True

    def close(self):
        with self._lock:
            for sub in self._subscribers:
                sub.closed = True

    @staticmethod
    def encode(frame):
        return (f"id: {frame['id']}\nevent: {frame['type']}\ndata: " + json.dumps(frame["data"], ensure_ascii=False, separators=(",", ":")) + "\n\n").encode()
