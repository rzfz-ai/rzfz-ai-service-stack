# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Docker client wrapper for per-user MCP-proxy container lifecycle (#36).

Mirrors agent-manager's docker_client.py — talks to docker-socket-proxy
(tcp://docker-socket-proxy:2375, the SAME constrained grants as agent-manager;
no BUILD / SECRETS / SWARM), labels every container `razzfazz.managed=true`,
sets `no-new-privileges`, and connects each proxy to `mcp-network` ONLY.

#61 NEW-2: the proxy joins `mcp-network` and NOTHING else. mcp-manager itself
is NOT on mcp-network (see modules/mcp-manager/compose.yml), so a proxy cannot
reach mcp-manager:5000/internal/* by container name and enumerate other users'
proxies + bearer tokens. Caddy (the only other member of mcp-network) bridges
external traffic to each proxy via its per-host route, forward-auth + bearer
gated. The user's agents reach the proxy THROUGH Caddy (the gated subdomain),
never by container name. The /internal token gate (see blueprints/internal.py)
remains the primary control; this network split is defense-in-depth.

SECURITY: the per-user proxy receives ONLY that user's decrypted credentials,
passed as environment variables on a container that has NO docker-socket access
(mcp proxies never need it). Credentials live in the container's env/tmpfs, not
on a host-mounted volume / disk.
"""

import logging

import docker

logger = logging.getLogger(__name__)


class MCPDockerClient:
    def __init__(self, base_url: str, network: str):
        self._client = docker.DockerClient(base_url=base_url)
        self._network = network
        self._ensure_network()

    def _ensure_network(self):
        try:
            self._client.networks.get(self._network)
        except docker.errors.NotFound:
            logger.info("Creating MCP network: %s", self._network)
            self._client.networks.create(self._network, driver="bridge")

    # #36 follow-up: a proxy may NEVER join the manager's `default` network (that
    # would let it reach mcp-manager:5000/internal and enumerate other users) —
    # this is the hard isolation invariant. Some proxies must reach a shared
    # backend that is NOT on mcp-network; they may join a SCOPED extra network the
    # backend is on (e.g. `cognee-backend`, where cognee lives), but mcp-manager
    # is NOT on that network so /internal stays unreachable.
    #
    # #36 BLOCKER-1: `coding-agents` is ALSO forbidden — that bridge carries all
    # the coding-agent SANDBOXES, so a per-user proxy there is reachable by any
    # user's sandbox by container name (cross-user private-memory read/write). Use
    # the dedicated `cognee-backend` net instead. `default` is refused outright.
    _FORBIDDEN_EXTRA_NETWORKS = {"default", "razzfazz-stack_default", "coding-agents"}

    def create_container(self, name: str, image: str, environment: dict,
                         mem_limit: str = "256m", cpu_limit: float = 0.5,
                         labels: dict = None, command=None,
                         tmpfs: dict = None, extra_networks=None) -> str:
        """Create a per-user MCP-proxy container (stopped). Returns container ID.

        No docker socket is ever mounted (MCP proxies don't need it). `tmpfs`
        mounts an in-memory filesystem (default /run/mcp) so any creds the proxy
        writes transiently never hit disk.

        `extra_networks`: optional SCOPED networks a proxy joins IN ADDITION to
        mcp-network so it can reach a shared backend (e.g. cognee on
        `coding-agents`). The manager's `default` network is REFUSED — joining it
        would breach the /internal isolation invariant.
        """
        logger.info("Creating MCP proxy container %s from %s", name, image)
        create_kwargs = dict(
            image=image,
            name=name,
            environment=environment,
            mem_limit=mem_limit,
            nano_cpus=int(cpu_limit * 1e9),
            network=self._network,
            labels=labels or {},
            restart_policy={"Name": "unless-stopped"},
            detach=True,
            security_opt=["no-new-privileges:true"],
            tmpfs=tmpfs or {"/run/mcp": "rw,noexec,nosuid,size=8m"},
        )
        if command:
            create_kwargs["command"] = command
        container = self._client.containers.create(**create_kwargs)

        # HIGH-1 (#61): the proxy stays on mcp-network (+ any SCOPED extra net).
        # It must NOT join the manager's stack default network — that would let a
        # user who influences any proxy reach mcp-manager:5000/internal/* and
        # enumerate other users' proxies + bearer tokens. Caddy reaches each proxy
        # via its per-host route (Caddy is on mcp-network); agents reach the proxy
        # THROUGH Caddy (the gated subdomain), never by container name.
        for net in (extra_networks or []):
            if not net or net in self._FORBIDDEN_EXTRA_NETWORKS or net == self._network:
                logger.warning("Refusing to attach proxy %s to network %r (isolation "
                               "invariant / redundant)", name, net)
                continue
            try:
                self._client.networks.get(net).connect(container.id)
                logger.info("Attached proxy %s to scoped backend network %s", name, net)
            except Exception:
                logger.exception("Could not attach proxy %s to network %s", name, net)
        return container.id

    def start_container(self, name: str):
        self._client.containers.get(name).start()
        logger.info("Started %s", name)

    def stop_container(self, name: str, timeout: int = 10):
        try:
            self._client.containers.get(name).stop(timeout=timeout)
            logger.info("Stopped %s", name)
        except docker.errors.NotFound:
            logger.warning("Container %s not found for stop", name)

    def remove_container(self, name: str, force: bool = True):
        try:
            self._client.containers.get(name).remove(force=force)
            logger.info("Removed %s", name)
        except docker.errors.NotFound:
            logger.warning("Container %s not found for removal", name)

    def get_container_state(self, name: str):
        try:
            return self._client.containers.get(name).status
        except docker.errors.NotFound:
            return None

    def get_container_logs(self, name: str, tail: int = 200) -> str:
        try:
            return self._client.containers.get(name).logs(tail=tail).decode(
                "utf-8", errors="replace")
        except docker.errors.NotFound:
            return ""

    def list_mcp_containers(self) -> list:
        containers = self._client.containers.list(
            all=True, filters={"label": "razzfazz.mcp.managed=true"})
        return [
            {"id": c.id, "name": c.name, "status": c.status,
             "mcp_id": c.labels.get("razzfazz.mcp.id", ""),
             "user": c.labels.get("razzfazz.mcp.user", "")}
            for c in containers
        ]

    def ping(self) -> bool:
        try:
            self._client.ping()
            return True
        except Exception:
            return False
