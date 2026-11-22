import json
import os
import sys
import time
import django

sys.path.append('/')
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "authentik.root.settings")
django.setup()

from authentik.core.models import Application, Group, User
from authentik.policies.models import PolicyBinding

# #1465: which compose profile each bound app belongs to. An app whose profile
# is not active on this box has no blueprint applied and therefore no
# Application row — waiting 12 x 5 s for it (get_app_with_retry) burned a
# minute per such app on every reconcile (5 min per upgrade on 0.91, which has
# no wazuh; the host-side outpost reconcile then reported "not green" for a
# box that was fine). None = core app, always present. A tuple = any of these
# profiles. Every ensure_binding() slug MUST have an entry —
# tests/unit/consistency/test_1465_policy_bindings_skip_inactive_profiles.py.
APP_PROFILES = {
    "administration": "monitor",
    "backup": None,
    "chat": "chat",
    "config": None,
    "help": None,
    "licenses": None,
    "workflow-automation": "dify",
    # every profile that runs a gpustack* service (compose is the authority;
    # the guard derives this tuple from it)
    # #1448 (cutover C8): llm-cuda merged into llm-legacy.
    # #1447: the 2.x `llm` profile is removed (part a) and `llm-cpu` folded
    # into `llm-legacy` (part b) — one GPUStack profile, one entry.
    "llm-management": "llm-legacy",
    "llm-manager": "llm-manager",
    # #1652: the Zot registry management UI on hub.<domain>. It ships with the
    # `llm-registry` profile and it is fleet INFRASTRUCTURE — browse, search
    # and delete image tags. Unbound it was reachable by every authenticated
    # user, which is the one asymmetry in this table that mattered.
    "fleet-hub": "llm-registry",
    "gitea": "gitea",
    "lightrag": "lightrag",
    "cognee": "cognee",
    "agents": "agents",
    "my-agents": "agents",
    "mcp": "agents",
    "crawl4ai": "crawl4ai",
    "docling": "docling",
    "element-web": "matrix",
    # #2681: Synapse's native-OIDC application. matrix.<domain> has no
    # forward-auth (Caddy: "Synapse handles auth internally via OIDC"), so this
    # binding is the ONLY gate on a Matrix login from any client — unbound,
    # every Authentik user could sign in to Matrix (measured: 0 bindings).
    "synapse": "matrix",
    "observability": "observability",
    "paperclip": "paperclip",
    "stirling-pdf": "stirling-pdf",
    "paperless-ngx": "paperless-ngx",
    "vaultwarden": "vaultwarden",
    "infisical": "infisical",
    "onyx": "onyx",
    "openuem": "openuem",
    "wazuh": "wazuh",
    # #2476: OpenBao's forward-auth app and its native-OIDC app.
    "openbao": "openbao",
    "openbao-oidc": "openbao",
    # #2507: the native-OIDC applications (blueprints openwebui-oidc/ and
    # gitea-oidc/). Their app's profile, plus the switch in OIDC_APP_SWITCHES.
    "openwebui-oidc": "chat",
    "gitea-oidc": "gitea",
}

# #2507: an OAuth2 application with policy_engine_mode `any` and NO binding is
# open to EVERY authenticated Authentik user (#2090) — which is what the
# openwebui-oidc / gitea-oidc applications were on prod until they were bound
# by hand on 2026-09-26. They exist only when their switch is on (init-authentik
# templates the provider blueprint then), so the switch decides whether the
# binding pass WAITS for them; with it off (or not passed) a registered app is
# still bound, without the wait.
OIDC_APP_SWITCHES = {
    "openwebui-oidc": "ENABLE_OPENWEBUI_OIDC",
    "gitea-oidc": "ENABLE_GITEA_AUTHENTIK_OIDC",
}


# #2681: the applications that are open to EVERY signed-in user ON PURPOSE,
# each with its reason. Every other application a blueprint creates must be
# bound by an ensure_binding() call below — an Application with zero
# PolicyBindings is open to every authenticated user (#2090), so silence is an
# access-control gap, not a default. Guarded by
# tests/unit/consistency/test_2681_every_blueprint_app_is_bound_or_open_by_design.py;
# the enable paths (module_hooks.close_unbound_apps) skip these slugs.
UNBOUND_BY_DESIGN = {
    # M028 / INIT_VERSION 2.7: the post-login landing page. Authentik redirects
    # every user there after login; a binding locks them out of it (the wipe in
    # __main__ removes leftovers).
    "start": "post-login landing page for every authenticated user",
    # #1652, decided 2026-09-08: search for everyone with a login; it is also
    # the search backend of chat and Dify, so a group would make "search works"
    # depend on which surface asked.
    "searxng": "search for every authenticated user; also the chat/Dify search backend",
    # #855: the hidden native-OIDC half of Wazuh. The gate is the forward-auth
    # app `wazuh` on the whole vhost; only /auth/openid/* (the callback) is
    # exempt, so a session minted here reaches nothing behind the gate.
    "wazuh-oidc": "hidden native-OIDC app; the gate is the forward-auth app `wazuh`",
}


def _active_profiles():
    """COMPOSE_PROFILES as the callers pass it (`docker exec -e`). None when
    the variable is absent — then nothing is skipped (pre-#1465 behaviour), so
    a caller that forgets the env cannot silently drop bindings."""
    raw = os.environ.get("COMPOSE_PROFILES")
    if raw is None:
        return None
    return {p.strip() for p in raw.split(",") if p.strip()}


def _profile_active(app_slug):
    """True when the app's profile is active (or the app is core, or the
    caller passed no profile list). Unknown slugs count as active: better one
    stray 60 s wait than a binding silently never made."""
    active = _active_profiles()
    if active is None:
        return True
    want = APP_PROFILES.get(app_slug, None)
    if want is None:
        return True
    if isinstance(want, str):
        want = (want,)
    return any(p in active for p in want)

def _oidc_switch_on(app_slug):
    """#2507: False only for a native-OIDC app whose switch is not on (the
    callers pass the switches via `docker exec -e`; an absent switch means
    "not known to be on"). True for every other app."""
    switch = OIDC_APP_SWITCHES.get(app_slug)
    if switch is None:
        return True
    return os.environ.get(switch, "").strip().lower() == "true"

def get_app_with_retry(slug, retries=12, delay=5):
    for i in range(retries):
        try:
            return Application.objects.get(slug=slug)
        except Application.DoesNotExist:
            print(f"App {slug} not found, retrying ({i+1}/{retries})...")
            time.sleep(delay)
    raise Application.DoesNotExist(f"App {slug} not found after retries.")

def get_group_with_retry(name, retries=12, delay=5):
    for i in range(retries):
        try:
            return Group.objects.get(name=name)
        except Group.DoesNotExist:
            print(f"Group {name} not found, retrying ({i+1}/{retries})...")
            time.sleep(delay)
    raise Group.DoesNotExist(f"Group {name} not found after retries.")

#: The box-wide access gate (A1 D10, the staging canary): bound on every
#: Application while RAZZFAZZ_ACCESS_GROUP names a group. See
#: ensure_access_group_restriction().
GATE_POLICY = "razzfazz.ai - Box access group"
#: Per-app policy that replaces the app's group bindings while restricted.
APP_ACCESS_POLICY_PREFIX = "razzfazz.ai - App access: "


#: Tag appended to the meta_description of every restricted Application. It
#: survives a hand-deleted policy (whose bindings cascade away); a blueprint
#: re-apply rewrites the description, and that case is what the snapshot
#: (SNAPSHOT_PATH) covers.
ACCESS_MARKER = "[rzfz: access group]"


