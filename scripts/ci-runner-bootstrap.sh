#!/usr/bin/env bash
# ci-runner-bootstrap.sh — provision a DISPOSABLE Gitea Actions runner for the
# razzfazz.ai release pipeline (#246 / #247). Run ONCE on a fresh Ubuntu 24.04+
# VM, as a user with sudo. Turns a blank VM into a registered `razzfazz-ci`
# runner that can execute .gitea/workflows/ci.yml (and the later pipeline slices).
#
#   sudo RUNNER_TOKEN=<token> bash scripts/ci-runner-bootstrap.sh
#
# The registration token comes from:
#   git.razzfazz.ai → the razzfazz-ai-service-stack repo (or org) → Settings →
#   Actions → Runners → "Create new runner" → copy the REGISTRATION token.
#
# Env knobs (all optional except RUNNER_TOKEN):
#   RUNNER_TOKEN         (required) Gitea runner registration token
#   GITEA_URL            default https://git.razzfazz.ai
#   RUNNER_NAME          default razzfazz-ci-<hostname>
#   RUNNER_LABELS        default razzfazz-ci:host
#                        host exec (not container) so the api-tier's
#                        docker-in-docker postgres fixtures use the host docker.
#                        `runs-on: razzfazz-ci` in ci.yml matches this label.
#   ACT_RUNNER_VERSION   default 1.0.8  (match the existing org runner / current line)
#   NODE_MAJOR           default 20   (actions/checkout@v4 + upload-artifact need node20)
#   RUNNER_USER          default act-runner   (dedicated service account)
#   RUNNER_HOME          default /opt/act-runner
#   SKIP_SCANNERS=1      skip trivy/grype/syft/nuclei (only needed for the later
#                        security-review / SBOM slices, not for ci.yml)
#   CI_RUNNER_FORCE=1    override the live-box safety guard (do NOT use on a real box)
#   RUNNER_ONLY=1        LIVE-BOX mode: register a runner on a box that already runs
#                        a stack (test-0.91 / demo-0.208 / prod). Skips the docker
#                        install, the daemon.json address-pool write + `docker
#                        restart` (which would BOUNCE the live stack), the scanner
#                        install, and the disposable-box guard. Installs only the
#                        gitea-runner + node + systemd unit. ⚠ Do NOT set
#                        RAZZFAZZ_TEST_FORCE on a RUNNER_ONLY box (except 0.91) —
#                        the test-framework live-guard is what then refuses any
#                        destructive `down -v` there.
#
# ⚠ DISPOSABLE ONLY (default mode). The acceptance/day1 slices run under
# RAZZFAZZ_TEST_FORCE and do `docker compose down -v` — they DESTROY volumes.
# For live boxes use RUNNER_ONLY=1 (and no RAZZFAZZ_TEST_FORCE except on 0.91).
set -euo pipefail

GITEA_URL="${GITEA_URL:-https://git.razzfazz.ai}"
RUNNER_NAME="${RUNNER_NAME:-razzfazz-ci-$(hostname -s)}"
RUNNER_LABELS="${RUNNER_LABELS:-razzfazz-ci:host}"
ACT_RUNNER_VERSION="${ACT_RUNNER_VERSION:-1.0.8}"
NODE_MAJOR="${NODE_MAJOR:-20}"
RUNNER_USER="${RUNNER_USER:-act-runner}"
RUNNER_HOME="${RUNNER_HOME:-/opt/act-runner}"
RUNNER_ONLY="${RUNNER_ONLY:-0}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo). Docker install + systemd unit need it."
[ -n "${RUNNER_TOKEN:-}" ] || die "RUNNER_TOKEN is required (Gitea → Settings → Actions → Runners → Create new runner)."

