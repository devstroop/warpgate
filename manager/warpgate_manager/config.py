"""Runtime configuration, resolved once from environment variables."""

import os

# Proxy container image / naming / networking
WARPGATE_IMAGE = os.environ.get("WARPGATE_IMAGE", "warpgate:local")
WARPGATE_PREFIX = os.environ.get("WARPGATE_PREFIX", "warpgate-")
WARPGATE_COUNT = int(os.environ.get("WARPGATE_COUNT", "3"))
WARPGATE_NETWORK = os.environ.get("WARPGATE_NETWORK", "warpgate-net")

# Manager HTTP API
MANAGER_PORT = int(os.environ.get("MANAGER_PORT", "9090"))
MANAGER_MAX_POOL = int(os.environ.get("MANAGER_MAX_POOL", "20"))
MANAGER_RATE_LIMIT = int(os.environ.get("MANAGER_RATE_LIMIT", "10"))
RATE_LIMIT_WINDOW = 60  # seconds
MANAGER_DEBUG = os.environ.get("MANAGER_DEBUG") in ("1", "true", "True")
MANAGER_SKIP_INIT = os.environ.get("MANAGER_SKIP_INIT") in ("1", "true", "True")

# Health probe target for the HTTP (CONNECT) check. Defaults to Cloudflare —
# the same endpoint the WARP check already relies on. Configurable so the
# manager never depends on a third-party host the operator does not control.
MANAGER_HEALTH_HOST = os.environ.get("MANAGER_HEALTH_HOST", "cloudflare.com")
MANAGER_HEALTH_PORT = int(os.environ.get("MANAGER_HEALTH_PORT", "443"))

# Proxy container ports
SOCKS5_PORT = 1080
HTTP_PORT = 3128

# Create / rotate timeouts
PROXY_WAIT_TIMEOUT = 60  # seconds to wait for a proxy to become healthy
CREATE_RETRIES = 3

# Async task registry bounds (in-memory, lost on restart)
TASK_WORKERS = 3
TASK_MAX_ITEMS = 200
TASK_MAX_AGE = 3600  # seconds
# How long an Idempotency-Key is honored before it can be reused.
IDEMPOTENCY_MAX_AGE = 86400  # seconds