def _app_restricted(app):
    """True while this Application is under the access-group restriction: it
    carries the marker, or the gate is bound. Not keyed on policy_engine_mode,
    which a blueprint re-apply sets back to `any`."""
    return (ACCESS_MARKER in (getattr(app, "meta_description", "") or "")
            or PolicyBinding.objects.filter(target_id=app.pk, policy__name=GATE_POLICY).exists())


def ensure_binding(app_slug, group_name, order=0):
    if not _profile_active(app_slug) or not _oidc_switch_on(app_slug):
        # #1465 skips the WAIT, not the binding. #2090: the blueprints register
        # every Application regardless of profile, and an Application with
        # ZERO PolicyBindings is open to every authenticated user, not denied
        # (measured on 0.91: llm.<domain> → 302 into the login flow). So a
        # registered app of an inactive profile is bound like any other — one
        # lookup each, no retry, so a box without the app or the group still
        # pays nothing (the five minutes #1465 was about).
        if not Application.objects.filter(slug=app_slug).exists():
            print(f"[i] {app_slug}: not expected on this box (profile or switch off) and no Application registered — skipping binding.")
            return
        if not Group.objects.filter(name=group_name).exists():
            print(f"[i] {app_slug}: not expected on this box and group '{group_name}' absent — skipping binding (nothing to bind to).")
            return
        print(f"[i] {app_slug}: not expected on this box but the Application is registered — binding it (an unbound app is unrestricted, #2090).")
    try:
        app = get_app_with_retry(app_slug)
        group = get_group_with_retry(group_name)
        
        # An app under the access-group restriction keeps its group bindings
        # DISABLED (their groups live in the app's access policy). Enabling one
        # here would, under policy_engine_mode `all`, demand every group at once,
        # and under `any` (after a blueprint re-apply) admit its members without
        # the access group — for the minutes until the restriction step at the
        # end of this script runs.
        restricted = _app_restricted(app)
        # Check if binding exists
        existing = PolicyBinding.objects.filter(target_id=app.pk, group=group).first()
        if existing:
            print(f"Binding for {app_slug} -> {group_name} already exists.")
            if existing.order != order:
                existing.order = order
                existing.save()
            if not existing.enabled and not restricted:
                existing.enabled = True
                existing.save()
        else:
            print(f"Creating binding for {app_slug} -> {group_name}"
                  + (" (disabled: the app is under the access-group restriction)" if restricted else ""))
            PolicyBinding.objects.create(
                target_id=app.pk,
                group=group,
                enabled=not restricted,
                order=order
            )
    except Application.DoesNotExist:
        print(f"App {app_slug} could not be found after waiting.")
    except Group.DoesNotExist:
        print(f"Group {group_name} not found.")
    except Exception as e:
        print(f"Error binding {app_slug} -> {group_name}: {e}")

ADMIN_MFA_POLICY = "razzfazz.ai - Require Admin MFA"
#: Our own validation stage (07-mfa-policy.yaml): not_configured_action=configure.
#: Only our policy binding routes anyone to it.
ADMIN_MFA_STAGE = "razzfazz.ai - Admin MFA validation"
#: On default-authentication-flow only, `all` with this policy keeps an admin who
#: already has a device (validated by Authentik's own stage at order 30) from
#: being asked for the code twice.
ADMIN_MFA_NO_DEVICE_POLICY = "razzfazz.ai - Admin MFA: no device enrolled"
#: Authentik's own stage and flow. Never modified: its binding carries a second,
#: upstream policy (true for every non-passkey login), so configuring THAT stage
#: would force every user to enrol (#2551 review C1). Only the pre-#2551 binding
#: of our admin policy on it is removed.
UPSTREAM_MFA_STAGE = "default-authentication-mfa-validation"
#: A1 D11: every flow an admin can log in through. Both Google and Entra
#: authenticate through default-source-authentication; the two domain-
#: enrollment flows exist only where the google / entra blueprints are applied.
ADMIN_MFA_FLOWS = ("default-authentication-flow", "default-source-authentication",
                   "google-domain-enrollment", "entra-domain-enrollment")
LOGIN_STAGES = ("default-authentication-login", "default-source-authentication-login")
NO_DEVICE_FLOWS = ("default-authentication-flow",)


def ensure_mfa_admin_binding():
    """B-11 / #2551 / A1 D11 — admin MFA on every admin login flow (NIS2 21(2)(j)).

    For each flow in ADMIN_MFA_FLOWS: bind our dedicated stage
    (`razzfazz.ai - Admin MFA validation`, configure) directly before the
    flow's login stage and bind the `razzfazz.ai - Require Admin MFA` policy to
    that flow-stage binding. The policy (07-mfa-policy.yaml) matches Super
    Admins and authentik Admins, so a non-admin never meets the stage.
    Authentik's own MFA stage and its bindings are never touched.

    RAZZFAZZ_REQUIRE_ADMIN_MFA: missing or empty means false (the blueprint's
    `${RAZZFAZZ_REQUIRE_ADMIN_MFA:-false}`): whoever installs a box would
    otherwise have to enrol a second factor that cannot be handed over to the
    customer, who enables enforcement after the handover. any other non-empty
    value (true, 1, yes, on, or a typo) binds our stage; false/0/no/off or
    empty REMOVES our stage binding from all four flows
    together (a stage binding whose policy bindings are all disabled would run
    for everyone), and `rzfz status` then shows an INFO row.
    Idempotent. Returns
    {flow slug: "bound" | "disabled" | "absent" | "no-login-stage" | "error"}.

    PolicyBinding.target is the flow-stage binding's PolicyBindingModel parent
    pk (`policybindingmodel_ptr_id`), which differs from its own pk on
    Authentik 2026.x (M034 S04: using fsb.pk was an FK violation on a fresh
    install).
    """
    from authentik.flows.models import Flow, FlowStageBinding
    from authentik.policies.expression.models import ExpressionPolicy
    from authentik.policies.models import PolicyBinding
    from authentik.stages.authenticator_validate.models import AuthenticatorValidateStage

    raw = os.environ.get("RAZZFAZZ_REQUIRE_ADMIN_MFA", "").strip().lower()
    enabled = raw not in ("", "false", "0", "no", "off")
    try:
        policy = ExpressionPolicy.objects.filter(name=ADMIN_MFA_POLICY).first()
        no_device = ExpressionPolicy.objects.filter(name=ADMIN_MFA_NO_DEVICE_POLICY).first()
        stage = AuthenticatorValidateStage.objects.filter(name=ADMIN_MFA_STAGE).first()
        upstream = AuthenticatorValidateStage.objects.filter(name=UPSTREAM_MFA_STAGE).first()
    except Exception as e:  # noqa: BLE001
        print(f"B-11: error looking up the admin-MFA objects: {e}")
        return {slug: "error" for slug in ADMIN_MFA_FLOWS}
    if not policy or not stage or not no_device:
        print("B-11: admin-MFA policy or stage not found (07-mfa-policy.yaml not applied yet); skipping.")
        return {slug: "absent" for slug in ADMIN_MFA_FLOWS}
    result = {}
    for slug in ADMIN_MFA_FLOWS:
        try:
            flow = Flow.objects.filter(slug=slug).first()
            if not flow:
                result[slug] = "absent"
                continue
            if upstream is not None:
                # The pre-#2551 wiring bound our admin policy on Authentik's own
                # stage; harmless there, but it is not ours to leave behind.
                old = FlowStageBinding.objects.filter(target=flow, stage=upstream).first()
                if old is not None:
                    old_pk = getattr(old, "policybindingmodel_ptr_id", None) or old.pk
                    for pb in list(PolicyBinding.objects.filter(policy=policy, target_id=old_pk)):
                        pb.delete()
            fsb = FlowStageBinding.objects.filter(target=flow, stage=stage).first()
            if not enabled:
                if fsb is not None:
                    fsb.delete()
                result[slug] = "disabled"
                print(f"B-11: admin MFA on {slug}: disabled (RAZZFAZZ_REQUIRE_ADMIN_MFA={raw})")
                continue
            mode = "all" if slug in NO_DEVICE_FLOWS else "any"
            created = False
            if fsb is None:
                logins = [b.order for b in FlowStageBinding.objects.filter(target=flow)
                          if getattr(b.stage, "name", "") in LOGIN_STAGES]
                if not logins:
                    print(f"B-11: {slug} has no login stage; admin MFA NOT bound there.")
                    result[slug] = "no-login-stage"
                    continue
                # Evaluated when the stage is reached: the pending user (the
                # source's user, or the identified one) is known by then.
                fsb = FlowStageBinding.objects.create(target=flow, stage=stage, order=min(logins) - 1,
                                                      evaluate_on_plan=False, re_evaluate_policies=True,
                                                      policy_engine_mode=mode)
                created = True
            try:
                if getattr(fsb, "policy_engine_mode", mode) != mode:
                    fsb.policy_engine_mode = mode
                    fsb.save()
                target_pk = getattr(fsb, "policybindingmodel_ptr_id", None) or fsb.pk
                # (policy, order, failure_result). The admin policy fails closed
                # for nobody: an error there must never route a non-admin to the
                # stage. The no-device policy fails OPEN to the stage: if its
                # expression errors, an admin without a device is still forced
                # to enrol (worst case: an enrolled admin sees the code twice).
                wanted = [(policy, 0, False)] + ([(no_device, 10, True)] if slug in NO_DEVICE_FLOWS else [])
                for pol, order, fail_result in wanted:
                    pb = PolicyBinding.objects.filter(policy=pol, target_id=target_pk).first()
                    if pb is None:
                        PolicyBinding.objects.create(policy=pol, target_id=target_pk, enabled=True,
                                                     order=order, timeout=30, failure_result=fail_result)
                    elif pb.enabled is not True or pb.order != order or pb.failure_result != fail_result:
                        pb.enabled, pb.order, pb.failure_result = True, order, fail_result
                        pb.save()
            except Exception:
                # A stage binding without its policy bindings would run for everyone.
                if created:
                    fsb.delete()
                raise
            result[slug] = "bound"
        except Exception as e:  # noqa: BLE001 — one flow must not stop the others
            print(f"B-11: error binding admin MFA on {slug}: {e}")
            result[slug] = "error"
            continue
        print(f"B-11: admin MFA on {slug}: {result[slug]} (RAZZFAZZ_REQUIRE_ADMIN_MFA={raw or 'unset → false'})")
    return result