# --- Safety guard: refuse to turn a live box into a destructive-test runner ----
# RUNNER_ONLY=1 intentionally targets live boxes → skip the guard (the protection
# there is "never set RAZZFAZZ_TEST_FORCE", enforced by the test-framework guard).
if [ "${CI_RUNNER_FORCE:-0}" != "1" ] && [ "$RUNNER_ONLY" != "1" ]; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qiE '^(caddy|postgres|authentik|gpustack)'; then
        die "a live razzfazz stack is running here. This runner runs DESTRUCTIVE tests. Use a disposable VM, RUNNER_ONLY=1 for a live-box runner, or CI_RUNNER_FORCE=1 if you are certain."
    fi
fi
if [ "$RUNNER_ONLY" = "1" ]; then
    command -v docker >/dev/null 2>&1 || die "RUNNER_ONLY=1 expects docker already present on this box."
    printf '\n\033[1;33m⚠ RUNNER_ONLY: live-box runner. NOT touching docker/daemon.json; NOT setting RAZZFAZZ_TEST_FORCE.\033[0m\n'
fi

case "$(uname -m)" in
    x86_64)  ARCH=amd64 ;;
    aarch64) ARCH=arm64 ;;
    *) die "unsupported arch $(uname -m)" ;;
esac

log "Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# gettext-base ships `envsubst`. tests/test-sso-oidc.sh renders Authentik
# blueprints with it, and without the tool that suite reported THREE false
# greens (#1292's class: a missing tool inside `eval … >/dev/null 2>&1` is
# indistinguishable from a passing check). The suite now refuses to start
# instead, so a runner missing this package fails loud rather than lying.
# Not needed on a box: the host never runs envsubst — core/init-authentik.sh
# runs inside the authentik-init container, which bakes gettext (#253).
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git jq unzip gettext-base \
    python3 python3-venv python3-pip build-essential

if [ "$RUNNER_ONLY" != "1" ]; then
log "Docker (Ubuntu docker.io + compose v2 — matches the fleet/appliance install)"
# Parity: razzfazz-ai-box-setup.sh installs exactly `docker.io docker-compose-v2`
# on real boxes, so CI must exercise the same engine + compose plugin the
# customers run — NOT docker-ce from get.docker.com. Ubuntu 24.04 universe
# provides both (BuildKit is built into the engine; `docker compose build`
# needs no separate buildx CLI). Add `docker-buildx` only if a later build
# slice calls the `docker buildx` subcommand directly.
if ! command -v docker >/dev/null 2>&1; then
    apt-get install -y docker.io docker-compose-v2
fi
systemctl enable --now docker

log "Docker default-address-pools (CI runs many parallel ephemeral-postgres compose projects)"
# Each ephemeral-postgres test fixture creates its OWN docker network. Under high
# xdist parallelism (one worker per core) that outruns Docker's ~31-subnet default
# pool → 'all predefined address pools have been fully subnetted' → api-tier tests
# error. A /16 carved into /24s gives 256 networks. Fresh VM has no daemon.json to
# merge; if one exists, hand-merge this key instead of clobbering.
install -d -m755 /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
    cat > /etc/docker/daemon.json <<'JSON'
{
  "default-address-pools": [
    { "base": "10.201.0.0/16", "size": 24 }
  ]
}
JSON
    systemctl restart docker
else
    echo "  /etc/docker/daemon.json exists — add default-address-pools (10.201.0.0/16,/24) manually + restart docker"
fi
else
    log "RUNNER_ONLY: skipping docker install + daemon.json/address-pool + docker restart (live stack must not be bounced)"
fi

# #381: every toolchain install is a version-PINNED release artifact with a
# sha256 verified against a checksum recorded HERE (not fetched alongside the
# artifact) — this host builds the release and produces its SBOM/CVE evidence,
# so `curl <mutable-ref> | bash` hands whoever controls the endpoint (or a
# proxy on the path) self-concealing code execution on the evidence chain.
# Bumping a tool = update version + both arch checksums from the project's
# release checksums file.
fetch_verified() {
    # $1=url $2=sha256 $3=dest-file
    curl -fsSL -o "$3" "$1"
    echo "$2  $3" | sha256sum -c --quiet || {
        echo "FATAL: checksum mismatch for $1 (supply-chain guard, #381)" >&2
        rm -f "$3"
        return 1
    }
}

