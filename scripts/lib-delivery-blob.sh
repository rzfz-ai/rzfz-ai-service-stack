# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# ==============================================================================
# scripts/lib-delivery-blob.sh — the delivery-credentials blob.
# SOURCED: no top-level `set`. Needs scripts/lib.sh (print_*, read_env_value).
# ------------------------------------------------------------------------------
# .delivery-credentials.enc holds the admin password a FULL factory reset puts
# back into .env. It must therefore always be the password the box was handed
# over with, not the one it was installed with:
#   - `rzfz init` writes it the first time (provisional);
#   - `rzfz set-admin-password` re-seals it with the password it just set;
#   - `rzfz post-install --refresh` (and so the upgrade) re-seals a stale one;
#   - a factory reset refuses to apply a blob that no longer matches .env.
# "Current" means: the blob decrypts to the AUTHENTIK_BOOTSTRAP_PASSWORD in
# .env — the value the box holds without any network.
# Format and key are unchanged (aes-256-cbc/pbkdf2, machine-id key). The
# plaintext travels on stdin and the key in the environment, never on argv.
# This is not a strong secret boundary: anyone who can read the box's files
# already reads the live password from .env.
# ==============================================================================

DELIVERY_BLOB="${DELIVERY_BLOB:-${SCRIPT_DIR:-.}/.delivery-credentials.enc}"

_delivery_key() {
    { cat /etc/machine-id 2>/dev/null; echo "razzfazz-delivery-v1"; } | sha256sum | cut -d' ' -f1
}

# read_delivery_blob: prints the sealed password, or nothing (rc 1 if unreadable).
read_delivery_blob() {
    [ -f "$DELIVERY_BLOB" ] || return 1
    command -v openssl >/dev/null 2>&1 || return 1
    RZFZ_OSSL_PASS="$(_delivery_key)" openssl enc -d -aes-256-cbc -pbkdf2 -pass env:RZFZ_OSSL_PASS -in "$DELIVERY_BLOB" 2>/dev/null \
        | sed -n 's/^AUTHENTIK_BOOTSTRAP_PASSWORD=//p'
}

# seal_delivery_blob <password>: atomic (temp file + rename) write, then a
# decrypt-compare. Prints the reason on stderr and returns 1 on any failure; the
# previous blob is left untouched in that case.
seal_delivery_blob() {
    local pw="$1" dir tmp got
    [ -n "$pw" ] || { print_error "delivery credentials: no password to seal." >&2; return 1; }
    command -v openssl >/dev/null 2>&1 || { print_error "delivery credentials: openssl is missing, the blob was not written." >&2; return 1; }
    dir="$(dirname "$DELIVERY_BLOB")"
    tmp="$(umask 077; mktemp "${dir}/.delivery-credentials.XXXXXX")" \
        || { print_error "delivery credentials: cannot create a temp file in ${dir}." >&2; return 1; }
    if ! printf 'AUTHENTIK_BOOTSTRAP_PASSWORD=%s\n' "$pw" \
        | RZFZ_OSSL_PASS="$(_delivery_key)" openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:RZFZ_OSSL_PASS -out "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        print_error "delivery credentials: encrypting the blob failed; the previous blob is unchanged." >&2
        return 1
    fi
    chmod 600 "$tmp"
    got="$(RZFZ_OSSL_PASS="$(_delivery_key)" openssl enc -d -aes-256-cbc -pbkdf2 -pass env:RZFZ_OSSL_PASS -in "$tmp" 2>/dev/null \
        | sed -n 's/^AUTHENTIK_BOOTSTRAP_PASSWORD=//p')"
    if [ "$got" != "$pw" ]; then
        rm -f "$tmp"
        print_error "delivery credentials: the new blob does not decrypt to the password; the previous blob is unchanged." >&2
        return 1
    fi
    if ! mv -f "$tmp" "$DELIVERY_BLOB"; then
        rm -f "$tmp"
        print_error "delivery credentials: cannot replace ${DELIVERY_BLOB}; the previous blob is unchanged." >&2
        return 1
    fi
}

# delivery_blob_state <env_file>: 0 = current, 1 = stale (decrypts, differs from
# .env), 2 = no usable blob (missing or undecryptable), 3 = .env has no password.
delivery_blob_state() {
    local env_file="$1" cur sealed
    cur="$(read_env_value "$env_file" AUTHENTIK_BOOTSTRAP_PASSWORD 2>/dev/null)"
    [ -n "$cur" ] || return 3
    sealed="$(read_delivery_blob 2>/dev/null)" || true
    [ -n "$sealed" ] || return 2
    [ "$sealed" = "$cur" ] || return 1
    return 0
}

# reseal_delivery_blob_if_stale <env_file>: re-seals a STALE blob with the
# current .env password and logs that it did (never the value). A missing blob is
# not created here (the box may have been set up without the feature).
reseal_delivery_blob_if_stale() {
    local env_file="$1" rc=0
    delivery_blob_state "$env_file" || rc=$?
    case "$rc" in
        1)  seal_delivery_blob "$(read_env_value "$env_file" AUTHENTIK_BOOTSTRAP_PASSWORD)" \
                && print_substep "Delivery credentials were stale (not the admin password in .env): re-sealed." ;;
        2)  [ -f "$DELIVERY_BLOB" ] \
                && print_warning "Delivery credentials blob cannot be decrypted on this machine; fix with: rzfz set-admin-password --all" \
                || true ;;
        *)  return 0 ;;
    esac
}
