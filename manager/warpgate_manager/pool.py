"""Pool of warpgate proxy containers — Docker interaction and shared state.

Everything in this module is shared between the Flask request threads and the
background health checker / task workers.  Mutations to ``pool`` and
``target_count`` go through ``pool_lock``; mutating reconcile operations
(create/scale/remove) additionally serialize on ``_ensure_lock`` so the
self-healing reconciler never races a user-initiated change.

Desired-state model:
  * ``target_count`` is the desired pool size.
  * Creating a proxy increments the target; removing one decrements it.
  * ``PATCH /v1/pool`` sets the target explicitly.
  * ``ensure_count`` converges the pool toward the target.
"""

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field

import docker
import docker.errors

from . import config

log = logging.getLogger("warpgate.pool")

# When the manager runs inside a container, its hostname is that container's
# short ID — used to avoid discovering the manager as a proxy.
SELF_CONTAINER_ID = socket.gethostname()


@dataclass
class ProxyEndpoint:
    name: str
    container_id: str
    healthy: bool = False
    socks5_ok: bool = False
    http_ok: bool = False
    warp_connected: bool = False
    warp_detail: str = "unknown"
    created_at: float = field(default_factory=time.time)
    # True while a recreate is in flight; the health checker must not evict the
    # entry (container_id points at the dying container mid-swap). Not exposed
    # through the API.
    busy: bool = False

    @property
    def socks5_url(self) -> str:
        return f"socks5://{self.name}:{config.SOCKS5_PORT}"

    @property
    def http_url(self) -> str:
        return f"http://{self.name}:{config.HTTP_PORT}"


# ── Docker client ───────────────────────────────────────────────────────

_DOCKER_CLIENT = None
_docker_lock = threading.Lock()


class _UnavailableClient:
    """Placeholder that raises on any access when Docker is unavailable."""

    def __getattr__(self, name):
        raise docker.errors.DockerException("Docker client unavailable at startup")


def reset_docker_client():
    """Drop the cached client so the next call re-initializes."""
    global _DOCKER_CLIENT
    _DOCKER_CLIENT = None


def get_docker_client():
    global _DOCKER_CLIENT
    with _docker_lock:
        if _DOCKER_CLIENT is None:
            try:
                _DOCKER_CLIENT = docker.from_env()
            except docker.errors.DockerException:
                _DOCKER_CLIENT = _UnavailableClient()
        if isinstance(_DOCKER_CLIENT, _UnavailableClient):
            _DOCKER_CLIENT = None  # allow retry on next call
            raise docker.errors.DockerException("Docker unavailable")
        return _DOCKER_CLIENT


# ── Health probes ───────────────────────────────────────────────────────

def socks5_is_alive(host: str, port: int = config.SOCKS5_PORT, timeout: float = 5.0) -> bool:
    """Verify a SOCKS5 proxy is alive via a real handshake."""
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(bytes([0x05, 0x01, 0x00]))
        resp = sock.recv(2)
        return resp == bytes([0x05, 0x00])
    except (OSError, socket.timeout):
        return False
    finally:
        if sock:
            sock.close()


def http_is_alive(
    host: str,
    port: int = config.HTTP_PORT,
    timeout: float = 5.0,
    connect_host: str | None = None,
    connect_port: int | None = None,
) -> bool:
    """Verify the HTTP proxy is alive via a CONNECT tunnel request.

    The CONNECT target defaults to ``MANAGER_HEALTH_HOST`` so the probe is not
    dependent on a third-party host; the operator controls the endpoint.
    """
    connect_host = connect_host or config.MANAGER_HEALTH_HOST
    connect_port = connect_port or config.MANAGER_HEALTH_PORT
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        sock.sendall(
            f"CONNECT {connect_host}:{connect_port} HTTP/1.1\r\n"
            f"Host: {connect_host}:{connect_port}\r\n\r\n".encode()
        )
        resp = sock.recv(1024)
        status_line = resp.split(b"\r\n", 1)[0]
        return b" 200 " in status_line
    except (OSError, socket.timeout):
        return False
    finally:
        if sock:
            sock.close()


