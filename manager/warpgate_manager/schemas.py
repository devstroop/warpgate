"""Serializers and manual request validation (no framework dependency).

Deliberately dependency-free: pydantic/Connexion are out of scope for this
release.  Every serializer here is covered by unit tests so the hand-rolled
validation cannot drift from the OpenAPI contract.
"""

import re
import time

from .pool import ProxyEndpoint
from .tasks import Task


class ValidationError(Exception):
    """Raised for request bodies that do not match the API contract."""


def iso8601(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def to_proxy_dict(ep: ProxyEndpoint) -> dict:
    return {
        "id": ep.name,
        "container_id": ep.container_id,
        "endpoints": {
            "socks5": ep.socks5_url,
            "http": ep.http_url,
        },
        "status": {
            "healthy": ep.healthy,
            "socks5": ep.socks5_ok,
            "http": ep.http_ok,
            "warp": {
                "connected": ep.warp_connected,
                "detail": ep.warp_detail,
            },
        },
        "created_at": iso8601(ep.created_at),
        "uptime_s": round(time.time() - ep.created_at, 1),
    }


def to_task_dict(t: Task) -> dict:
    return {
        "id": t.id,
        "type": t.type,
        "status": t.status,
        "result": t.result,
        "error": t.error,
        "error_code": t.error_code,
        "created_at": iso8601(t.created_at),
        "started_at": iso8601(t.started_at),
        "finished_at": iso8601(t.finished_at),
    }


def to_pool_dict(proxies: list[ProxyEndpoint], target: int, include_proxies: bool = True) -> dict:
    healthy = sum(1 for p in proxies if p.healthy)
    summary = {
        "target": target,
        "pool_size": len(proxies),
        "healthy": healthy,
        "degraded": len(proxies) - healthy,
    }
    if include_proxies:
        summary["proxies"] = [to_proxy_dict(p) for p in proxies]
    return summary


def to_config_dict(target: int) -> dict:
    from . import config
    return {
        "image": config.WARPGATE_IMAGE,
        "prefix": config.WARPGATE_PREFIX,
        "network": config.WARPGATE_NETWORK,
        "max_pool": config.MANAGER_MAX_POOL,
        "target": target,
    }


# ── Request validation ──────────────────────────────────────────────────

def _is_bool(x) -> bool:
    return isinstance(x, bool)


def validate_scale_target(body) -> int:
    """Validate a PATCH /v1/pool body. Returns the target integer."""
    if body is None or not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    target = body.get("target")
    if not isinstance(target, int) or _is_bool(target):
        raise ValidationError("target must be an integer")
    if target < 0:
        raise ValidationError("target must be >= 0")
    from . import config
    if target > config.MANAGER_MAX_POOL:
        raise ValidationError(f"target exceeds max pool size ({config.MANAGER_MAX_POOL})")
    return target


def validate_rotate_scope(body) -> str:
    """Validate a POST /v1/proxies/rotate body. Returns 'unhealthy'|'all'."""
    scope = "unhealthy"
    if body is not None:
        if not isinstance(body, dict):
            raise ValidationError("request body must be a JSON object")
        scope = body.get("scope", "unhealthy")
    if scope not in ("all", "unhealthy"):
        raise ValidationError("scope must be 'all' or 'unhealthy'")
    return scope


# Docker container names: [a-zA-Z0-9][a-zA-Z0-9_.-]*
PROXY_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
PROXY_NAME_MAX = 64


def validate_proxy_name(body) -> str | None:
    """Validate a POST /v1/proxies body. Returns the explicit name or None."""
    if body is None:
        return None
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    name = body.get("name")
    if name is None:
        return None
    if not isinstance(name, str):
        raise ValidationError("name must be a string")
    if not (1 <= len(name) <= PROXY_NAME_MAX):
        raise ValidationError(f"name must be 1-{PROXY_NAME_MAX} characters")
    if not PROXY_NAME_RE.match(name):
        raise ValidationError("name must match a docker container name ([a-zA-Z0-9][a-zA-Z0-9_.-]*)")
    return name
