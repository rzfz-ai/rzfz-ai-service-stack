# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""razzfazz-common — shared Flask helpers for razzfazz.ai UI containers.

Created in M026 Phase 2 / S03 to consolidate the duplicated Flask scaffolding
across the 5 UI containers (config, setup, backup, help, licenses). Adopted
by each container in its own S05 slice.

Public API:
    flask_app.create_base_app(name, ...) -> Flask
    auth.require_authentik_auth(...) -> decorator
    health.get_health_blueprint() -> Blueprint
    env_utils.parse_env_value, read_env_file, read_env_key, write_env_value
    session_config.configure_session(app, ...)
    version.stack_version(), register_context_processor(app)
    csrf.enable_csrf(app, verify_methods=...)         # added #148
    user_slug.make_user_slug(username) -> str         # added #61 NEW-1
"""

__version__ = "0.1.0"