def get_container_warp_status(container):
    """Return the warp-cli status detail string, or None on failure."""
    try:
        exit_code, output = container.exec_run(["warp-cli", "--accept-tos", "status"])
        if exit_code != 0:
            return None
        for line in output.decode().splitlines():
            if "Status update:" in line:
                return line.split(":", 1)[1].strip()
        return None
    except Exception as exc:
        log.debug("warp status exec failed for %s: %s", container.name, exc)
        return None


@dataclass
class HealthResult:
    running: bool = True
    socks5: bool = False
    http: bool = False
    warp_connected: bool = False
    warp_detail: str = "unknown"

    @property
    def healthy(self) -> bool:
        return self.running and self.socks5 and self.http and self.warp_connected


def probe_container(client, name: str, container_id: str) -> HealthResult | None:
    """Full health probe (container state + SOCKS5 + HTTP + WARP).

    Returns ``None`` when the container is gone entirely.
    """
    try:
        container = client.containers.get(container_id)
    except docker.errors.NotFound:
        return None
    except Exception as exc:
        log.debug("probe get failed for %s: %s", name, exc)
        return None
    if container.status != "running":
        return HealthResult(running=False)
    warp = get_container_warp_status(container)
    connected = False
    detail = "unknown"
    if warp is not None:
        connected = "Connected" in warp
        detail = warp
    return HealthResult(
        socks5=socks5_is_alive(name, config.SOCKS5_PORT),
        http=http_is_alive(name, config.HTTP_PORT),
        warp_connected=connected,
        warp_detail=detail,
    )


# ── Pool state ──────────────────────────────────────────────────────────

pool_lock = threading.Lock()
pool: list[ProxyEndpoint] = []
target_count = config.WARPGATE_COUNT

# Serializes mutating reconcile ops (scale/create/remove) so they never race
# the self-healing ensure_count called by the health checker.
_ensure_lock = threading.Lock()


def find_proxy(name: str) -> ProxyEndpoint | None:
    with pool_lock:
        for ep in pool:
            if ep.name == name:
                return ep
        return None


def update_endpoint(name: str, result: HealthResult) -> None:
    with pool_lock:
        for ep in pool:
            if ep.name == name:
                ep.healthy = result.healthy
                ep.socks5_ok = result.socks5
                ep.http_ok = result.http
                ep.warp_connected = result.warp_connected
                ep.warp_detail = result.warp_detail
                break


# ── Container lifecycle ─────────────────────────────────────────────────

def create_proxy_container(client, name: str):
    """Create and start a warpgate container. Returns the container or None.

    Per-proxy named volumes persist the WARP registration (``reg.json``) and
    cache across ``restart``/``recreate`` so a proxy keeps its WARP identity
    and does not re-register (re-registration is rate-limited by Cloudflare).
    Named volumes referenced in a create are auto-created by the daemon.
    """
    try:
        container = client.containers.create(
            image=config.WARPGATE_IMAGE,
            name=name,
            network=config.WARPGATE_NETWORK,
            cap_add=["NET_ADMIN"],
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
            sysctls={"net.ipv6.conf.all.disable_ipv6": "0"},
            environment={
                "WARP_WAIT_RETRIES": "15",
                "WARP_WAIT_INTERVAL": "2",
            },
            mounts=[
                docker.types.Mount(target="/var/lib/cloudflare-warp", source=f"{name}-data", type="volume"),
                docker.types.Mount(target="/var/cache", source=f"{name}-cache", type="volume"),
            ],
            mem_limit="256m",
            memswap_limit="512m",
            nano_cpus=500_000_000,
            auto_remove=True,
            detach=True,
        )
        container.start()
        log.info("created container %s", name)
        return container
    except docker.errors.APIError as e:
        if "Conflict" in str(e) and "already in use" in str(e):
            log.warning("name collision on %s, caller should retry", name)
        else:
            log.error("failed to create container %s: %s", name, e)
        return None


