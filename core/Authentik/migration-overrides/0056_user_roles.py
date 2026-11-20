# ==============================================================================
# razzfazz.ai override of authentik_core.0056_user_roles
# ==============================================================================
# Upstream migration (shipped in Authentik 2025.12.x) invokes a RunPython
# data migration (`migrate_object_permissions`) that calls the LIVE Role
# model class from Python, not the historical model from Django's ProjectState.
# On any 2025.10.x → 2025.12+ upgrade this crashes because:
#   - The live Role class expects authentik_rbac_role.group_id (not yet added)
#   - The data-migration queries compare UUID columns to integer values
#
# This override keeps every schema operation intact so Django's migration
# graph is unchanged and downstream migrations see the expected state, but
# replaces the buggy RunPython body with a no-op. For a vanilla
# razzfazz.ai stack (all permissions are group-based in practice) this
# has no visible effect — no user/group had object-level permissions
# that would have been copied into the new Role system.
#
# Operators whose installation relies on Django-level per-user object
# permissions should follow the manual backfill procedure in
# docs/authentik-upgrade.md before the next reboot of authentik-server.
# ==============================================================================
from django.db import migrations, models


def migrate_object_permissions_noop(apps, schema_editor):
    """razzfazz.ai: replaced the upstream buggy data migration with a no-op.
    See file header comment for rationale."""
    return


class Migration(migrations.Migration):

    dependencies = [
        ("guardian", "0004_role_permissions"),
        ("authentik_core", "0055_groupancestor_groupparentagenode_group_parents"),
        ("authentik_rbac", "0008_alter_role_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="roles",
            field=models.ManyToManyField(
                blank=True, related_name="users", to="authentik_rbac.role"
            ),
        ),
        migrations.RunPython(migrate_object_permissions_noop),
        migrations.AlterUniqueTogether(
            name="group",
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name="group",
            name="parents",
            field=models.ManyToManyField(
                blank=True,
                related_name="children",
                through="authentik_core.GroupParentageNode",
                to="authentik_core.group",
            ),
        ),
        migrations.RemoveField(
            model_name="group",
            name="parent",
        ),
        migrations.AlterField(
            model_name="group",
            name="name",
            field=models.TextField(unique=True, verbose_name="name"),
        ),
    ]