SUPER_ADMINS_GROUP = "razzfazz.ai Super Admins"


def plan_access_restriction(access_group, app_groups, restricted_apps=(), all_of=()):
    """A1 D10 (staging canary) — pure. Set: every app goes to
    policy_engine_mode=all, with the app's own groups folded into one policy
    beside the restriction. Unset: revert the apps that still carry the
    restriction (restricted_apps); [] only when unset AND nothing is
    restricted — an unrestricted box is left exactly as before. Apps in
    `all_of` were in policy_engine_mode `all` before: their groups are all
    required, not any one of them."""
    group = (access_group or "").strip()
    if not group:
        return [{"app": slug, "mode": "any", "revert": True} for slug in sorted(restricted_apps)]
    return [dict({"app": slug, "mode": "all", "app_groups": sorted(groups), "restrict_to": group},
                 **({"require_all": True} if slug in all_of else {}))
            for slug, groups in sorted(app_groups.items())]


def access_policy_expressions(app_groups, restrict_to, require_all=False, users=(), policies=(),
                              neg_groups=()):
    """(gate expression, per-app expression). The per-app policy requires the
    access group TOO, so the restriction survives a blueprint re-apply that
    puts the app back to policy_engine_mode `any` (gate OR app policy is then
    still "access group members only"). Every other grant the app had is
    folded in beside its groups: a user binding as `request.user.pk == <pk>`,
    a policy binding as `ak_call_policy(<name>).passing`, and a NEGATED binding
    as the negation of its term (`not …`, `!=`), which is what Authentik's
    binding-level `negate` does to that binding's result. `users` and
    `policies` are (value, negate) pairs; `neg_groups` are the groups of
    negated group bindings. So no binding of
    the app needs to stay enabled, and none can admit anyone on its own after
    such a reset. An explicit or-chain rather than a generator: Authentik runs
    the expression as a function body, and the helpers are that function's
    parameters."""
    member = "ak_is_group_member(request.user, name=%r)"
    gate = member % restrict_to
    terms = ([member % g for g in app_groups] + ["not " + member % g for g in neg_groups]
             + [("request.user.pk != %r" if neg else "request.user.pk == %r") % u for u, neg in users]
             + [("not ak_call_policy(%r).passing" if neg else "ak_call_policy(%r).passing") % n
                for n, neg in policies])
    if not terms:
        return "return " + gate, "return " + gate
    anyof = (" and " if require_all else " or ").join(terms)
    return "return " + gate, "return %s and (%s)" % (gate, anyof)


#: The pre-restriction state of every app the restriction changed: its mode,
#: its description and the enabled flag of each of its bindings. Written
#: BEFORE the first app is touched; clearing restores exactly this and then
#: deletes the file. Lives in the authentik-media volume: mounted read-write at
#: /data in authentik-worker (where this script runs), in the backup set and
#: the restore set (core/compose.yml), written by the worker's own uid.
#: A dot-directory at the VOLUME ROOT: Caddy mounts the volume and serves
#: /branding/media/* from media/ and /branding/*, /media/* from media/public/,
#: never the root; and both media migration loops (authentik-media-migrator,
#: upgrade.sh hop 1) glob /data/*, which skips dot entries.
SNAPSHOT_PATH = "/data/.razzfazz/access-group-snapshot.json"
#: Where round 1 put it — served publicly by Caddy's /branding/media/*. Moved
#: once, by _migrate_snapshot().
LEGACY_SNAPSHOT_PATH = "/data/media/razzfazz/access-group-snapshot.json"


_NOT_WRITABLE_HINT = ("authentik-media-migrator creates the directory owned by the worker (1000:1000, "
                      "mode 700); run: docker compose up authentik-media-migrator, then the refresh again.")


def _migrate_snapshot():
    """Move a snapshot from the old, publicly served path to SNAPSHOT_PATH.
    When both exist the new one wins and the old copy is deleted. Raises
    OSError when that fails — the caller then changes nothing."""
    if not os.path.exists(LEGACY_SNAPSHOT_PATH):
        return
    if os.path.exists(SNAPSHOT_PATH):
        os.remove(LEGACY_SNAPSHOT_PATH)
    else:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), mode=0o700, exist_ok=True)
        os.replace(LEGACY_SNAPSHOT_PATH, SNAPSHOT_PATH)
        os.chmod(SNAPSHOT_PATH, 0o600)
    try:
        os.rmdir(os.path.dirname(LEGACY_SNAPSHOT_PATH))
    except OSError:
        pass
    print(f"access group: snapshot moved to {SNAPSHOT_PATH} (the old path was publicly served)")