def remove_container(container) -> None:
    """Stop and remove a container. Handles stop timeouts and auto_remove."""
    try:
        container.stop(timeout=10)
    except docker.errors.APIError as e:
        log.warning("error stopping container %s: %s", container.name, e)
    except Exception as exc:
        log.warning("unexpected error stopping %s: %s", container.name, exc)
    try:
        container.remove(force=True)
        log.info("removed container %s", container.name)
    except docker.errors.NotFound:
        log.debug("container %s already removed by auto_remove", container.name)
    except docker.errors.APIError as e:
        log.error("error removing container %s: %s", container.name, e)
    except Exception as exc:
        log.error("unexpected error removing %s: %s", container.name, exc)


def _cleanup_volumes(client, name: str) -> None:
    """Drop the per-proxy data/cache volumes for a permanently removed proxy."""
    for vol in (f"{name}-data", f"{name}-cache"):
        try:
            client.volumes.get(vol).remove(force=True)
            log.info("removed volume %s", vol)
        except docker.errors.NotFound:
            pass
        except Exception as exc:
            log.warning("error removing volume %s: %s", vol, exc)


def recreate_container(client, ep: ProxyEndpoint):
    """Recreate a proxy under the same name (restart).

    With ``auto_remove`` the old container is removed asynchronously, so the
    new container waits for the name to free before creating — otherwise
    Docker races a "Conflict … already in use" error.  Updates the endpoint's
    ``container_id`` and resets its health flags in place.  The ``busy`` flag
    keeps the health checker from evicting the entry while the container id is
    mid-swap (it may briefly point at a dying/removed container).
    """
    name = ep.name
    with pool_lock:
        ep.busy = True
    try:
        try:
            old = client.containers.get(ep.container_id)
        except docker.errors.NotFound:
            old = None
        if old is not None:
            remove_container(old)
        for _ in range(30):
            try:
                client.containers.get(name)
            except docker.errors.NotFound:
                break
            time.sleep(1)
        new = create_proxy_container(client, name)
        if new is not None:
            with pool_lock:
                ep.container_id = new.id or ep.container_id
                ep.healthy = False
                ep.socks5_ok = False
                ep.http_ok = False
                ep.warp_connected = False
            log.info("recreated container %s", name)
        return new
    finally:
        with pool_lock:
            ep.busy = False


# ── Pool reconcile ──────────────────────────────────────────────────────

def ensure_count(client) -> None:
    """Scale the pool to the target number of running containers."""
    global pool, target_count
    with _ensure_lock:
        with pool_lock:
            target = target_count
            current = len(pool)
            if current == target:
                return
            to_create = 0
            removed: list[ProxyEndpoint] = []
            if current < target:
                to_create = min(target - current, config.MANAGER_MAX_POOL - current)
                if to_create <= 0:
                    log.warning("at max pool size (%d), cannot scale up", config.MANAGER_MAX_POOL)
                    return
                log.info("scaling up by %d to %d", to_create, target)
            else:
                to_remove = current - target
                log.info("scaling down by %d to %d", to_remove, target)
                # Never evict a proxy that is mid-recreate (scale-down while a
                # restart is in flight would orphan the new container).
                removed = [ep for ep in pool if not ep.busy][-to_remove:]
                if len(removed) < to_remove:
                    log.warning(
                        "scale-down deferred for %d busy proxy(ies), pool stays above target",
                        to_remove - len(removed),
                    )
                keep = [ep for ep in pool if ep not in removed]
                pool[:] = keep

        created: list[ProxyEndpoint] = []
        for _ in range(to_create):
            container = None
            cname = None
            for attempt in range(config.CREATE_RETRIES):
                suffix = os.urandom(4).hex()
                cname = f"{config.WARPGATE_PREFIX}{suffix}"
                container = create_proxy_container(client, cname)
                if container:
                    break
            if container and cname:
                created.append(ProxyEndpoint(name=cname, container_id=container.id or ""))
            else:
                log.error("failed to create proxy for a slot after %d attempts", config.CREATE_RETRIES)

        excess: list[ProxyEndpoint] = []
        with pool_lock:
            pool.extend(created)
            if len(pool) > config.MANAGER_MAX_POOL:
                excess = pool[config.MANAGER_MAX_POOL:]
                pool[:] = pool[:config.MANAGER_MAX_POOL]

        for ep in excess:
            try:
                c = client.containers.get(ep.container_id)
                remove_container(c)
            except docker.errors.NotFound:
                pass
            except Exception as exc:
                log.error("error cleaning up excess container %s: %s", ep.name, exc)
            _cleanup_volumes(client, ep.name)

        for ep in removed:
            try:
                c = client.containers.get(ep.container_id)
                remove_container(c)
            except docker.errors.NotFound:
                log.debug("scale-down: container %s already gone", ep.name)
            except Exception as exc:
                log.error("error cleaning up %s during scale-down: %s", ep.name, exc)
            _cleanup_volumes(client, ep.name)


