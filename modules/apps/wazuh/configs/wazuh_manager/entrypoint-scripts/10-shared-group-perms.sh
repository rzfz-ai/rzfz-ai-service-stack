#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
#
# #3392 — give every fleet-group directory to the `wazuh` group before remoted
# starts.
#
# wazuh-securityconfig-init (uid 1000) writes etc/shared/<group>/agent.conf into
# the wazuh-manager-config volume. The manager image's cont-init
# (0-wazuh-init::mount_files) copies that volume into /var/ossec as root with a
# plain `cp -r`, so a group directory the image does not already have arrives
# root:root 0755. remoted runs as `wazuh` and must create etc/shared/<group>/
# merged.mg there; it cannot, logs "Unable to open file
# 'etc/shared/<group>/merged.mg' due to [(13)-(Permission denied)]", and the
# agents of that group never receive its agent.conf.
#
# The image runs every /entrypoint-scripts/*.sh (cont-init 2-manager,
# function_entrypoint_scripts) AFTER mount_files and BEFORE
# `wazuh-control start`, as root. compose.yml mounts this directory there, so
# the repair runs on every start, before any daemon is up.
#
# What it does, per directory directly under etc/shared/:
#   - group `wazuh`, mode 0770 (the image's own etc/shared layout); the owner is
#     left as it is, so a directory the image created (wazuh-owned `default`)
#     keeps its owner and a copied one stays root-owned;
#   - an existing merged.mg that is not owned by `wazuh` is REMOVED (#2822: a
#     root-owned one makes remoted fail the same way); remoted writes a new
#     one from the group's agent.conf when it starts.
# Symlinks are skipped, never followed: etc/shared is writable by the `wazuh`
# group, i.e. by the daemons that parse agent input, so a link planted there
# must not steer a root chgrp onto anything outside the tree.
# #3436: merged.mg is removed, never chowned. Whatever sits under that name may
# have been put there by a process of the `wazuh` group: a hard link to a file
# elsewhere, or a root-only file of the same volume RENAMED there (its link
# count is 1, so no link-count check can tell). Handing it to `wazuh` gave the
# daemon that file. Removing the name changes no inode: rm unlinks whatever is
# at the name at that moment (a file or a link, never a directory) and follows
# nothing, so a swap between the check and the removal gains nothing either.
#
# The argument is the install root (default /var/ossec); the unit test passes a
# temporary tree.
set -euo pipefail

OSSEC_ROOT="${1:-/var/ossec}"
SHARED="$OSSEC_ROOT/etc/shared"
WAZUH_GROUP="wazuh"
WAZUH_USER="wazuh"

if [ ! -d "$SHARED" ] || [ -L "$SHARED" ]; then
    echo "[shared-group-perms] $SHARED is not a directory, nothing to do"
    exit 0
fi

rc=0
fixed=""
for entry in "$SHARED"/*; do
    [ -e "$entry" ] || continue        # empty glob
    [ -L "$entry" ] && continue        # never follow a link out of the tree
    [ -d "$entry" ] || continue        # agent-template.conf and other files
    group="${entry##*/}"
    if ! chgrp "$WAZUH_GROUP" "$entry" || ! chmod 0770 "$entry"; then
        echo "[shared-group-perms] ERROR: cannot give etc/shared/$group to group $WAZUH_GROUP;" \
             "remoted will not be able to write its merged.mg" >&2
        rc=1
        continue
    fi
    merged="$entry/merged.mg"
    if [ -f "$merged" ] && [ ! -L "$merged" ]; then
        owner="$(stat -c %U -- "$merged" 2>/dev/null || echo unknown)"
        if [ "$owner" != "$WAZUH_USER" ]; then
            if rm -f -- "$merged"; then
                echo "[shared-group-perms] etc/shared/$group/merged.mg (owner $owner) removed;" \
                     "remoted writes it again"
            else
                echo "[shared-group-perms] ERROR: cannot remove etc/shared/$group/merged.mg" \
                     "(owner $owner); remoted will not be able to rewrite it" >&2
                rc=1
            fi
        fi
    fi
    fixed="${fixed}${fixed:+ }${group}"
done

echo "[shared-group-perms] etc/shared group directories set to group $WAZUH_GROUP, mode 0770: ${fixed:-none}"
exit "$rc"
