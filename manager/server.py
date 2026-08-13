"""WarpGate Manager — single control-plane for managing warpgate proxy containers.

Manages a pool of warpgate containers (WARP + 3proxy) via the Docker API.
SOCKS5/HTTP proxy traffic goes directly to containers — this is a management
API only.
"""

import hmac
import logging
import os
import socket
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import wraps

import docker
import docker.errors
import docker.models.containers
from flask import Flask, jsonify, request

# ── Configuration ───────────────────────────────────────────────

WARPATE_IMAGE = os.environ.get("WARPATE_IMAGE", "warpgate:local")
WARPATE_PREFIX = os.environ.get("WARPATE_PREFIX", "warpgate-")
WARPATE_COUNT = int(os.environ.get("WARPATE_COUNT", "3"))
WARPATE_NETWORK = os.environ.get("WARPATE_NETWORK", "warpgate-net")
_API_KEY: str | None = None  # lazily initialized in _get_api_key()
_api_key_lock = threading.Lock()
MANAGER_PORT = int(os.environ.get("MANAGER_PORT", "9090"))
MAX_POOL_SIZE = int(os.environ.get("MANAGER_MAX_POOL", "20"))

RATE_LIMIT_ATTEMPTS = int(os.environ.get("MANAGER_RATE_LIMIT", "10"))
RATE_LIMIT_WINDOW = 60  # seconds

# Reusable Docker client (lazily initialized, resets on failure)
_DOCKER_CLIENT = None
_docker_lock = threading.Lock()


class _UnavailableClient:
    """Placeholder that raises on any access when Docker is unavailable."""
    def __getattr__(self, name):
        raise docker.errors.DockerException("Docker client unavailable at startup")


def _reset_docker_client():
    """Reset the cached Docker client so the next call re-initializes."""
    global _DOCKER_CLIENT
    _DOCKER_CLIENT = None


def _get_docker_client():
    global _DOCKER_CLIENT
    # Hold the lock for the entire check-and-return to eliminate the race
    # where multiple threads see _UnavailableClient simultaneously.
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


def _get_api_key() -> str | None:
    """Return the configured API key, or None when auth is disabled."""
    global _API_KEY
    if _API_KEY is None:
        with _api_key_lock:
            # Double-check after acquiring lock
            if _API_KEY is None:
                _API_KEY = os.environ.get("MANAGER_API_KEY")
    return _API_KEY


SOCKS5_PORT = 1080
PROXY_WAIT_TIMEOUT = 60
CREATE_RETRIES = 3

app = Flask(__name__)

# ── Rate limiter ────────────────────────────────────────────

_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_MAX_KEYS = 10_000  # safety cap to prevent unbounded growth


