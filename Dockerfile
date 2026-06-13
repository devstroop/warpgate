FROM ubuntu:noble

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg software-properties-common \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare WARP repo
RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
        | gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main" \
       | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update && apt-get install -y cloudflare-warp \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH

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

COPY 3proxy.cfg /etc/3proxy/3proxy.cfg
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
