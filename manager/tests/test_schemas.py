"""Serializer and validator unit tests."""

import time

import pytest

from warpgate_manager import config, schemas
from warpgate_manager.tasks import Task

from tests.conftest import make_proxy


def test_proxy_dict_shape():
    ep = make_proxy(
        "warpgate-abc12345",
        healthy=True,
        socks5_ok=True,
        http_ok=True,
        warp_connected=True,
        warp_detail="Connected",
        created_at=time.time() - 120,
    )
    d = schemas.to_proxy_dict(ep)
    assert d["id"] == "warpgate-abc12345"
    assert d["container_id"] == "cid-warpgate-abc12345"
    assert d["endpoints"]["socks5"] == "socks5://warpgate-abc12345:1080"
    assert d["endpoints"]["http"] == "http://warpgate-abc12345:3128"
    assert d["status"] == {
        "healthy": True,
        "socks5": True,
        "http": True,
        "warp": {"connected": True, "detail": "Connected"},
    }
    assert d["created_at"].endswith("Z")
    assert isinstance(d["uptime_s"], float)


def test_task_dict_iso8601_and_nullable():
    t = Task("proxy.create")
    d = schemas.to_task_dict(t)
    assert d["type"] == "proxy.create"
    assert d["status"] == "pending"
    assert d["started_at"] is None
    assert d["finished_at"] is None
    assert d["created_at"].endswith("Z")


def test_validate_scale_target():
    assert schemas.validate_scale_target({"target": 3}) == 3
    assert schemas.validate_scale_target({"target": 0}) == 0
    with pytest.raises(schemas.ValidationError):
        schemas.validate_scale_target(None)
    with pytest.raises(schemas.ValidationError):
        schemas.validate_scale_target({"target": "3"})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_scale_target({"target": True})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_scale_target({"target": -1})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_scale_target({"target": config.MANAGER_MAX_POOL + 1})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_scale_target({"noop": 1})


def test_validate_rotate_scope():
    assert schemas.validate_rotate_scope(None) == "unhealthy"
    assert schemas.validate_rotate_scope({}) == "unhealthy"
    assert schemas.validate_rotate_scope({"scope": "all"}) == "all"
    with pytest.raises(schemas.ValidationError):
        schemas.validate_rotate_scope({"scope": "everything"})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_rotate_scope("all")


def test_validate_proxy_name():
    assert schemas.validate_proxy_name(None) is None
    assert schemas.validate_proxy_name({}) is None
    assert schemas.validate_proxy_name({"name": "warpgate-custom"}) == "warpgate-custom"
    with pytest.raises(schemas.ValidationError):
        schemas.validate_proxy_name({"name": "has space"})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_proxy_name({"name": 12})
    with pytest.raises(schemas.ValidationError):
        schemas.validate_proxy_name("not-an-object")
    with pytest.raises(schemas.ValidationError):
        schemas.validate_proxy_name({"name": "x" * 65})


def test_task_dict_error_code_nullable():
    t = Task("proxy.create")
    assert schemas.to_task_dict(t)["error_code"] is None
