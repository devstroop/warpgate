"""Async task registry + worker pool for mutating API operations.

Tasks live in memory only and are lost when the manager restarts — clients
poll ``GET /v1/tasks/{id}`` and should treat ``TASK_NOT_FOUND`` after a
restart as a dropped operation.  The list is bounded (``TASK_MAX_ITEMS``)
and finished tasks are pruned by age (``TASK_MAX_AGE``).
"""

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config

log = logging.getLogger("warpgate.tasks")

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


class TaskError(Exception):
    """A failed task carries a machine-readable ``code`` alongside the message.

    ``registry.submit`` maps an unannotated exception to ``INTERNAL``; raising
    ``TaskError`` gives API clients a code to react to (e.g. ``PROXY_NOT_FOUND``
    for a delete that raced another delete).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Task:
    __slots__ = (
        "id", "type", "status", "result", "error", "error_code",
        "created_at", "started_at", "finished_at",
    )

    def __init__(self, type_: str):
        self.id = uuid.uuid4().hex
        self.type = type_
        self.status = PENDING
        self.result = None
        self.error = None
        self.error_code = None
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None

    def start(self):
        self.started_at = time.time()
        self.status = RUNNING

    def succeed(self, result):
        self.result = result
        self.status = SUCCEEDED
        self.finished_at = time.time()

    def fail(self, error):
        if isinstance(error, TaskError):
            self.error_code = error.code
            self.error = str(error)
        else:
            self.error_code = "INTERNAL"
            self.error = str(error)
        self.status = FAILED
        self.finished_at = time.time()


class TaskRegistry:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._idem: dict[str, str] = {}
        self._idem_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=config.TASK_WORKERS,
            thread_name_prefix="task-worker",
        )

    def create(self, type_: str) -> Task:
        task = Task(type_)
        with self._lock:
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._prune_locked()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def submit(self, task: Task, fn, *args, **kwargs) -> None:
        def run():
            try:
                task.start()
                result = fn(*args, **kwargs)
                task.succeed(result)
            except Exception as exc:
                log.error("task %s (%s) failed: %s", task.id, task.type, exc)
                task.fail(exc)
        self._executor.submit(run)

    def get_idempotent(self, key: str) -> Task | None:
        """Return the task previously recorded for an Idempotency-Key, if any."""
        with self._idem_lock:
            task_id = self._idem.get(key)
            if task_id is None:
                return None
            task = self._tasks.get(task_id)
            if task is None:
                self._idem.pop(key, None)
                return None
            return task

    def set_idempotent(self, key: str, task_id: str) -> None:
        with self._idem_lock:
            self._idem[key] = task_id
            self._prune_idem_locked()

    def _prune_idem_locked(self) -> None:
        cutoff = time.time() - config.IDEMPOTENCY_MAX_AGE
        stale = [
            key for key, task_id in self._idem.items()
            if self._tasks.get(task_id) is None or self._tasks[task_id].created_at < cutoff
        ]
        for key in stale:
            self._idem.pop(key, None)
        if len(self._idem) > config.TASK_MAX_ITEMS:
            oldest = sorted(self._idem, key=lambda k: self._tasks[self._idem[k]].created_at)
            for key in oldest[: len(self._idem) - config.TASK_MAX_ITEMS]:
                self._idem.pop(key, None)

    def _prune_locked(self) -> None:
        # Hard cap on retained tasks (newest kept)
        while len(self._tasks) > config.TASK_MAX_ITEMS:
            oldest = self._order.pop(0)
            self._tasks.pop(oldest, None)
        # Age-based pruning of finished tasks
        cutoff = time.time() - config.TASK_MAX_AGE
        stale = [
            tid for tid in self._order
            if self._tasks[tid].finished_at is not None and self._tasks[tid].finished_at < cutoff
        ]
        for tid in stale:
            self._order.remove(tid)
            self._tasks.pop(tid, None)


registry = TaskRegistry()