def _read_snapshot():
    """The snapshot, or None when there is none. Raises ValueError when the
    file exists but is unreadable or incomplete — the callers then change
    nothing: without it neither a restore nor a safe addition is possible."""
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise ValueError(f"{SNAPSHOT_PATH} is unreadable: {e}")
    if (not isinstance(data, dict) or data.get("complete") is not True
            or not isinstance(data.get("apps"), dict)):
        raise ValueError(f"{SNAPSHOT_PATH} is incomplete")
    return data


def _write_snapshot(data):
    """Atomic: a crash leaves the previous file (or none), never a partial one."""
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), mode=0o700, exist_ok=True)
    tmp = SNAPSHOT_PATH + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, SNAPSHOT_PATH)


def _capture(app):
    return {"mode": app.policy_engine_mode,
            "description": getattr(app, "meta_description", "") or "",
            "bindings": {str(b.pk): bool(b.enabled) for b in PolicyBinding.objects.filter(target_id=app.pk)}}


def _unmarked(description):
    return (description or "").replace(" " + ACCESS_MARKER, "").replace(ACCESS_MARKER, "")


def _loud(msg):
    print(msg)
    print(msg, file=sys.stderr)


def ensure_access_group_restriction():
    """A1 D10 — RAZZFAZZ_ACCESS_GROUP restricts EVERY Application to one group.

    Set: the pre-state of every app not yet restricted is added to the snapshot
    (SNAPSHOT_PATH, never overwritten while a restriction is active), then each
    app gets the marker in its description, policy_engine_mode `all`, two
    expression-policy bindings — the gate (`razzfazz.ai - Box access group`,
    order -100) and its own `razzfazz.ai - App access: <slug>` (order 0: access
    group AND any of the app's groups; all of them for an app that was in mode
    `all`; the app's user and policy grants folded in too) — and every other
    binding of the app disabled. Each
    step only ever narrows access until the final state is reached.
    Empty: every app in the snapshot gets back exactly its recorded mode,
    description and binding flags; a restricted app the snapshot does not know
    (a per-instance agent app created restricted) gets the default: its
    bindings on, mode `any`. Then our policies and the snapshot are deleted. A
    restricted box WITHOUT a snapshot is left as it is, with a WARNING.
    Not passed at all: nothing changes (a hand-run without `-e` must not lift
    a canary box's restriction).

    Refuses — changes NOTHING — when the named group does not exist or has no
    members (a gate on it blocks every user), or when the snapshot is
    unreadable. A box already restricted stays restricted to the group it had.

    Only Application targets are touched. Authentik's admin interface and its
    recovery path (`ak create_recovery_key`, `rzfz auth-recovery`) are not
    Applications, so a Super Admin outside the group keeps both; the login
    flows (and admin MFA on them) are never touched.

    Returns "not-passed" | "unrestricted" | "restricted" | "missing-group" |
    "empty-group" | "bad-snapshot" | "no-snapshot" | "error".
    """
    from authentik.policies.expression.models import ExpressionPolicy

    raw = os.environ.get("RAZZFAZZ_ACCESS_GROUP")
    if raw is None:
        print("[i] access group: RAZZFAZZ_ACCESS_GROUP not passed — the restriction is left as it is.")
        return "not-passed"
    group = raw.strip()
    try:
        _migrate_snapshot()
    except OSError as e:
        _loud(f"ERROR access group: cannot move {LEGACY_SNAPSHOT_PATH} to {SNAPSHOT_PATH} ({e}) — "
              f"NOTHING was restricted or restored. {_NOT_WRITABLE_HINT}")
        return "bad-snapshot"
    try:
        snap = _read_snapshot()
    except ValueError as e:
        _loud(f"ERROR access group: {e} — NOTHING was restricted or restored. Restore the file from "
              f"a backup, or move it aside once every app's bindings have been checked by hand.")
        return "bad-snapshot"
    apps = {a.slug: a for a in Application.objects.all()}
    restricted = []
    for slug, app in apps.items():
        if _app_restricted(app) or PolicyBinding.objects.filter(
                target_id=app.pk, policy__name=APP_ACCESS_POLICY_PREFIX + slug).exists():
            restricted.append(slug)

    if group:
        target = Group.objects.filter(name=group).first()
        if target is None:
            _loud(f"ERROR access group: '{group}' does not exist in Authentik — NOTHING was restricted "
                  f"or changed. Create the group or correct RAZZFAZZ_ACCESS_GROUP, then run: "
                  f"rzfz post-install --refresh")
            return "missing-group"
        members = set(target.users.values_list("pk", flat=True))
        if not members:
            _loud(f"ERROR access group: '{group}' has no members — restricting to it would block every "
                  f"user, so NOTHING was restricted or changed. Add the users first.")
            return "empty-group"
        admins = Group.objects.filter(name=SUPER_ADMINS_GROUP).first()
        if admins is not None and not members & set(admins.users.values_list("pk", flat=True)):
            print(f"WARNING access group: no Super Admin is a member of '{group}' — admins keep "
                  f"Authentik's admin interface (https://auth.<domain>/if/admin/) but no application.")
        # The snapshot first: nothing is touched until the pre-state is on disk.
        # Only apps not restricted yet are added — a restricted app's current
        # state is not its pre-state — and a recorded app is never re-recorded.
        fresh = snap is None
        snap = snap or {"version": 1, "complete": True, "apps": {}}
        new = {slug: _capture(app) for slug, app in apps.items()
               if slug not in snap["apps"] and slug not in restricted}
        if new or fresh:
            snap["apps"].update(new)
            try:
                _write_snapshot(snap)
            except OSError as e:
                _loud(f"ERROR access group: the snapshot location is not writable: cannot write "
                      f"{SNAPSHOT_PATH} ({e}) — NOTHING was restricted. {_NOT_WRITABLE_HINT}")
                return "error"
    elif snap is None:
        if restricted:
            print(f"WARNING access group: {', '.join(sorted(restricted))} still carry the restriction but "
                  f"there is no snapshot ({SNAPSHOT_PATH}) to restore them from — NOTHING was changed. "
                  f"Restore the file from a backup, or reset these apps' bindings and mode by hand.")
            return "no-snapshot"
        return "unrestricted"

    # The grants folded into each app's own policy — groups, users, policies,
    # each with its binding's `negate`: every binding that STILL EXISTS and
    # was ON in the recorded pre-state, plus bindings created since (by
    # ensure_binding while restricted). A binding that was disabled before the
    # apply must not start admitting anyone. The live `enabled` flag carries
    # no meaning while restricted — the restriction switched every binding
    # off itself — so a grant is revoked by deleting its binding; a hand-made
    # flag change is overwritten by the next apply and discarded at clear.
    app_groups, app_extra = {}, {}
    for slug, app in apps.items():
        recorded = snap["apps"].get(slug, {}).get("bindings", {}) if snap else {}
        groups, neg_groups, users, policies = set(), set(), set(), set()
        for b in PolicyBinding.objects.filter(target_id=app.pk):
            name = getattr(getattr(b, "policy", None), "name", None)
            if name in (GATE_POLICY, APP_ACCESS_POLICY_PREFIX + slug) or not recorded.get(str(b.pk), True):
                continue
            neg = bool(getattr(b, "negate", False))
            if getattr(b, "group", None) is not None:
                (neg_groups if neg else groups).add(b.group.name)
            elif getattr(b, "user", None) is not None:
                users.add((b.user.pk, neg))
            elif name is not None:
                policies.add((name, neg))
        app_groups[slug] = sorted(groups)
        app_extra[slug] = (sorted(users), sorted(policies), sorted(neg_groups))
    all_of = {slug for slug, pre in (snap["apps"] if snap else {}).items() if pre.get("mode") == "all"}
    status = "restricted" if group else "unrestricted"
    todo = plan_access_restriction(group, app_groups,
                                   restricted if group else set(restricted) | (set(snap["apps"]) & set(apps)),
                                   all_of)
    for step in todo:
        app = apps[step["app"]]
        own = APP_ACCESS_POLICY_PREFIX + app.slug
        try:
            if step.get("revert"):
                _revert_app(app, own, snap["apps"].get(app.slug))
                continue
            users, policies, neg_groups = app_extra[app.slug]
            gate_expr, own_expr = access_policy_expressions(step["app_groups"], step["restrict_to"],
                                                            step.get("require_all", False), users, policies,
                                                            neg_groups)
            gate, _ = ExpressionPolicy.objects.update_or_create(name=GATE_POLICY,
                                                                 defaults={"expression": gate_expr})
            policy, _ = ExpressionPolicy.objects.update_or_create(name=own, defaults={"expression": own_expr})
            desc = getattr(app, "meta_description", "") or ""
            if app.policy_engine_mode != "all" or ACCESS_MARKER not in desc:
                app.policy_engine_mode = "all"
                app.meta_description = desc if ACCESS_MARKER in desc else (desc + " " + ACCESS_MARKER).strip()
                app.save(update_fields=["policy_engine_mode", "meta_description"])
            PolicyBinding.objects.update_or_create(policy=gate, target_id=app.pk,
                                                   defaults={"order": -100, "enabled": True})
            PolicyBinding.objects.update_or_create(policy=policy, target_id=app.pk,
                                                   defaults={"order": 0, "enabled": True})
            # Every other binding off — its grant lives in the app's own policy now.
            PolicyBinding.objects.filter(target_id=app.pk).exclude(
                policy__name__in=(GATE_POLICY, own)).update(enabled=False)
            print(f"access group: {app.slug} restricted to {step['restrict_to']!r} "
                  f"(own groups kept in {own!r})")
        except Exception as e:  # noqa: BLE001 — one app must not stop the others
            _loud(f"ERROR access group: {app.slug}: {e}")
            status = "error"
    if not group and status != "error":
        ExpressionPolicy.objects.filter(name=GATE_POLICY).delete()
        ExpressionPolicy.objects.filter(name__startswith=APP_ACCESS_POLICY_PREFIX).delete()
        try:
            os.remove(SNAPSHOT_PATH)
        except FileNotFoundError:
            pass
        except OSError as e:
            _loud(f"ERROR access group: the snapshot location is not writable: cannot delete "
                  f"{SNAPSHOT_PATH} ({e}) — the apps are restored, but the snapshot remains. "
                  f"{_NOT_WRITABLE_HINT}")
            status = "error"
    return status


