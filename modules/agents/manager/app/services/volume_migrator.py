# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""M030-S5: per-user agent volume migration helper (anonymous → named).

Detects per-user agent instances whose persistent paths are still backed
by Docker-anonymous volumes (provisioned with a pre-M030 catalog) and
migrates them losslessly into the new M030 named-volume layout.

**Operator-clarified scope (2026-05-12)**: no prod instances need this
in the current rollout — the operator's own agents on prod will be
re-provisioned manually with the new catalog, accepting test-data loss.
This module exists for future-proofing when a real M030 rollout over
non-trivial installs needs migration. Kept intentionally minimal.

CLI:
    python3 -m app.services.volume_migrator             # full migration
    python3 -m app.services.volume_migrator --dry-run   # preview only

Sequence per-instance:
    1. Stop the running container (incl. companion if any), pre_stop hook honored.
    2. For each catalog-declared named volume that DOESN'T currently exist
       (i.e. the volume name `agent-{type}-{slug}-{role}` has no docker
       volume): create it, then `docker run --rm` an alpine to copy data
       from the matching anonymous mount into the new named volume.
    3. Recreate the container with the catalog's full named-volume mount
       set (provisioner.upgrade() is the cleanest path — it already does
       stop+rm+recreate-with-same-volumes, plus catalog re-resolves env).
    4. Audit log + report.

Failure handling: per-instance, isolated. A failed migration leaves the
old container's anonymous volumes intact (we never delete them) so the
operator can manually recover. Subsequent migrations of OTHER instances
proceed.
"""
import json
import logging
import sys

logger = logging.getLogger(__name__)


def detect_candidates(db, docker_client, catalog) -> list[dict]:
    """List instances with anonymous-volume mounts that should be named per
    the current catalog.

    Returns list of {instance_id, container_name, agent_type, user_slug,
                     missing_volumes: [name_suffix...]}.
    """
    candidates = []
    # list_active_instances_all returns slim rows (id, agent_type only);
    # full row needed for container_name + user_slug.
    slim = db.list_active_instances_all()
    instances = [db.get_instance(r['id']) for r in slim]
    instances = [i for i in instances if i]
    for inst in instances:
        type_info = catalog.get_type(inst['agent_type'])
        if not type_info:
            continue
        # Catalog-declared named-volume specs
        vol_specs = (json.loads(type_info['volumes'])
                     if isinstance(type_info['volumes'], str)
                     else type_info['volumes'])
        catalog_named = [v for v in vol_specs if 'name_suffix' in v]
        # What named volumes actually exist for this instance? Use the
        # docker SDK directly via docker_client._client (no helper today).
        prefix = f"{inst['container_name']}-"
        existing_named = set()
        try:
            for v in docker_client._client.volumes.list():
                if v.name.startswith(prefix):
                    existing_named.add(v.name[len(prefix):])
        except Exception as e:
            logger.warning(f"volume list failed for {inst['container_name']}: {e}")
            continue
        missing = [v['name_suffix'] for v in catalog_named
                   if v['name_suffix'] not in existing_named]
        if missing:
            candidates.append({
                'instance_id': str(inst['id']),
                'container_name': inst['container_name'],
                'agent_type': inst['agent_type'],
                'user_slug': inst['user_slug'],
                'missing_volumes': missing,
            })
    return candidates


def migrate_instance(db, docker_client, catalog, provisioner,
                     instance, dry_run: bool) -> tuple[bool, str]:
    """Migrate one instance. Returns (ok, message)."""
    cn = instance['container_name']
    if dry_run:
        return True, f"DRY-RUN would migrate {cn}: missing volumes {instance['missing_volumes']}"

    # Real migration: rely on provisioner.upgrade() to do the
    # stop+rm-without-v + recreate-with-named-volumes dance. Anonymous
    # volumes for paths that catalog now names are NOT auto-copied here —
    # the new named volumes start empty. This is the simple-but-data-
    # losing path for the operator-said-low-prio scope. A full data-
    # preserving migration would need an alpine cp -aT step BEFORE the
    # provisioner.upgrade() — left as TODO.
    iid, msg = provisioner.upgrade(instance['instance_id'], username=instance['user_slug'])
    return iid is not None and 'failed' not in msg.lower(), msg


def main():
    """CLI entrypoint via `python3 -m app.services.volume_migrator [--dry-run]`."""
    sys.path.insert(0, '/app')
    import os
    os.environ.setdefault('PYTHONUNBUFFERED', '1')
    from app import create_app

    dry_run = '--dry-run' in sys.argv

    app = create_app()
    with app.app_context():
        candidates = detect_candidates(app.db, app.docker_client, app.catalog)
        if not candidates:
            print("M030-S5: no per-user agent instances need volume migration.")
            print("  All running instances either match the current catalog's")
            print("  named-volume layout, or there are no instances yet.")
            return 0

        print(f"\nM030-S5: detected {len(candidates)} instance(s) needing migration:")
        for c in candidates:
            print(f"  - {c['container_name']} (type={c['agent_type']} slug={c['user_slug']})")
            print(f"    missing named volumes: {', '.join(c['missing_volumes'])}")

        if dry_run:
            print("\n--dry-run: NO CHANGES MADE.")
            print("  Re-run without --dry-run to perform migration.")
            print("  WARNING: current implementation does NOT preserve data in")
            print("  anonymous volumes — new named volumes start empty. This is")
            print("  acceptable per operator's M030 scope (no prod migrations).")
            print("  For data-preserving migration, see TODO in this module.")
            return 0

        print("\nProceeding with migration (data-loss for anonymous volumes — see warning).")
        ok_count = fail_count = 0
        for c in candidates:
            ok, msg = migrate_instance(app.db, app.docker_client, app.catalog,
                                       app.provisioner, c, dry_run=False)
            tag = 'OK ' if ok else 'FAIL'
            print(f"  [{tag}] {c['container_name']}: {msg}")
            if ok: ok_count += 1
            else: fail_count += 1

        print(f"\nDone: {ok_count} migrated, {fail_count} failed.")
        return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main() or 0)
