"""token-only schema (O2) — full Phase-1 data model

Revision ID: 0002_token_only_schema
Revises: 0001_baseline
Create Date: 2026-08-10

Creates the consolidated TOKEN-ONLY data model (spec §6 + the operator's
2026-08-10 correction): entitlement (subscriptions/installations), fleet
(workers), catalog (models/model_artifacts/model_artifact_files/
model_presets), deployments (deployments/deployment_instances), and keys +
chargeback (cost_centers/api_keys/usage_events).

NO `model_pricing` table; `usage_events` has NO `cost`/currency column —
only prompt/completion/cached token counters. `api_keys` budget is a TOKEN
cap (`max_budget_tokens`), never money.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002_token_only_schema"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_UUID = pg.UUID(as_uuid=True)
_TS = sa.TIMESTAMP(timezone=True)
_UUID_DEFAULT = sa.text("gen_random_uuid()")
_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("subscription_number", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("plan", sa.Text()),
        sa.Column("seats", sa.Integer()),
        sa.Column("valid_until", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_table(
        "installations",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("subscription_id", _UUID, sa.ForeignKey("subscriptions.id"), nullable=False),
        sa.Column("box_fingerprint", sa.Text(), nullable=False),
        sa.Column("credential_ref", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("quota_bytes", sa.BigInteger()),
        sa.Column("last_seen", _TS),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_table(
        "workers",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("labels", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("last_heartbeat", _TS),
    )
    op.create_table(
        "models",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("repo_id", sa.Text()),
        sa.Column("revision", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("source_tier", sa.Text()),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_table(
        "model_artifacts",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("model_id", _UUID, sa.ForeignKey("models.id"), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("quant", sa.Text()),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("hardware_class", sa.Text()),
        sa.Column("variant_tag", sa.Text()),
        sa.Column("modalities", pg.ARRAY(sa.Text())),
        sa.Column("context_length", sa.Integer()),
        sa.Column("apple_silicon_optimized", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("source", sa.Text()),
    )
    op.create_table(
        "model_artifact_files",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column(
            "artifact_id", _UUID,
            sa.ForeignKey("model_artifacts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("blob_ref", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()),
    )
    op.create_table(
        "model_presets",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("model_id", _UUID, sa.ForeignKey("models.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("use_case", sa.Text()),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("params", pg.JSONB(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("warnings", pg.JSONB()),
    )
    op.create_table(
        "deployments",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("model_name", sa.Text(), nullable=False, unique=True),
        sa.Column("model_id", _UUID, sa.ForeignKey("models.id"), nullable=False),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("params", pg.JSONB(), nullable=False),
        sa.Column("quant_policy", pg.JSONB()),
        sa.Column("replicas", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("worker_selector", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("updated_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_table(
        "deployment_instances",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column(
            "deployment_id", _UUID,
            sa.ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("worker_id", _UUID, sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("artifact_id", _UUID, sa.ForeignKey("model_artifacts.id")),
        sa.Column("endpoint", sa.Text()),
        sa.Column("params_effective", pg.JSONB()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("started_at", _TS),
    )
    op.create_table(
        "cost_centers",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("team", sa.Text()),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", _UUID, primary_key=True, server_default=_UUID_DEFAULT),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("cost_center_id", _UUID, sa.ForeignKey("cost_centers.id"), nullable=False),
        sa.Column("allowed_models", pg.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("max_budget_tokens", sa.BigInteger()),
        sa.Column("budget_duration", pg.INTERVAL()),
        sa.Column("rpm_limit", sa.Integer()),
        sa.Column("tpm_limit", sa.Integer()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", _TS, nullable=False, server_default=_NOW),
        sa.Column("expires_at", _TS),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.Text()),
        sa.Column("api_key_id", _UUID, sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("cost_center_id", _UUID, sa.ForeignKey("cost_centers.id"), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("ts", _TS, nullable=False, server_default=_NOW),
    )
    op.create_index("ix_usage_events_cost_center_ts", "usage_events", ["cost_center_id", "ts"])
    op.create_index("ix_usage_events_api_key_ts", "usage_events", ["api_key_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_api_key_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_cost_center_ts", table_name="usage_events")
    op.drop_table("usage_events")
    op.drop_table("api_keys")
    op.drop_table("cost_centers")
    op.drop_table("deployment_instances")
    op.drop_table("deployments")
    op.drop_table("model_presets")
    op.drop_table("model_artifact_files")
    op.drop_table("model_artifacts")
    op.drop_table("models")
    op.drop_table("workers")
    op.drop_table("installations")
    op.drop_table("subscriptions")
