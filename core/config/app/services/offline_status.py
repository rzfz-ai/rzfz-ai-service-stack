# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Offline / network status summary — the SHARED read-only view for #184 P2.

Two surfaces render the same picture and must never disagree:

  * the Config Portal **Offline / Network** page (``blueprints/offline.py``), and
  * the ``rzfz status`` **OFFLINE / NETWORK** category (``cli/status.sh``).

This module is the one place that computes it: the box's network mode
(online / proxied / offline) + registry mirror, which overlays are composed, and
the ``rzfz verify-images`` / ``rzfz verify-models`` present/missing summaries. It
NEVER mutates anything — read-only on ``.env`` + the two verify CLIs.

The verify CLIs are shelled out to (``cli/verify-images.sh --quiet`` /
``cli/verify-models.sh --quiet``) rather than re-implemented, so the panel shows
exactly what the release gate checks. Both print a canonical summary line —
``<tool>: N expected, P present, M missing.`` — which :func:`parse_verify_summary`
parses into counts; degradation lines (no docker / empty enumeration) map to an
``unknown`` state so the panel degrades gracefully instead of asserting.

Import-safe (``from app.services import offline_status``) AND runnable stand-alone
(``python3 offline_status.py --stack-root <dir> --json``) — imports only stdlib.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

# The box-local overlay filenames (kept in lockstep with scripts/lib.sh).
OFFLINE_OVERLAY = 'compose.offline.yml'
PROXY_OVERLAY = 'compose.corporate-proxy.yml'
NOBUILD_OVERLAY = 'compose.no-build.yml'
REGISTRY_MIRROR_OVERLAY = 'compose.registry-mirror.yml'

# The canonical summary line both verify CLIs print (expected_images.py /
# expected_models.py): "<tool>: N expected, P present, M missing."
_SUMMARY_RE = re.compile(
    r'(\d+)\s+expected,\s+(\d+)\s+present,\s+(\d+)\s+missing')


def parse_verify_summary(text):
    """Parse a ``rzfz verify-images`` / ``verify-models`` output into a summary.

    Returns ``{'state', 'expected', 'present', 'missing', 'note'}`` where
    ``state`` is:
      * ``'ok'``       — a summary line with 0 missing,
      * ``'missing'``  — a summary line with >0 missing,
      * ``'unknown'``  — no summary line (enumeration degraded / no docker / the
                         volume couldn't be listed / nothing expected).
    Counts are ``None`` when unknown. ``note`` carries a short reason for the
    unknown / degraded case (best-effort, human-readable).
    """
    text = text or ''
    m = _SUMMARY_RE.search(text)
    if m:
        expected, present, missing = (int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)))
        return {
            'state': 'ok' if missing == 0 else 'missing',
            'expected': expected,
            'present': present,
            'missing': missing,
            'note': '',
        }
    # No summary line — figure out WHY (fail-open contract of the verify CLIs).
    low = text.lower()
    if 'could not enumerate' in low:
        note = 'could not enumerate the expected set (docker/compose unavailable)'
    elif 'could not list' in low:
        note = 'could not list the gpustack-data volume (docker exec unavailable)'
    elif 'no standard-model' in low or 'no ' in low and 'expected' in low:
        note = 'nothing expected for the active profiles'
    elif not text.strip():
        note = 'no output (verify CLI unavailable or timed out)'
    else:
        note = 'unrecognized verify output'
    return {'state': 'unknown', 'expected': None, 'present': None,
            'missing': None, 'note': note}


def _read_env_value(env_path, key):
    """Grep a single ``.env`` value (never sources it — values carry spaces /
    shell metachars; per the fleet ``feedback_dotenv_no_source`` rule)."""
    try:
        with open(env_path, encoding='utf-8') as fh:
            for line in fh:
                line = line.rstrip('\n')
                if line.startswith(key + '='):
                    val = line[len(key) + 1:]
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        val = val[1:-1]
                    return val.strip('\r')
    except OSError:
        pass
    return ''


