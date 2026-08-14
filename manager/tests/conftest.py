"""Shared fixtures: a fake Docker client, deterministic probes, clean state."""

import re
import time

import pytest
import docker.errors

from warpgate_manager import pool as poolmod
from warpgate_manager import server
from warpgate_manager.tasks import registry


class FakeContainer:
    def __init__(self, name, api=None, cid=None, status="running"):
        self.name = name
        self.id = cid or ("cid-" + name)
        self.short_id = self.id[:12]
        self.status = status
        self._api = api
        self.started = False
        self.executed_cmds = []
        self.mounts = None

    def start(self):
        self.started = True

    def stop(self, timeout=None):
        self.status = "exited"

    def remove(self, force=False):
        if self._api is not None:
            self._api.containers_by_id.pop(self.id, None)
            self._api.by_name.pop(self.name, None)

    def exec_run(self, cmd, **kwargs):
        self.executed_cmds.append(cmd)
        return (0, b"Registration status: Complete\nStatus update: Connected\n")


class FakeVolume:
    def __init__(self, name, api=None):
        self.name = name
        self._api = api
        self.removed = False

    def remove(self, force=False):
        self.removed = True
        if self._api is not None:
            self._api.volumes.pop(self.name, None)


class FakeVolumesAPI:
    def __init__(self):
        self.volumes = {}

    def get(self, name):
        v = self.volumes.get(name)
        if v is None:
            raise docker.errors.NotFound("no such volume")
        return v

    def create(self, name):
        v = FakeVolume(name, api=self)
        self.volumes[name] = v
        return v


class FakeContainersAPI:
    def __init__(self):
        self.containers_by_id = {}
        self.by_name = {}
        self._seq = 0

    def create(self, **kwargs):
        name = kwargs["name"]
        self._seq += 1
        c = FakeContainer(name, api=self, cid=f"cid-{self._seq}")
        c.mounts = kwargs.get("mounts")
        self.containers_by_id[c.id] = c
        self.by_name[name] = c
        return c

    def get(self, key):
        c = self.containers_by_id.get(key) or self.by_name.get(key)
        if c is None:
            raise docker.errors.NotFound("no such container")
        return c

    def list(self, all=False, filters=None):
        result = [c for c in self.containers_by_id.values() if c.status == "running"]
        if filters and filters.get("name"):
            pattern = filters["name"]
            result = [c for c in result if re.search(pattern, c.name)]
        return result


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainersAPI()
        self.volumes = FakeVolumesAPI()


@pytest.fixture
def fake_docker(monkeypatch):
    client = FakeDockerClient()
    monkeypatch.setattr(poolmod, "get_docker_client", lambda: client)
    monkeypatch.setattr(poolmod, "socks5_is_alive", lambda host, port=1080, timeout=5.0: True)
    monkeypatch.setattr(poolmod, "http_is_alive", lambda *a, **k: True)
    return client


@pytest.fixture
def clean_state():
    with poolmod.pool_lock:
        poolmod.pool[:] = []
    poolmod.target_count = 0
    registry._tasks.clear()
    registry._order.clear()
    registry._idem.clear()
    server._rate_limit_store.clear()
    yield
    with poolmod.pool_lock:
        poolmod.pool[:] = []
    registry._tasks.clear()
    registry._order.clear()
    registry._idem.clear()


@pytest.fixture
def no_auth(monkeypatch):
    monkeypatch.setattr(server, "_get_api_key", lambda: None)


@pytest.fixture
def client(fake_docker, clean_state, no_auth):
    server.app.config["TESTING"] = True
    return server.app.test_client()


@pytest.fixture
def auth_client(fake_docker, clean_state, monkeypatch):
    monkeypatch.setattr(server, "_get_api_key", lambda: "test-secret")
    server.app.config["TESTING"] = True
    return server.app.test_client()


def make_proxy(name, **kwargs):
    with poolmod.pool_lock:
        ep = poolmod.ProxyEndpoint(name=name, container_id=f"cid-{name}", **kwargs)
        poolmod.pool.append(ep)
        return ep


def wait_for_task(task, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = registry.get(task.id)
        if cur.status in ("succeeded", "failed"):
            return cur
        time.sleep(0.02)
    raise AssertionError("task did not finish in time")
