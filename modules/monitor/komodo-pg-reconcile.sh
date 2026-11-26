#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #3309: Komodo's own PostgreSQL cluster (postgres-komodo) gets its own password.
#
# The cluster was initialised with the stack's main superuser password, and
# ferretdb and komodo-core connect with it, so both held the password of the
# MAIN cluster's superuser. The image applies POSTGRES_PASSWORD only when it
# initialises an empty data directory, so an existing cluster keeps the old
# one; this one-shot moves the role to KOMODO_PG_PASSWORD on every start:
#   * the Komodo password already logs in over the network: nothing to do
#     (every start on a migrated box);
#   * otherwise it sets the new one as the cluster's superuser over the
#     cluster's UNIX SOCKET, shared with postgres-komodo through a volume.
#     Local socket logins are trusted in that image (backup and restore use
#     them through `docker exec`), so #3337: this container never receives
#     the old password (the main cluster superuser's) at all. That covers an
#     upgraded box, monitor enabled later and a restored older archive alike;
#   * then the new password is probed over the network (pg_hba enforces
#     scram-sha-256 there; the socket would accept anything) and a failure
#     exits non-zero, so ferretdb never starts against a wrong password.
# The password reaches psql through the environment only: PGPASSWORD for the
# probe, `\getenv` for the ALTER, never argv; statement logging is off for the
# session that sets it.
#
# #3374: clusters initialised before F-018 still end their pg_hba with
# `host all all all trust` (pg_hba is written once, at first init). There the
# network accepts ANY password, so a successful network login proves nothing.
# A deliberately wrong password is tried first, as a control. If it is
# accepted, the password is set over the socket unconditionally (idempotent),
# and every NON-LOOPBACK network (`host*`) rule with method `trust` becomes
# `scram-sha-256` in pg_hba.conf (a copy of the original is kept), so the
# probe means something again. postgres-komodo's only network client is
# ferretdb, which holds the Komodo password; backup, restore and the Komodo
# store helper use the local socket through `docker exec`. Local rules and the
# image's own LOOPBACK rules (127.0.0.0/8, ::1, localhost, samehost) keep
# `trust`: FerretDB's DocumentDB extension opens an internal loopback
# connection without a password (create_indexes_non_concurrently), and
# komodo-core cannot create its indexes without it (aged-box journey on 0.91,
# 2026-11-23).
set -euo pipefail

host="${KOMODO_PG_HOST:-postgres-komodo}"
sock="${KOMODO_PG_SOCKET_DIR:-/var/run/postgresql}"
data="${KOMODO_PG_DATA_DIR:-/var/lib/postgresql/data}"
user="${POSTGRES_USER:-docker}"
: "${KOMODO_PG_PASSWORD:?KOMODO_PG_PASSWORD is empty: rzfz init / upgrade mints it}"

logs_in() {
    PGPASSWORD="$1" psql -X -h "$host" -U "$user" -d postgres -tAc 'SELECT 1' 2>/dev/null | grep -qx 1
}

# Over the local socket (trusted inside Komodo's own cluster).
on_socket() {
    psql -X -h "$sock" -U "$user" -d postgres -tA -v ON_ERROR_STOP=1 "$@"
}

socket_answers() {
    on_socket -c 'SELECT 1' 2>/dev/null | grep -qx 1
}

set_password() {
    on_socket -q <<'SQL'
SET log_statement = 'none';
SET log_min_duration_statement = -1;
\getenv komodo_pw KOMODO_PG_PASSWORD
SELECT format('ALTER ROLE CURRENT_USER PASSWORD %L', :'komodo_pw') \gexec
SQL
}

count_rules() {
    on_socket -c "SELECT count(*) FROM pg_hba_file_rules WHERE $1" 2>/dev/null | tr -d '[:space:]'
}