def _revert_app(app, own, pre):
    """Lift the restriction from one app. With its snapshot entry `pre`: exactly
    the recorded binding flags, mode and description. Without one (an app that
    was born restricted): group bindings on, mode `any`, marker removed. Every
    intermediate state is narrower than the end state: bindings are switched
    on under `all`, ours removed, and only then the mode goes back."""
    recorded = (pre or {}).get("bindings", {})
    for b in PolicyBinding.objects.filter(target_id=app.pk):
        if getattr(b.policy, "name", None) in (GATE_POLICY, own):
            continue
        # Recorded: its recorded flag. Not recorded: created while restricted
        # (by ensure_binding, the agent manager or by hand) and switched off by
        # the apply, so on.
        want = recorded.get(str(b.pk), True)
        if bool(b.enabled) != want:
            b.enabled = want
            b.save()
    PolicyBinding.objects.filter(target_id=app.pk, policy__name__in=(GATE_POLICY, own)).delete()
    mode = (pre or {}).get("mode", "any")
    desc = (pre or {}).get("description", _unmarked(getattr(app, "meta_description", "")))
    if app.policy_engine_mode != mode or (getattr(app, "meta_description", "") or "") != desc:
        app.policy_engine_mode = mode
        app.meta_description = desc
        app.save(update_fields=["policy_engine_mode", "meta_description"])
    print(f"access group: {app.slug} restriction removed "
          f"({'restored from the snapshot' if pre else 'group bindings on, mode any'})")


#: Modules the stack no longer ships (2026.11: OpenHands). On a box that ran
#: one, every pass deletes what its blueprint had created: the application (its
#: tile in the user's app library), its forward-auth provider (so the outpost
#: attach below cannot pick it up again), its access group and the blueprint
#: instance. Nothing else: no user and no other group is touched. Idempotent.
RETIRED_MODULES = {
    "openhands": {
        "provider": "Caddy Forward Auth Provider for OpenHands",
        "group": "razzfazz.ai OpenHands Users",
        "blueprint": "Module - OpenHands AI Agent",
    },
}


def remove_retired_modules(retired=None):
    """Delete the Authentik objects of the modules in RETIRED_MODULES.

    Returns the list of "<kind> <name>" strings it deleted (empty when there
    was nothing left, which is the case on every pass after the first)."""
    from authentik.blueprints.models import BlueprintInstance
    from authentik.providers.proxy.models import ProxyProvider
    removed = []
    for slug, spec in (RETIRED_MODULES if retired is None else retired).items():
        for kind, qs in (
            ("blueprint", BlueprintInstance.objects.filter(name=spec["blueprint"])),
            ("application", Application.objects.filter(slug=slug)),
            ("provider", ProxyProvider.objects.filter(name=spec["provider"])),
            ("group", Group.objects.filter(name=spec["group"])),
        ):
            try:
                if qs.exists():
                    qs.delete()
                    removed.append(f"{kind} {slug}")
            except Exception as e:  # noqa: BLE001 - one object never blocks the rest
                print(f"retired module {slug}: could not delete its {kind}: {e}")
    for item in removed:
        print(f"retired module: deleted {item}")
    return removed


def ensure_outpost_provider(provider_name):
    """Ensure a proxy provider is attached to the embedded outpost."""
    try:
        from authentik.outposts.models import Outpost
        from authentik.providers.proxy.models import ProxyProvider
        outpost = Outpost.objects.filter(name="authentik Embedded Outpost").first()
        if not outpost:
            print(f"Embedded outpost not found, skipping provider {provider_name}")
            return
        provider = ProxyProvider.objects.filter(name=provider_name).first()
        if not provider:
            print(f"Provider {provider_name} not found, skipping")
            return
        if provider in outpost.providers.all():
            print(f"Provider {provider_name} already in outpost")
        else:
            outpost.providers.add(provider)
            outpost.save()
            print(f"Added provider {provider_name} to outpost")
    except Exception as e:
        print(f"Error adding provider {provider_name} to outpost: {e}")


