# ./Caddy/Dockerfile
FROM caddy:2.11.7-builder AS builder

# Plugins: forwardproxy (SSRF proxy for Dify) + rate-limit (F-B7)
#
# #2571: both plugins are pinned to a full commit sha. Unpinned, xcaddy builds
# whatever the plugin's default branch is at build time, so two builds of the
# same release could ship different SSRF-proxy code, including the
# rebinding-safe dial in forwardproxy.go (resolve once, check every IP against
# the ACL, dial that IP). Neither repo tags releases that carry the current
# code (forwardproxy's last tag is 1.0.1; caddy-ratelimit's is v0.1.0), so the
# pin is the commit. To bump: take the new sha from the repo, rebuild, rerun
# the SSRF-proxy and rate-limit suites. Guarded by
# tests/unit/consistency/test_2571_xcaddy_plugins_are_pinned.py.
#   forwardproxy   master 2026-03-21 "Add :443 to the quickstart (#190)"
#   caddy-ratelimit master 2026-06-12 "fix(metrics): re-register collectors on each config reload (#102)"
#
# #2336: a stock caddy binary carries Go standard library, grpc, x/net and
# x/text findings (High, fix published). Two inputs change, the caddy version does
# not (#2837: 2.11.7 itself already requires chi 5.3.2 and compress 1.20.1, so
# those two pins follow it; a pin below what caddy requires would DOWNGRADE it):
#   * GOTOOLCHAIN pins the Go release that compiles it (the builder image brings
#     its own, older one; `go` fetches the pinned toolchain from the module proxy,
#     checksum-verified, the same network the build already needs for modules);
#   * the vulnerable modules are raised by naming one of their packages as an
#     extra `--with <package>@<tag>`: xcaddy runs `go get <package>@<tag>`, which
#     raises the whole module (and what it requires, e.g. x/net 0.60 needs
#     x/crypto 0.57 and x/text 0.42). The packages are compiled into caddy
#     already; the extra import changes nothing at runtime. A `go.mod replace`
#     does NOT work here: xcaddy's `--replace` of a module by itself at another
#     version left grpc and x/net at the old versions (measured).
# The check at the end fails the build if the toolchain or any raised module is
# not what is pinned here. Versions: Go/OSV vulnerability data, 2026-10-20, each
# present on proxy.golang.org.
ENV GOTOOLCHAIN=go1.27.3
RUN xcaddy build \
    --with github.com/caddyserver/forwardproxy@0aab84dad4fc2830789f34e27b4d7bc22a40889e \
    --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db \
    --with golang.org/x/net/http2@v0.60.0 \
    --with golang.org/x/text/unicode/norm@v0.42.0 \
    --with golang.org/x/crypto/chacha20poly1305@v0.57.0 \
    --with google.golang.org/grpc@v1.83.2 \
    --with github.com/klauspost/compress/zstd@v1.20.1 \
    --with github.com/go-chi/chi/v5@v5.3.2
RUN set -eu; \
    go version -m /usr/bin/caddy > /tmp/caddy.mods; \
    head -n1 /tmp/caddy.mods | grep -qx '/usr/bin/caddy: go1.27.3' \
        || { echo "FATAL(#2336): caddy was not built with go1.27.3" >&2; head -n1 /tmp/caddy.mods >&2; exit 1; }; \
    for m in golang.org/x/net:v0.60.0 golang.org/x/text:v0.42.0 golang.org/x/crypto:v0.57.0 google.golang.org/grpc:v1.83.2 github.com/klauspost/compress:v1.20.1 github.com/go-chi/chi/v5:v5.3.2; do \
        awk -v n="${m%%:*}" -v v="${m##*:}" '($1=="dep"||$1=="mod") && $2==n && $3==v {f=1} END{exit !f}' /tmp/caddy.mods \
            || { echo "FATAL(#2336): caddy was built without $m" >&2; exit 1; }; \
    done

FROM caddy:2.11.7
# #2336: the runtime base (Alpine) is brought to today's security level; the caddy
# binary itself is the one built above.
RUN apk upgrade --no-cache

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY entrypoint.sh /entrypoint.sh
# #2675: the Dify SSRF deny/allow lists, sourced by the entrypoint.
COPY ssrf-box.sh /ssrf-box.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