# Prints pg_hba with every NON-LOOPBACK host* rule's method `trust` replaced
# by scram-sha-256. The method is the field after the address, or after the
# mask when the address is an IP with a separate mask. Comments, blank lines,
# local rules and loopback rules (127.0.0.0/8, ::1, localhost, samehost) pass
# through unchanged.
harden_hba_text() {
    awk '
        function loopback(addr, mask,   slash, ip, len) {
            if (addr == "localhost" || addr == "samehost") return 1
            slash = index(addr, "/")
            if (slash) {
                ip = substr(addr, 1, slash - 1); len = substr(addr, slash + 1) + 0
                if (ip ~ /^127\./ && len >= 8) return 1
                if (ip == "::1" && len == 128) return 1
                return 0
            }
            if (mask != "") {
                if (addr ~ /^127\./ && mask ~ /^255\./) return 1
                if (addr == "::1" && tolower(mask) == "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff") return 1
            }
            return 0
        }
        # pg_hba applies the FIRST matching rule: a converted network rule must never come
        # before loopback trust (DocumentDB connects to host=localhost without a password).
        # Before the first converted rule, explicit loopback trust is inserted for whichever
        # of 127.0.0.1 / ::1 has not had a loopback trust rule yet.
        function ensure_loopback() {
            if (!seen4) { print "host    all   all   127.0.0.1/32   trust   # rzfz #3374: loopback stays trust (FerretDB/DocumentDB)"; seen4 = 1 }
            if (!seen6) { print "host    all   all   ::1/128        trust   # rzfz #3374: loopback stays trust (FerretDB/DocumentDB)"; seen6 = 1 }
        }
        /^[[:space:]]*(#|$)/ { print; next }
        $1 ~ /^host(ssl|nossl|gssenc|nogssenc)?$/ {
            m = 5; mask = ""
            if ($4 !~ /\// && $4 !~ /^(all|samehost|samenet)$/ && $5 ~ /^[0-9a-fA-F.:]+$/ && $5 ~ /[.:]/) { m = 6; mask = $5 }
            if ($m == "trust" && loopback($4, mask)) {
                if ($4 == "localhost" || $4 == "samehost") { seen4 = 1; seen6 = 1 }
                else if ($4 ~ /^127\./) seen4 = 1
                else seen6 = 1
            }
            if ($m == "trust" && !loopback($4, mask)) { ensure_loopback(); $m = "scram-sha-256" }
            print
            next
        }
        { print }
    ' "$1"
}

# The method of the FIRST rule a passwordless loopback TCP connection to the postgres database as
# the cluster user would match (pg_hba is first-match), for 127.0.0.1 ($1 = 4) or ::1 ($1 = 6).
# hostssl/hostgssenc rules are skipped: DocumentDB's localhost connection uses neither.
loopback_first_method() {
    local ip family
    if [ "$1" = 4 ]; then ip='127.0.0.1'; family='^[0-9.]+$'; else ip='::1'; family=':'; fi
    on_socket -v u="$user" -v ip="$ip" -v fam="$family" <<'SQL' 2>/dev/null | tr -d '[:space:]'
SELECT auth_method FROM pg_hba_file_rules
 WHERE error IS NULL AND type IN ('host', 'hostnossl', 'hostnogssenc')
   AND database && ARRAY['all', 'postgres'] AND user_name && ARRAY['all', :'u']
   AND (address IN ('all', 'samehost', 'localhost')
        OR (address ~ :'fam' AND (:'ip'::inet & netmask::inet) = (address::inet & netmask::inet)))
 ORDER BY line_number LIMIT 1;
SQL
}

# The pg_hba_file_rules condition for a rule that a NON-LOOPBACK network login can match with trust.
NETWORK_TRUST="type LIKE 'host%' AND auth_method = 'trust' AND error IS NULL AND NOT (
    address IN ('localhost', 'samehost')
    OR (address ~ '^127[.]' AND netmask ~ '^255[.]')
    OR (address = '::1' AND lower(netmask) = 'ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'))"

# 0 hardened (or nothing to do), 1 failed and rolled back, 2 cannot (no writable pg_hba.conf).
harden_hba() {
    local hba="$data/pg_hba.conf" keep="$data/pg_hba.conf.rzfz-3374.bak" tmp="$data/pg_hba.conf.rzfz-tmp"
    [ -f "$hba" ] && [ -w "$hba" ] && [ -w "$data" ] || return 2
    [ -e "$keep" ] || cp -p "$hba" "$keep"
    harden_hba_text "$hba" > "$tmp" || { rm -f "$tmp"; return 1; }
    chown --reference="$hba" "$tmp" 2>/dev/null || true
    chmod --reference="$hba" "$tmp"
    local before errors_before
    before=$(cat "$hba")
    # A rule Postgres already rejects (e.g. hostssl without SSL) is not ours to judge: the
    # rewrite may add no NEW error, and no rule that can match may still trust a network login.
    errors_before=$(count_rules 'error IS NOT NULL')
    mv -f "$tmp" "$hba"
    if ! on_socket -c 'SELECT pg_reload_conf()' >/dev/null \
       || ! [ "$(count_rules 'error IS NOT NULL')" -le "${errors_before:-0}" ] 2>/dev/null \
       || [ "$(count_rules "$NETWORK_TRUST")" != "0" ] \
       || [ "$(loopback_first_method 4)" != "trust" ] \
       || [ "$(loopback_first_method 6)" != "trust" ]; then
        printf '%s\n' "$before" > "$hba"
        on_socket -c 'SELECT pg_reload_conf()' >/dev/null 2>&1 || true
        return 1
    fi
    return 0
}

control="rzfz-control-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
if logs_in "$control"; then
    echo "komodo-pg-reconcile: the cluster accepts ANY password over the network (a legacy pg_hba trust rule); a network login proves nothing here."
    if ! socket_answers; then
        echo "komodo-pg-reconcile: the cluster's local socket (${sock}) does not answer, so the password cannot be set." >&2
        echo "komodo-pg-reconcile: ferretdb and Komodo stay down until the role's password is repaired." >&2
        exit 1
    fi
    set_password
    echo "komodo-pg-reconcile: the Komodo password is set over the local socket."
    rc=0
    harden_hba || rc=$?
    if [ "$rc" -eq 2 ]; then
        # #3385: Komodo is kept running (the password IS set), so this exits 0;
        # `rzfz status` FAILs on the remaining network trust rule, and the
        # upgrade prints that status at its end.
        echo "komodo-pg-reconcile: WARNING: pg_hba.conf is not writable at ${data}; its network rules still trust every login (host ... trust). The password is set, so ferretdb and Komodo work; the network rule needs hardening by hand — rzfz status reports it until it is fixed." >&2
        exit 0
    fi
    if [ "$rc" -ne 0 ]; then
        echo "komodo-pg-reconcile: hardening pg_hba.conf failed; the original file is back in place." >&2
        exit 1
    fi
    echo "komodo-pg-reconcile: non-loopback network rules now require a password (trust -> scram-sha-256; loopback rules unchanged; the original is kept as pg_hba.conf.rzfz-3374.bak)."
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if logs_in "$KOMODO_PG_PASSWORD" && ! logs_in "$control"; then
            echo "komodo-pg-reconcile: done — the Komodo cluster has its own password, and a wrong one is refused."
            exit 0
        fi
        sleep 0.5
    done
    echo "komodo-pg-reconcile: after hardening, the Komodo password does not log in or a wrong one still does — refusing to let ferretdb start." >&2
    exit 1
fi

if logs_in "$KOMODO_PG_PASSWORD"; then
    echo "komodo-pg-reconcile: the Komodo cluster already uses its own password — ok."
    exit 0
fi

if ! socket_answers; then
    echo "komodo-pg-reconcile: the Komodo password does not log in as ${user} on ${host}, and the cluster's local socket (${sock}) does not answer." >&2
    echo "komodo-pg-reconcile: ferretdb and Komodo stay down until the role's password is repaired." >&2
    exit 1
fi

echo "komodo-pg-reconcile: moving the Komodo cluster to its own password..."
set_password

if ! logs_in "$KOMODO_PG_PASSWORD"; then
    echo "komodo-pg-reconcile: the new password was set but does not log in — refusing to let ferretdb start." >&2
    exit 1
fi
echo "komodo-pg-reconcile: done — the Komodo cluster has its own password."
