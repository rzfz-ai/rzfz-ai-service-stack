#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
set -euo pipefail
# scripts/install-wazuh-agent.sh — installs and registers wazuh-agent on this
# box against a Wazuh manager (Profile: wazuh, #855 W2).
#
# Usage:
#   WAZUH_AUTHD_PASSWORD=... sudo -E scripts/install-wazuh-agent.sh <MANAGER_IP> [options]
#   sudo scripts/install-wazuh-agent.sh <MANAGER_IP> --password-file /path/to/secret [options]
#   printf '%s' "$secret" | sudo scripts/install-wazuh-agent.sh <MANAGER_IP> [options]
#
#   <MANAGER_IP>            the manager's WAZUH_MANAGER_HOST_BIND address
#
# Options:
#   --password-file PATH    read the enrolment secret from PATH (mode 0600
#                           recommended). Beats the env var and stdin.
#   --containers LIST       comma-separated container NAMES whose stdout should
#                           be shipped. Default: empty (no container logs).
#                           DELIBERATELY not a glob — see DECISION-11: whoever
#                           can read the Wazuh dashboard can read the stdout of
#                           every container named here, and container stdout
#                           routinely contains tokens.
#   --agent-name NAME       defaults to $(hostname)
#   --stack-root PATH       the stack checkout whose config/, core/ and
#                           compose.yml the FIM block should watch. Defaults to
#                           this script's parent directory, VALIDATED (#1933):
#                           an absolute path, not `/`, holding compose.yml. A
#                           value given here that fails that is an ERROR; a
#                           DERIVED one that fails it drops the stack half of
#                           the FIM block (with a warning) so a host without a
#                           checkout still gets an agent.
#   --deb PATH              install this local wazuh-agent .deb instead of
#                           adding the apt repo (air-gapped boxes; see below)
#   --group NAME            enrol into this Wazuh group (linux-servers,
#                           llm-workers, ci-runners, macos-workstations, or one
#                           of your own). The group must already exist on the
#                           manager (scripts/wazuh-fleet-groups.sh --ensure);
#                           authd refuses enrolment into an unknown group.
#                           Applies at enrolment only; move an enrolled agent
#                           with scripts/wazuh-fleet-groups.sh --assign.
#   -h | --help             this text
#
# THE ENROLMENT SECRET IS NEVER AN ARGUMENT (#855 rev-B, review MEDIUM 12).
# It used to be `$2`, which puts it in /proc/<pid>/cmdline for every local user
# for the whole apt run, and in root's shell history. It now comes from, in
# order: --password-file, the WAZUH_AUTHD_PASSWORD environment variable, or
# stdin. Passing it positionally is refused with a pointer to this block rather
# than silently reinterpreted as a container list.
#
# VERSION PINNING (#855 rev-B, review MEDIUM 13). The agent is pinned to
# WAZUH_AGENT_VERSION (default: the manager's WAZUH_VERSION) and then
# `apt-mark hold`-ed. The `4.x stable` repo installs LATEST, and Wazuh does not
# support an agent NEWER than its manager — so on the day 4.15 is uploaded,
# every freshly installed agent would outrun a manager pinned at 4.14.10 and
# stop reporting. `apt-mark hold` keeps `apt-get upgrade` from doing the same
# thing later, unattended.
#
# AIR-GAPPED BOXES: fetch the .deb once on a connected box —
#   curl -fLO "https://packages.wazuh.com/4.x/apt/pool/main/w/wazuh-agent/wazuh-agent_${WAZUH_VERSION}-1_amd64.deb"
# — copy it over, and run this script with `--deb /path/to/wazuh-agent_*.deb`.
# No apt repo is added and no network is touched in that mode.
#
# RE-RUN AFTER EVERY STACK RECREATE (#855 rev-B, review MEDIUM 14). Docker's
# json-file driver writes /var/lib/docker/containers/<ID>/<ID>-json.log, and the
# ID changes on every `up --force-recreate` / upgrade, so the container-log
# paths written here go stale. This script is idempotent AND self-correcting:
# it PRUNES the blocks it previously wrote before writing the current ones, so
# a re-run repairs the paths instead of appending a second, growing set of dead
# ones. Re-run it after every stack upgrade on every monitored box.
#
# Covers the three telemetry sources #855 W2 asks for:
#   1. journald — host system logs
#   2. container logs — only the allow-listed ones
#   3. FIM (syscheck) — /etc and the stack root, with report_changes OFF on the
#      stack dir because .env lives there
#
# HOST-level install (systemd package), not a compose service: it runs on every
# box that should be monitored, including boxes that never enable the `wazuh`
# profile themselves.

usage() {
    # Prints the "# Usage:" … "# -h | --help" header block, comment marks
    # stripped. Keyed on the two anchor lines rather than on line numbers so
    # editing the header above cannot silently truncate --help.
    sed -n '/^# Usage:/,/^#   -h | --help/p' "${BASH_SOURCE[0]}" \
        | sed 's/^# \{0,1\}//'
}

# NOTE: no apostrophe in a "${var:?word}" message. Inside "${var:?word}" bash
# still treats a single quote as opening a quoted string, so the plan's
# "manager box's .env" made the whole file unparseable (bash -n: unexpected EOF
# looking for `'').
# --help before anything else, or `$0 --help` is read as a manager address and
# then blocks reading a secret from stdin.
case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