def add_proxy(client, name: str, container) -> ProxyEndpoint:
    """Register a created container into the pool and bump the target.

    The target is bumped so the self-healing reconciler does not scale the
    just-created proxy away on the next health sweep.
    """
    global target_count
    with _ensure_lock:
        with pool_lock:
            if any(ep.name == name for ep in pool):
                raise RuntimeError(f"proxy {name} already in pool")
            ep = ProxyEndpoint(name=name, container_id=container.id or "")
            pool.append(ep)
            target_count = min(target_count + 1, config.MANAGER_MAX_POOL)
        return ep


def remove_proxy(client, name: str) -> bool:
    """Remove a proxy from the pool and Docker, lowering the target."""
    global pool, target_count
    with _ensure_lock:
        with pool_lock:
            matches = [ep for ep in pool if ep.name == name]
            if not matches:
                return False
            ep = matches[0]
            pool.remove(ep)
            target_count = max(0, target_count - 1)

        try:
            c = client.containers.get(ep.container_id)
            remove_container(c)
        except docker.errors.NotFound:
            pass
        except Exception as exc:
            log.error("error removing proxy %s: %s", name, exc)
        _cleanup_volumes(client, name)
        return True


# ── Rotation ────────────────────────────────────────────────────────────

def _rotate_one(client, name: str) -> bool:
    """Rotate WARP on a single proxy and verify reconnection."""
    try:
        # Anchored regex: docker-py's "name" filter is a substring match, so
        # a bare name could match a different container whose name contains it.
        containers = client.containers.list(filters={"name": f"^{name}$"})
        if not containers:
            return False
        c = containers[0]
        log.info("rotating WARP on %s", name)
        c.exec_run(["warp-cli", "--accept-tos", "disconnect"], timeout=10)
        time.sleep(2)
        c.exec_run(["warp-cli", "--accept-tos", "connect"], timeout=10)
        for _ in range(30):
            status = get_container_warp_status(c)
            if status and "Connected" in status:
                log.info("warp rotation verified on %s", name)
                return True
            time.sleep(2)
        log.warning("warp rotation on %s did not complete within timeout", name)
        return False
    except Exception as exc:
        log.error("rotate failed for %s: %s", name, exc)
        return False


def rotate_one(client, name: str) -> bool:
    """Rotate a single proxy and refresh its recorded health."""
    ok = _rotate_one(client, name)
    ep = find_proxy(name)
    if ep is not None:
        result = probe_container(client, name, ep.container_id)
        if result is not None:
            update_endpoint(name, result)
    return ok


def rotate_pool(client, scope: str) -> dict:
    """Rotate all (or only unhealthy) proxies. Returns counts per outcome."""
    with pool_lock:
        if scope == "all":
            names = [ep.name for ep in pool]
        else:
            names = [ep.name for ep in pool if not ep.healthy]
    rotated: list[str] = []
    failed: list[str] = []
    for name in names:
        if rotate_one(client, name):
            rotated.append(name)
        else:
            failed.append(name)
    return {"rotated": rotated, "failed": failed, "count": len(rotated) + len(failed)}


# ── Wait for health ─────────────────────────────────────────────────────

