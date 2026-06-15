FROM ubuntu:noble

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg iproute2 \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH

# Cloudflare WARP repo (arch-aware)
RUN set -eux; \
    case "${TARGETARCH}" in \
        arm64|aarch64) deb_arch=arm64 ;; \
        *)             deb_arch=amd64 ;; \
    esac; \
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg; \
    echo "deb [arch=${deb_arch} signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main" \
       | tee /etc/apt/sources.list.d/cloudflare-client.list; \
    apt-get update && apt-get install -y cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="${TARGETARCH}"; \
    case "$arch" in \
        arm64|aarch64) arch=arm64 ;; \
        arm)           arch=arm ;; \
        *)             arch=x86_64 ;; \
    esac; \
    version="$(curl -fsSL https://api.github.com/repos/3proxy/3proxy/releases/latest \
        | grep '"tag_name"' | cut -d'"' -f4)"; \
    curl -fsSL -o /tmp/3proxy.deb \
        "https://github.com/3proxy/3proxy/releases/download/${version}/3proxy-${version}.${arch}.deb"; \
    dpkg -i /tmp/3proxy.deb; \
    rm -f /tmp/3proxy.deb

RUN rm -f /etc/3proxy/3proxy.cfg

EXPOSE 1080 3128

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
  CMD ss -tlnp | grep -q ':1080' || ss -tlnp | grep -q ':3128' || exit 1

COPY 3proxy.cfg /etc/3proxy/3proxy.cfg
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