def resolve_network_mode(env_path):
    """Effective mode (online|proxied|offline) — mirrors
    scripts/lib.sh::razzfazz_network_mode (explicit value, else derived from the
    legacy booleans, offline winning over proxied)."""
    mode = _read_env_value(env_path, 'RAZZFAZZ_NETWORK_MODE')
    if mode in ('online', 'proxied', 'offline'):
        return mode
    off = _read_env_value(env_path, 'RAZZFAZZ_OFFLINE').lower()
    if off in ('1', 'true', 'yes', 'on'):
        return 'offline'
    cp = _read_env_value(env_path, 'RAZZFAZZ_CORPORATE_PROXY').lower()
    if cp in ('1', 'true', 'yes', 'on'):
        return 'proxied'
    return 'online'


def overlays_composed(compose_file):
    """Which known overlays are in the ``COMPOSE_FILE`` chain (``:``-joined)."""
    parts = set(p for p in (compose_file or '').split(':') if p)
    return {
        'offline': OFFLINE_OVERLAY in parts,
        'corporate_proxy': PROXY_OVERLAY in parts,
        'no_build': NOBUILD_OVERLAY in parts,
        'registry_mirror': REGISTRY_MIRROR_OVERLAY in parts,
    }


def _default_runner(cmd, cwd, timeout):
    """Run ``cmd`` and return combined stdout+stderr text (empty on failure)."""
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return (r.stdout or '') + (r.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return ''


def collect(stack_root, runner=_default_runner, run_verify=True,
            image_timeout=90, model_timeout=45):
    """The full read-only status dict for the panel + ``rzfz status``.

    ``runner(cmd, cwd, timeout) -> text`` is injectable so tests drive it without
    docker. ``run_verify=False`` skips the (potentially slow) verify shell-outs
    and reports both as ``unknown`` — used where only the mode/overlays matter.
    """
    env_path = os.path.join(stack_root, '.env')
    mode = resolve_network_mode(env_path)
    mirror = _read_env_value(env_path, 'RAZZFAZZ_REGISTRY_MIRROR')
    compose_file = _read_env_value(env_path, 'COMPOSE_FILE')
    overlays = overlays_composed(compose_file)

    if run_verify:
        vi_sh = os.path.join(stack_root, 'cli', 'verify-images.sh')
        vm_sh = os.path.join(stack_root, 'cli', 'verify-models.sh')
        images = parse_verify_summary(
            runner([vi_sh, '--quiet'], stack_root, image_timeout))
        models = parse_verify_summary(
            runner([vm_sh, '--quiet'], stack_root, model_timeout))
    else:
        images = parse_verify_summary('')
        models = parse_verify_summary('')

    return {
        'network_mode': mode,
        'is_offline': mode == 'offline',
        'registry_mirror': mirror,
        'compose_file': compose_file,
        'overlays': overlays,
        'images': images,
        'models': models,
        # Only meaningful offline: a truly air-gapped box should also silence the
        # host-daemon egress. This is opt-in host hardening, never automatic.
        'harden_hint': (mode == 'offline'),
    }


# --------------------------------------------------------------------------- #
# Stand-alone CLI — cli/status.sh may shell out to `--json`.                  #
# --------------------------------------------------------------------------- #

def _main(argv):
    import argparse

    ap = argparse.ArgumentParser(
        prog='offline_status',
        description='Read-only offline / network status summary (#184 P2).')
    ap.add_argument('--stack-root', default=os.getcwd())
    ap.add_argument('--json', action='store_true',
                    help='print the status dict as JSON')
    ap.add_argument('--no-verify', action='store_true',
                    help='skip the verify-images/verify-models shell-outs')
    args = ap.parse_args(argv)

    data = collect(args.stack_root, run_verify=not args.no_verify)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"network mode      : {data['network_mode']}")
        print(f"registry mirror   : {data['registry_mirror'] or '(none)'}")
        ov = data['overlays']
        print(f"overlays          : offline={ov['offline']} "
              f"no-build={ov['no_build']} proxy={ov['corporate_proxy']} "
              f"registry-mirror={ov['registry_mirror']}")
        for label, key in (('verify-images', 'images'), ('verify-models', 'models')):
            s = data[key]
            if s['state'] == 'unknown':
                print(f"{label:<17} : unknown ({s['note']})")
            else:
                print(f"{label:<17} : {s['present']}/{s['expected']} present, "
                      f"{s['missing']} missing")
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(_main(sys.argv[1:]))