def wait_for_healthy(client, names: list[str], timeout: float = config.PROXY_WAIT_TIMEOUT) -> None:
    """Poll the given proxies until they are healthy or the timeout elapses."""
    deadline = time.time() + timeout
    pending = set(names)
    while pending and time.time() < deadline:
        for name in list(pending):
            ep = find_proxy(name)
            if ep is None:
                pending.discard(name)
                continue
            result = probe_container(client, name, ep.container_id)
            if result is None:
                pending.discard(name)
                continue
            update_endpoint(name, result)
            if result.healthy:
                log.info("proxy %s is now healthy", name)
                pending.discard(name)
        if pending:
            time.sleep(3)


# ── Discovery / startup / background health checker ────────────────────

def discover_pool(client) -> list[ProxyEndpoint]:
    """Discover existing warpgate containers matching our prefix."""
    results: list[ProxyEndpoint] = []
    try:
        containers = client.containers.list(all=False, filters={"status": "running"})
    except docker.errors.APIError as e:
        log.error("failed to list containers: %s", e)
        return results

    for c in containers:
        if c.short_id == SELF_CONTAINER_ID or c.id == SELF_CONTAINER_ID:
            continue
        if c.name and c.name.startswith(config.WARPGATE_PREFIX):
            ep = ProxyEndpoint(name=c.name, container_id=c.id or "")
            result = probe_container(client, c.name, c.id or "")
            if result is not None:
                ep.healthy = result.healthy
                ep.socks5_ok = result.socks5
                ep.http_ok = result.http
                ep.warp_connected = result.warp_connected
                ep.warp_detail = result.warp_detail
            results.append(ep)
    return results


_health_thread_started = False


def start_health_checker() -> None:
    global _health_thread_started
    if _health_thread_started:
        return
    _health_thread_started = True
    t = threading.Thread(target=_health_loop, daemon=True, name="health-checker")
    t.start()
    log.debug("health checker started")


def _health_cycle(client) -> None:
    """One pass of the health checker: probe, evict stale, replenish.

    A proxy is only evicted when its container is gone *and* it is not
    mid-recreate — probing by ``container_id`` alone can observe a stale id
    while ``recreate_container`` swaps it, which would otherwise orphan the
    replacement container.
    """
    with pool_lock:
        snapshot = list(pool)
    for ep in snapshot:
        result = probe_container(client, ep.name, ep.container_id)
        if result is None or not result.running:
            with pool_lock:
                busy = ep.busy
            if busy:
                log.debug("skip eviction of %s — recreate in progress", ep.name)
                continue
            try:
                still_there = client.containers.list(filters={"name": f"^{ep.name}$"})
            except Exception as exc:
                log.debug("name lookup failed for %s: %s", ep.name, exc)
                continue
            if still_there:
                log.debug("container %s still present by name; skipping eviction", ep.name)
                continue
            log.warning("removing stale entry %s — container gone/stopped", ep.name)
            with pool_lock:
                pool[:] = [live for live in pool if live.name != ep.name]
            _cleanup_volumes(client, ep.name)
            continue
        update_endpoint(ep.name, result)
    # Replenish if the pool dropped below target
    ensure_count(client)


def _health_loop() -> None:
    while True:
        time.sleep(30)
        try:
            client = get_docker_client()
        except docker.errors.DockerException:
            log.warning("health check: docker unavailable, retrying in 30s")
            continue
        try:
            _health_cycle(client)
        except Exception as exc:
            log.error("health check cycle failed: %s", exc)
            reset_docker_client()  # force reconnection on next cycle


def initialize_pool() -> None:
    """Discover existing containers and ensure the target count at startup.

    The health checker is started even when Docker is unavailable at startup —
    it re-tries ``get_docker_client`` every cycle, so the pool self-heals once
    the daemon comes up instead of staying dead until the manager restarts.
    """
    global pool
    try:
        client = get_docker_client()
    except docker.errors.DockerException as e:
        log.warning("Docker unavailable at startup: %s", e)
        start_health_checker()
        return
    discovered = discover_pool(client)
    with pool_lock:
        pool = discovered
    log.info("discovered %d existing warpgate containers", len(discovered))
    ensure_count(client)
    with pool_lock:
        actual = len(pool)
    log.info("pool initialized, target=%d actual=%d", target_count, actual)
    start_health_checker()
