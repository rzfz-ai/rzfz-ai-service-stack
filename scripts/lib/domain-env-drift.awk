# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# #2719: judge running containers' environments after a re-domain. POSIX awk
# (mawk). Loaded AFTER scripts/lib/redomain-hosts.awk, whose under(),
# leftover() and host_of() are the one host rule of the re-domain (shared,
# not copied):
#   awk -v mode=drift -v envfile=.env -f scripts/lib/redomain-hosts.awk \
#       -f scripts/lib/domain-env-drift.awk  < inspect-output
# ENVIRON OLD (may be empty) and NEW (empty: the env file's MAIN_DOMAIN).
#
# Input: per container a header "@@/<name>|<service>" then KEY=VALUE lines.
# Prints "<service> <container> <KEY>" per stale value, with a 4th field "env"
# when the container's value equals the env file's own (the env file is the
# cause; recreating cannot help). Values are never printed.
#
# A value is stale when
#   * its key is a domain key the re-domain rewrites (MAIN_DOMAIN, *_DOMAIN
#     except the IdP allow-list *_OAUTH_DOMAIN) and differs from the env
#     file's resolved value, or
#   * OLD is known and the value names a leftover host: for the keys the
#     re-domain rewrites (*_DOMAIN, *_URL, *_ORIGINS) any comma/space token,
#     for every other key only URL-shaped tokens (scheme://host). A user@host
#     token (mail addresses: SMTP_FROM, *_EMAIL) is never matched: the
#     re-domain does not rewrite them, so recreating cannot change them.
# A value holding an unresolved ${...} (compose env_file entries are passed
# literally) is neither old nor new and is skipped.

function dkey(k) { return (k == "MAIN_DOMAIN" || k ~ /_DOMAIN$/) && k !~ /_OAUTH_DOMAIN$/ }
# ALLOWED_SENDER_DOMAINS / HOSTNAME (smtp-relay) hold bare hosts, so every token counts
function rkey(k) { return dkey(k) || k ~ /_URL$/ || k ~ /_ORIGINS$/ || k == "ALLOWED_SENDER_DOMAINS" || k == "HOSTNAME" }
# Keys the compose files DERIVE from MAIN_DOMAIN, judged against the current
# domain even when the old one is unknown (`rzfz status`): the mail relay's
# sender allow-list and the LLM manager's advertise URL / worker registry.
function skey(k) { return k == "ALLOWED_SENDER_DOMAINS" || k == "LLM_MANAGER_ADVERTISE_URL" || k == "LLM_MANAGER_WORKER_REGISTRY" }
function ekey(k) { return k == "ALLOWED_SENDER_DOMAINS" ? "SMTP_ALLOWED_SENDER_DOMAINS" : k }
# 1 when the derived value no longer follows the current domain
function derived_stale(k, v,   c, p, j, h) {
    if (new == "" || ((ekey(k) in raw) && resolve(raw[ekey(k)]) != "")) return 0   # an admin's own value
    c = split(v, p, /[ ,;]+/)
    if (k == "ALLOWED_SENDER_DOMAINS") {
        for (j = 1; j <= c; j++) if (tolower(p[j]) != "" && under(tolower(p[j]), new)) return 0
        return 1
    }
    h = tolower(host_of(v))
    if (h == "" || h !~ /\./ || h ~ /^[0-9.]+$/ || h ~ /:/) return 0   # a bare name, an IP: not a stack host
    return !under(h, new)
}
function envval(v,   q, e) {
    sub(/^[ \t]+/, "", v)
    q = substr(v, 1, 1)
    if (q == "\"" || q == "'") {
        e = index(substr(v, 2), q)
        return e ? substr(v, 2, e - 1) : substr(v, 2)
    }
    sub(/[ \t]+#.*$/, "", v)        # KEY=value # comment
    sub(/[ \t]+$/, "", v)
    return v
}
# ${NAME}, ${NAME:-default} (default when unset or empty), ${NAME-default}
# (default when unset); nested references are resolved up to 16 deep.
function resolve(v,   guard, ref, def, p, rep) {
    for (guard = 0; guard < 16 && match(v, /\$\{[A-Za-z0-9_]+(:?-[^}]*)?\}/); guard++) {
        ref = substr(v, RSTART + 2, RLENGTH - 3)
        if ((p = index(ref, ":-")) > 0) {
            def = substr(ref, p + 2); ref = substr(ref, 1, p - 1)
            rep = ((ref in raw) && raw[ref] != "") ? raw[ref] : def
        } else if ((p = index(ref, "-")) > 0) {
            def = substr(ref, p + 1); ref = substr(ref, 1, p - 1)
            rep = (ref in raw) ? raw[ref] : def
        } else {
            rep = (ref in raw) ? raw[ref] : ""
        }
        v = substr(v, 1, RSTART - 1) rep substr(v, RSTART + RLENGTH)
    }
    return v
}
function token_stale(tok, any_token,   h) {
    if (old == "" || tok ~ /\$\{/) return 0
    if (index(tok, "://") == 0) {
        if (!any_token || index(tok, "@") > 0) return 0
    }
    h = tolower(host_of(tok))
    return h != "" && leftover(h)
}
BEGIN {
    if (mode == "drift") {
        while ((getline line < envfile) > 0) {
            sub(/^[ \t]*export[ \t]+/, "", line)
            if (line ~ /^[ \t]*#/ || line !~ /^[A-Za-z_][A-Za-z0-9_]*=/) continue
            k = substr(line, 1, index(line, "=") - 1)
            raw[k] = envval(substr(line, index(line, "=") + 1))
        }
        close(envfile)
        for (k in raw) if (dkey(k)) want[k] = tolower(resolve(raw[k]))
        if (new == "" && ("MAIN_DOMAIN" in want)) {
            new = want["MAIN_DOMAIN"]
            new_under_old = (old != "" && new != old && under(new, old))
        }
    }
}
mode == "drift" && /^@@/ {
    hdr = substr($0, 3); sub(/^\//, "", hdr)
    cname = substr(hdr, 1, index(hdr, "|") - 1)
    svc = substr(hdr, index(hdr, "|") + 1)
    if (svc == "") svc = cname
    next
}
mode == "drift" && cname != "" && index($0, "=") > 1 {
    k = substr($0, 1, index($0, "=") - 1)
    v = substr($0, index($0, "=") + 1)
    if (k ~ /_OAUTH_DOMAIN$/ || v ~ /\$\{/) next
    stale = 0
    if (dkey(k) && (k in want) && want[k] !~ /\$/ && tolower(v) != want[k]) stale = 1
    envcause = 0
    if (!stale) {
        cnt = split(v, parts, /[ ,;]+/)
        for (j = 1; j <= cnt; j++) if (token_stale(parts[j], rkey(k))) { stale = 1; break }
        # the container holds exactly what the env file says: the cause is the
        # env file itself (a hand-added value), and no recreate can change it
        if (stale && (k in raw) && resolve(raw[k]) == v) envcause = 1
    }
    if (!stale && skey(k) && derived_stale(k, v)) stale = 1
    if (stale) print svc, cname, k (envcause ? " env" : "")
}