def ensure_embedded_outpost_host():
    """#70 — Set the embedded outpost's config.authentik_host.

    On a CLEAN install the embedded outpost ships with an empty
    `config.authentik_host`. The forward-auth flow (and the proxy
    outpost's external-host derivation) then falls back to the listen
    address, so SSO redirects land on http://0.0.0.0:9000 and login is
    impossible on every fresh box. The AUTHENTIK_HOST_BROWSER env does
    NOT drive the embedded outpost — only this config field does.

    We read the canonical browser-facing host from the worker's own
    AUTHENTIK_HOST env (compose sets it to https://${AUTHENTIK_DOMAIN}),
    normalise it to a trailing-slash URL, and write it into the outpost
    config blob. Idempotent — a no-op when already correct.
    """
    try:
        from authentik.outposts.models import Outpost

        # Prefer the explicit browser host; fall back to AUTHENTIK_DOMAIN.
        host = (os.environ.get("AUTHENTIK_HOST")
                or os.environ.get("AUTHENTIK_HOST_BROWSER")
                or "").strip()
        if not host:
            domain = os.environ.get("AUTHENTIK_DOMAIN", "").strip()
            if domain:
                host = f"https://{domain}"
        if not host:
            print("ensure_embedded_outpost_host: no AUTHENTIK_HOST/AUTHENTIK_DOMAIN "
                  "in env — cannot set embedded-outpost host (SSO may redirect to 0.0.0.0).")
            return
        # Normalise to a single trailing slash (the outpost config expects a base URL).
        host = host.rstrip("/") + "/"

        # Match the embedded outpost robustly: its managed id ends with
        # "/embedded" and its name contains "Embedded".
        outpost = (
            Outpost.objects.filter(managed__endswith="embedded").first()
            or Outpost.objects.filter(name__icontains="Embedded").first()
            or Outpost.objects.filter(name="authentik Embedded Outpost").first()
        )
        if not outpost:
            print("ensure_embedded_outpost_host: embedded outpost not found — skipping.")
            return

        cfg = dict(outpost._config or {})
        current = cfg.get("authentik_host", "")
        current_browser = cfg.get("authentik_host_browser", "")
        # authentik_host_browser must stay in lockstep with authentik_host. On a
        # MAIN_DOMAIN change the blueprint re-template already fixes authentik_host,
        # so `current == host` was true and this function returned EARLY — leaving a
        # STALE authentik_host_browser on the OLD domain, which 302s the OIDC login
        # to the old host -> ERR_SSL_PROTOCOL_ERROR (2026-08 Profida re-domain bug).
        # Consider BOTH before the early return, and pin both to the public host.
        if current == host and current_browser == host:
            print(f"ensure_embedded_outpost_host: already set to {host}")
            return
        cfg["authentik_host"] = host
        cfg["authentik_host_browser"] = host
        outpost._config = cfg
        outpost.save(update_fields=["_config"])
        print(f"ensure_embedded_outpost_host: set authentik_host/browser -> {host!r} "
              f"(was {current!r} / {current_browser!r})")
    except Exception as e:
        print(f"Error setting embedded-outpost authentik_host: {e}")


