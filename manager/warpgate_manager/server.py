"""WarpGate Manager API v1 — Flask wiring.

Routes stay thin: pool logic lives in ``pool``, task orchestration in
``tasks``, serialization/validation in ``schemas``.

Security notes:
  * All routes except ``/v1/health``, ``/v1/openapi.yaml``,
    ``/v1/docs`` and ``OPTIONS`` require ``Authorization: Bearer <key>``
    when ``MANAGER_API_KEY`` is set, so load-balancer probes work regardless.
  * Failed auth is rate-limited per client IP (``MANAGER_RATE_LIMIT`` per
    window); authenticated traffic is not throttled.
  * The API key crosses the wire in plaintext — only expose this on a trusted
    network or behind a TLS-terminating reverse proxy.
"""

import hmac
import logging
import os
import sys
import threading
import time
import uuid
from collections import defaultdict
from functools import wraps
from pathlib import Path

import docker
import docker.errors
from flask import Flask, Response, jsonify, request, send_file

from . import config
from . import pool as poolmod
from . import schemas
from .tasks import TaskError, registry

app = Flask(__name__)
log = logging.getLogger("warpgate.api")

REQUEST_ID_HEADER = "X-Request-ID"

# ── Rate limiter (failed auth only) ─────────────────────────────────────

_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_rate_limit_lock = threading.Lock()


def _check_rate_limit(key: str, max_attempts: int | None = None, window: int | None = None) -> bool:
    """Record an attempt; returns False when the key is over the limit.

    The attempt is recorded under lock before returning, so there is no
    check-then-act window for concurrent callers to bypass the limit.
    """
    max_attempts = config.MANAGER_RATE_LIMIT if max_attempts is None else max_attempts
    window = config.RATE_LIMIT_WINDOW if window is None else window
    now = time.time()
    with _rate_limit_lock:
        records = _rate_limit_store[key]
        _rate_limit_store[key] = [t for t in records if now - t < window]
        if len(_rate_limit_store[key]) >= max_attempts:
            return False
        _rate_limit_store[key].append(now)
    return True


# ── API key ─────────────────────────────────────────────────────────────

_API_KEY: str | None = None
_api_key_lock = threading.Lock()


def _get_api_key() -> str | None:
    global _API_KEY
    if _API_KEY is None:
        with _api_key_lock:
            if _API_KEY is None:
                # Empty string is treated as unset (avoids bricking the API
                # with a misconfigured MANAGER_API_KEY="").
                _API_KEY = os.environ.get("MANAGER_API_KEY") or None
    return _API_KEY


# ── Error envelope ──────────────────────────────────────────────────────

def error_response(status: int, code: str, message: str):
    return jsonify({
        "error": {
            "code": code,
            "message": message,
            "request_id": getattr(request, "request_id", None),
        }
    }), status


# ── Request plumbing ────────────────────────────────────────────────────

AUTH_EXEMPT = {
    "/v1/health",
    "/v1/openapi.yaml",
    "/v1/docs",
}


@app.before_request
def before_request():
    request.request_id = uuid.uuid4().hex
    if request.method == "OPTIONS":
        return None
    if request.path.rstrip("/") in AUTH_EXEMPT:
        return None
    api_key = _get_api_key()
    if api_key is None:
        return None
    client_ip = request.remote_addr or "unknown"
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {api_key}"
    if not hmac.compare_digest(auth, expected):
        if not _check_rate_limit(f"auth:{client_ip}"):
            log.warning("auth rate limit exceeded for %s", client_ip)
            return error_response(429, "RATE_LIMITED", "too many requests")
        log.warning("failed authentication attempt from %s", client_ip)
        return error_response(401, "UNAUTHORIZED", "unauthorized")


@app.after_request
def after_request(resp):
    rid = getattr(request, "request_id", None)
    if rid:
        resp.headers[REQUEST_ID_HEADER] = rid
    # CORS for browser-based tooling (Swagger UI) and cross-origin clients.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    if request.method == "OPTIONS":
        resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


