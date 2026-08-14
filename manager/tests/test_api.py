"""End-to-end API tests against the Flask test client (fake Docker)."""

import os

import pytest

from warpgate_manager import config, pool as poolmod, server
from warpgate_manager.tasks import registry

from tests.conftest import FakeContainer, make_proxy, wait_for_task


def seed_container(client, ep):
    c = FakeContainer(ep.name, api=client.containers, cid=ep.container_id)
    client.containers.containers_by_id[c.id] = c
    client.containers.by_name[c.name] = c
    return c


# ── Health / readiness ──────────────────────────────────────────────────

def test_health_returns_200_and_shape(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"status", "pool_size", "healthy", "degraded"}
    assert body["status"] == "initializing"
    assert "X-Request-ID" in resp.headers


def test_health_exempt_from_auth(auth_client):
    assert auth_client.get("/v1/health").status_code == 200


def test_ready_503_when_empty(client):
    assert client.get("/v1/ready").status_code == 503


def test_ready_200_when_healthy_proxy_exists(client):
    make_proxy("warpgate-aaa11111", healthy=True)
    resp = client.get("/v1/ready")
    assert resp.status_code == 200
    assert resp.get_json()["healthy"] == 1


# ── Auth ────────────────────────────────────────────────────────────────

def test_auth_required(auth_client):
    assert auth_client.get("/v1/pool").status_code == 401
    resp = auth_client.get("/v1/pool", headers={"Authorization": "Bearer test-secret"})
    assert resp.status_code == 200


