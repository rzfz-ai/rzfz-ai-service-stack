# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Alembic environment for the LLM Manager.

Self-contained: works whether invoked via the ``alembic`` CLI from the
module dir, or programmatically from the test-suite (which sets
``sqlalchemy.url`` on the Config). We force the orchestrator dir onto
sys.path and evict any sibling ``app`` package so ``from app.db import
Base`` always resolves to THIS module (several stack suites ship an
``app`` package — Python caches the first one imported).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# --- make our `app` package win, regardless of caller state ------------------
ORCH_DIR = Path(__file__).resolve().parents[1]
for _m in list(sys.modules):
    if _m == "app" or _m.startswith("app."):
        del sys.modules[_m]
if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401  (registers all tables on Base.metadata)

config = context.config

# Prefer an explicit env var when the caller didn't inject a URL.
if not config.get_main_option("sqlalchemy.url"):
    env_url = os.environ.get("LLM_MANAGER_DATABASE_URL")
    if env_url:
        config.set_main_option("sqlalchemy.url", env_url)

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # Logging config is best-effort; never let it abort a migration.
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
