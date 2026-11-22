# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""The role catalogue (#2473, decision record D2).

Reads core/rbac/roles.yaml, which every own component mounts at
/etc/razzfazz/rbac/roles.yaml. Stdlib + PyYAML only and no package-relative
imports: the LLM Manager (FastAPI, no razzfazz_common) loads THIS file by path
from the bind mount /opt/razzfazz/rbac/razzfazz_rbac.py. The Agent Manager and
the MCP manager import it as razzfazz_common.rbac (their compose mounts
core/common/razzfazz_common).

The role->group mapping is loaded once per process and cached; a changed
catalogue takes effect when the container is recreated (the file ships with the
code and changes only on upgrade). A user's group membership is not cached: it
arrives in X-Authentik-Groups on every request, so removing someone from a group
takes effect immediately.

A missing or malformed catalogue raises CatalogueError. There is no fallback:
a silent default is a second source of truth, and a wrong one.

No `from __future__ import annotations` on purpose: the file is loaded by path
(not registered in sys.modules) and @dataclass resolves string annotations there.
"""
import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

import yaml

CATALOGUE_ENV = "RZFZ_RBAC_CATALOGUE"
DEFAULT_PATH = "/etc/razzfazz/rbac/roles.yaml"
SCHEMA_VERSION = 1


class CatalogueError(RuntimeError):
    """The role catalogue is missing or malformed."""


@dataclass(frozen=True)
class Catalogue:
    roles: Mapping[str, frozenset[str]]
    components: Mapping[str, Mapping[str, tuple[str, ...]]]
    kb_prefix: str
    #: app -> {catalogue role (or "*"): app role}, in catalogue order; the first
    #: role (in that order) the user holds wins, "*" is everyone else (D5, A12).
    downstream: Mapping[str, Mapping[str, str]] = field(default_factory=lambda: MappingProxyType({}))
    #: app -> the Authentik group whose members may use the app at all (D5, A12).
    module_access: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def role_groups(self, role: str) -> frozenset[str]:
        try:
            return self.roles[role]
        except KeyError:
            raise CatalogueError(f"unknown role {role!r}") from None

    def groups_for(self, component: str, permission: str) -> frozenset[str]:
        try:
            roles = self.components[component][permission]
        except KeyError:
            raise CatalogueError(f"{component}/{permission} is not in the role catalogue") from None
        out: set = set()
        for role in roles:
            out |= self.role_groups(role)
        return frozenset(out)

    def allows(self, component: str, permission: str, groups: Iterable[str]) -> bool:
        if isinstance(groups, (str, bytes)):
            raise TypeError("groups must be an iterable of group names, not a string (split X-Authentik-Groups first)")
        return bool(self.groups_for(component, permission) & set(groups or ()))

    def all_groups(self) -> frozenset[str]:
        out: set = set()
        for groups in self.roles.values():
            out |= groups
        return frozenset(out)


def _mapping(value, what: str, source: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise CatalogueError(f"{source}: {what} must be a non-empty mapping")
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise CatalogueError(f"{source}: {what} has a key that is not a non-empty string: {key!r}")
    return value


def _str_list(value, what: str, source: str) -> list:
    """A non-empty list of non-empty strings (never a bare string, never null)."""
    if value is None or (isinstance(value, list) and not value):
        raise CatalogueError(f"{source}: {what} has no groups" if what.endswith(".groups") else f"{source}: {what} is empty")
    if not isinstance(value, list):
        raise CatalogueError(f"{source}: {what} must be a list of strings, not {type(value).__name__}")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogueError(f"{source}: {what} holds an entry that is not a non-empty string: {item!r}")
    return [item.strip() for item in value]


def parse_catalogue(doc, source: str) -> Catalogue:
    """Validate SHAPE as well as values; every malformed input is a CatalogueError.

    An empty or wrong-typed value is never read as "nobody allowed" (or, for a
    bare string, as a set of characters): the catalogue is refused.
    """
    version = doc.get("schema_version") if isinstance(doc, dict) else None
    if type(version) is not int or version != SCHEMA_VERSION:
        raise CatalogueError(f"{source}: schema_version must be {SCHEMA_VERSION}")
    roles: dict = {}
    for name, spec in _mapping(doc.get("roles"), "roles", source).items():
        if not isinstance(spec, dict):
            raise CatalogueError(f"{source}: role {name!r} must be a mapping")
        roles[name] = frozenset(_str_list(spec.get("groups"), f"role {name!r}.groups", source))
    components: dict = {}
    for comp, perms in _mapping(doc.get("components"), "components", source).items():
        entry = {}
        for perm, rlist in _mapping(perms, f"components.{comp}", source).items():
            names = _str_list(rlist, f"{comp}/{perm}", source)
            for r in names:
                if r not in roles:
                    raise CatalogueError(f"{source}: {comp}/{perm} names unknown role {r!r}")
            entry[perm] = tuple(names)
        components[comp] = MappingProxyType(entry)
    prefix = "KB: "
    if "knowledge_scopes" in doc:
        ks = doc["knowledge_scopes"]
        if not isinstance(ks, dict):
            raise CatalogueError(f"{source}: knowledge_scopes must be a mapping")
        if "group_prefix" in ks:
            prefix = ks["group_prefix"]
            if not isinstance(prefix, str) or not prefix:
                raise CatalogueError(f"{source}: knowledge_scopes.group_prefix must be a non-empty string")
    downstream, module_access = _parse_downstream(doc, roles, source)
    return Catalogue(roles=MappingProxyType(roles), components=MappingProxyType(components), kb_prefix=prefix,
                     downstream=downstream, module_access=module_access)


def _parse_downstream(doc: dict, roles: dict, source: str):
    """`downstream:` and `module_access:` (A12). Both optional, but they come as
    a pair: every app with a role table names its module-access group and vice
    versa. A malformed block is a CatalogueError like every other one, so the
    reconcile that reads it changes nothing instead of guessing."""
    has_ds, has_ma = "downstream" in doc, "module_access" in doc
    if not has_ds and not has_ma:
        return MappingProxyType({}), MappingProxyType({})
    if has_ds != has_ma:
        raise CatalogueError(f"{source}: downstream and module_access must be declared together")
    downstream: dict = {}
    for app, table in _mapping(doc["downstream"], "downstream", source).items():
        entry: dict = {}
        for role, app_role in _mapping(table, f"downstream.{app}", source).items():
            if role != "*" and role not in roles:
                raise CatalogueError(f"{source}: downstream.{app} names unknown role {role!r}")
            if not isinstance(app_role, str) or not app_role.strip():
                raise CatalogueError(
                    f"{source}: downstream.{app}.{role} must be a non-empty app role string, not {app_role!r}")
            entry[role] = app_role.strip()
        downstream[app] = MappingProxyType(entry)
    module_access: dict = {}
    for app, group in _mapping(doc["module_access"], "module_access", source).items():
        if not isinstance(group, str) or not group.strip():
            raise CatalogueError(f"{source}: module_access.{app} must be a non-empty group name, not {group!r}")
        module_access[app] = group.strip()
    if set(downstream) != set(module_access):
        raise CatalogueError(
            f"{source}: downstream and module_access must name the same apps "
            f"(downstream: {sorted(downstream)}, module_access: {sorted(module_access)})")
    return MappingProxyType(downstream), MappingProxyType(module_access)


def load_catalogue(path: str | None = None) -> Catalogue:
    p = path or os.environ.get(CATALOGUE_ENV) or DEFAULT_PATH
    try:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        raise CatalogueError(
            f"role catalogue not readable at {p}: {e.strerror} — every own component "
            f"mounts core/rbac/roles.yaml there (#2473)") from e
    except UnicodeDecodeError as e:
        raise CatalogueError(f"{p}: not valid UTF-8: {e}") from e
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise CatalogueError(f"{p}: not valid YAML: {e}") from e
    return parse_catalogue(doc, p)


_CACHE: Catalogue | None = None


def catalogue() -> Catalogue:
    global _CACHE
    if _CACHE is None:
        _CACHE = load_catalogue()
    return _CACHE


def reset_cache() -> None:
    global _CACHE
    _CACHE = None
