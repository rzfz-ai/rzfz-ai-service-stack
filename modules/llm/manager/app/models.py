# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""ORM models for the LLM Manager — O2, TOKEN-ONLY data model.

Consolidated from spec §6, WITH the operator's 2026-08-10 correction
(supersedes §6 `model_pricing` + all of §10): chargeback is TOKEN-ONLY.
There is **no `model_pricing` table** and **no `cost`/currency column** on
`usage_events`; per-key budgets are TOKEN/rpm/tpm caps, never money.

`cost_centers` remains purely a grouping label.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INTERVAL, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base


def _uuid_pk() -> Column:
    return Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _created_at() -> Column:
    return Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


# --- Entitlement (Phase-3 surface; tables present now as a stub) ------------
class Subscription(Base):
    __tablename__ = "subscriptions"
    id = _uuid_pk()
    subscription_number = Column(Text, nullable=False, unique=True)  # enrollment secret
    status = Column(Text, nullable=False, server_default=text("'active'"))
    plan = Column(Text)
    seats = Column(Integer)
    valid_until = Column(TIMESTAMP(timezone=True))
    created_at = _created_at()

    installations = relationship(
        "Installation", back_populates="subscription", cascade="all, delete-orphan"
    )


class Installation(Base):
    __tablename__ = "installations"
    id = _uuid_pk()
    subscription_id = Column(
        UUID(as_uuid=True), ForeignKey("subscriptions.id"), nullable=False
    )
    box_fingerprint = Column(Text, nullable=False)
    credential_ref = Column(Text)  # reference into Infisical (no plaintext)
    status = Column(Text, nullable=False, server_default=text("'active'"))
    quota_bytes = Column(BigInteger)
    last_seen = Column(TIMESTAMP(timezone=True))
    created_at = _created_at()

    subscription = relationship("Subscription", back_populates="installations")


# --- Fleet -------------------------------------------------------------------
class Worker(Base):
    __tablename__ = "workers"
    id = _uuid_pk()
    # #336: unique — check-then-insert races otherwise leave duplicate rows
    # that make every later one_or_none() raise MultipleResultsFound (500s on
    # each node report, permanently, until manual cleanup).
    name = Column(Text, nullable=False, unique=True)
    # #284: manager-owned display label. Node registration keys on `name` and
    # never writes this — a rename survives the node's next report.
    display_name = Column(Text, nullable=True)
    address = Column(Text, nullable=False)
    labels = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(Text, nullable=False, server_default=text("'unknown'"))
    last_heartbeat = Column(TIMESTAMP(timezone=True))
    # #340: folded into the per-worker command-key HMAC. Bumping it
    # (rotate-key) revokes THIS worker's credential without touching the
    # fleet. 0 keeps the legacy HMAC input, so existing keys stay valid.
    key_epoch = Column(Integer, nullable=False, server_default=text("0"))


# --- Catalog & artifacts -----------------------------------------------------
class Model(Base):
    __tablename__ = "models"
    id = _uuid_pk()
    name = Column(Text, nullable=False, unique=True)  # logical, e.g. 'qwen3.6' (#336)
    repo_id = Column(Text)
    revision = Column(Text)
    description = Column(Text)
    source_tier = Column(Text)  # 'rzfz-mirror'|'huggingface'|'mistral'
    created_at = _created_at()

    artifacts = relationship(
        "ModelArtifact", back_populates="model", cascade="all, delete-orphan"
    )
    presets = relationship(
        "ModelPreset", back_populates="model", cascade="all, delete-orphan"
    )


