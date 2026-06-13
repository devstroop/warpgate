"""
warp-proxy manager — REST API to manage warp-proxy Docker containers.

GET    /              — list all managed warp-proxy instances
GET    /{name}        — get details for a single instance
POST   /create        — spin up a new warp-proxy container
POST   /restart       — restart a container (or start a stopped one)
POST   /renew         — trigger WARP reconnect on a container
DELETE /delete        — stop and destroy a container
GET    /health        — health check (no auth required)

All routes except /health require X-API-Key header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("warp-manager")

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "proxies.json"
WARP_IMAGE = os.environ.get("WARP_IMAGE", "warp-proxy:latest")
SOCKS_BASE = int(os.environ.get("SOCKS_BASE", "1080"))
HTTP_BASE = int(os.environ.get("HTTP_BASE", "3128"))
MAX_INSTANCES = int(os.environ.get("MAX_INSTANCES", "10"))
API_KEY = os.environ.get("MANAGER_API_KEY", "")
STOP_TIMEOUT = int(os.environ.get("STOP_TIMEOUT", "10"))
DOCKER_TIMEOUT = int(os.environ.get("DOCKER_TIMEOUT", "60"))

# Docker label keys
LABEL_MANAGED = "warp.manager"
LABEL_SOCKS = "warp.socks"
LABEL_HTTP = "warp.http"
LABEL_CREATED = "warp.created"

if not API_KEY:
    log.warning("MANAGER_API_KEY is not set — manager API is open to anyone on the network!")

# global lock for all state-modifying operations — prevents races between
# port allocation, docker commands, and state persistence
_global_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _docker(*args: str, timeout: int = DOCKER_TIMEOUT) -> str:
    """Run a docker CLI command, return stdout.  Raise on failure."""
    cmd = ["docker"] + list(args)
    log.info("docker %s", " ".join(args))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip()
        log.error("docker failed: %s", err)
        raise RuntimeError(err)
    return proc.stdout.strip()


def _port_is_free(port: int) -> bool:
    """Check whether a TCP port is free on the host."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) != 0
    except OSError:
        return False


def _load_state() -> dict:
    """Load persisted proxy registry."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    """Persist proxy registry atomically (temp file + rename)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


def _next_ports(state: dict) -> tuple[int, int]:
    """Return the next available (socks, http) port pair."""
    used_socks = {v["socks_port"] for v in state.values()}
    used_http = {v["http_port"] for v in state.values()}
    for offset in range(MAX_INSTANCES):
        s = SOCKS_BASE + offset
        h = HTTP_BASE + offset
        if s not in used_socks and h not in used_http:
            return s, h
    raise HTTPException(409, "No free ports — MAX_INSTANCES reached")


def _is_running(name: str) -> bool:
    """Return True if the container is in 'running' state."""
    try:
        inspect = json.loads(_docker("inspect", name))
        return inspect[0]["State"]["Status"] == "running"
    except RuntimeError:
        return False


def _ensure_unused_ports(socks_port: int, http_port: int) -> None:
    """Raise if either port is already in use on the host."""
    if not _port_is_free(socks_port):
        raise HTTPException(409, f"Host port {socks_port} already in use")
    if not _port_is_free(http_port):
        raise HTTPException(409, f"Host port {http_port} already in use")


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