WAZUH_MANAGER_IP="${1:?Usage: $0 <MANAGER_IP> [--password-file F] [--containers LIST] [--agent-name N] [--stack-root P] [--deb PATH] [--group NAME]}"
shift

CONTAINER_ALLOWLIST=""
AGENT_NAME=""
PASSWORD_FILE=""
AGENT_DEB=""
AGENT_GROUP=""
GROUP_FLAG_SEEN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --password-file) PASSWORD_FILE="${2:?--password-file needs a path}"; shift 2 ;;
        --containers)    CONTAINER_ALLOWLIST="${2:?--containers needs a value}"; shift 2 ;;
        --agent-name)    AGENT_NAME="${2:?--agent-name needs a value}"; shift 2 ;;
        --stack-root)    STACK_ROOT="${2:?--stack-root needs a value}"; shift 2 ;;
        --deb)           AGENT_DEB="${2:?--deb needs a path}"; shift 2 ;;
        --group)
            # `${2-}`, not `${2:?}`: an EMPTY name is refused below with the
            # group message, and a missing one here - both exit 2.
            [ $# -ge 2 ] || { echo "[install-wazuh-agent] ERROR: --group needs a name" >&2; exit 2; }
            GROUP_FLAG_SEEN=1; AGENT_GROUP="$2"; shift 2 ;;
        -h|--help)       usage; exit 0 ;;
        -*)
            echo "[install-wazuh-agent] ERROR: unknown option $1" >&2
            usage >&2
            exit 2 ;;
        *)
            echo "[install-wazuh-agent] ERROR: unexpected positional argument '$1'." >&2
            echo "  The enrolment secret is NO LONGER an argument (#855 rev-B): on argv it" >&2
            echo "  is readable in /proc/<pid>/cmdline by every local user for the whole" >&2
            echo "  install, and it lands in root history. Pass it as WAZUH_AUTHD_PASSWORD" >&2
            echo "  in the environment, via --password-file, or on stdin. Container names" >&2
            echo "  now go in --containers, the agent name in --agent-name." >&2
            exit 2 ;;
    esac
done

