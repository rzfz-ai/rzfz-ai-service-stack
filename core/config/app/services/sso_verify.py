# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2794 — did the Google/Entra sources actually land after a Settings save?

Saving the auth settings recreates authentik-init, which writes the SSO
blueprints; Authentik then applies them asynchronously and may REFUSE a source
(a wrong Entra tenant fails the JWKS fetch: AADSTS90002). The old flow said
"saved" regardless. This module reads, from inside authentik-worker, the status
of the SSO blueprint instances and which sources exist, and turns that into a
verdict the apply can report.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

SSO_PROVIDERS = ("google", "entra")
FLOW_FILE = "z-15-sso-flow-integration.yaml"

#: Runs INSIDE authentik-worker (`python -c`), like module_hooks.CHECK_SCRIPT.
CHECK_SCRIPT = r'''
import json, os, sys
sys.path.append("/")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
import django
django.setup()
from authentik.blueprints.models import BlueprintInstance
from authentik.sources.oauth.models import OAuthSource
inst = [{"path": i.path, "name": i.name, "status": str(i.status)}
        for i in BlueprintInstance.objects.all()
        if i.path.startswith(("google-", "entra-", "z-15-sso"))]
print("RZFZ-SSO " + json.dumps({
    "instances": inst,
    "sources": sorted(OAuthSource.objects.filter(slug__in=["google", "entra"]).values_list("slug", flat=True)),
}))
'''


def expected_providers(env):
    """The providers the init will deploy for this .env (mirrors
    deploy_sso_blueprints in core/init-authentik.sh)."""
    out = []
    if env.get("DEPLOYMENT_SCENARIO") == "google" or env.get("ENABLE_GOOGLE_OAUTH") == "true":
        out.append("google")
    if (env.get("ENABLE_ENTRA_OAUTH") == "true" and env.get("ENTRA_TENANT_ID")
            and env.get("ENTRA_CLIENT_ID") and env.get("ENTRA_CLIENT_SECRET")):
        out.append("entra")
    return out


def assess(state, expected):
    """(problems, pending) for one reading of the worker."""
    problems, pending = [], False
    sources = set(state.get("sources", ()))
    for inst in state.get("instances", ()):
        path, status = inst.get("path", ""), inst.get("status", "")
        wanted = path == FLOW_FILE or any(path.startswith(p + "-") and p in expected
                                          for p in SSO_PROVIDERS)
        if not wanted:
            continue
        if status == "error":
            problems.append(f"blueprint '{inst.get('name') or path}' is in error")
        elif status not in ("successful",):
            pending = True
    for prov in expected:
        if prov not in sources:
            problems.append(f"the {prov} source does not exist")
            pending = pending or not any(i.get("path", "").startswith(prov + "-")
                                         for i in state.get("instances", ()))
    return problems, pending


def read_state(run=subprocess.run):
    r = run(["docker", "exec", "authentik-worker", "python", "-c", CHECK_SCRIPT],
            capture_output=True, text=True, timeout=120)
    line = next((ln for ln in (getattr(r, "stdout", "") or "").splitlines()
                 if ln.startswith("RZFZ-SSO ")), None)
    if r.returncode != 0 or line is None:
        raise RuntimeError(f"could not read the Authentik blueprint state (exit {r.returncode})")
    return json.loads(line[len("RZFZ-SSO "):])


def worker_reason(run=subprocess.run, providers=SSO_PROVIDERS):
    """Best effort: the last worker log line that explains a refused source."""
    try:
        r = run(["docker", "logs", "--tail", "400", "authentik-worker"],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
    hits = [ln for ln in text.splitlines()
            if any(k in ln for k in ("AADSTS", "oauthsource", "entra", "google"))
            and any(k in ln.lower() for k in ("error", "invalid", "exception", "aadsts"))]
    return hits[-1].strip()[:300] if hits else ""


DEFAULT_TIMEOUT = 300
#: Worker-log markers of a TRANSIENT failure: Authentik's blueprint discovery
#: raced itself and tried to create an instance that already exists. The
#: instance applies on the next pass (measured: the source appeared ~2.3 min
#: after the save), so this is "keep waiting", not a verdict.
TRANSIENT_MARKERS = ("integrityerror", "duplicate key", "already exists", "instance exists",
                     "unique constraint")


def verify_timeout(env=None):
    """SSO_VERIFY_TIMEOUT (seconds) from the .env or the process, default 300."""
    raw = (env or {}).get("SSO_VERIFY_TIMEOUT") or os.environ.get("SSO_VERIFY_TIMEOUT") or ""
    try:
        val = int(str(raw).strip())
    except ValueError:
        return DEFAULT_TIMEOUT
    return val if val > 0 else DEFAULT_TIMEOUT


def is_transient(reason):
    low = (reason or "").lower()
    return any(m in low for m in TRANSIENT_MARKERS)


def verify(expected, *, run=subprocess.run, sleep=time.sleep, clock=time.monotonic,
           timeout=DEFAULT_TIMEOUT, interval=10, on_progress=None):
    """Poll up to `timeout` s. Returns None when the SSO blueprints are healthy,
    else the reason string.

    While the deadline has not passed a missing source or a pending/failed
    instance is "still applying" (reported through `on_progress`, once per
    change), and an error whose worker-log reason is a transient
    IntegrityError / "instance exists" is retried. It fails early only on a
    DEFINITIVE error (an instance in error with a non-transient reason, e.g.
    AADSTS90002), and at the deadline."""
    deadline = clock() + timeout
    said = None

    def say(msg):
        nonlocal said
        if on_progress and msg != said:
            said = msg
            on_progress(msg)

    while True:
        try:
            problems, pending = assess(read_state(run), expected)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            problems, pending = [str(exc)], True
        if not problems and not pending:
            return None
        errored = any("is in error" in p for p in problems)
        why = worker_reason(run) if errored else ""
        if errored and why and not is_transient(why):
            return "; ".join(problems) + f" ({why})"
        if clock() >= deadline:
            if not problems:
                return "the SSO blueprints were still applying after %d s" % timeout
            if not why:
                why = worker_reason(run)
            return "; ".join(problems) + (f" ({why})" if why else "")
        say("Authentik is still applying the SSO sources...")
        sleep(interval)