NODE_VERSION="${NODE_VERSION:-20.20.2}"
case "$ARCH" in
    amd64) NODE_ARCH=x64;   NODE_SHA256=df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d ;;
    arm64) NODE_ARCH=arm64; NODE_SHA256=73093db209e4e9e09dd7d15a47aeaab1b74833830df03efa5f942a1122c5fa71 ;;
esac
log "Node ${NODE_VERSION} pinned dist tarball (JS actions: checkout@v4, upload-artifact@v3)"
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -lt "$NODE_MAJOR" ]; then
    tmp="$(mktemp -d)"
    fetch_verified "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
        "$NODE_SHA256" "$tmp/node.tar.xz"
    tar -xJf "$tmp/node.tar.xz" -C /usr/local --strip-components=1 \
        "node-v${NODE_VERSION}-linux-${NODE_ARCH}/bin" \
        "node-v${NODE_VERSION}-linux-${NODE_ARCH}/lib" \
        "node-v${NODE_VERSION}-linux-${NODE_ARCH}/include"
    rm -rf "$tmp"
fi

if [ "${SKIP_SCANNERS:-0}" != "1" ] && [ "$RUNNER_ONLY" != "1" ]; then
    log "Security scanners (trivy / grype / syft / nuclei) — for security-review + SBOM slices"
    TRIVY_VERSION="${TRIVY_VERSION:-0.74.0}"
    SYFT_VERSION="${SYFT_VERSION:-1.51.0}"
    GRYPE_VERSION="${GRYPE_VERSION:-0.117.0}"
    case "$ARCH" in
        amd64) TRIVY_ARCH=64bit
               TRIVY_SHA256=2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a
               SYFT_SHA256=2a2e837a2c8d59ec9af5472ee22d3b04ee463c4e44476ecf993fd1e5ab6ebc7f
               GRYPE_SHA256=38525dab1e06f162ebaa02f94d82d1f807076b011a44180cf2777edf1a7b9c26 ;;
        arm64) TRIVY_ARCH=ARM64
               TRIVY_SHA256=b94ce1976bbf3c15b514b605ee88be7c6d94a29be2302847ff01cb794d47aad5
               SYFT_SHA256=6c0466811541ea03add5213a60a1562f0851e4c0b0ecfdee1a694a9455285900
               GRYPE_SHA256=935f628bdf9331ffdd946931ea5fdb50045d3970ba52670cbeb44a88f127291b ;;
    esac
    tmp="$(mktemp -d)"
    fetch_verified "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-${TRIVY_ARCH}.tar.gz" \
        "$TRIVY_SHA256" "$tmp/trivy.tar.gz" \
        && tar -xzf "$tmp/trivy.tar.gz" -C "$tmp" trivy \
        && install -m0755 "$tmp/trivy" /usr/local/bin/trivy
    fetch_verified "https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/syft_${SYFT_VERSION}_linux_${ARCH}.tar.gz" \
        "$SYFT_SHA256" "$tmp/syft.tar.gz" \
        && tar -xzf "$tmp/syft.tar.gz" -C "$tmp" syft \
        && install -m0755 "$tmp/syft" /usr/local/bin/syft
    fetch_verified "https://github.com/anchore/grype/releases/download/v${GRYPE_VERSION}/grype_${GRYPE_VERSION}_linux_${ARCH}.tar.gz" \
        "$GRYPE_SHA256" "$tmp/grype.tar.gz" \
        && tar -xzf "$tmp/grype.tar.gz" -C "$tmp" grype \
        && install -m0755 "$tmp/grype" /usr/local/bin/grype
    rm -rf "$tmp"
    # nuclei: pinned release binary (projectdiscovery), verified like the other
    # four. It sat inside this same #381 block with a bare `curl` and no
    # sha256 despite a pinned version, and the trailing `||` also masked an
    # unzip/install failure — so the bootstrap reported "Done." with a scanner
    # that might be absent or half-written.
    # Bumping NUCLEI_VERSION = record both arch sha256 HERE, read once from the
    # release's per-arch checksum manifest on github.com/projectdiscovery/nuclei
    # (recorded in this file, never fetched next to the artifact at install time
    # — that would verify transport, not origin).
    NUCLEI_VERSION="${NUCLEI_VERSION:-3.4.10}"
    NUCLEI_SHA256=""
    case "${NUCLEI_VERSION}:${ARCH}" in
        3.4.10:amd64) NUCLEI_SHA256=234c12cc5288af071abdcd6f854245b6067345556e1235cf96b76725c1004357 ;;
        3.4.10:arm64) NUCLEI_SHA256=d1ed1a5c0df49d8fcd64cab4ff5840b793d6bf133b082bb6a7f67d5fc0f9c327 ;;
    esac
    if [ -z "$NUCLEI_SHA256" ]; then
        # Refuse rather than install unverified. nuclei is optional (the
        # security-review skill runs containerized scanners), so this is a loud
        # skip, not a fatal — but it is never a silent unverified install.
        echo "WARN: no pinned sha256 for nuclei ${NUCLEI_VERSION}/${ARCH} — REFUSING to install it unverified (#381). Record both arch sha256 in this script from the upstream release's checksum manifest before bumping."
    else
        tmp="$(mktemp -d)"
        if fetch_verified \
                "https://github.com/projectdiscovery/nuclei/releases/download/v${NUCLEI_VERSION}/nuclei_${NUCLEI_VERSION}_linux_${ARCH}.zip" \
                "$NUCLEI_SHA256" "$tmp/nuclei.zip" \
           && unzip -o "$tmp/nuclei.zip" -d "$tmp" >/dev/null \
           && install -m0755 "$tmp/nuclei" /usr/local/bin/nuclei; then
            log "  nuclei ${NUCLEI_VERSION} installed (checksum verified)"
        else
            echo "WARN: nuclei install FAILED (fetch, checksum, unzip or install) — /usr/local/bin/nuclei may be absent. Install manually if the security slice needs it."
        fi
        rm -rf "$tmp"
    fi
