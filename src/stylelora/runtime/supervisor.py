import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from uuid import uuid4

from ..project_db.validation import primitive
from .errors import ServiceError, safe_error

TERMINAL = {"done", "failed", "canceled", "interrupted"}


@dataclass
class Task:
    operation_id: str
    kind: str
    project_id: str | None
    status: str = "pending"
    completed: int = 0
    total: int | None = None
    result: object = None
    error: object = None
    revision: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    cancelable: bool = True
    source_id: int | None = None

    def view(self):
        return {"operation_id": self.operation_id, "kind": self.kind, "project_id": self.project_id,
                "status": self.status, "completed": self.completed, "total": self.total, "result": self.result,
                "error": self.error, "version": f"{self.operation_id}-{self.revision}"}


class Supervisor:
    def __init__(self, runtime, workers=2):
        self.runtime = runtime
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="stylelora-files")
        self.control = ThreadPoolExecutor(1, thread_name_prefix="stylelora-lifecycle")
        self.tasks, self.lock = {}, threading.RLock()

    def submit(self, kind, fn, *, context=None, control=False, finalizer=None, cancelable=None):
        with self.lock:
            if sum(not task.finished.is_set() for task in self.tasks.values()) >= 32:
                raise ServiceError(429, "task:queue_full")
            if self.runtime.draining:
                raise ServiceError(503, "process:draining")
            if context:
                self.runtime.retain(context)
            task = Task(str(uuid4()), kind, context.project_id if context else None,
                        cancelable=not control if cancelable is None else cancelable)
            self.tasks[task.operation_id] = task
            try:
                (self.control if control else self.pool).submit(self._run, task, fn, context, finalizer)
            except BaseException:
                del self.tasks[task.operation_id]
                if context:
                    self.runtime.release(context)
                raise
            return task.view()

    def _run(self, task, fn, context, finalizer):
        try:
            with self.lock:
                task.status = "canceled" if task.cancel.is_set() else "running"
                task.revision += 1
            self.notify(task)
            if task.status != "canceled":
                result = fn(task)
                with self.lock:
                    task.result = primitive(result)
                    task.status = "canceled" if task.cancel.is_set() and task.kind.startswith(("import", "filesystem")) else "done"
                    task.revision += 1
        except BaseException as exc:
            with self.lock:
                task.status = "canceled" if task.cancel.is_set() else "failed"
                task.error = safe_error(exc).body(str(uuid4()))
                task.revision += 1
        finally:
            self.notify(task)
            if finalizer:
                finalizer()
            if context:
                self.runtime.release(context)
            task.finished.set()

    def notify(self, task):
        self.runtime.emit("operation." + (task.status if task.status in TERMINAL else "progress"),
                          {"operation_id": task.operation_id, "kind": task.kind, "completed": task.completed,
                           "total": task.total, "phase": task.status, "reason_code": task.error["code"] if task.error else None,
                           "result_url": f"/api/operations/{task.operation_id}"}, project_id=task.project_id,
                          resource={"kind": "operation", "id": task.operation_id}, version=task.view()["version"])

    def progress(self, task, *, completed, total):
        with self.lock:
            task.completed, task.total = completed, total
        self.notify(task)

    def get(self, operation_id):
        with self.lock:
            task = self.tasks.get(operation_id)
            if task is None:
                raise ServiceError(410, "session:expired")
            if task.project_id and not self.runtime.is_current(task.project_id, allow_closing=True):
                raise ServiceError(410, "project:handle_expired")
            return task.view()

    def list(self, project_id=None, limit=50, cursor=None):
        if project_id:
            self.runtime.require(project_id)
        identity = f"{self.runtime.instance_id}:{self.runtime.generation}:operations:{project_id}"
        state = self.runtime.cursors.decode(cursor, identity) if cursor else None
        with self.lock:
            values = [task for task in self.tasks.values() if task.project_id is None or self.runtime.is_current(task.project_id, allow_closing=True)]
            if project_id:
                values = [task for task in values if task.project_id in (None, project_id)]
            upper = state["upper"] if state else len(values)
            start = state["last"] if state else 0
            chosen = values[:upper][start:start+limit]
            items = [{**task.view(), "result": None, "result_url": f"/api/operations/{task.operation_id}"} for task in chosen]
        more = start+len(chosen) < min(upper, len(values))
        return {"items": items, "limit": limit, "has_more": more, "upper_id": upper,
                "next_cursor": self.runtime.cursors.encode({"identity": identity, "upper": upper, "last": start+limit}) if more else None}

    def cancel_task(self, operation_id, expected):
        from .contracts import Versions
        with self.lock:
            view = self.get(operation_id)
            task = self.tasks[operation_id]
            Versions.require(expected, view["version"])
            if task.kind.startswith('job.'):
                raise ServiceError(409, 'operation:use_job_control')
            if task.kind.startswith('prompt.'):
                raise ServiceError(409, 'operation:use_prompt_control')
            if not task.cancelable:
                raise ServiceError(409, "operation:not_cancelable")
            if task.status not in TERMINAL:
                task.cancel.set()
            return task.view()

    def stop_project(self, project_id):
        with self.lock:
            for task in self.tasks.values():
                if task.project_id == project_id and not task.finished.is_set():
                    task.cancel.set()

    def close(self):
        with self.lock:
            for task in self.tasks.values():
                if task.cancelable:
                    task.cancel.set()
        self.control.shutdown(wait=True)
        self.pool.shutdown(wait=True)