class ModelArtifact(Base):
    __tablename__ = "model_artifacts"
    id = _uuid_pk()
    model_id = Column(UUID(as_uuid=True), ForeignKey("models.id"), nullable=False)
    format = Column(Text, nullable=False)  # 'gguf'|'safetensors'|'ollama'
    quant = Column(Text)  # 'Q6_K'|'AWQ'|'FP8'|'NVFP4' ...
    engine = Column(Text, nullable=False)  # 'llamacpp'|'vllm'|'ollama'
    hardware_class = Column(Text)  # 'amd-gfx1151'|'cuda'|'apple-silicon'|'cpu'
    variant_tag = Column(Text)  # e.g. 'mlx'
    modalities = Column(ARRAY(Text))  # {'text','image'}
    context_length = Column(Integer)
    apple_silicon_optimized = Column(Boolean, server_default=text("false"))
    size_bytes = Column(BigInteger)
    source = Column(Text)

    model = relationship("Model", back_populates="artifacts")
    files = relationship(
        "ModelArtifactFile", back_populates="artifact", cascade="all, delete-orphan"
    )


class ModelArtifactFile(Base):
    __tablename__ = "model_artifact_files"
    id = _uuid_pk()
    artifact_id = Column(
        UUID(as_uuid=True),
        ForeignKey("model_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(Text, nullable=False)  # 'weights'|'mmproj'|'bundle'
    blob_ref = Column(Text, nullable=False)
    sha256 = Column(Text, nullable=False)
    size_bytes = Column(BigInteger)

    artifact = relationship("ModelArtifact", back_populates="files")


class ModelPreset(Base):
    __tablename__ = "model_presets"
    id = _uuid_pk()
    model_id = Column(UUID(as_uuid=True), ForeignKey("models.id"), nullable=False)
    name = Column(Text, nullable=False)
    use_case = Column(Text)
    engine = Column(Text, nullable=False)
    params = Column(JSONB, nullable=False)
    origin = Column(Text, nullable=False)  # 'rzfz'|'hf-modelcard'|'user'
    warnings = Column(JSONB)

    model = relationship("Model", back_populates="presets")


# --- Deployments -------------------------------------------------------------
class Deployment(Base):
    __tablename__ = "deployments"
    id = _uuid_pk()
    model_name = Column(Text, nullable=False, unique=True)  # client-facing (LiteLLM)
    # #284: console-only display label. `model_name` above stays the immutable
    # client-facing LiteLLM routing id; this is what the console shows.
    display_name = Column(Text, nullable=True)
    model_id = Column(UUID(as_uuid=True), ForeignKey("models.id"), nullable=False)
    engine = Column(Text, nullable=False)
    # Serve task: 'chat' (default) | 'embed' | 'rerank'. Drives the engine's
    # serve-mode flags on the node (--embeddings/--reranking) AND the LiteLLM
    # model_info.mode so /v1/embeddings + /v1/rerank route to this deployment.
    task = Column(Text, nullable=False, server_default=text("'chat'"))
    params = Column(JSONB, nullable=False)
    quant_policy = Column(JSONB)
    # #298: weight source persisted so the replica scheduler can place more
    # instances — files staged/served + the HF repo to fetch them if absent.
    source_files = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    hf_repo = Column(Text)
    # #296: free-form operator tags (UI metadata only — NEVER passed to the engine;
    # kept out of `params` so it can't become a bogus CLI flag). The central tag
    # catalog (GET /api/tags) is the distinct set across all deployments.
    tags = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    replicas = Column(Integer, nullable=False, server_default=text("1"))
    # #227 admission control: estimated resident footprint (weights+KV+compute, GB)
    # from the deploy-time memory-impact calc. Used to gate over-subscription of a
    # worker's VRAM carveout. NEVER passed to the engine.
    est_gb = Column(Float)
    # #549 R1: the runner image THIS deployment is pinned to (e.g.
    # "llama-vulkan-runner:b9100"). NULL = the node's default for its hardware
    # class — the pre-R1 behaviour, so existing rows are untouched. Persisted so
    # resume and replica scale-up re-launch on the SAME runner version the
    # operator deployed on, not whatever the node's default has since become.
    runner_image = Column(Text)
    worker_selector = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    model = relationship("Model")
    instances = relationship(
        "DeploymentInstance", back_populates="deployment", cascade="all, delete-orphan"
    )


class DeploymentInstance(Base):
    __tablename__ = "deployment_instances"
    id = _uuid_pk()
    deployment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )
    worker_id = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    # Engine instance identity (container name / node instance_id). Distinguishes
    # replicas + same-model-per-worker: two engines of one model on one worker are
    # distinct rows keyed by instance_id, not collapsed into one. NULL for
    # endpoint-only backends (e.g. the Mac gateway) that key by (deployment,worker).
    instance_id = Column(Text)
    artifact_id = Column(UUID(as_uuid=True), ForeignKey("model_artifacts.id"))
    endpoint = Column(Text)
    # Per-backend upstream key (P2-B3). Injected router-side (litellm_params.
    # api_key); NULL = no key. NOT a client key — the manager must present it
    # verbatim to the backend, so it's stored recoverable, not hashed.
    api_key = Column(Text)
    params_effective = Column(JSONB)
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    # #287/#286: short human phase detail — "pulling 42%" while weights download,
    # or the failure reason when an engine won't load. NULL when nothing to say.
    detail = Column(Text)
    started_at = Column(TIMESTAMP(timezone=True))
    # #2019: set on the OLD instance of a blue-green `apply-params` — unload me
    # as soon as a READY sibling of this deployment exists on this worker. A
    # timestamp rather than a pointer to the replacement, because when the
    # decision is made the replacement has no row yet (the node's report creates
    # it); and the age is what lets an overlap whose new engine never becomes
    # ready be abandoned by the clock, leaving the old engine serving.
    retiring_since = Column(TIMESTAMP(timezone=True))

    deployment = relationship("Deployment", back_populates="instances")
    worker = relationship("Worker")


# --- Keys & TOKEN-ONLY chargeback -------------------------------------------
class CostCenter(Base):
    __tablename__ = "cost_centers"
    id = _uuid_pk()
    name = Column(Text, nullable=False)
    team = Column(Text)
    created_at = _created_at()

    keys = relationship("ApiKey", back_populates="cost_center")


class ApiKey(Base):
    __tablename__ = "api_keys"
    id = _uuid_pk()
    key_hash = Column(LargeBinary, nullable=False, unique=True)  # sha256(key)
    key_prefix = Column(Text, nullable=False)  # display-only, e.g. 'rzfz-sk-ab12'
    cost_center_id = Column(
        UUID(as_uuid=True), ForeignKey("cost_centers.id"), nullable=False
    )
    allowed_models = Column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # TOKEN cap (not money) over budget_duration. NULL = unlimited.
    max_budget_tokens = Column(BigInteger)
    budget_duration = Column(INTERVAL)
    rpm_limit = Column(Integer)
    tpm_limit = Column(Integer)
    status = Column(Text, nullable=False, server_default=text("'active'"))
    created_at = _created_at()
    expires_at = Column(TIMESTAMP(timezone=True))
    # #314: owner attribution — the Authentik username of whoever the key was
    # minted FOR (self-issue: the caller; admin-minted-for-another: the named
    # owner). NULLABLE — existing rows (incl. the #350 seeded
    # `playground-internal` accounting identity, which has no human owner)
    # predate this column and stay NULL rather than being back-filled with a
    # guess. Enables per-person usage rollup ("my usage") on top of the
    # existing per-key metering.
    owner_username = Column(Text)

    cost_center = relationship("CostCenter", back_populates="keys")


class NodeCommand(Base):
    """manager→node command queue (#261). The manager enqueues (admin); the
    worker's worker-agent long-polls its own pending rows (node-key), executes via
    drivers, and reports the result. Only enumerated driver ops — never shell."""

    __tablename__ = "node_commands"
    id = _uuid_pk()
    worker_id = Column(
        UUID(as_uuid=True), ForeignKey("workers.id", ondelete="CASCADE"), nullable=False
    )
    kind = Column(Text, nullable=False)  # restart_engine|stop_engine|load_engine|tail_logs
    args = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status = Column(Text, nullable=False, server_default=text("'pending'"))  # pending|claimed|done|failed
    result = Column(JSONB)
    created_at = _created_at()
    claimed_at = Column(TIMESTAMP(timezone=True))
    finished_at = Column(TIMESTAMP(timezone=True))


class RuntimeSetting(Base):
    """Operator-set runtime override (Phase-3 S5). A generic key/value store
    the management UI writes so a knob can change WITHOUT a container restart
    (the env-derived Settings dataclass is immutable + read fresh per call).
    Sole consumer today: ``metering_mode`` (env is the default; a row here
    overrides it live). No secrets are ever stored here."""

    __tablename__ = "runtime_settings"
    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class RunnerUpgrade(Base):
    """#549 R3: one per-node runner-upgrade sequence, durable across manager
    restarts (#359). State machine:

        deploying    deploy_runner enqueued — waiting for the pull. Nothing on
                     the node has been touched yet (#1677 (a)).
        relaunching  runner pulled; deployments repinned + relaunched; waiting
                     for the node's reports to show them ready (the health gate)
        done | failed | rolled_back

    ``captured`` carries one entry per deployment that was serving at the
    intervention, and it is where the per-switch state lives rather than in
    columns of its own: ``container`` (the engine serving it THEN — what a
    blue-green cutover retires) and ``overlap`` (this switch started the new
    engine alongside the old instead of draining first, #1867).

    Advancement is EVENT-driven — command results and registration reports — so
    no background thread exists to die silently; timeouts are checked lazily at
    the same points and on reads.
    """

    __tablename__ = "runner_upgrades"
    id = _uuid_pk()
    worker_id = Column(UUID(as_uuid=True), ForeignKey("workers.id"), nullable=False)
    image = Column(Text, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'deploying'"))
    captured = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    deploy_command_id = Column(UUID(as_uuid=True))
    error = Column(Text)
    deadline = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class EnrollJtiSpent(Base):
    """#340: one row per USED enrollment token — the replay guard. The enroll
    exchange claims the jti atomically (INSERT ON CONFLICT DO NOTHING); a
    replayed token finds the row and is refused."""

    __tablename__ = "enroll_jti_spent"
    jti = Column(Text, primary_key=True)
    worker_name = Column(Text, nullable=False)
    used_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


class UsageEvent(Base):
    """TOKEN-ONLY usage record. No `cost`/currency column — by design."""

    __tablename__ = "usage_events"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id = Column(Text)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=False)
    cost_center_id = Column(
        UUID(as_uuid=True), ForeignKey("cost_centers.id"), nullable=False
    )
    model = Column(Text, nullable=False)
    prompt_tokens = Column(Integer, nullable=False, server_default=text("0"))
    completion_tokens = Column(Integer, nullable=False, server_default=text("0"))
    cached_tokens = Column(Integer, nullable=False, server_default=text("0"))
    # D6 hybrid: usage recorded via the tokenizer backstop / fail-open path is
    # flagged so it can be reconciled later. Never monetary.
    estimated = Column(Boolean, nullable=False, server_default=text("false"))
    ts = Column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())


