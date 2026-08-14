"""Task registry and worker behavior tests."""

import time

from warpgate_manager.tasks import FAILED, PENDING, RUNNING, SUCCEEDED, TaskError, registry

from tests.conftest import wait_for_task


def test_create_get_roundtrip(clean_state):
    task = registry.create("proxy.create")
    assert registry.get(task.id) is not None
    assert registry.get("does-not-exist") is None


def test_submit_success(clean_state):
    task = registry.create("pool.scale")
    registry.submit(task, lambda x=42: {"target": x})
    done = wait_for_task(task)
    assert done.status == SUCCEEDED
    assert done.result == {"target": 42}
    assert done.started_at is not None
    assert done.finished_at is not None


def test_submit_failure(clean_state):
    task = registry.create("pool.scale")

    def boom():
        raise ValueError("kaboom")

    registry.submit(task, boom)
    done = wait_for_task(task)
    assert done.status == FAILED
    assert "kaboom" in done.error


def test_pending_task_has_no_start_time(clean_state):
    task = registry.create("proxy.create")
    assert task.status == PENDING
    assert task.started_at is None


def test_fail_task_error_code(clean_state):
    task = registry.create("pool.scale")

    def boom():
        raise TaskError("PROXY_NOT_FOUND", "proxy gone")

    registry.submit(task, boom)
    done = wait_for_task(task)
    assert done.status == FAILED
    assert done.error_code == "PROXY_NOT_FOUND"
    assert done.error == "proxy gone"


def test_fail_unannotated_error_code_internal(clean_state):
    task = registry.create("pool.scale")

    def boom():
        raise ValueError("kaboom")

    registry.submit(task, boom)
    done = wait_for_task(task)
    assert done.status == FAILED
    assert done.error_code == "INTERNAL"


def test_idempotency_key_roundtrip(clean_state):
    a = registry.create("pool.scale")
    registry.set_idempotent("k1", a.id)
    assert registry.get_idempotent("k1") is a


def test_idempotency_key_dropped_when_task_pruned(clean_state):
    a = registry.create("pool.scale")
    registry.set_idempotent("k1", a.id)
    registry._tasks.pop(a.id, None)
    registry._order.remove(a.id)
    assert registry.get_idempotent("k1") is None