def _rate_limit_cleanup():
    """Evict stale entries from the rate-limit store to prevent memory leaks.

    Runs periodically (every 100 writes) to bound memory usage.  IPA
    addresses that stop sending requests eventually get pruned.
    """
    now = time.time()
    with _rate_limit_lock:
        stale_keys = [
            k for k, v in _rate_limit_store.items()
            if not any(now - t < RATE_LIMIT_WINDOW for t in v)
        ]
        for k in stale_keys:
            del _rate_limit_store[k]
        # Enforce a hard cap on distinct keys (safety net)
        if len(_rate_limit_store) > _RATE_LIMIT_MAX_KEYS:
            # Keep the most recently active keys
            sorted_keys = sorted(
                _rate_limit_store.keys(),
                key=lambda k: max(_rate_limit_store[k], default=0),
                reverse=True,
            )
            for k in sorted_keys[_RATE_LIMIT_MAX_KEYS // 2:]:
                del _rate_limit_store[k]


_cleanup_counter = 0


def _check_rate_limit(key: str, max_attempts: int = RATE_LIMIT_ATTEMPTS, window: int = RATE_LIMIT_WINDOW) -> bool:
    """Returns True if allowed, False if rate limited.

    The attempt is recorded under lock before returning, so concurrent
    callers see an up-to-date count — there is no TOCTOU window where
    multiple requests can bypass the limit.
    """
    global _cleanup_counter
    now = time.time()
    with _rate_limit_lock:
        records = _rate_limit_store[key]
        # Prune expired entries for this key
        _rate_limit_store[key] = [t for t in records if now - t < window]
        if len(_rate_limit_store[key]) >= max_attempts:
            return False
        _rate_limit_store[key].append(now)

    # Periodic global cleanup (once per 100 writes)
    _cleanup_counter += 1
    if _cleanup_counter >= 100:
        _cleanup_counter = 0
        _rate_limit_cleanup()

    return True


# ── Proxy state ─────────────────────────────────────────────────

@dataclass
class ProxyEndpoint:
    name: str
    container_id: str
    healthy: bool = False
    warp_status: str = "unknown"
    in_flight: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def socks5_url(self) -> str:
        return f"socks5://{self.name}:{SOCKS5_PORT}"


pool_lock = threading.Lock()
pool: list[ProxyEndpoint] = []
target_count = WARPATE_COUNT

# When the manager itself runs inside a container, its hostname is that
# container's short ID — used to avoid discovering the manager as a proxy.
SELF_CONTAINER_ID = socket.gethostname()


# ── Docker helpers ──────────────────────────────────────────────

def socks5_is_alive(host: str, port: int = SOCKS5_PORT, timeout: float = 5) -> bool:
    """Verify a SOCKS5 proxy is alive via real handshake."""
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


def get_container_warp_status(container):
    """Check WARP status inside a container via docker exec."""
    try:
        exit_code, output = container.exec_run(
            ["warp-cli", "--accept-tos", "status"],
        )
        if exit_code != 0:
            return None
        for line in output.decode().splitlines():
            if "Status update:" in line:
                return line.split(":", 1)[1].strip()
        return None
    except Exception as exc:
        app.logger.debug("warp status exec failed for %s: %s", container.name, exc)
        return None


def container_is_healthy(container) -> bool:
    """Check that WARP is connected and SOCKS5 is alive."""
    name = container.name
    if not socks5_is_alive(name, SOCKS5_PORT):
        return False
    status = get_container_warp_status(container)
    return status is not None and "Connected" in status


def create_proxy_container(client, name: str):
    """Create and start a warpgate container."""
    try:
        container = client.containers.create(
            image=WARPATE_IMAGE,
            name=name,
            network=WARPATE_NETWORK,
            cap_add=["NET_ADMIN"],
            devices=["/dev/net/tun:/dev/net/tun:rwm"],
            sysctls={"net.ipv6.conf.all.disable_ipv6": "0"},
            environment={
                "WARP_WAIT_RETRIES": "15",
                "WARP_WAIT_INTERVAL": "2",
            },
            mem_limit="256m",
            memswap_limit="512m",
            nano_cpus=500_000_000,
            auto_remove=True,
            detach=True,
        )
        container.start()
        app.logger.info("created container %s", name)
        return container
    except docker.errors.APIError as e:
        # Name collision — caller should retry with a new name
        if "Conflict" in str(e) and "already in use" in str(e):
            app.logger.warning("name collision on %s, caller should retry", name)
        else:
            app.logger.error("failed to create container %s: %s", name, e)
        return None


def remove_container(container) -> None:
    """Stop and remove a container. Handles stop timeouts and auto_remove."""
    try:
        container.stop(timeout=10)
    except docker.errors.APIError as e:
        app.logger.warning("error stopping container %s: %s", container.name, e)
    except Exception as exc:
        app.logger.warning("unexpected error stopping %s: %s", container.name, exc)
    # auto_remove is set at creation time, so the container is removed
    # automatically on stop. Attempt explicit removal as a fallback.
    try:
        container.remove(force=True)
        app.logger.info("removed container %s", container.name)
    except docker.errors.NotFound:
        app.logger.debug("container %s already removed by auto_remove", container.name)
    except docker.errors.APIError as e:
        app.logger.error("error removing container %s: %s", container.name, e)
    except Exception as exc:
        app.logger.error("unexpected error removing %s: %s", container.name, exc)


# ── Pool management ─────────────────────────────────────────────

def discover_pool(client) -> list[ProxyEndpoint]:
    """Discover existing warpgate containers matching our prefix."""
    results: list[ProxyEndpoint] = []
    try:
        containers = client.containers.list(
            all=False,
            filters={"status": "running"},
        )
    except docker.errors.APIError as e:
        app.logger.error("failed to list containers: %s", e)
        return results

    for c in containers:
        # Skip the manager's own container: its hostname is the container's
        # short ID, and its name may match the proxy prefix.
        if c.short_id == SELF_CONTAINER_ID or c.id == SELF_CONTAINER_ID:
            continue
        if c.name and c.name.startswith(WARPATE_PREFIX):
            ep = ProxyEndpoint(name=c.name, container_id=c.id or "")
            ep.healthy = container_is_healthy(c)
            ep.warp_status = get_container_warp_status(c) or "unknown"
            results.append(ep)
    return results


# Serialize ensure_count so concurrent scale/health ops don't race
_ensure_lock = threading.Lock()


def ensure_count(client) -> None:
    """Scale the pool to the target number of running containers."""
    global pool, target_count
    with _ensure_lock:
        target = target_count  # Snapshot under lock for consistency
        to_create = 0
        removed: list[ProxyEndpoint] = []
        with pool_lock:
            current = len(pool)
            if current == target:
                return

            if current < target:
                to_create = min(target - current, MAX_POOL_SIZE - current)
                if to_create <= 0:
                    app.logger.warning("at max pool size (%d), cannot scale up", MAX_POOL_SIZE)
                    return
                app.logger.info("scaling up by %d to %d", to_create, target)

            else:
                to_remove = current - target
                app.logger.info("scaling down by %d to %d", to_remove, target)
                removed = pool[-to_remove:]
                pool[:] = pool[:-to_remove]

        # ── Scale-up: create containers outside pool_lock ────
        created: list[ProxyEndpoint] = []
        for _ in range(to_create):
            container = None
            cname = None
            for attempt in range(3):
                suffix = os.urandom(4).hex()
                cname = f"{WARPATE_PREFIX}{suffix}"
                container = create_proxy_container(client, cname)
                if container:
                    break
            if container and cname:
                cid = container.id or ""
                created.append(ProxyEndpoint(name=cname, container_id=cid))
            else:
                app.logger.error("failed to create proxy for a slot after 3 attempts")

        with pool_lock:
            pool.extend(created)
            # Enforce MAX_POOL_SIZE in case the pool grew beyond limits
            # due to concurrent operations (TOCTOU race mitigation).
            excess: list[ProxyEndpoint] = []
            if len(pool) > MAX_POOL_SIZE:
                excess = pool[MAX_POOL_SIZE:]
                pool[:] = pool[:MAX_POOL_SIZE]

        # Clean up excess containers created due to races
        for ep in excess:
            try:
                c = client.containers.get(ep.container_id)
                remove_container(c)
            except docker.errors.NotFound:
                pass
            except Exception as exc:
                app.logger.error("error cleaning up excess container %s: %s", ep.name, exc)

        # ── Scale-down: remove Docker containers outside pool_lock ──
        for ep in removed:
            try:
                c = client.containers.get(ep.container_id)
                remove_container(c)
            except docker.errors.NotFound:
                app.logger.debug("scale-down: container %s already gone", ep.name)
            except Exception as exc:
                app.logger.error("error cleaning up %s during scale-down: %s", ep.name, exc)


def remove_proxy(client, name: str) -> bool:
    """Remove a single proxy from the pool and Docker."""
    global pool, target_count
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
        app.logger.error("error removing proxy %s: %s", name, exc)
    return True


def _rotate_one(client, name: str) -> bool:
    """Rotate WARP on a single proxy and verify reconnection."""
    try:
        containers = client.containers.list(filters={"name": name})
        if not containers:
            return False
        c = containers[0]
        app.logger.info("rotating WARP on %s", name)
        c.exec_run(["warp-cli", "--accept-tos", "disconnect"], timeout=10)
        time.sleep(2)
        c.exec_run(["warp-cli", "--accept-tos", "connect"], timeout=10)
        # Verify reconnection
        for _ in range(30):
            status = get_container_warp_status(c)
            if status and "Connected" in status:
                app.logger.info("warp rotation verified on %s", name)
                return True
            time.sleep(2)
        app.logger.warning("warp rotation on %s did not complete within timeout", name)
        return False
    except Exception as exc:
        app.logger.error("rotate failed for %s: %s", name, exc)
        return False


# ── Background health checker (single thread) ───────────────────

_health_thread_started = False


def start_health_checker():
    global _health_thread_started
    if _health_thread_started:
        return
    _health_thread_started = True
    t = threading.Thread(target=_health_loop, daemon=True, name="health-checker")
    t.start()
    app.logger.debug("health checker started")


def _health_loop():
    """Background: periodically check all proxy health every 30s."""
    while True:
        time.sleep(30)
        try:
            # Obtain a fresh client each cycle so a Docker daemon restart is
            # detected on the next iteration rather than hanging on a stale
            # connection for the whole 30-second sweep.
            client = _get_docker_client()
        except docker.errors.DockerException:
            app.logger.warning("health check: docker unavailable, retrying in 30s")
            continue
        try:
            with pool_lock:
                snapshot = list(pool)
            for ep in snapshot:
                try:
                    c = client.containers.get(ep.container_id)
                    # Remove stopped containers from pool to prevent leaks
                    if c.status != "running":
                        app.logger.warning(
                            "removing stale entry %s — container status: %s",
                            ep.name, c.status,
                        )
                        with pool_lock:
                            pool[:] = [live for live in pool if live.name != ep.name]
                        continue
                    healthy = container_is_healthy(c)
                    warp = get_container_warp_status(c) or "unknown"
                    with pool_lock:
                        for live in pool:
                            if live.name == ep.name:
                                live.healthy = healthy
                                live.warp_status = warp
                                break
                except docker.errors.NotFound:
                    app.logger.warning("removing stale entry %s — container gone", ep.name)
                    with pool_lock:
                        pool[:] = [live for live in pool if live.name != ep.name]
                except Exception as exc:
                    app.logger.debug("health check error for %s: %s", ep.name, exc)
                    with pool_lock:
                        for live in pool:
                            if live.name == ep.name:
                                live.healthy = False
                                break
            # Replenish if pool dropped below target
            ensure_count(client)
        except Exception as exc:
            app.logger.error("health check cycle failed: %s", exc)
            _reset_docker_client()  # force reconnection on next cycle


_wait_active = False
_wait_lock = threading.Lock()


def spawn_wait_for_new_proxies():
    global _wait_active
    with _wait_lock:
        if _wait_active:
            return
        _wait_active = True
    thread = threading.Thread(
        target=_wait_for_new_proxies,
        daemon=True,
        name="proxy-waiter",
    )
    thread.start()


def _wait_for_new_proxies():
    """Background: wait for recently created proxies to become healthy."""
    global _wait_active
    deadline = time.time() + PROXY_WAIT_TIMEOUT
    try:
        client = _get_docker_client()
    except Exception as exc:
        app.logger.error("failed to connect to docker in wait thread: %s", exc)
        with _wait_lock:
            _wait_active = False
        return
    try:
        while time.time() < deadline:
            with pool_lock:
                for ep in pool:
                    if ep.healthy:
                        continue
                    try:
                        c = client.containers.get(ep.container_id)
                        if container_is_healthy(c):
                            ep.healthy = True
                            ep.warp_status = get_container_warp_status(c) or "connected"
                            app.logger.info("proxy %s is now healthy", ep.name)
                    except docker.errors.NotFound:
                        app.logger.debug("proxy %s container not found during wait", ep.name)
                    except Exception as exc:
                        app.logger.debug("wait check for %s: %s", ep.name, exc)
            time.sleep(3)
    finally:
        with _wait_lock:
            _wait_active = False


# ── Flask routes ────────────────────────────────────────────────

def with_client(f):
    """Inject Docker client into route handler."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            client = _get_docker_client()
            return f(client, *args, **kwargs)
        except docker.errors.DockerException as e:
            app.logger.error("Docker unavailable: %s", e)
            _reset_docker_client()  # allow reconnection on next request
            return jsonify({"error": f"Docker unavailable"}), 503
    return wrapper


@app.route("/health")
@with_client
def health(client):
    with pool_lock:
        healthy_count = sum(1 for ep in pool if ep.healthy)
        total = len(pool)
    if total == 0:
        return jsonify({"status": "initializing", "pool_size": 0}), 503
    ok = healthy_count > 0
    return jsonify({
        "status": "ok" if ok else "degraded",
        "pool_size": total,
        "healthy": healthy_count,
    }), 200 if ok else 503


@app.route("/status")
@with_client
def status(client):
    with pool_lock:
        healthy_count = sum(1 for ep in pool if ep.healthy)
        total = len(pool)
        proxies = [
            {
                "name": ep.name,
                "socks5": ep.socks5_url,
                "healthy": ep.healthy,
                "warp": ep.warp_status,
                "in_flight": ep.in_flight,
                "uptime_s": round(time.time() - ep.created_at, 1),
            }
            for ep in pool
        ]
    return jsonify({
        "version": "1.0",
        "pool_size": total,
        "healthy": healthy_count,
        "degraded": total - healthy_count,
        "target": target_count,
        "proxies": proxies,
    })


@app.route("/proxies", methods=["GET"])
@with_client
def list_proxies(client):
    with pool_lock:
        proxies = [
            {
                "name": ep.name,
                "socks5": ep.socks5_url,
                "healthy": ep.healthy,
                "warp": ep.warp_status,
            }
            for ep in pool
        ]
    return jsonify(proxies)


@app.route("/proxies", methods=["POST"])
@with_client
def create_proxy_route(client):
    suffix = os.urandom(4).hex()
    name = f"{WARPATE_PREFIX}{suffix}"
    with pool_lock:
        if len(pool) >= MAX_POOL_SIZE:
            return jsonify({"error": f"pool at max size ({MAX_POOL_SIZE})"}), 400
    container = create_proxy_container(client, name)
    if not container:
        return jsonify({"error": f"failed to create proxy {name}"}), 500
    cid = container.id or ""
    ep = ProxyEndpoint(name=name, container_id=cid)
    with pool_lock:
        if len(pool) >= MAX_POOL_SIZE:
            remove_container(container)
            return jsonify({"error": f"pool at max size ({MAX_POOL_SIZE})"}), 400
        pool.append(ep)
    spawn_wait_for_new_proxies()
    return jsonify({"name": name, "socks5": f"socks5://{name}:{SOCKS5_PORT}"}), 201


@app.route("/proxies/<name>", methods=["DELETE"])
@with_client
def delete_proxy_route(client, name):
    if remove_proxy(client, name):
        return jsonify({"status": "removed", "name": name})
    return jsonify({"error": f"proxy {name} not found"}), 404


@app.route("/proxies/<name>/rotate", methods=["POST"])
@with_client
def rotate_proxy_route(client, name):
    ok = _rotate_one(client, name)
    if not ok:
        return jsonify({"error": f"rotate failed for {name}"}), 500
    # Look up container_id under lock, then health-check outside
    container_id = None
    with pool_lock:
        for ep in pool:
            if ep.name == name:
                container_id = ep.container_id
                break
    if container_id is None:
        return jsonify({"error": f"proxy {name} not found in pool"}), 404
    try:
        c = client.containers.get(container_id)
        healthy = container_is_healthy(c)
        warp = get_container_warp_status(c) or "unknown"
    except docker.errors.NotFound:
        return jsonify({"error": f"container {name} not found after rotation"}), 500
    except Exception as exc:
        app.logger.error("health check after rotate for %s: %s", name, exc)
        healthy = False
        warp = "unknown"
    with pool_lock:
        for ep in pool:
            if ep.name == name:
                ep.healthy = healthy
                ep.warp_status = warp
                break
    return jsonify({
        "status": "rotated",
        "name": name,
        "healthy": healthy,
        "warp": warp,
    })


@app.route("/scale", methods=["POST"])
@with_client
def scale(client):
    global target_count
    count = request.args.get("count", type=int)
    if count is None:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            count = data.get("count", target_count)
        else:
            count = target_count
    if count < 0:
        return jsonify({"error": "count must be >= 0"}), 400
    if count > MAX_POOL_SIZE:
        return jsonify({"error": f"count exceeds max pool size ({MAX_POOL_SIZE})"}), 400
    with pool_lock:
        target_count = count
    ensure_count(client)
    with pool_lock:
        pool_size = len(pool)
    return jsonify({
        "status": "scaled",
        "target": count,
        "pool_size": pool_size,
    })


# ── Startup ─────────────────────────────────────────────────────

def initialize_pool():
    """Discover existing containers and ensure target count on startup."""
    try:
        client = _get_docker_client()
    except docker.errors.DockerException as e:
        app.logger.warning("Docker unavailable at startup: %s", e)
        return

    try:
        global pool
        discovered = discover_pool(client)
        with pool_lock:
            pool = discovered
        app.logger.info("discovered %d existing warpgate containers", len(discovered))
        ensure_count(client)
        with pool_lock:
            actual = len(pool)
        app.logger.info("pool initialized, target=%d actual=%d", target_count, actual)
    finally:
        pass  # Keep the shared client alive for health checker and routes
    start_health_checker()


# ── Auth middleware for Flask routes ────────────────────────────

@app.before_request
def authenticate():
    api_key = _get_api_key()
    if api_key is None:
        return  # auth disabled — no MANAGER_API_KEY configured
    client_ip = request.remote_addr or "unknown"
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {api_key}"
    if not hmac.compare_digest(auth, expected):
        # Rate-limit only failed authentication attempts, not all requests.
        # This allows legitimate traffic (e.g. health checks from load
        # balancers) to pass freely once authenticated.
        if not _check_rate_limit(f"auth:{client_ip}"):
            app.logger.warning("rate limit exceeded for %s", client_ip)
            return jsonify({"error": "too many requests"}), 429
        app.logger.warning("failed authentication attempt from %s", client_ip)
        return jsonify({"error": "unauthorized"}), 401


# ── Main ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("MANAGER_DEBUG") else logging.INFO,
        format="[warpgate] %(message)s",
        stream=sys.stderr,
    )
    # Warm the API key lookup so auth state is resolved once at startup
    _get_api_key()
    if not os.environ.get("MANAGER_SKIP_INIT"):
        initialize_pool()
    debug = bool(os.environ.get("MANAGER_DEBUG"))
    app.run(host="0.0.0.0", port=MANAGER_PORT, threaded=True, debug=debug)