async def _health_monitor(app: FastAPI):
    """Periodically check all managed proxies and update state."""
    while True:
        await asyncio.sleep(60)
        try:
            state = _load_state()
            changed = False
            for name in list(state):
                try:
                    inspect = json.loads(_docker("inspect", name))
                    new_status = inspect[0]["State"]["Status"]
                    old_status = state[name].get("status")
                    if new_status != old_status:
                        state[name]["status"] = new_status
                        changed = True
                        log.info("Proxy %s status: %s → %s", name, old_status or "unknown", new_status)
                    # refresh warp IP if running
                    if new_status == "running":
                        try:
                            ip = _docker("exec", name, "cat", "/var/cache/warp-ip.txt", timeout=10).strip()
                            if ip and ip != state[name].get("warp_ip"):
                                state[name]["warp_ip"] = ip
                                changed = True
                        except RuntimeError:
                            pass
                except RuntimeError:
                    if name in state:
                        del state[name]
                        changed = True
                        log.warning("Proxy %s disappeared from Docker, pruned from state", name)
            if changed:
                _save_state(state)
        except Exception:
            log.exception("Health monitor iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Sync persisted state with running Docker containers on startup."""
    log.info("Starting warp-proxy manager")

    state = _load_state()
    running: set[str] = set()
    try:
        out = _docker(
            "ps", "-a",
            "--filter", f"label={LABEL_MANAGED}=true",
            "--format", "{{.Names}}",
        )
        running = set(out.splitlines()) if out else set()
    except RuntimeError:
        log.warning("Could not query Docker for managed containers")

    # drop state entries whose containers don't exist
    cleaned = {k: v for k, v in state.items() if k in running}
    if len(cleaned) != len(state):
        log.info("Pruned %d stale state entries", len(state) - len(cleaned))
        _save_state(cleaned)

    # detect orphan containers (managed label but not in state)
    orphaned = running - set(cleaned)
    if orphaned:
        log.warning("Found %d orphaned containers not in state: %s", len(orphaned), orphaned)

    app.state.proxies = cleaned
    log.info("Manager ready — tracking %d proxies", len(cleaned))

    # start background health monitor
    monitor_task = asyncio.create_task(_health_monitor(app))

    yield

    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
    log.info("Shutting down warp-proxy manager")


app = FastAPI(title="warp-proxy-manager", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# auth middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if request.url.path == "/health" or request.method == "OPTIONS":
        return await call_next(request)
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(401, "Missing or invalid X-API-Key header")
    return await call_next(request)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class ProxyInfo(BaseModel):
    name: str
    socks_port: int
    http_port: int
    status: str
    warp_ip: str | None = None
    created: str


class CreateRequest(BaseModel):
    socks_port: int | None = Field(default=None, ge=1024, le=65535)
    http_port: int | None = Field(default=None, ge=1024, le=65535)


class ContainerRef(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# shared — resolve a single proxy
# ---------------------------------------------------------------------------

def _get_one(name: str) -> dict:
    """Load state and return meta for *name*, or 404."""
    state = _load_state()
    if name not in state:
        raise HTTPException(404, f"Unknown proxy: {name}")
    return state[name]


def _build_proxy_info(name: str, meta: dict) -> ProxyInfo:
    """Resolve live status & WARP IP for a proxy."""
    status = "unknown"
    warp_ip: str | None = meta.get("warp_ip")

    try:
        inspect = json.loads(_docker("inspect", name))
        status = inspect[0]["State"]["Status"]
    except RuntimeError:
        status = "missing"

    if status == "running":
        try:
            warp_ip = _docker("exec", name, "cat", "/var/cache/warp-ip.txt", timeout=10)
        except RuntimeError:
            pass

    return ProxyInfo(
        name=name,
        socks_port=meta["socks_port"],
        http_port=meta["http_port"],
        status=status,
        warp_ip=warp_ip.strip() if warp_ip else None,
        created=meta["created"],
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/", response_model=list[ProxyInfo])
def list_proxies():
    """Return every warp-proxy container managed by this server."""
    state = _load_state()
    return [_build_proxy_info(name, meta) for name, meta in sorted(state.items())]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/{name}", response_model=ProxyInfo)
def get_proxy(name: str):
    """Return details for a single proxy by container name."""
    meta = _get_one(name)
    return _build_proxy_info(name, meta)


@app.post("/create", response_model=ProxyInfo, status_code=201)
async def create_proxy(body: CreateRequest | None = None):
    """Launch a new warp-proxy container with unique ports."""
    async with _global_lock:
        state = _load_state()
        socks_port, http_port = _next_ports(state)

        if body:
            if body.socks_port is not None:
                socks_port = body.socks_port
            if body.http_port is not None:
                http_port = body.http_port

        # check for conflicts in state
        for v in state.values():
            if v["socks_port"] == socks_port:
                raise HTTPException(409, f"SOCKS5 port {socks_port} already in use")
            if v["http_port"] == http_port:
                raise HTTPException(409, f"HTTP port {http_port} already in use")

        # check host-level port availability
        _ensure_unused_ports(socks_port, http_port)

        name = f"warp-proxy-{socks_port}"
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        cmd = [
            "run", "-d",
            "--name", name,
            "--label", f"{LABEL_MANAGED}=true",
            "--label", f"{LABEL_SOCKS}={socks_port}",
            "--label", f"{LABEL_HTTP}={http_port}",
            "--label", f"{LABEL_CREATED}={created}",
            "-p", f"{socks_port}:1080",
            "-p", f"{http_port}:3128",
            "-v", f"{name}-warp-data:/var/lib/cloudflare-warp",
            "-v", f"{name}-warp-cache:/var/cache",
            "--cap-add", "NET_ADMIN",
            "--cap-add", "SYS_ADMIN",
            "--device", "/dev/net/tun",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=0",
            "--memory", "256m",
            "--memory-swap", "512m",
            "--cpus", "1",
            "--pids-limit", "100",
            "--restart", "unless-stopped",
            WARP_IMAGE,
        ]

        try:
            _docker(*cmd, timeout=30)
        except RuntimeError as e:
            raise HTTPException(500, f"docker run failed: {e}") from e

        meta = {
            "socks_port": socks_port,
            "http_port": http_port,
            "created": created,
        }
        state[name] = meta
        _save_state(state)
        log.info("Created proxy %s (socks=%d http=%d)", name, socks_port, http_port)

    return ProxyInfo(
        name=name,
        socks_port=socks_port,
        http_port=http_port,
        status="starting",
        created=created,
    )


@app.post("/restart", response_model=ProxyInfo)
def restart_proxy(body: ContainerRef):
    """Restart a warp-proxy container (or start it if stopped)."""
    name = body.name
    _get_one(name)  # 404 if unknown

    try:
        inspect = json.loads(_docker("inspect", name))
        current = inspect[0]["State"]["Status"]
    except RuntimeError:
        raise HTTPException(404, f"Container {name} not found in Docker")

    if current == "running":
        try:
            _docker("restart", "-t", str(STOP_TIMEOUT), name)
        except RuntimeError as e:
            raise HTTPException(500, f"docker restart failed: {e}") from e
    else:
        try:
            _docker("start", name)
        except RuntimeError as e:
            raise HTTPException(500, f"docker start failed: {e}") from e

    log.info("Restarted proxy %s (was %s)", name, current)
    state = _load_state()
    return _build_proxy_info(name, state[name])


@app.post("/renew", response_model=dict)
def renew_proxy(body: ContainerRef):
    """Trigger a WARP reconnect inside the container (disconnect → connect)."""
    name = body.name
    _get_one(name)  # 404 if unknown

    if not _is_running(name):
        raise HTTPException(409, f"Container {name} is not running")

    try:
        _docker("exec", name, "warp-cli", "--accept-tos", "disconnect")
        time.sleep(1)
        _docker("exec", name, "warp-cli", "--accept-tos", "connect")
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e

    log.info("Renewed WARP on %s", name)
    return {"name": name, "action": "renew", "status": "ok"}


@app.delete("/delete", response_model=dict)
async def delete_proxy(body: ContainerRef):
    """Stop and remove a warp-proxy container and its volumes."""
    async with _global_lock:
        name = body.name
        state = _load_state()
        if name not in state:
            raise HTTPException(404, f"Unknown proxy: {name}")

        errors: list[str] = []

        # stop with timeout
        try:
            _docker("stop", "-t", str(STOP_TIMEOUT), name)
        except RuntimeError as e:
            errors.append(f"stop: {e}")

        # remove container
        try:
            _docker("rm", "-v", name)
        except RuntimeError as e:
            errors.append(f"rm: {e}")

        # remove named volumes
        for vol_suffix in ("-warp-data", "-warp-cache"):
            vol_name = f"{name}{vol_suffix}"
            try:
                _docker("volume", "rm", "-f", vol_name)
            except RuntimeError:
                pass  # volume may not exist

        if errors:
            raise HTTPException(500, "; ".join(errors))

        del state[name]
        _save_state(state)
        log.info("Deleted proxy %s", name)
        return {"name": name, "action": "deleted", "status": "ok"}



