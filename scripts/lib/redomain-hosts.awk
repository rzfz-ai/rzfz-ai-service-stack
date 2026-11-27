# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Re-domain leftover finder for scripts/redomain-reconcile.sh. POSIX awk (mawk).
# The rule is razzfazz_common.redomain's: a host is a leftover when it IS
# ENVIRON["OLD"] or ends in "." OLD, unless the new domain is itself under the
# old one and the host is already under the new domain.
#   -v mode=env    input: an env file; ENVIRON KEY / SKIP are key EREs. Counts
#                  the keys (not comments) whose comma-separated values name
#                  a leftover host.  Prints "<n> <key> <key>...".
#   -v mode=hosts  input: one quoted JSON string per line (Caddy's config).
#                  Prints "<n> <host> <host>..." (distinct, at most 10 named).
#   -v mode=drift  (#2719) scripts/lib/domain-env-drift.awk, loaded after this
#                  file, judges running containers' environments with the same
#                  under()/leftover()/host_of().
# Values are never printed, only key names and host names.

function under(h, d) {
    return h == d || (length(h) > length(d) && substr(h, length(h) - length(d)) == "." d)
}
function leftover(h) {
    h = tolower(h)
    if (!under(h, old)) return 0
    if (new_under_old && under(h, new)) return 0
    return 1
}
function host_of(tok,    p, i) {
    gsub(/^[[:space:]"']+/, "", tok); gsub(/[[:space:]"']+$/, "", tok)
    p = index(tok, "://")
    if (p) {
        tok = substr(tok, p + 3)
        i = match(tok, /[\/?#]/); if (i) tok = substr(tok, 1, i - 1)
        while ((i = index(tok, "@")) > 0) tok = substr(tok, i + 1)
    }
    sub(/:.*/, "", tok)
    return tok
}
BEGIN {
    old = tolower(ENVIRON["OLD"]); new = tolower(ENVIRON["NEW"])
    k = ENVIRON["KEY"]; x = ENVIRON["SKIP"]
    new_under_old = (new != old && under(new, old))
    n = 0; names = ""
}
mode == "env" {
    if ($0 ~ /^[[:space:]]*#/ || index($0, "=") == 0) next
    key = substr($0, 1, index($0, "=") - 1); gsub(/[[:space:]]/, "", key)
    if (key !~ k || (x != "" && key ~ x)) next
    cnt = split(substr($0, index($0, "=") + 1), parts, ",")
    for (j = 1; j <= cnt; j++) if (leftover(host_of(parts[j]))) { n++; names = names " " key; break }
    next
}
mode == "hosts" {
    h = tolower(host_of($0))
    if (h != "" && leftover(h) && !(h in seen)) {
        seen[h] = 1; n++
        if (n <= 10) names = names " " h
    }
}
# Other modes (scripts/lib/domain-env-drift.awk, loaded after this file to
# reuse under/leftover/host_of) print their own result.
END { if (mode == "env" || mode == "hosts") print n names }