# --- #818: configure the mappers HERE, not wherever the ORM is first touched -
#
# SQLAlchemy resolves `relationship("SomeClass")` strings lazily, and the pass
# that does it is **process-global**: the first instantiation of ANY mapped
# class anywhere in the process runs `configure_mappers()` over EVERY live
# registry. Two consequences we do not want:
#
#   * The resolution of THIS module's relationship strings is performed by
#     whichever unrelated caller happens to touch an ORM class first, at
#     whatever moment that is — including, in a process that evicts and
#     re-imports `app.*` (the test harness does; see
#     tests/unit/llm-manager/conftest.py), a moment when this registry is no
#     longer the live one and has been partially collected. Then
#     `Model.artifacts -> "ModelArtifact"` no longer resolves and the caller
#     gets `InvalidRequestError: expression 'ModelArtifact' failed to locate a
#     name` for code that did nothing wrong. That landmine cost real coverage:
#     #304 had to hand-write a `FakeDeploymentInstance` and #307 Slice 1 had to
#     pin `deploy()`'s wiring on literal source text rather than call the route.
#   * A failure in mapper setup would surface at the first request rather than
#     at import, where it belongs.
#
# Configuring at the end of the module — the one point where every class above
# is guaranteed to be defined — makes it deterministic and makes this registry
# a no-op for every later global pass. Keep this line LAST.
Base.registry.configure()
