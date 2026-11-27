# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""SQLAlchemy engine/session plumbing for the LLM Manager.

The declarative ``Base`` lives here; ``app.models`` registers the ORM
mappings on it. The engine + sessionmaker are created LAZILY on first use
(``get_engine()`` / ``get_sessionmaker()``) so importing this module never
opens a connection — it must import cleanly off-box even with an
unreachable ``LLM_MANAGER_DATABASE_URL`` (SQLAlchemy's ``create_engine``
does not connect until the first query, so we stay import-safe).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings

Base = declarative_base()

_engine: Engine | None = None
_sessionmaker: sessionmaker | None = None
# Reentrant: get_sessionmaker() calls get_engine() while conceptually building
# lazy state; a plain Lock would self-deadlock on the first in-request use.
_lock = threading.RLock()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = create_engine(
                    get_settings().database_url,
                    future=True,
                    pool_pre_ping=True,
                    # Bound a dead-datastore connect so a request/metrics scrape
                    # fails fast instead of hanging indefinitely.
                    connect_args={"connect_timeout": 10},
                )
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _sessionmaker
    if _sessionmaker is None:
        engine = get_engine()  # resolve OUTSIDE the lock (no nested acquire)
        with _lock:
            if _sessionmaker is None:
                _sessionmaker = sessionmaker(
                    bind=engine, future=True, expire_on_commit=False
                )
    return _sessionmaker


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context: commit on success, rollback on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Test helper — dispose + clear the cached engine/sessionmaker so a new
    ``LLM_MANAGER_DATABASE_URL`` takes effect on the next ``get_engine()``."""
    global _engine, _sessionmaker
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _sessionmaker = None
