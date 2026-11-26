// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
// #2559 — the Mac thin worker: our agent in front of a loopback Ollama. It is a
// normal worker (placeable, drainable), not an external backend, but it has no
// model mount and no runner images; what it has instead is an Ollama version
// and a pin, and the two runtime upgrades.

type MacFields = {
  engine?: string | null;
  hardware?: string | null;
  external?: boolean;
  agent_managed?: boolean;
  ollama_version?: string | null;
  ollama_pinned?: string | null;
};

// An Ollama run by our agent.
export function isAgentManagedOllama(w: MacFields): boolean {
  return (w.engine || "").toLowerCase() === "ollama" && !!w.agent_managed && !w.external;
}

// A pre-existing endpoint with no agent of ours (a bare Ollama included).
export function isExternalBackend(w: MacFields): boolean {
  return !!w.external || w.hardware === "external"
    || ((w.engine || "").toLowerCase() === "ollama" && !w.agent_managed);
}

export type RuntimeAction = { component: "ollama" | "agent"; label: string; confirm: string };

// The runtime-upgrade actions this worker offers. None for anything but an
// agent-managed Ollama. Ollama only when the bundle pins a version other than
// the one running; the agent always (it moves to the master's current bundle).
export function runtimeActions(w: MacFields & { name: string }): RuntimeAction[] {
  if (!isAgentManagedOllama(w)) return [];
  const out: RuntimeAction[] = [];
  if (w.ollama_pinned && w.ollama_pinned !== w.ollama_version) {
    out.push({
      component: "ollama",
      label: `Upgrade Ollama to ${w.ollama_pinned}`,
      confirm: `Upgrade Ollama on ${w.name} from ${w.ollama_version ?? "an unknown version"} to ${w.ollama_pinned}?\n\n`
        + "Ollama restarts. The models on this worker are out of routing until it answers again.",
    });
  }
  out.push({
    component: "agent",
    label: "Upgrade agent",
    confirm: `Upgrade the agent on ${w.name} to the master's current bundle?\n\n`
      + "The agent restarts with the new bundle. If that bundle pins a newer Ollama, "
      + "the Ollama upgrade is offered here afterwards.",
  });
  return out;
}
