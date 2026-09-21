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
    "observability": "observability",
    "paperclip": "paperclip",
    "stirling-pdf": "stirling-pdf",
    "paperless-ngx": "paperless-ngx",
    "vaultwarden": "vaultwarden",
    "infisical": "infisical",
    "onyx": "onyx",
    "openhands": "openhands",
    "openuem": "openuem",
    "wazuh": "wazuh",
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

def ensure_binding(app_slug, group_name, order=0):
    if not _profile_active(app_slug):
        # #1465 skips the WAIT, not the binding. #2090: the blueprints register
        # every Application regardless of profile, and an Application with
        # ZERO PolicyBindings is open to every authenticated user, not denied
        # (measured on 0.91: llm.<domain> → 302 into the login flow). So a
        # registered app of an inactive profile is bound like any other — one
        # lookup each, no retry, so a box without the app or the group still
        # pays nothing (the five minutes #1465 was about).
        if not Application.objects.filter(slug=app_slug).exists():
            print(f"[i] {app_slug}: profile inactive on this box and no Application registered — skipping binding.")
            return
        if not Group.objects.filter(name=group_name).exists():
            print(f"[i] {app_slug}: profile inactive and group '{group_name}' absent — skipping binding (nothing to bind to).")
            return
        print(f"[i] {app_slug}: profile inactive but the Application is registered — binding it (an unbound app is unrestricted, #2090).")
    try:
        app = get_app_with_retry(app_slug)
        group = get_group_with_retry(group_name)
        
        # Check if binding exists
        existing = PolicyBinding.objects.filter(target_id=app.pk, group=group).first()
        if existing:
            print(f"Binding for {app_slug} -> {group_name} already exists.")
            if existing.order != order:
                existing.order = order
                existing.save()
            if not existing.enabled:
                existing.enabled = True
                existing.save()
        else:
            print(f"Creating binding for {app_slug} -> {group_name}")
            PolicyBinding.objects.create(
                target_id=app.pk,
                group=group,
                enabled=True,
                order=order
            )
    except Application.DoesNotExist:
        print(f"App {app_slug} could not be found after waiting.")
    except Group.DoesNotExist:
        print(f"Group {group_name} not found.")
    except Exception as e:
        print(f"Error binding {app_slug} -> {group_name}: {e}")

def ensure_mfa_admin_binding():
    """B-11 — Tighter MFA policy default (NIS2 21(2)(j)).

    Wires the `razzfazz.ai - Require Admin MFA` expression policy
    (declared in core/Authentik/blueprints/base/07-mfa-policy.yaml) to
    the existing `default-authentication-flow` ↔
    `default-authentication-mfa-validation` flowstagebinding. The
    declarative blueprint serializer can't easily express a nested
    `!Find` for a flowstagebinding's composite key (target+stage), so
    we wire it here with the Django ORM.

    The PolicyBinding's `enabled` field is sourced from
    `RAZZFAZZ_REQUIRE_ADMIN_MFA` (default `true` for new installs;
    documented in `.env.example`). Setting it to `false` flips the
    binding's `enabled` to False on the next init-authentik run; the
    policy + binding rows stay in the DB so flipping back to `true`
    later doesn't recreate IDs.

    Idempotent: re-running with the same env var value is a no-op
    (binding lookup is by (policy, target) — both stable).
    """
    from authentik.flows.models import Flow, FlowStageBinding
    from authentik.policies.expression.models import ExpressionPolicy
    from authentik.policies.models import PolicyBinding
    from authentik.stages.authenticator_validate.models import AuthenticatorValidateStage

    raw = os.environ.get("RAZZFAZZ_REQUIRE_ADMIN_MFA", "true").strip().lower()
    enabled = raw not in ("false", "0", "no", "off", "")
    try:
        policy = ExpressionPolicy.objects.filter(name="razzfazz.ai - Require Admin MFA").first()
        if not policy:
            print("B-11: Expression policy 'razzfazz.ai - Require Admin MFA' not found "
                  "(blueprint 07-mfa-policy.yaml may not have been applied yet).")
            return
        flow = Flow.objects.filter(slug="default-authentication-flow").first()
        if not flow:
            print("B-11: default-authentication-flow not found; skipping.")
            return
        stage = AuthenticatorValidateStage.objects.filter(name="default-authentication-mfa-validation").first()
        if not stage:
            print("B-11: default-authentication-mfa-validation stage not found; skipping.")
            return
        fsb = FlowStageBinding.objects.filter(target=flow, stage=stage).first()
        if not fsb:
            print("B-11: FlowStageBinding for MFA-validation not found on default flow; skipping.")
            return
        # M034 S04: PolicyBinding.target is an FK to PolicyBindingModel. In
        # Authentik 2026.2.x, FlowStageBinding's own pk DIFFERS from its
        # PolicyBindingModel parent pk (the MTI parent link is exposed as
        # `policybindingmodel_ptr_id`, NOT shared with `.pk` as in older
        # versions). Using `fsb.pk` therefore inserts a target_id that isn't in
        # authentik_policies_policybindingmodel → FK violation on a FRESH install
        # (S02 finding 2026-05-24). Use the parent-link pk, falling back to
        # fsb.pk on older Authentik where they coincide.
        target_pk = getattr(fsb, "policybindingmodel_ptr_id", None) or fsb.pk
        existing = PolicyBinding.objects.filter(policy=policy, target_id=target_pk).first()
        if existing:
            changed = False
            if existing.enabled != enabled:
                existing.enabled = enabled
                changed = True
            if existing.order != 0:
                existing.order = 0
                changed = True
            if changed:
                existing.save()
                print(f"B-11: Updated admin-MFA PolicyBinding "
                      f"(enabled={enabled}, RAZZFAZZ_REQUIRE_ADMIN_MFA={raw or 'unset (default true)'}).")
            else:
                print(f"B-11: admin-MFA PolicyBinding already in desired state "
                      f"(enabled={enabled}).")
        else:
            PolicyBinding.objects.create(
                policy=policy,
                target_id=target_pk,
                enabled=enabled,
                order=0,
                timeout=30,
            )
            print(f"B-11: Created admin-MFA PolicyBinding "
                  f"(enabled={enabled}, RAZZFAZZ_REQUIRE_ADMIN_MFA={raw or 'unset (default true)'}).")
    except Exception as e:
        print(f"B-11: Error wiring admin-MFA PolicyBinding: {e}")


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
            wiped, _ = _PB.objects.filter(target_id=_start.pk).delete()
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

    # OpenHands — AI software development agent
    ensure_binding("openhands", "razzfazz.ai OpenHands Users", 0)
    ensure_binding("openhands", "razzfazz.ai Super Admins", 1)

    # OpenUEM — unified endpoint management (#1075)
    ensure_binding("openuem", "razzfazz.ai OpenUEM Users", 0)
    ensure_binding("openuem", "razzfazz.ai Super Admins", 1)

    # Wazuh — SIEM/XDR security monitoring (#855)
    ensure_binding("wazuh", "razzfazz.ai Wazuh Users", 0)
    ensure_binding("wazuh", "razzfazz.ai Super Admins", 1)

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
    # `true`); see ensure_mfa_admin_binding() docstring.
    ensure_mfa_admin_binding()

    print("Policy bindings applied.")
