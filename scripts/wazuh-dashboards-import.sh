#!/usr/bin/env bash
# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
# Imports the Wazuh dashboards (OpenSearch Dashboards saved objects, NDJSON)
# into a running Wazuh dashboard. Object ids are stable, so a re-run overwrites.
#
# Usage: wazuh-dashboards-import.sh [--dry-run] [--insecure] [--cacert FILE]
#          [--dir DIR]... --credentials-file FILE DASHBOARD_URL
#   FILE holds one line, user:password. Credentials are never accepted on the
#   command line (argv is visible to every local user): the file is read here and
#   handed to curl through a private config descriptor.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_DIR="$SCRIPT_DIR/../modules/apps/wazuh/configs/wazuh_dashboard/saved_objects"
dry=0; insecure=0; cacert=""; creds=""; url=""; dirs=()

die() { echo "wazuh-dashboards-import: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) dry=1 ;;
        --insecure) insecure=1 ;;
        --cacert) shift; cacert="${1:?--cacert needs a file}" ;;
        --dir) shift; dirs+=("${1:?--dir needs a directory}") ;;
        --credentials-file) shift; creds="${1:?--credentials-file needs a path}" ;;
        --password*|--pass*|--user|--user=*|-u|-u?*|--token*|--api-key*|--basic)
            die "credentials are never taken from the command line; put user:password in a file and use --credentials-file" ;;
        -*) die "unknown option: $1" ;;
        *) [ -z "$url" ] || die "only one dashboard URL is accepted"; url="$1" ;;
    esac
    shift
done

[ -n "$url" ] || die "a dashboard URL is required (for example https://wazuh.example:5601)"
case "$url" in
    *://*@*) die "the URL must not carry credentials; use --credentials-file" ;;
    http://*|https://*) ;;
    *) die "the URL must start with http:// or https://" ;;
esac
url="${url%/}"
[ ${#dirs[@]} -gt 0 ] || dirs=("$DEFAULT_DIR")

files=()
for d in "${dirs[@]}"; do
    [ -d "$d" ] || die "not a directory: $d"
    while IFS= read -r f; do files+=("$f"); done < <(find "$d" -maxdepth 1 -name '*.ndjson' | LC_ALL=C sort)
done
[ ${#files[@]} -gt 0 ] || die "no .ndjson files found"

endpoint="$url/api/saved_objects/_import?overwrite=true"

if [ "$dry" = 1 ]; then
    echo "dry run: would POST to $endpoint"
    for f in "${files[@]}"; do echo "  $f ($(wc -l < "$f") objects)"; done
    exit 0
fi

[ -n "$creds" ] || die "--credentials-file is required"
[ -r "$creds" ] || die "cannot read credentials file: $creds"
IFS= read -r cred_line < "$creds" || true
case "$cred_line" in *:*) ;; *) die "credentials file must hold user:password on its first line" ;; esac

curl_opts=(-sS --fail-with-body -H 'osd-xsrf: true')
[ "$insecure" = 1 ] && curl_opts+=(-k)
[ -z "$cacert" ] || curl_opts+=(--cacert "$cacert")

rc=0
for f in "${files[@]}"; do
    esc="${cred_line//\\/\\\\}"; esc="${esc//\"/\\\"}"
    if out="$(curl "${curl_opts[@]}" -K <(printf 'user = "%s"\n' "$esc") \
                 -X POST -F "file=@$f" "$endpoint")" \
       && printf '%s' "$out" | grep -q '"success":true'; then
        echo "imported $(basename "$f")"
    else
        echo "FAILED $(basename "$f"): $out" >&2; rc=1
    fi
done
exit $rc