# #2526: a mistyped group enrols the agent into `default` (or not at all),
# where none of the group's FIM/SCA policy applies - and nothing says so.
# Refuse it HERE, before the secret is read or a package touched. `--group ""`
# is refused too: an empty name would silently mean "no group".
if [ "$GROUP_FLAG_SEEN" = 1 ]; then
    if ! [[ "$AGENT_GROUP" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || [ "$AGENT_GROUP" = "." ] || [ "$AGENT_GROUP" = ".." ]; then
        echo "[install-wazuh-agent] ERROR: invalid --group '$AGENT_GROUP' (letters, digits, _ . - ; 1-64 chars; not . or ..)" >&2
        exit 2
    fi
fi

AGENT_NAME="${AGENT_NAME:-$(hostname)}"

# ── STACK_ROOT is DERIVED, so it gets validated, never trusted (#1933) ─────
#
# Measured on a test box (2026-09-10) in the block this script had written on
# 2026-09-08: `<directories …>//config</directories>`, `//core`,
# `//compose.yml`, `<nodiff>//.env</nodiff>`. None of those paths exists. The
# derivation had produced `/`, and `f"{stack_root}/config"` turned that into
# `//config`. The double slash is the signature.
#
# `${STACK_ROOT:-…}` catches empty and unset. It does NOT catch a value that is
# wrong, and `/` is wrong in the one way that leaves no symptom: the block is
# written, the agent starts, wazuh watches nothing, and nobody sees a warning.
#
# The two consequences are not equal. The stack directory going unwatched is a
# missing capability. The `nodiff` lines missing their target is a latent
# SECRET leak: `nodiff` exists (DECISION-11) so that a change to `.env` is
# reported WITHOUT its contents. Anyone repairing the `directories` lines and
# not the `nodiff` lines ships secret diffs into the index. So the two halves
# are written together or not at all — never one of them.
STACK_ROOT_EXPLICIT=0
[ -n "${STACK_ROOT:-}" ] && STACK_ROOT_EXPLICIT=1
if [ "$STACK_ROOT_EXPLICIT" -eq 0 ]; then
    # `|| true`: an unreadable or deleted cwd makes the subshell fail, and
    # under `set -e` that would abort the whole install for a value we are
    # about to validate anyway.
    STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)" || true
fi

# A stack root is an ABSOLUTE path, is not `/`, and holds compose.yml. The
# compose.yml test is what makes this a check on the thing itself rather than
# on the shape of the string: `/opt/wrong` is absolute and not `/`, and it is
# still not a stack root.
stack_root_is_valid() {
    [ -n "${1:-}" ] || return 1
    case "$1" in /) return 1 ;; /*) ;; *) return 1 ;; esac
    [ -f "$1/compose.yml" ]
}

if ! stack_root_is_valid "$STACK_ROOT"; then
    if [ "$STACK_ROOT_EXPLICIT" -eq 1 ]; then
        # The operator said something, and it was wrong. Refusing is the only
        # honest answer: continuing would monitor a directory they did not name.
        echo "[install-wazuh-agent] ERROR: STACK_ROOT='$STACK_ROOT' is not a stack root." >&2
        echo "  It must be an absolute path (not '/') containing compose.yml." >&2
        echo "  Writing the FIM block with it would monitor paths that do not exist" >&2
        echo "  and would leave the nodiff exemptions for .env pointing nowhere (#1933)." >&2
        exit 2
    fi
    # DERIVED and not a stack root: this host may legitimately have no repo —
    # a thin inference node (#1932), or enrolment driven from post-install
    # (#1874). Aborting the whole installer there would trade a working agent
    # for a FIM half that has nothing to watch on this host. So: drop the
    # stack half entirely, say so loudly, and install everything else.
    echo "[install-wazuh-agent] WARNING: no stack root found (derived '$STACK_ROOT')." >&2
    echo "  The FIM block will cover /etc ONLY. config/, core/ and compose.yml are" >&2
    echo "  NOT monitored on this host, and no nodiff exemption is written." >&2
    echo "  If this box does have a stack checkout, re-run with" >&2
    echo "  STACK_ROOT=/path/to/razzfazz-ai-service-stack (or --stack-root PATH)." >&2
    STACK_ROOT=""
fi
# Default to the manager's pin so the two cannot silently diverge.
WAZUH_AGENT_VERSION="${WAZUH_AGENT_VERSION:-${WAZUH_VERSION:-4.14.10}}"

# ── Enrolment secret: file > env > stdin. Never argv. ──────────────────────
if [ -n "$PASSWORD_FILE" ]; then
    [ -r "$PASSWORD_FILE" ] || {
        echo "[install-wazuh-agent] ERROR: cannot read --password-file $PASSWORD_FILE" >&2
        exit 1
    }
    # Strip a trailing newline only; the secret itself is taken verbatim.
    WAZUH_AUTHD_PASSWORD="$(head -c 4096 -- "$PASSWORD_FILE" | sed -e '$a\' | head -n 1)"
elif [ -n "${WAZUH_AUTHD_PASSWORD:-}" ]; then
    : # already in the environment
elif [ ! -t 0 ]; then
    IFS= read -r WAZUH_AUTHD_PASSWORD || true
else
    echo "[install-wazuh-agent] ERROR: no enrolment secret." >&2
    echo "  The manager runs <auth><use_password>yes</use_password></auth>, so an" >&2
    echo "  agent without the secret cannot enrol - which is the point (WZ-4)." >&2
    echo "  Provide it as WAZUH_AUTHD_PASSWORD in the environment, with" >&2
    echo "  --password-file, or on stdin. NEVER as a command-line argument." >&2
    exit 1
fi
[ -n "${WAZUH_AUTHD_PASSWORD:-}" ] || {
    echo "[install-wazuh-agent] ERROR: the enrolment secret resolved EMPTY." >&2
    exit 1
}

echo "[install-wazuh-agent] manager=${WAZUH_MANAGER_IP} agent=${AGENT_NAME} group=${AGENT_GROUP:-<none>} version=${WAZUH_AGENT_VERSION} containers=[${CONTAINER_ALLOWLIST}]"

if [ "$(id -u)" -ne 0 ]; then
    echo "[install-wazuh-agent] ERROR: must run as root (systemd package install)." >&2
    exit 1
fi

if ! command -v dpkg >/dev/null 2>&1; then
    echo "[install-wazuh-agent] ERROR: this installer targets Debian/Ubuntu (dpkg not found)." >&2
    exit 1
fi

if [ -n "$AGENT_DEB" ]; then
    # Air-gap path: no repo, no network.
    [ -r "$AGENT_DEB" ] || {
        echo "[install-wazuh-agent] ERROR: cannot read --deb $AGENT_DEB" >&2
        exit 1
    }
    # WAZUH_AGENT_GROUP (#2526): the package applies it on FIRST install only;
    # the ossec.conf editor below sets the group on every run.
    # --force-hold: a re-run after a manager upgrade moves an agent that an
    # earlier run held (see `apt-mark hold` below) to the new pin.
    WAZUH_MANAGER="${WAZUH_MANAGER_IP}" WAZUH_AGENT_NAME="${AGENT_NAME}" WAZUH_AGENT_GROUP="${AGENT_GROUP}" \
        dpkg -i --force-hold -- "$AGENT_DEB"
else
    # Wazuh's official apt repo + signing key.
    #
    # DEARMOR, do not save verbatim (#1791). `GPG-KEY-WAZUH` is ASCII-ARMOURED —
    # it begins `-----BEGIN PGP PUBLIC KEY BLOCK-----`. Writing that straight to
    # a `.gpg` path produced a file apt refuses to read, and the agent could not
    # be installed on ANY current-Ubuntu box. Measured on a test box (Ubuntu 26.04,
    # 2026-09-08):
    #
    #   W: The key(s) in the keyring /usr/share/keyrings/wazuh.gpg are ignored
    #      as the file has an unsupported filetype.
    #   E: The repository '…/4.x/apt stable InRelease' is not signed.
    #   -> exit 100, nothing installed
    #
    # A `.gpg` keyring must be BINARY; armoured keys belong in a `.asc`. Piping
    # through `gpg --dearmor` gives the binary form and is a no-op on input that
    # is already binary, so it keeps working if upstream ever switches.
    #
    # The guard reads the KEYRING, not just the list file. It used to test only
    # `wazuh.list`, which this failure creates before it aborts — so every retry
    # skipped the key step and failed again, further from the cause. "Idempotent"
    # has to mean "heals a half-finished state", not "does nothing the second
    # time".
    # An ARMOURED file left by the previous version of this script is 3141 bytes
    # and therefore passes a mere "-s" test while apt still refuses it. Every box
    # that already ran the old script carries exactly that file, so the repair
    # has to recognise it: a binary keyring never contains the armour header.
    if [ ! -s /usr/share/keyrings/wazuh.gpg ] || \
       grep -qs "BEGIN PGP PUBLIC KEY BLOCK" /usr/share/keyrings/wazuh.gpg || \
       [ ! -f /etc/apt/sources.list.d/wazuh.list ]; then
        command -v gpg >/dev/null 2>&1 || {
            echo "[install-wazuh-agent] ERROR: gpg is required to dearmor the Wazuh signing key (apt install gnupg)." >&2
            exit 1
        }
        curl -fsS https://packages.wazuh.com/key/GPG-KEY-WAZUH \
            | gpg --batch --yes --dearmor -o /usr/share/keyrings/wazuh.gpg
        chmod 0644 /usr/share/keyrings/wazuh.gpg
        echo "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main" \
            > /etc/apt/sources.list.d/wazuh.list
        apt-get update -qq
    fi

    # PINNED, not latest (MEDIUM 13). `4.x stable` tracks the newest release,
    # and Wazuh does not support agent > manager.
    #
    # MOVING THE PIN. After a manager upgrade this script is re-run to bring
    # the agent to the manager's new version. Two things stopped that before:
    # the repo block above skips `apt-get update` when the repo is already set
    # up, so apt did not know the new version yet ("Version ... was not
    # found"); and the agent is held, so apt refused to change it. Refresh the
    # index whenever the installed agent is not already on the pin, and allow
    # apt to move the held package to exactly that pinned version.
    INSTALLED_AGENT_VERSION="$(dpkg-query -W -f='${Version}' wazuh-agent 2>/dev/null || true)"
    case "$INSTALLED_AGENT_VERSION" in
        "${WAZUH_AGENT_VERSION}"-*) ;;
        *) apt-get update -qq ;;
    esac
    WAZUH_MANAGER="${WAZUH_MANAGER_IP}" WAZUH_AGENT_NAME="${AGENT_NAME}" WAZUH_AGENT_GROUP="${AGENT_GROUP}" \
        apt-get install -y --allow-change-held-packages "wazuh-agent=${WAZUH_AGENT_VERSION}-*"
fi

# Keep an unattended `apt-get upgrade` from walking the agent past the manager.
apt-mark hold wazuh-agent

# #1984: DockerListener ships INSIDE the agent (/var/ossec/wodles/docker/) but
# runs under `#!/usr/bin/env python3` and imports the Docker SDK, which the
# wazuh-agent package does not depend on. Measured on a test box (2026-09-12): the
# wodle starts, logs "Starting to listening Docker events", and the module then
# produces nothing — the import failure is not surfaced as an agent error. So
# the dependency is installed here, where its absence is still visible, rather
# than discovered as an empty dashboard weeks later.
if ! python3 -c 'import docker' >/dev/null 2>&1; then
    apt-get install -y python3-docker ||         echo "[install-wazuh-agent] WARN: python3-docker missing — docker-listener will collect nothing"
fi

OSSEC_CONF=/var/ossec/etc/ossec.conf

# ── Enrollment secret (WZ-4) ───────────────────────────────────────────────
# The manager runs <auth><use_password>yes</use_password></auth> (Task 9), so
# an agent without this file cannot enrol — which is the point.
umask 027
printf '%s' "$WAZUH_AUTHD_PASSWORD" > /var/ossec/etc/authd.pass
chown root:wazuh /var/ossec/etc/authd.pass
chmod 0640 /var/ossec/etc/authd.pass

# ── ossec.conf edits, all idempotent ───────────────────────────────────────
python3 - "$OSSEC_CONF" "$STACK_ROOT" "$CONTAINER_ALLOWLIST" "$AGENT_GROUP" <<'PYEOF'
import os
import re
import subprocess
import sys

path, stack_root, allowlist = sys.argv[1], sys.argv[2], sys.argv[3]
group = sys.argv[4] if len(sys.argv) > 4 else ""

# Normalised HERE, at the point where paths are composed, and not only in the
# caller's validation (#1933). Two reasons, both measured:
#   * a tab-completed `--stack-root /srv/stack/` passes every validity check —
#     it is absolute, it is not `/`, it holds compose.yml — and still produced
#     `/srv/stack//config`. Same dead-path signature, different cause.
#   * `/` collapses to the empty string, so the root that caused this issue
#     cannot compose a path even if it reaches this far. The generator does not
#     have to trust that its caller validated anything.
stack_root = stack_root.rstrip("/")
with open(path) as f:
    text = f.read()

#: Everything this script writes sits between these markers, so a re-run can
#: PRUNE its own previous output instead of appending to it (#855 rev-B,
#: review MEDIUM 14). Without the prune, `up --force-recreate` -> new container
#: IDs -> a re-run left the DEAD localfile paths in place and added the live
#: ones next to them, growing the file on every upgrade and leaving wazuh
#: complaining about files that no longer exist.
BEGIN = "  <!-- BEGIN razzfazz.ai #855 managed block - regenerated by scripts/install-wazuh-agent.sh -->"
END = "  <!-- END razzfazz.ai #855 managed block -->"

# Prune a previous run's block, whatever it contained.
pruned = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "",
                text, flags=re.S)
had_block = pruned != text
text = pruned

blocks = []

# 1. journald — host system logs.
if "<location>journald</location>" not in text:
    blocks.append("""
  <localfile>
    <log_format>journald</log_format>
    <location>journald</location>
  </localfile>
""")

# 2. Container logs — ONLY the allow-listed containers (DECISION-11). Docker's
#    json-file driver writes /var/lib/docker/containers/<id>/<id>-json.log, so
#    each name is resolved to its id at install time. A container that does not
#    exist yet is skipped with a warning rather than silently widening the set.
#
#    These are the paths that go stale on recreate; they live inside the
#    managed block above precisely so the next run replaces them.
#
#    `--type=container` and the id check are both load-bearing (#1788). A bare
#    `docker inspect NAME` resolves across OBJECT TYPES: containers first, then
#    images. On a box where the container is stopped or gone but its image is
#    still pulled - the normal state after a profile is disabled - the bare form
#    succeeds and returns the IMAGE id (`sha256:...`), and we would write
#    /var/lib/docker/containers/sha256:.../sha256:...-json.log: a path that
#    never exists, that wazuh then complains about on every read, and that no
#    re-run cleans up because it looks like a perfectly good entry. Anything
#    that is not a 64-hex container id is refused the same way a missing
#    container is - including an empty answer, which would otherwise produce
#    the `containers//-json.log` double slash.
CID_RE = re.compile(r"^[0-9a-f]{64}$")
# 2b. #1984: container EVENTS. The per-container log paths below carry
#     application output; they say nothing about a container STARTING, STOPPING
#     or being exec'd into, and the Docker dashboard filters on `rule.groups:
#     docker`, which ONLY this module produces. Without it that dashboard is
#     permanently empty on a box whose logs are flowing fine — measured on a test box,
#     2026-09-12: enabling it turned 0 into 146 docker-group alerts, including
#     `exec_start` for every container the operator entered.
#
#     The manager already ships the rules (0455-docker_rules.xml,
#     0560-docker_integration_rules.xml); only the producing side was missing.
#     DockerListener ships with the agent but needs the Python docker SDK, which
#     the agent package does not pull in — install-time dependency, checked
#     below rather than assumed.
if "docker-listener" not in text:
    blocks.append("""
  <wodle name="docker-listener">
    <disabled>no</disabled>
    <interval>10m</interval>
    <attempts>5</attempts>
    <run_on_start>yes</run_on_start>
  </wodle>
""")

#: #2526: the name is written into an XML comment and a <label>, so it is held
#: to Docker's own name alphabet, minus `--` (it ends an XML comment). `<`, `&`
#: or a space can then never reach ossec.conf.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
for name in [n.strip() for n in allowlist.split(",") if n.strip()]:
    if not NAME_RE.match(name) or "--" in name:
        print(f"[install-wazuh-agent] WARN: {name!r} is not a plain container name - skipped")
        continue
    try:
        cid = subprocess.check_output(
            ["docker", "inspect", "--type=container", "-f", "{{.Id}}", name],
            text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        print(f"[install-wazuh-agent] WARN: container {name!r} not found - skipped")
        continue
    if not CID_RE.match(cid):
        print(f"[install-wazuh-agent] WARN: {name!r} did not resolve to a "
              f"container id ({cid!r}) - skipped")
        continue
    loc = f"/var/lib/docker/containers/{cid}/{cid}-json.log"
    if loc not in text:
        # #1984: the NAME goes in beside the id. The id is what Docker's
        # json-file driver needs, but it changes on every recreate, and a path
        # pointing at a container that no longer exists collects nothing while
        # the agent still reports healthy. Recording the name makes a stale
        # entry diagnosable from the file alone instead of requiring a
        # `docker inspect` per id to find out which service went silent.
        #
        # #2526: rules cannot key on a comment, so the name also rides on every
        # event as the `rzfz.container` field (<label> works for json logs only,
        # which these are). The manager's rzfz_00_stack_rules.xml keys on it.
        blocks.append(f"""
  <!-- {name} (container id resolved at install time; re-run this script after a
       recreate, or this path silently collects nothing) -->
  <localfile>
    <log_format>json</log_format>
    <location>{loc}</location>
    <label key="rzfz.container">{name}</label>
  </localfile>
""")

# 2c. #2526: Caddy's minimal access log (core/Caddy/Caddyfile, rzfz_access_log):
#     one JSON line per request to a stack site - status, method, host, path,
#     client IP, duration - which the container's stdout does not carry. Same
#     `rzfz.container` label as the container log, so rules 100403/100404 see
#     the sites' 5xx and 401/403. Written only when caddy is allow-listed and the
#     file exists (CADDY_ACCESS_LOG on, caddy started); otherwise re-run this
#     script after enabling it, like after a recreate.
_names = [n.strip() for n in allowlist.split(",") if n.strip()]
if stack_root and "caddy" in _names:
    access_log = f"{stack_root}/data/caddy-access/access.log"
    if os.path.isfile(access_log):
        if access_log not in text:
            blocks.append(f"""
  <!-- caddy minimal access log (#2526) -->
  <localfile>
    <log_format>json</log_format>
    <location>{access_log}</location>
    <label key="rzfz.container">caddy</label>
  </localfile>
""")
    else:
        print(f"[install-wazuh-agent] NOTE: {access_log} not there - the stack sites' access log "
              "is not collected. CADDY_ACCESS_LOG=auto turns it on only with the wazuh profile or "
              "WAZUH_AGENT_AUTO_ENROLL=true; for an agent installed by hand set CADDY_ACCESS_LOG=true "
              "in .env, recreate caddy, then re-run this script")

# 2d. The stack's own audit trails: JSON Lines written by AuditLogger
#     (core/common/razzfazz_common/audit_log.py), one file per container that
#     writes one: razzfazz-config (AUDIT_LOG_PATH, default
#     /var/log/razzfazz/config-audit.log, on the razzfazz-config-audit volume)
#     and mcp-manager (#3433: Microsoft 365 writes, sends and deletes through
#     the MCP gateway; MCP_AUDIT_LOG_PATH, default /var/log/razzfazz/
#     mcp-audit.log, on the mcp-manager-audit volume); the other managers keep
#     their audit rows in their databases. The label is
#     `rzfz.audit` (not `rzfz.container`): these are the stack's own records,
#     keyed on by modules/apps/wazuh/configs/wazuh_manager/ruleset/rules/
#     rzfz_audit_rules.xml. The file lives on a named volume, so the host path
#     is read from the container's mounts at install time and does not change
#     with the container id. AuditLogger rotates by renaming (TimedRotating
#     FileHandler) and re-creating the same name, which the log collector
#     follows; the exact name is listed, never a wildcard that would also
#     re-read the rotated copies. Not tied to the container-log allow-list: it
#     is an audit file, not container stdout. Written only when the file exists.
#     backup-service (#3458) writes one event per backup run (core/backup/
#     run-outcome.sh) into backups/.rzfz-audit/ on the host; the file appears
#     only with the first run after the upgrade, so this installer creates it
#     empty (a regular file, never through a symlink): a box enrolled before
#     its first nightly still ships the failed or refused run.
AUDIT_SOURCES = (
    # (container, default path inside the container, env var that overrides it)
    ("razzfazz-config", "/var/log/razzfazz/config-audit.log", "AUDIT_LOG_PATH"),
    ("mcp-manager", "/var/log/razzfazz/mcp-audit.log", "MCP_AUDIT_LOG_PATH"),
    ("backup-service", "/archive/.rzfz-audit/backup-audit.log", "RZFZ_BACKUP_AUDIT_LOG"),
)
# The sources whose file this installer creates (empty) when it is missing.
AUDIT_CREATE_MISSING = ("backup-service",)


def _create_audit_file(path):
    """An empty audit file at PATH: the directory is created when missing and
    must not be a symlink, the file is opened without following one. False
    when it cannot be made safely."""
    d = os.path.dirname(path)
    try:
        if not os.path.isdir(os.path.dirname(d)):
            return False
        if not os.path.lexists(d):
            os.mkdir(d, 0o750)
        if os.path.islink(d) or not os.path.isdir(d):
            return False
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        os.close(fd)
        return True
    except FileExistsError:
        return os.path.isfile(path) and not os.path.islink(path)
    except OSError:
        return False
HOSTPATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
for cname, default_in, env_key in AUDIT_SOURCES:
    try:
        raw = subprocess.check_output(
            ["docker", "inspect", "--type=container", "-f",
             "{{range .Config.Env}}E {{.}}\n{{end}}{{range .Mounts}}M {{.Destination}}|{{.Source}}\n{{end}}", cname],
            text=True, stderr=subprocess.DEVNULL)
    except Exception:
        print(f"[install-wazuh-agent] NOTE: container {cname!r} not found - its audit log is not collected")
        continue
    in_path, mounts = default_in, []
    for ln in raw.splitlines():
        if ln.startswith(f"E {env_key}=") and ln.split("=", 1)[1].strip():
            in_path = ln.split("=", 1)[1].strip()
        elif ln.startswith("M ") and "|" in ln:
            dest, _, src = ln[2:].partition("|")
            mounts.append((dest.rstrip("/"), src.rstrip("/")))
    host = ""
    # the longest destination that contains the audit file wins
    for dest, src in sorted(mounts, key=lambda m: -len(m[0])):
        if dest and src and in_path.startswith(dest + "/"):
            host = src + in_path[len(dest):]
            break
    if not host or not HOSTPATH_RE.match(host) or "//" in host or "/../" in host:
        print(f"[install-wazuh-agent] NOTE: no host path for {in_path} of {cname} - its audit log is not collected")
        continue
    if not os.path.isfile(host) and cname in AUDIT_CREATE_MISSING and _create_audit_file(host):
        print(f"[install-wazuh-agent] created the empty audit log of {cname}: {host}")
    if not os.path.isfile(host):
        print(f"[install-wazuh-agent] NOTE: {host} not there - the audit log of {cname} is not collected "
              "(start the container, then re-run this script)")
        continue
    if host not in text:
        blocks.append(f"""
  <!-- {cname} audit log (JSON Lines, AuditLogger); rotated by rename, same name -->
  <localfile>
    <log_format>json</log_format>
    <location>{host}</location>
    <label key="rzfz.audit">{cname}</label>
  </localfile>
""")

# 3. FIM. report_changes is ON for /etc (diffs are the value there) and OFF for
#    the stack root, which sits next to .env — we want to know THAT a file
#    changed without shipping its contents into the index (DECISION-11).
#
#    The idempotency marker is a string this block ACTUALLY emits. The plan's
#    version tested for `<directories realtime="yes">{stack_root}`, which the
#    block never writes (every emitted directories tag also carries a
#    report_changes attribute), so the guard could never match and the block
#    would be appended again on every re-run.
#
#    `stack_root` ARRIVES EMPTY when the caller could not establish one (#1933),
#    and then the stack half is omitted whole. The `directories` lines and the
#    `nodiff` lines are one unit: nodiff is what keeps `.env` CONTENTS out of
#    the index, so a config carrying the watch without the exemption is worse
#    than one carrying neither. Nothing here composes a path out of an empty
#    root — that is how `//config` was born.
if stack_root:
    fim_stack = f"""    <directories realtime="yes" report_changes="no">{stack_root}/config</directories>
    <directories realtime="yes" report_changes="no">{stack_root}/core</directories>
    <directories realtime="yes" report_changes="no">{stack_root}/compose.yml</directories>
    <nodiff>{stack_root}/.env</nodiff>
    <nodiff>{stack_root}/.env.dify</nodiff>
    <nodiff>{stack_root}/certs</nodiff>
"""
    # The marker is a string this block actually emits, and it has to differ
    # between the two variants — otherwise a box that was once installed
    # without a stack root would never gain the stack half on a later re-run.
    fim_marker = f"<nodiff>{stack_root}/.env</nodiff>"
else:
    fim_stack = ""
    fim_marker = '<directories realtime="yes" report_changes="yes">/etc</directories>'

if fim_marker not in text:
    blocks.append(f"""
  <syscheck>
    <directories realtime="yes" report_changes="yes">/etc</directories>
{fim_stack}  </syscheck>
""")

# 4. Active response OFF (DECISION-11): manager -> agent remote command
#    execution as root is not a default this stack accepts.
if "<active-response>" not in text:
    blocks.append("""
  <active-response>
    <disabled>yes</disabled>
  </active-response>
""")

# 5. Rootcheck trojan signatures on a uutils box (#1983). Ubuntu 26.04 ships
#    coreutils as ONE Rust multi-call binary under /usr/lib/cargo/bin/coreutils/
#    (/bin/cat, /bin/ls, … are symlinks into it). Rootcheck's stock signatures
#    for ten of those names lead with the bare alternative `bash`, written when
#    coreutils were small C programs that never contained that string; the Rust
#    binary carries every utility's text, `bash` included (measured on a test box:
#    8 occurrences in md5sum, `dpkg -V coreutils` clean). Result: twenty level-7
#    "Trojaned version of file" alerts on day one of every 26.04 box.
#
#    Design decision (2026-09): suppress the ten named binaries, conditional
#    on the box actually shipping uutils, with the reason in the config. Two
#    facts shape HOW (both verified, not assumed):
#      * check_rc_trojans.c consults no ignore list — `<rootcheck><ignore>`
#        cannot reach this check; the ONLY lever is the file <rootkit_trojans>
#        points at.
#      * etc/shared/rootkit_trojans.txt is MANAGER-PUSHED (merged.mg carries
#        it), so an in-place edit is clobbered on the next shared-config sync.
#    So: a SEPARATE file, derived from the shipped one, with the bare `bash`
#    alternative removed from exactly those ten rows — every other alternative
#    (`^/bin/sh`, `file\.h`, `proc\.h`, the `/dev/` classes) stays, so the
#    trojan check on those names survives — and <rootkit_trojans> repointed at
#    it. A GNU-coreutils box keeps the stock file untouched, and a box that
#    stops being uutils gets the stock file back on the next run.
UUTILS_NAMES = ("ls", "env", "echo", "chown", "chmod", "chgrp", "cat", "uname", "date", "md5sum")
UUTILS_DIR = os.environ.get("RZFZ_UUTILS_DIR", "/usr/lib/cargo/bin/coreutils/")   # test seam; the default is 26.04's
ossec_root = os.path.dirname(os.path.dirname(path))              # /var/ossec
shipped = os.path.join(ossec_root, "etc", "shared", "rootkit_trojans.txt")
derived_rel = "etc/rootcheck/rootkit_trojans.uutils.txt"
derived = os.path.join(ossec_root, derived_rel)
probe = os.environ.get("RZFZ_COREUTILS_PROBE", "/bin/cat")       # test seam; the default is the host
is_uutils = os.path.realpath(probe).startswith(UUTILS_DIR)
TROJANS_RE = re.compile(r"<rootkit_trojans>([^<]*)</rootkit_trojans>")

def _narrow(line):
    """Drop the bare `bash` alternative from ONE signature row; keep the rest."""
    m = re.match(r"^(\S+)(\s+)!(.*)!(.*)$", line.rstrip("\n"))
    if not m or m.group(1) not in UUTILS_NAMES:
        return line, False
    alts = [a for a in m.group(3).split("|") if a != "bash"]
    changed = len(alts) != len(m.group(3).split("|"))
    return f"{m.group(1)}{m.group(2)}!{'|'.join(alts)}!{m.group(4)}\n", changed

if is_uutils and os.path.isfile(shipped):
    with open(shipped) as f:
        rows = f.readlines()
    out, narrowed = [], []
    for row in rows:
        new, changed = _narrow(row)
        out.append(new)
        if changed:
            narrowed.append(row.split()[0])
    header = (
        "# GENERATED by scripts/install-wazuh-agent.sh (#1983) - do not edit; re-run the script.\n"
        "# Derived from etc/shared/rootkit_trojans.txt (manager-pushed, left untouched).\n"
        f"# This host ships uutils-coreutils: {probe} -> {os.path.realpath(probe)}\n"
        "# The Rust multi-call binary contains the string 'bash' (every utility's text is in\n"
        "# one file), so the stock signatures' bare 'bash' alternative fires on every\n"
        "# coreutils name - 20 false 'Trojaned version of file' alerts per rootcheck pass.\n"
        f"# The bare 'bash' alternative is removed from these rows ONLY: {', '.join(narrowed)}.\n"
        "# Every other alternative in those rows and every other row is the stock signature.\n"
    )
    os.makedirs(os.path.dirname(derived), exist_ok=True)
    with open(derived, "w") as f:
        f.write(header + "".join(out))
    os.chmod(derived, 0o640)
    if TROJANS_RE.search(text) and derived_rel not in text:
        text = TROJANS_RE.sub(f"<rootkit_trojans>{derived_rel}</rootkit_trojans>", text, count=1)
    print(f"[install-wazuh-agent] uutils-coreutils detected ({os.path.realpath(probe)}): "
          f"rootcheck reads {derived_rel} - bare 'bash' dropped from {len(narrowed)} rows, "
          f"{len(rows)} rows kept (#1983)")
elif is_uutils:
    print(f"[install-wazuh-agent] WARN: uutils-coreutils detected but {shipped} is not there yet "
          "(manager has not pushed shared config?) - rootcheck keeps the stock signatures; re-run after enrolment (#1983)")
elif derived_rel in text:
    # No longer a uutils box: give rootcheck the stock file back.
    text = text.replace(f"<rootkit_trojans>{derived_rel}</rootkit_trojans>",
                        "<rootkit_trojans>etc/shared/rootkit_trojans.txt</rootkit_trojans>", 1)
    print("[install-wazuh-agent] GNU coreutils on this host: rootcheck back on the stock signatures (#1983)")

# #2526: the enrolment group lives in <client><enrollment><groups>, OUTSIDE the
# managed block (the <client> block is the package's). Set, replace, or leave
# alone - never duplicate. It takes effect ONLY when the agent enrols (no key
# yet): an agent that is already enrolled keeps the groups the manager holds,
# whatever this file says. To move an enrolled agent, run on the manager box
#   scripts/wazuh-fleet-groups.sh --assign <agent name> <group>
# Writing it here still matters: a re-enrolment (lost key) lands in the right
# group, and the file says what the box is meant to be.
group_changed = False
if group:
    m = re.search(r"<client>.*?</client>", text, flags=re.S)
    if m:
        client = m.group(0)
        enr = re.search(r"<enrollment>.*?</enrollment>", client, flags=re.S)
        if enr:
            block = enr.group(0)
            if re.search(r"<groups>.*?</groups>", block, flags=re.S):
                new_block = re.sub(r"<groups>.*?</groups>", lambda _m: f"<groups>{group}</groups>",
                                   block, count=1, flags=re.S)
            else:
                new_block = block.replace("<enrollment>", f"<enrollment>\n      <groups>{group}</groups>", 1)
            new_client = client.replace(block, new_block, 1)
        else:
            new_client = client.replace(
                "</client>",
                f"  <enrollment>\n      <enabled>yes</enabled>\n      <groups>{group}</groups>\n    </enrollment>\n  </client>", 1)
        if new_client != client:
            text = text.replace(client, new_client, 1)
            group_changed = True
            print(f"[install-wazuh-agent] enrolment group set to {group!r} (#2526)")
    else:
        print("[install-wazuh-agent] WARN: no <client> block in ossec.conf - the group is set only "
              "by the package's first install (#2526)")

repointed = text != pruned and TROJANS_RE.search(text) and TROJANS_RE.search(text).group(1) != (TROJANS_RE.search(pruned).group(1) if TROJANS_RE.search(pruned) else None)

if blocks:
    body = BEGIN + "".join(blocks) + END + "\n"
    text = text.replace("</ossec_config>", body + "</ossec_config>", 1)
    with open(path, "w") as f:
        f.write(text)
    print(f"[install-wazuh-agent] wrote {len(blocks)} block(s) to {path}"
          + (" (previous managed block replaced)" if had_block else ""))
elif had_block or repointed or group_changed:
    with open(path, "w") as f:
        f.write(text)
    print(f"[install-wazuh-agent] wrote {path}"
          + (" - previous managed block removed, nothing left to configure" if had_block and not blocks else "")
          + (" - rootcheck signature file repointed (#1983)" if repointed else "")
          + (" - enrolment group updated (#2526)" if group_changed else ""))
else:
    print(f"[install-wazuh-agent] {path} already configured - no change")
PYEOF

systemctl daemon-reload
systemctl enable --now wazuh-agent

echo "[install-wazuh-agent] Done."
echo "  Verify enrollment ON THE MANAGER BOX:"
echo "    docker exec wazuh-manager /var/ossec/bin/agent_control -l"
echo "  RE-RUN THIS SCRIPT after every stack upgrade or \`up --force-recreate\`:"
echo "    container IDs change and the shipped log paths go stale (silently)."