fi

log "gitea-runner ${ACT_RUNNER_VERSION} (act_runner was renamed gitea-runner at 1.0)"
# Distribution path + asset name changed at 1.0:
#   1.0+ : dl.gitea.com/gitea-runner/<v>/gitea-runner-<v>-linux-<arch>
#   0.2.x: dl.gitea.com/act_runner/<v>/act_runner-<v>-linux-<arch>
# Installed to /usr/local/bin/act_runner regardless (the systemd unit + register
# call that path); the binary works under any filename.
case "$ACT_RUNNER_VERSION" in
    0.*) RUNNER_DL="https://dl.gitea.com/act_runner/${ACT_RUNNER_VERSION}/act_runner-${ACT_RUNNER_VERSION}-linux-${ARCH}" ;;
    *)   RUNNER_DL="https://dl.gitea.com/gitea-runner/${ACT_RUNNER_VERSION}/gitea-runner-${ACT_RUNNER_VERSION}-linux-${ARCH}" ;;
esac
# OPS-4 / #381: verify the runner daemon against a checksum PINNED here, exactly
# like node/trivy/syft/grype above — this file previously curl'd it with no
# verification while enforcing the policy on every other tool, yet the runner runs
# root-equivalent on prod (RUNNER_ONLY=1) and produces the SBOM/CVE evidence.
# Bumping ACT_RUNNER_VERSION = update both arch checksums from
# https://dl.gitea.com/gitea-runner/<version>/gitea-runner-<version>-linux-<arch>.sha256
case "${ACT_RUNNER_VERSION}:${ARCH}" in
    1.0.8:amd64) RUNNER_SHA256=027d726127bb67e191d57052fdb66e74ec7f76966f790a18727147fa2b8005e5 ;;
    1.0.8:arm64) RUNNER_SHA256=a86ab4412be5fe5c3d86d132c1142f4b89e1b0ec80ec73b1a48a6ebf93a7023b ;;
    *) echo "FATAL: no pinned checksum for gitea-runner ${ACT_RUNNER_VERSION}/${ARCH} (supply-chain guard, #381/OPS-4). Record both arch sha256 from dl.gitea.com/gitea-runner/${ACT_RUNNER_VERSION}/*.sha256 before bumping." >&2; exit 1 ;;