if __name__ == "__main__":
    print("Applying Access Policy Bindings...")

    # 2026.11: before any binding or outpost attach, so a removed module's
    # provider is never re-attached to the outpost.
    remove_retired_modules()

    # M028 — Start Portal must be reachable by EVERY authenticated user (Authentik's
    # post-login flow redirects them to start.<domain>; if they aren't authorized for
    # the `start` Application the flow returns "Erlaubnis verweigert" / Access Denied
    # — caught on 0.78 with a non-admin `tester` user 2026-05-08).
    # An Application with policy_engine_mode=any AND zero PolicyBindings is open to
    # every authenticated user, which is exactly what we want for `start`. Wipe any
    # leftover binding that was created by the previous "Super Admins, order=0" rule.
    try:
        from authentik.core.models import Application as _A
        from authentik.policies.models import PolicyBinding as _PB
        _start = _A.objects.filter(slug="start").first()
        if _start:
            # A1 D10: the access-group restriction's own bindings stay; wiping
            # them would open `start` to everyone until the end of this run.
            wiped, _ = _PB.objects.filter(target_id=_start.pk).exclude(
                policy__name__in=(GATE_POLICY, APP_ACCESS_POLICY_PREFIX + "start")).delete()
            if wiped:
                print(f"M028 — wiped {wiped} stale PolicyBinding(s) on `start` (now open to every authenticated user).")
    except Exception as _e:
        print(f"Could not wipe `start` bindings: {_e}")

    # Chat
    ensure_binding("chat", "razzfazz.ai Chat Users", 0)
    ensure_binding("chat", "razzfazz.ai Super Admins", 1)
    
    # Workflow
    ensure_binding("workflow-automation", "razzfazz.ai Workflow Automation Users", 0)
    ensure_binding("workflow-automation", "razzfazz.ai Super Admins", 1)
    
    # LLM Management
    ensure_binding("llm-management", "razzfazz.ai LLM Management Users", 0)
    ensure_binding("llm-management", "razzfazz.ai Super Admins", 1)

    # LLM Manager console (#843, #842 follow-up) — three-tier RBAC (User/
    # Admin/Super Admin). Without these bindings only Super Admins reach
    # llm-manager.<domain> at all, so the ADMIN/USER tiers require_role()
    # gates on in app/authz.py are unreachable by anyone else.
    ensure_binding("llm-manager", "razzfazz.ai LLM Users", 0)
    ensure_binding("llm-manager", "razzfazz.ai LLM Admins", 1)
    ensure_binding("llm-manager", "razzfazz.ai Super Admins", 2)

    # Fleet Hub — the registry management UI (#1652). Same family, same gate as
    # the LLM Manager itself: whoever may administer LLM infrastructure may
    # administer its images. Deliberately NOT its own new group — a group
    # nobody is ever put into is a gate that only looks like one, and #1635 is
    # what that costs.
    ensure_binding("fleet-hub", "razzfazz.ai LLM Admins", 0)
    ensure_binding("fleet-hub", "razzfazz.ai Super Admins", 1)

    # Administration
    ensure_binding("administration", "razzfazz.ai Docker Management Users", 0)
    ensure_binding("administration", "razzfazz.ai Super Admins", 1)
    
    # Backup
    ensure_binding("backup", "razzfazz.ai Docker Management Users", 0)
    ensure_binding("backup", "razzfazz.ai Super Admins", 1)

    # Licenses
    ensure_binding("licenses", "razzfazz.ai Chat Users", 0)
    ensure_binding("licenses", "razzfazz.ai Workflow Automation Users", 1)
    ensure_binding("licenses", "razzfazz.ai LLM Management Users", 2)
    ensure_binding("licenses", "razzfazz.ai Docker Management Users", 3)
    ensure_binding("licenses", "razzfazz.ai Super Admins", 4)
    
    # Gitea (Git)
    ensure_binding("gitea", "razzfazz.ai Git Users", 0)
    ensure_binding("gitea", "razzfazz.ai Super Admins", 1)

    # #2507: the native-OIDC applications get the SAME gate as the forward-auth
    # app of the same service — otherwise any Authentik user can complete an
    # OIDC login into Open WebUI / Gitea (policy_engine_mode any, no binding).
    # Prod was bound by hand exactly like this on 2026-09-26.
    ensure_binding("openwebui-oidc", "razzfazz.ai Chat Users", 0)
    ensure_binding("openwebui-oidc", "razzfazz.ai Super Admins", 1)
    ensure_binding("gitea-oidc", "razzfazz.ai Git Users", 0)
    ensure_binding("gitea-oidc", "razzfazz.ai Super Admins", 1)

    # LightRAG (RAG Knowledge Base)
    ensure_binding("lightrag", "razzfazz.ai RAG Users", 0)
    ensure_binding("lightrag", "razzfazz.ai Super Admins", 1)

    # Cognee (GraphRAG Knowledge Engine)
    ensure_binding("cognee", "razzfazz.ai Cognee Users", 0)
    ensure_binding("cognee", "razzfazz.ai Super Admins", 1)

    # AI Agents — agent-manager dashboard (admin-style entry)
    ensure_binding("agents", "razzfazz.ai AI Agents Users", 0)
    ensure_binding("agents", "razzfazz.ai Super Admins", 1)
    # NB: the per-instance agent subdomains (opencode-<token>.agents.<domain>, …)
    # are gated by per-instance forward_single providers that agent-manager
    # registers/deregisters dynamically at provision/stop (see
    # app/services/authentik_client.py) — NOT by a static app binding here.

    # Crawl4AI — RAG-friendly web crawler
    ensure_binding("crawl4ai", "razzfazz.ai Crawl4AI Users", 0)
    ensure_binding("crawl4ai", "razzfazz.ai Super Admins", 1)

    # Docling (Document Converter)
    ensure_binding("docling", "razzfazz.ai Docling Users", 0)
    ensure_binding("docling", "razzfazz.ai Super Admins", 1)

    # Element Web — Matrix browser client
    ensure_binding("element-web", "razzfazz.ai Matrix Users", 0)
    ensure_binding("element-web", "razzfazz.ai Super Admins", 1)
    # #2681: Synapse's OIDC app — the same gate as Element Web. It is the only
    # gate on matrix.<domain>, which any Matrix client can sign in to.
    ensure_binding("synapse", "razzfazz.ai Matrix Users", 0)
    ensure_binding("synapse", "razzfazz.ai Super Admins", 1)

    # MCP & Agent Manager — the single user-facing combined app (slug
    # my-agents; renamed from "My Agents" in #36/#61). Same provider as
    # `agents`; the combined dashboard at AGENTS_DOMAIN manages BOTH agents
    # and MCP integrations. Gated by the single "AI Agents Users" group.
    ensure_binding("my-agents", "razzfazz.ai AI Agents Users", 0)
    ensure_binding("my-agents", "razzfazz.ai Super Admins", 1)

    # MCP Manager (backend forward-auth, slug `mcp`) — #36/#61. Hidden,
    # tile-less app that only exists so mcp.<domain> is SSO-gated + routed
    # through the outpost. Bound to the SAME single group as the combined
    # app so a user in "AI Agents Users" who follows the "MCP Integrations"
    # link from the combined dashboard is allowed through, and a user NOT in
    # the group is denied at mcp.<domain> exactly as at the agent dashboard.
    # (No-op when the `mcp` app is absent — profile inactive.)
    ensure_binding("mcp", "razzfazz.ai AI Agents Users", 0)
    ensure_binding("mcp", "razzfazz.ai Super Admins", 1)
    # #2472 (operator decision 2026-11-16): a chat-only person connects the
    # personal MCP servers Open WebUI shows them (Gitea, cognee-personal) on
    # mcp.<domain>, so Chat Users reach it too. The mcp-manager authorizes per
    # caller (the Authentik sub): a person sees and stores only their own
    # connections; its admin pages stay behind the catalogue's mcp-manager
    # admin role. The combined agent dashboard (my-agents) stays agents-only.
    ensure_binding("mcp", "razzfazz.ai Chat Users", 2)

    # Observability — OpenLIT LLM observability
    ensure_binding("observability", "razzfazz.ai Observability Users", 0)
    ensure_binding("observability", "razzfazz.ai Super Admins", 1)

    # Paperclip — AI company orchestration
    ensure_binding("paperclip", "razzfazz.ai Paperclip Users", 0)
    ensure_binding("paperclip", "razzfazz.ai Super Admins", 1)

    # Stirling-PDF (PDF Tools)
    ensure_binding("stirling-pdf", "razzfazz.ai Stirling-PDF Users", 0)
    ensure_binding("stirling-pdf", "razzfazz.ai Super Admins", 1)

    # F-040: Policy bindings for services that were missing them
    # Paperless-ngx — document management
    ensure_binding("paperless-ngx", "razzfazz.ai Paperless Users", 0)
    ensure_binding("paperless-ngx", "razzfazz.ai Super Admins", 1)

    # Vaultwarden — password manager (OIDC app, not forward-auth, but still needs binding)
    ensure_binding("vaultwarden", "razzfazz.ai Vaultwarden Users", 0)
    ensure_binding("vaultwarden", "razzfazz.ai Super Admins", 1)

    # Infisical — secrets management
    ensure_binding("infisical", "razzfazz.ai Infisical Users", 0)
    ensure_binding("infisical", "razzfazz.ai Super Admins", 1)

    # Onyx — AI enterprise search
    ensure_binding("onyx", "razzfazz.ai Onyx Users", 0)
    ensure_binding("onyx", "razzfazz.ai Super Admins", 1)

    # OpenUEM — unified endpoint management (#1075)
    ensure_binding("openuem", "razzfazz.ai OpenUEM Users", 0)
    ensure_binding("openuem", "razzfazz.ai Super Admins", 1)

    # Wazuh — SIEM/XDR security monitoring (#855)
    ensure_binding("wazuh", "razzfazz.ai Wazuh Users", 0)
    ensure_binding("wazuh", "razzfazz.ai Super Admins", 1)

    # OpenBao — secrets management (#2476). BOTH applications: the proxy app
    # gates bao.<domain>, and the OIDC app is what OpenBao's own sign-in
    # (UI and CLI) authorizes against; an unbound OAuth2 app is open to every
    # Authentik user (#2090). The groups are the role catalogue's
    # openbao/admin ∪ openbao/user, literal here because authentik-worker has
    # no catalogue mount (test_2476_openbao_blueprint.py holds them equal).
    ensure_binding("openbao", "razzfazz.ai Super Admins", 0)
    ensure_binding("openbao", "razzfazz.ai AI Agents Users", 1)
    ensure_binding("openbao", "razzfazz.ai LLM Users", 2)
    ensure_binding("openbao-oidc", "razzfazz.ai Super Admins", 0)
    ensure_binding("openbao-oidc", "razzfazz.ai AI Agents Users", 1)
    ensure_binding("openbao-oidc", "razzfazz.ai LLM Users", 2)

    # Attach ALL proxy providers to the embedded outpost (idempotent).
    # This ensures every forward-auth provider works after blueprints re-apply
    # or after a profile toggle adds new providers.
    #
    # rc6.7 #41 / rc6.8 fix: previous implementation called
    # `outpost.providers.all()` inside the loop, which could miss newly-added
    # providers due to QuerySet caching. We snapshot the attached set once
    # via a primary-key query, diff against the full provider set, and bulk-
    # add the missing ones.
    #
    # M028 S05-redo / 2026-05-09 fix: race condition. Authentik's blueprint
    # controller processes blueprints asynchronously after the worker boots.
    # When init-authentik.sh re-applies blueprints (INIT_VERSION bump) it
    # only sleeps 15s before invoking this script — which can be too short
    # on slower boxes, leaving late-arriving ProxyProviders un-attached and
    # the matching subdomains 404'ing from the outpost. Caught on 0.78 with
    # 15/25 providers orphaned after an upgrade.
    # Fix: poll until the provider count is stable across two consecutive
    # 15-second intervals, then attach. Bounded by a max-iteration budget
    # so a perpetual blueprint-apply loop can't hang the script forever.
    try:
        from authentik.outposts.models import Outpost
        from authentik.providers.proxy.models import ProxyProvider
        outpost = Outpost.objects.filter(name="authentik Embedded Outpost").first()
        if outpost:
            prev_count = -1
            stable_iters = 0
            for attempt in range(8):  # 8 * 15s = 2 min ceiling
                cur_count = ProxyProvider.objects.count()
                if cur_count == prev_count:
                    stable_iters += 1
                    if stable_iters >= 2:
                        break
                else:
                    stable_iters = 0
                    prev_count = cur_count
                print(
                    f"Waiting for blueprint controller to settle… "
                    f"{cur_count} ProxyProviders so far (iter {attempt+1}/8)"
                )
                time.sleep(15)

            outpost.refresh_from_db()
            attached_ids = set(
                outpost.providers.values_list("pk", flat=True)
            )
            all_providers = list(ProxyProvider.objects.all())
            missing = [p for p in all_providers if p.pk not in attached_ids]
            if missing:
                outpost.providers.add(*missing)
                outpost.save()
                # Touch _config to force outpost-controller reload
                cfg = dict(outpost._config or {})
                cfg["__razzfazz_attach_rev"] = int(time.time())
                outpost._config = cfg
                outpost.save(update_fields=["_config"])
                for p in missing:
                    print(f"Added provider to outpost: {p.name} (id={p.pk})")
            else:
                print(
                    f"All {len(all_providers)} proxy providers already "
                    f"attached to embedded outpost"
                )

            # 2026-05-09: verify-and-reattach loop. Authentik's own
            # `outpost_controller` task runs asynchronously and rebuilds the
            # outpost.providers m2m from its own config view; if our attach
            # ran concurrently, the controller's write can clobber our
            # additions. Caught on 0.91 during the bootstrap upgrade — 8 of
            # 25 providers we attached during init were detached again
            # within seconds. Wait, verify, re-attach if drift detected.
            expected_ids = {p.pk for p in all_providers}
            for verify_round in range(3):
                time.sleep(20)
                outpost.refresh_from_db()
                still_attached = set(outpost.providers.values_list("pk", flat=True))
                drift = expected_ids - still_attached
                if not drift:
                    if verify_round > 0:
                        print(
                            f"Outpost m2m stable after {verify_round + 1} "
                            f"verify round(s)."
                        )
                    break
                drift_list = [p for p in all_providers if p.pk in drift]
                print(
                    f"Outpost m2m drift detected (round {verify_round + 1}): "
                    f"{len(drift_list)} providers were detached after the "
                    f"initial attach. Re-attaching..."
                )
                outpost.providers.add(*drift_list)
                outpost.save()
                cfg = dict(outpost._config or {})
                cfg["__razzfazz_attach_rev"] = int(time.time())
                outpost._config = cfg
                outpost.save(update_fields=["_config"])
                for p in drift_list:
                    print(f"  re-attached: {p.name} (id={p.pk})")
        else:
            print("Embedded outpost not found — skipping provider attachment")
    except Exception as e:
        print(f"Error attaching providers to outpost: {e}")

    # #70 — Set the embedded outpost's config.authentik_host so the forward-auth
    # SSO flow redirects to https://auth.<domain>/ instead of http://0.0.0.0:9000
    # (empty on a clean install → login impossible on every fresh box). Idempotent.
    ensure_embedded_outpost_host()

    # NB: the legacy `setup` Application (setup.<domain> first-run wizard) was
    # REMOVED in 2026.07 (#22 — first-run is now `rzfz init`, dangerous ops are
    # `rzfz setup`). Binding it here made get_app_with_retry("setup") spin the
    # full 12×5s = 60s retry budget on EVERY post-install / reconcile run and
    # then print the misleading "App setup could not be found after waiting." — a
    # confusing stall on a clean box. Do NOT re-add a `setup` binding.

    # Configuration UI (Super Admins only)
    ensure_binding("config", "razzfazz.ai Super Admins", 0)

    # Help Center (all authenticated users)
    ensure_binding("help", "razzfazz.ai Chat Users", 0)
    ensure_binding("help", "razzfazz.ai Workflow Automation Users", 1)
    ensure_binding("help", "razzfazz.ai LLM Management Users", 2)
    ensure_binding("help", "razzfazz.ai Docker Management Users", 3)
    ensure_binding("help", "razzfazz.ai Git Users", 4)
    ensure_binding("help", "razzfazz.ai Super Admins", 5)

    # #1148: the superuser name comes from the environment — `rzfz-admin` on a
    # new install, `akadmin` on a box that predates the rename. The literal
    # stays as the FALLBACK, because this script also runs against boxes whose
    # .env has not migrated yet.
    admin_username = os.environ.get("RAZZFAZZ_ADMIN_USERNAME") or "akadmin"
    try:
        user = User.objects.filter(username=admin_username).first()
        if user is None and admin_username != "akadmin":
            user = User.objects.get(username="akadmin")   # not renamed (yet)
        elif user is None:
            raise User.DoesNotExist(admin_username)

        # #1148 review, finding 10: `rename_bootstrap_admin()` REFUSES when the
        # target name is already taken — deliberately, so it cannot clobber an
        # existing account. But then this block looks the same name up and
        # promotes whoever holds it into "razzfazz.ai Super Admins". A refused
        # rename would have handed full admin to an unrelated user who simply
        # registered as `rzfz-admin` first. The bootstrap superuser is the one
        # Authentik marks as such; anything else is somebody else.
        if not getattr(user, "is_superuser", False):
            print(f"REFUSING to promote {user.username!r}: it is not the "
                  f"bootstrap superuser. The rename was probably refused "
                  f"because that name was already taken (#1148) — rename the "
                  f"real superuser by hand, or point RAZZFAZZ_ADMIN_USERNAME "
                  f"at a free name.")
        else:
            admin_email = f"razzfazz-ai-admin@{os.environ.get('MAIN_DOMAIN', 'localhost')}"
            if user.email != admin_email:
                user.email = admin_email
                user.save(update_fields=["email"])
                print(f"Set {user.username} email to {admin_email}")
            group = get_group_with_retry("razzfazz.ai Super Admins")

            # Authentik Group (UUID) -> User M2M is likely via group.users
            if not group.users.filter(pk=user.pk).exists():
                group.users.add(user)
                print(f"Added {user.username} to razzfazz.ai Super Admins")
            else:
                print(f"{user.username} is already in razzfazz.ai Super Admins")
    except Exception as e:
        print(f"Error adding {admin_username} to Super Admins: {e}")

    # rc6.7 #26: prune retired Application + Provider entries (M020 → M023 cleanup).
    # Hermes / Moltis / Coding Tools migrated from stack-global apps to per-user
    # provisioning via the agent-manager + My Agents drawer. Their old global apps
    # remain in the DB on upgraded boxes and 502 because no upstream container exists.
    # Clean install (0.91, 0.92) was already correct; this fixes upgraded installs.
    from authentik.providers.proxy.models import ProxyProvider
    retired_app_slugs = ["moltis", "hermes-agent", "coding-tools"]
    for slug in retired_app_slugs:
        deleted, _ = Application.objects.filter(slug=slug).delete()
        if deleted:
            print(f"Pruned retired Application slug={slug}")
    retired_provider_names = [
        "Caddy Forward Auth Provider for Moltis",
        "Caddy Forward Auth Provider for Hermes",
        "Caddy Forward Auth Provider for Coding Tools",
    ]
    for name in retired_provider_names:
        deleted, _ = ProxyProvider.objects.filter(name=name).delete()
        if deleted:
            print(f"Pruned retired ProxyProvider name={name}")

    # B-11 — Tighter MFA policy default. Wires the
    # `razzfazz.ai - Require Admin MFA` expression policy (declared in
    # blueprints/base/07-mfa-policy.yaml) to the MFA-validation
    # flowstagebinding. Honours RAZZFAZZ_REQUIRE_ADMIN_MFA (default
    # `false`); see ensure_mfa_admin_binding() docstring.
    ensure_mfa_admin_binding()

    # A1 D10 — the staging canary: RAZZFAZZ_ACCESS_GROUP restricts every
    # Application to one group. LAST, after every ensure_binding() above, so
    # an app registered or bound during this run is covered too.
    access = ensure_access_group_restriction()

    print("Policy bindings applied.")
    if access in ("missing-group", "empty-group", "bad-snapshot", "error"):
        # Loud for the callers (post-install prints its "reported issues" warning).
        sys.exit(3)