@app.errorhandler(404)
def not_found(_e):
    return error_response(404, "NOT_FOUND", "resource not found")


@app.errorhandler(405)
def method_not_allowed(_e):
    return error_response(405, "METHOD_NOT_ALLOWED", "method not allowed")


@app.errorhandler(500)
def internal_error(_e):
    return error_response(500, "INTERNAL", "internal server error")


def with_client(f):
    """Inject the Docker client into a route handler."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            client = poolmod.get_docker_client()
            return f(client, *args, **kwargs)
        except docker.errors.DockerException as e:
            log.error("Docker unavailable: %s", e)
            poolmod.reset_docker_client()  # allow reconnection on next request
            return error_response(503, "DOCKER_UNAVAILABLE", "docker unavailable")
    return wrapper


def _accepted(task):
    resp = jsonify(schemas.to_task_dict(task))
    resp.headers["Location"] = f"/v1/tasks/{task.id}"
    return resp, 202


def _idempotent_task(key: str | None, task_type: str):
    """Return the task recorded under an Idempotency-Key, if any.

    Raises ``TaskError(IDEMPOTENCY_CONFLICT)`` when the key was already used
    for a different operation type — replays must hit the same endpoint.
    """
    if key:
        existing = registry.get_idempotent(key)
        if existing is not None:
            if existing.type != task_type:
                raise TaskError(
                    "IDEMPOTENCY_CONFLICT",
                    f"idempotency key already used for task type '{existing.type}'",
                )
            return existing
    return None


def _submit_idempotent(key: str | None, task_type: str, fn, *args):
    """Create + submit a task, honoring Idempotency-Key (dedupes retries)."""
    task = _idempotent_task(key, task_type)
    if task is not None:
        return task
    task = registry.create(task_type)
    if key:
        registry.set_idempotent(key, task.id)
    registry.submit(task, fn, *args)
    return task


# ── Task bodies ─────────────────────────────────────────────────────────

def _run_scale(client, target: int) -> dict:
    with poolmod.pool_lock:
        poolmod.target_count = target
    poolmod.ensure_count(client)
    with poolmod.pool_lock:
        proxies = list(poolmod.pool)
    return schemas.to_pool_dict(proxies, target, include_proxies=False)


def _run_create(client, name: str | None = None) -> dict:
    container = None
    cname = name
    if cname is None:
        for attempt in range(config.CREATE_RETRIES):
            cname = f"{config.WARPGATE_PREFIX}{os.urandom(4).hex()}"
            container = poolmod.create_proxy_container(client, cname)
            if container:
                break
    else:
        if poolmod.find_proxy(cname) is not None:
            raise TaskError("PROXY_EXISTS", f"proxy {cname} already exists")
        container = poolmod.create_proxy_container(client, cname)
    if container is None or cname is None:
        raise TaskError("CREATE_FAILED", "failed to create proxy container")
    ep = poolmod.add_proxy(client, cname, container)
    poolmod.wait_for_healthy(client, [cname])
    return schemas.to_proxy_dict(ep)


def _run_remove(client, name: str) -> dict:
    if not poolmod.remove_proxy(client, name):
        raise TaskError("PROXY_NOT_FOUND", f"proxy {name} not found")
    return {"status": "removed", "id": name}


def _run_rotate(client, name: str) -> dict:
    ok = poolmod.rotate_one(client, name)
    ep = poolmod.find_proxy(name)
    if ep is None:
        raise TaskError("PROXY_NOT_FOUND", f"proxy {name} not found")
    return {"rotated": ok, "proxy": schemas.to_proxy_dict(ep)}


def _run_restart(client, name: str) -> dict:
    ep = poolmod.find_proxy(name)
    if ep is None:
        raise TaskError("PROXY_NOT_FOUND", f"proxy {name} not found")
    new = poolmod.recreate_container(client, ep)
    if new is None:
        raise TaskError("RECREATE_FAILED", f"failed to recreate container {name}")
    poolmod.wait_for_healthy(client, [name])
    return {"restarted": True, "proxy": schemas.to_proxy_dict(poolmod.find_proxy(name))}


def _run_rotate_bulk(client, scope: str) -> dict:
    return poolmod.rotate_pool(client, scope)


# ── Routes: health ──────────────────────────────────────────────────────

@app.route("/v1/health")
def health():
    with poolmod.pool_lock:
        healthy = sum(1 for p in poolmod.pool if p.healthy)
        total = len(poolmod.pool)
    status = "ok" if healthy > 0 else ("initializing" if total == 0 else "degraded")
    # Liveness: the manager process is up. Pool degradation is reported in
    # the body — never kills the controller that heals the pool.
    return jsonify({"status": status, "pool_size": total, "healthy": healthy, "degraded": total - healthy})


# ── Routes: pool ────────────────────────────────────────────────────────

@app.route("/v1/pool")
def get_pool():
    include = request.args.get("include", "proxies")
    if include not in ("proxies", "none"):
        return error_response(422, "VALIDATION_ERROR", "include must be 'proxies' or 'none'")
    include_proxies = include != "none"
    with poolmod.pool_lock:
        proxies = list(poolmod.pool)
        target = poolmod.target_count
    return jsonify(schemas.to_pool_dict(proxies, target, include_proxies=include_proxies))


@app.route("/v1/pool", methods=["PATCH"])
@with_client
def patch_pool(client):
    key = request.headers.get("Idempotency-Key")
    try:
        existing = _idempotent_task(key, "pool.scale")
        if existing is not None:
            return _accepted(existing)
        body = request.get_json(silent=True)
        target = schemas.validate_scale_target(body)
        task = _submit_idempotent(key, "pool.scale", _run_scale, client, target)
    except schemas.ValidationError as exc:
        return error_response(422, "VALIDATION_ERROR", str(exc))
    except TaskError as exc:
        return error_response(409, exc.code, str(exc))
    return _accepted(task)


# ── Routes: proxies ─────────────────────────────────────────────────────

@app.route("/v1/proxies")
def list_proxies():
    healthy_filter = request.args.get("healthy")
    with poolmod.pool_lock:
        proxies = list(poolmod.pool)
    if healthy_filter is not None:
        if healthy_filter not in ("true", "false"):
            return error_response(422, "VALIDATION_ERROR", "healthy must be 'true' or 'false'")
        want = healthy_filter == "true"
        proxies = [p for p in proxies if p.healthy == want]
    return jsonify([schemas.to_proxy_dict(p) for p in proxies])


@app.route("/v1/proxies", methods=["POST"])
@with_client
def create_proxy(client):
    key = request.headers.get("Idempotency-Key")
    try:
        existing = _idempotent_task(key, "proxy.create")
        if existing is not None:
            return _accepted(existing)
        body = request.get_json(silent=True)
        name = schemas.validate_proxy_name(body)
        with poolmod.pool_lock:
            if len(poolmod.pool) >= config.MANAGER_MAX_POOL:
                return error_response(409, "POOL_AT_CAPACITY", f"pool at max size ({config.MANAGER_MAX_POOL})")
            if name is not None and any(ep.name == name for ep in poolmod.pool):
                return error_response(409, "PROXY_EXISTS", f"proxy {name} already exists")
        task = _submit_idempotent(key, "proxy.create", _run_create, client, name)
    except schemas.ValidationError as exc:
        return error_response(422, "VALIDATION_ERROR", str(exc))
    except TaskError as exc:
        return error_response(409, exc.code, str(exc))
    return _accepted(task)


@app.route("/v1/proxies/<proxy_id>")
def get_proxy(proxy_id):
    ep = poolmod.find_proxy(proxy_id)
    if ep is None:
        return error_response(404, "PROXY_NOT_FOUND", f"proxy {proxy_id} not found")
    return jsonify(schemas.to_proxy_dict(ep))


@app.route("/v1/proxies/<proxy_id>", methods=["DELETE"])
@with_client
def delete_proxy(client, proxy_id):
    key = request.headers.get("Idempotency-Key")
    try:
        existing = _idempotent_task(key, "proxy.remove")
        if existing is not None:
            return _accepted(existing)
        if poolmod.find_proxy(proxy_id) is None:
            return error_response(404, "PROXY_NOT_FOUND", f"proxy {proxy_id} not found")
        task = _submit_idempotent(key, "proxy.remove", _run_remove, client, proxy_id)
    except TaskError as exc:
        return error_response(409, exc.code, str(exc))
    return _accepted(task)


@app.route("/v1/proxies/<proxy_id>/rotate", methods=["POST"])
@with_client
def rotate_proxy(client, proxy_id):
    key = request.headers.get("Idempotency-Key")
    try:
        existing = _idempotent_task(key, "proxy.rotate")
        if existing is not None:
            return _accepted(existing)
        if poolmod.find_proxy(proxy_id) is None:
            return error_response(404, "PROXY_NOT_FOUND", f"proxy {proxy_id} not found")
        task = _submit_idempotent(key, "proxy.rotate", _run_rotate, client, proxy_id)
    except TaskError as exc:
        return error_response(409, exc.code, str(exc))
    return _accepted(task)


@app.route("/v1/proxies/<proxy_id>/restart", methods=["POST"])
@with_client
def restart_proxy(client, proxy_id):
    key = request.headers.get("Idempotency-Key")
    try:
        existing = _idempotent_task(key, "proxy.restart")
        if existing is not None:
            return _accepted(existing)
        if poolmod.find_proxy(proxy_id) is None:
            return error_response(404, "PROXY_NOT_FOUND", f"proxy {proxy_id} not found")
        task = _submit_idempotent(key, "proxy.restart", _run_restart, client, proxy_id)
    except TaskError as exc:
        return error_response(409, exc.code, str(exc))
    return _accepted(task)


@app.route("/v1/proxies/rotate", methods=["POST"])
@with_client
def rotate_proxies_bulk(client):
    key = request.headers.get("Idempotency-Key")
    try:
        existing = _idempotent_task(key, "proxy.rotate_bulk")
        if existing is not None:
            return _accepted(existing)
        body = request.get_json(silent=True)
        scope = schemas.validate_rotate_scope(body)
        task = _submit_idempotent(key, "proxy.rotate_bulk", _run_rotate_bulk, client, scope)
    except schemas.ValidationError as exc:
        return error_response(422, "VALIDATION_ERROR", str(exc))
    except TaskError as exc:
        return error_response(409, exc.code, str(exc))
    return _accepted(task)


# ── Routes: tasks ───────────────────────────────────────────────────────

@app.route("/v1/tasks/<task_id>")
def get_task(task_id):
    task = registry.get(task_id)
    if task is None:
        return error_response(404, "TASK_NOT_FOUND", f"task {task_id} not found")
    resp = jsonify(schemas.to_task_dict(task))
    if task.status in ("pending", "running"):
        resp.headers["Retry-After"] = "1"
    return resp


# ── Routes: spec / docs ─────────────────────────────────────────────────

_OPENAPI_PATH = Path(__file__).with_name("openapi.yaml")


@app.route("/v1/openapi.yaml")
def openapi_spec():
    return send_file(_OPENAPI_PATH, mimetype="application/yaml")


SWAGGER_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>WarpGate Manager API v1</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css"/>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function () {
      window.ui = SwaggerUIBundle({ url: "/v1/openapi.yaml", dom_id: "#swagger-ui" });
    };
  </script>
</body>
</html>
"""


@app.route("/v1/docs")
def docs():
    return Response(SWAGGER_UI_HTML, mimetype="text/html")


# ── Startup ─────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if config.MANAGER_DEBUG else logging.INFO,
        format="[warpgate] %(message)s",
        stream=sys.stderr,
    )
    _get_api_key()  # resolve auth state once at startup
    if not config.MANAGER_SKIP_INIT:
        poolmod.initialize_pool()
    app.run(host="0.0.0.0", port=config.MANAGER_PORT, threaded=True, debug=config.MANAGER_DEBUG)


if __name__ == "__main__":
    main()