esac
tmp="$(mktemp -d)"
fetch_verified "$RUNNER_DL" "$RUNNER_SHA256" "$tmp/act_runner"
install -m0755 "$tmp/act_runner" /usr/local/bin/act_runner
rm -rf "$tmp"

log "Service account + working dir"
id -u "$RUNNER_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$RUNNER_USER"
# Add to the docker group so the runner can use the host docker. Non-fatal:
# snap-docker installs have no `docker` group (socket is root-owned under
# /var/snap/docker), and some runners (orchestrator: publish + Gitea-API dispatch)
# don't need docker at all. If a docker-using job later targets such a runner,
# wire snap-docker socket access separately.
if getent group docker >/dev/null 2>&1; then
    usermod -aG docker "$RUNNER_USER"
else
    echo "  (no 'docker' group — snap docker or docker-less box; runner user not added to docker group)"
fi
mkdir -p "$RUNNER_HOME"
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_HOME"

log "Register runner '$RUNNER_NAME' with labels '$RUNNER_LABELS' at $GITEA_URL"
# The registration token arrives on STDIN and the rest as POSITIONAL args —
# never single-quote interpolation into a `bash -c` string:
#   * `--token '$RUNNER_TOKEN'` put the secret in the sudo/bash command line,
#     i.e. /proc/<pid>/cmdline, readable by any local user;
#   * a value containing `'` closed the quote and executed as the runner user
#     (same for $RUNNER_HOME / $RUNNER_NAME / $RUNNER_LABELS).
# `_` is the placeholder for $0, so the arguments start at "$1".
# Residual, and deliberately not papered over: act_runner takes the token as a
# CLI FLAG, so it is visible in *that* process's own cmdline for the length of
# the register call. This removes the two wrapper processes that had no reason
# to carry it at all.
printf '%s' "$RUNNER_TOKEN" | sudo -u "$RUNNER_USER" bash -c '
    IFS= read -r _tok || true
    cd "$1" || exit 1
    act_runner register --no-interactive \
        --instance "$2" --token "$_tok" \
        --name "$3" --labels "$4"
' _ "$RUNNER_HOME" "$GITEA_URL" "$RUNNER_NAME" "$RUNNER_LABELS"

log "systemd unit act-runner.service"
# Resolve the docker systemd unit so we don't hard-Require a missing one:
# apt docker.io = docker.service; snap docker = snap.docker.dockerd.service;
# docker-less (orchestrator publish/dispatch runner) = no docker dep at all.
DOCKER_UNIT=""
if systemctl cat docker.service >/dev/null 2>&1; then DOCKER_UNIT="docker.service"
elif systemctl cat snap.docker.dockerd.service >/dev/null 2>&1; then DOCKER_UNIT="snap.docker.dockerd.service"; fi
if [ -n "$DOCKER_UNIT" ]; then
    UNIT_DEPS="After=$DOCKER_UNIT
Requires=$DOCKER_UNIT"
else
    UNIT_DEPS="After=network-online.target
Wants=network-online.target"
fi
cat >/etc/systemd/system/act-runner.service <<EOF
[Unit]
Description=Gitea act_runner (${RUNNER_LABELS})
$UNIT_DEPS

[Service]
User=$RUNNER_USER
WorkingDirectory=$RUNNER_HOME
ExecStart=/usr/local/bin/act_runner daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now act-runner.service

log "Done."
cat <<EOF

Runner '$RUNNER_NAME' is registered and running.
Verify:
  - git.razzfazz.ai → Settings → Actions → Runners → '$RUNNER_NAME' shows Idle/Online
  - systemctl status act-runner.service
  - push a commit to a feat/** or 2026.*-integration branch → CI (unit + api) runs

Reminder: this VM is DISPOSABLE. Do not put anything on it you can't lose.
EOF
