# ./Caddy/Dockerfile
FROM caddy:2.11.4-builder AS builder

# Plugins: forwardproxy (SSRF proxy for Dify) + rate-limit (F-B7)
RUN xcaddy build \
    --with github.com/caddyserver/forwardproxy \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2.11.4

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]