def test_bad_key_and_rate_limit(auth_client, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_RATE_LIMIT", 3)
    for _ in range(3):
        assert auth_client.get("/v1/pool").status_code == 401
    assert auth_client.get("/v1/pool").status_code == 429


def test_request_id_present_on_errors(auth_client):
    resp = auth_client.get("/v1/pool")
    assert resp.status_code == 401
    assert "X-Request-ID" in resp.headers
    assert resp.get_json()["error"]["code"] == "UNAUTHORIZED"
    assert resp.get_json()["error"]["request_id"] == resp.headers["X-Request-ID"]


# ── Pool ────────────────────────────────────────────────────────────────

def test_get_pool_summary(client):
    make_proxy("warpgate-aaa11111", healthy=True)
    make_proxy("warpgate-bbb22222", healthy=False)
    resp = client.get("/v1/pool")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pool_size"] == 2
    assert body["healthy"] == 1
    assert body["degraded"] == 1
    assert len(body["proxies"]) == 2


def test_get_pool_can_omit_proxies(client):
    make_proxy("warpgate-aaa11111", healthy=True)
    body = client.get("/v1/pool?include=none").get_json()
    assert "proxies" not in body


def test_scale_pool(client, fake_docker):
    resp = client.patch("/v1/pool", json={"target": 2})
    assert resp.status_code == 202
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    with poolmod.pool_lock:
        assert len(poolmod.pool) == 2
        assert poolmod.target_count == 2


def test_scale_invalid_target(client):
    resp = client.patch("/v1/pool", json={"target": "many"})
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_pool_at_capacity_returns_409(client, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_MAX_POOL", 1)
    make_proxy("warpgate-aaa11111")
    resp = client.post("/v1/proxies")
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "POOL_AT_CAPACITY"


# ── Proxies ─────────────────────────────────────────────────────────────

def test_create_proxy_async_and_bumps_target(client, fake_docker):
    resp = client.post("/v1/proxies")
    assert resp.status_code == 202
    assert resp.headers["Location"].startswith("/v1/tasks/")
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    with poolmod.pool_lock:
        assert len(poolmod.pool) == 1
        assert poolmod.target_count == 1
    ep = poolmod.pool[0]
    assert ep.name.startswith("warpgate-")
    assert ep.healthy is True
    # Reconcile must not scale the just-created proxy away
    poolmod.ensure_count(fake_docker)
    with poolmod.pool_lock:
        assert len(poolmod.pool) == 1


def test_list_proxies_and_healthy_filter(client):
    make_proxy("warpgate-aaa11111", healthy=True)
    make_proxy("warpgate-bbb22222", healthy=False)
    all_proxies = client.get("/v1/proxies").get_json()
    assert len(all_proxies) == 2
    healthy = client.get("/v1/proxies?healthy=true").get_json()
    assert [p["id"] for p in healthy] == ["warpgate-aaa11111"]
    unhealthy = client.get("/v1/proxies?healthy=false").get_json()
    assert [p["id"] for p in unhealthy] == ["warpgate-bbb22222"]


def test_get_single_proxy_and_404(client):
    make_proxy("warpgate-aaa11111", healthy=True)
    resp = client.get("/v1/proxies/warpgate-aaa11111")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == "warpgate-aaa11111"
    missing = client.get("/v1/proxies/nope")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "PROXY_NOT_FOUND"


def test_delete_proxy_async(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    with poolmod.pool_lock:
        poolmod.target_count = 1
    resp = client.delete("/v1/proxies/warpgate-aaa11111")
    assert resp.status_code == 202
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    with poolmod.pool_lock:
        assert len(poolmod.pool) == 0
        assert poolmod.target_count == 0


def test_delete_missing_returns_404(client):
    resp = client.delete("/v1/proxies/nope")
    assert resp.status_code == 404


def test_rotate_proxy_async(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    resp = client.post("/v1/proxies/warpgate-aaa11111/rotate")
    assert resp.status_code == 202
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    assert task.result["rotated"] is True


def test_rotate_missing_returns_404(client):
    resp = client.post("/v1/proxies/nope/rotate")
    assert resp.status_code == 404


def test_restart_recreates_container(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    old_id = ep.container_id
    seed_container(fake_docker, ep)
    resp = client.post("/v1/proxies/warpgate-aaa11111/restart")
    assert resp.status_code == 202
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    assert task.result["restarted"] is True
    current = poolmod.find_proxy("warpgate-aaa11111")
    assert current.container_id != old_id
    assert fake_docker.containers.get(current.container_id) is not None


def test_bulk_rotate_scope_validation(client):
    bad = client.post("/v1/proxies/rotate", json={"scope": "everything"})
    assert bad.status_code == 422
    ok = client.post("/v1/proxies/rotate")
    assert ok.status_code == 202


# ── Tasks ───────────────────────────────────────────────────────────────

def test_get_task_pending_retry_after(client):
    task = registry.create("pool.scale")
    resp = client.get(f"/v1/tasks/{task.id}")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "pending"
    assert resp.headers.get("Retry-After") == "1"


def test_get_task_not_found(client):
    resp = client.get("/v1/tasks/does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "TASK_NOT_FOUND"


# ── Config / spec / docs ────────────────────────────────────────────────

def test_get_config(client):
    resp = client.get("/v1/config")
    assert resp.status_code == 200
    body = resp.get_json()
    assert set(body) == {"image", "prefix", "network", "max_pool", "target"}


def test_openapi_and_docs(client):
    spec = client.get("/v1/openapi.yaml")
    assert spec.status_code == 200
    assert spec.mimetype == "application/yaml"
    assert b"openapi: 3.1.0" in spec.data
    docs = client.get("/v1/docs")
    assert docs.status_code == 200
    assert b"swagger-ui" in docs.data.lower()


def test_unknown_route_returns_envelope(client):
    resp = client.get("/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "NOT_FOUND"


# ── Idempotency / named create ──────────────────────────────────────────

def test_create_proxy_with_name(client, fake_docker):
    resp = client.post("/v1/proxies", json={"name": "warpgate-custom"})
    assert resp.status_code == 202
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    with poolmod.pool_lock:
        assert any(ep.name == "warpgate-custom" for ep in poolmod.pool)
        assert poolmod.target_count == 1


def test_create_proxy_duplicate_name_409(client, fake_docker):
    make_proxy("warpgate-custom")
    resp = client.post("/v1/proxies", json={"name": "warpgate-custom"})
    assert resp.status_code == 409
    assert resp.get_json()["error"]["code"] == "PROXY_EXISTS"


def test_create_proxy_invalid_name_422(client):
    assert client.post("/v1/proxies", json={"name": "has space"}).status_code == 422
    assert client.post("/v1/proxies", json={"name": 12}).status_code == 422
    assert client.post("/v1/proxies", json={"name": "x" * 65}).status_code == 422
    assert client.post("/v1/proxies", json="not-an-object").status_code == 422


def test_create_proxy_race_error_code(client, fake_docker, monkeypatch):
    monkeypatch.setattr(poolmod, "create_proxy_container", lambda client, name: None)
    resp = client.post("/v1/proxies")
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "failed"
    assert task.error_code == "CREATE_FAILED"


def test_remove_missing_task_has_error_code(client, fake_docker):
    task = registry.create("proxy.remove")
    registry.submit(task, server._run_remove, fake_docker, "nope")
    done = wait_for_task(task)
    assert done.status == "failed"
    assert done.error_code == "PROXY_NOT_FOUND"


def test_idempotency_key_deduplicates_create(client, fake_docker):
    key = "create-1"
    r1 = client.post("/v1/proxies", headers={"Idempotency-Key": key})
    r2 = client.post("/v1/proxies", headers={"Idempotency-Key": key})
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.get_json()["id"] == r2.get_json()["id"]
    task = wait_for_task(registry.get(r1.get_json()["id"]))
    assert task.status == "succeeded"
    with poolmod.pool_lock:
        assert len(poolmod.pool) == 1


def test_idempotency_key_scale(client, fake_docker):
    key = "scale-1"
    r1 = client.patch("/v1/pool", json={"target": 1}, headers={"Idempotency-Key": key})
    r2 = client.patch("/v1/pool", json={"target": 1}, headers={"Idempotency-Key": key})
    assert r1.get_json()["id"] == r2.get_json()["id"]


def test_idempotency_key_cross_type_conflict(client, fake_docker):
    key = "shared-key"
    r1 = client.patch("/v1/pool", json={"target": 1}, headers={"Idempotency-Key": key})
    assert r1.status_code == 202
    r2 = client.post("/v1/proxies", headers={"Idempotency-Key": key})
    assert r2.status_code == 409
    assert r2.get_json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ── Strict query parsing ────────────────────────────────────────────────

def test_invalid_include_422(client):
    resp = client.get("/v1/pool?include=banana")
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_healthy_filter_422(client):
    resp = client.get("/v1/proxies?healthy=banana")
    assert resp.status_code == 422
    assert resp.get_json()["error"]["code"] == "VALIDATION_ERROR"


# ── Auth edge cases ─────────────────────────────────────────────────────

def test_empty_api_key_disables_auth(monkeypatch):
    monkeypatch.setitem(os.environ, "MANAGER_API_KEY", "")
    monkeypatch.setattr(server, "_API_KEY", None)
    c = server.app.test_client()
    assert c.get("/v1/pool").status_code == 200


# ── Health checker race / volumes ───────────────────────────────────────

def test_health_cycle_removes_gone_container(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    fake_docker.containers.containers_by_id.pop(ep.container_id, None)
    fake_docker.containers.by_name.pop(ep.name, None)
    poolmod._health_cycle(fake_docker)
    assert poolmod.find_proxy("warpgate-aaa11111") is None


def test_health_cycle_skips_busy_proxy(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    with poolmod.pool_lock:
        ep.busy = True
    fake_docker.containers.containers_by_id.pop(ep.container_id, None)
    fake_docker.containers.by_name.pop(ep.name, None)
    poolmod._health_cycle(fake_docker)
    assert poolmod.find_proxy("warpgate-aaa11111") is not None


def test_health_cycle_skips_present_by_name(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    c = seed_container(fake_docker, ep)
    # Probe by (stale) container_id fails, but the container still exists by name.
    fake_docker.containers.containers_by_id.pop(ep.container_id, None)
    c.id = "cid-other"
    c.short_id = "cid-other"
    fake_docker.containers.containers_by_id["cid-other"] = c
    ep.container_id = "cid-stale"
    with poolmod.pool_lock:
        poolmod.target_count = 1
    poolmod._health_cycle(fake_docker)
    assert poolmod.find_proxy("warpgate-aaa11111") is not None


def test_health_cycle_keeps_entry_on_name_lookup_error(client, fake_docker, monkeypatch):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    fake_docker.containers.containers_by_id.pop(ep.container_id, None)
    fake_docker.containers.by_name.pop(ep.name, None)
    with poolmod.pool_lock:
        poolmod.target_count = 1

    def boom(*args, **kwargs):
        raise RuntimeError("transient docker failure")

    monkeypatch.setattr(fake_docker.containers, "list", boom)
    poolmod._health_cycle(fake_docker)
    assert poolmod.find_proxy("warpgate-aaa11111") is not None


def test_health_cycle_eviction_cleans_volumes(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    fake_docker.volumes.create("warpgate-aaa11111-data")
    fake_docker.volumes.create("warpgate-aaa11111-cache")
    fake_docker.containers.containers_by_id.pop(ep.container_id, None)
    fake_docker.containers.by_name.pop(ep.name, None)
    poolmod._health_cycle(fake_docker)
    assert poolmod.find_proxy("warpgate-aaa11111") is None
    assert "warpgate-aaa11111-data" not in fake_docker.volumes.volumes
    assert "warpgate-aaa11111-cache" not in fake_docker.volumes.volumes


def test_initialize_pool_starts_checker_when_docker_down(monkeypatch):
    import docker.errors

    started = []

    def docker_down():
        raise docker.errors.DockerException("daemon down")

    monkeypatch.setattr(poolmod, "get_docker_client", docker_down)
    monkeypatch.setattr(poolmod, "start_health_checker", lambda: started.append(True))
    poolmod.initialize_pool()
    assert started == [True]


def test_restart_mounts_persistent_warp_volume(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    resp = client.post("/v1/proxies/warpgate-aaa11111/restart")
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    current = poolmod.find_proxy("warpgate-aaa11111")
    new = fake_docker.containers.get(current.container_id)
    targets = {m["Target"] for m in new.mounts}
    assert "/var/lib/cloudflare-warp" in targets
    assert "/var/cache" in targets


def test_delete_cleans_warp_volumes(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    seed_container(fake_docker, ep)
    fake_docker.volumes.create("warpgate-aaa11111-data")
    fake_docker.volumes.create("warpgate-aaa11111-cache")
    with poolmod.pool_lock:
        poolmod.target_count = 1
    resp = client.delete("/v1/proxies/warpgate-aaa11111")
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    assert "warpgate-aaa11111-data" not in fake_docker.volumes.volumes
    assert "warpgate-aaa11111-cache" not in fake_docker.volumes.volumes


def test_rotate_uses_exact_container_name(client, fake_docker):
    ep = make_proxy("warpgate-aaa11111")
    c_exact = seed_container(fake_docker, ep)
    extra = FakeContainer("warpgate-aaa11111-extra", api=fake_docker.containers, cid="cid-extra")
    fake_docker.containers.containers_by_id["cid-extra"] = extra
    fake_docker.containers.by_name["warpgate-aaa11111-extra"] = extra
    resp = client.post("/v1/proxies/warpgate-aaa11111/rotate")
    task = wait_for_task(registry.get(resp.get_json()["id"]))
    assert task.status == "succeeded"
    assert any("disconnect" in cmd for cmd in c_exact.executed_cmds)
    assert extra.executed_cmds == []


def test_cors_headers_on_responses(client):
    resp = client.get("/v1/health")
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]


def test_options_preflight(client):
    resp = client.open(
        "/v1/pool",
        method="OPTIONS",
        headers={
            "Origin": "http://localhost:8080",
            "Access-Control-Request-Method": "PATCH",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PATCH, DELETE, OPTIONS"


def test_openapi_server_base_matches_paths(client):
    import yaml

    spec = yaml.safe_load(client.get("/v1/openapi.yaml").data)
    assert spec["servers"][0]["url"] == "/v1"
    # Paths must be relative to the server base, never carry the /v1 prefix
    # again (a /v1 server + /v1/* paths would double up to /v1/v1/*).
    for path in spec["paths"]:
        assert path.startswith("/") and not path.startswith("/v1"), path
    assert "/v1/health" not in spec["paths"]
