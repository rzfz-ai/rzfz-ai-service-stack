// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
// #2530 — the Fleet badge for a node's agent image. "Unknown" (no digest) and
// "not compared" (the hub did not answer) are never shown as "outdated".
export function agentImageBadge(w: { agent_image_digest?: string | null; agent_image_current?: boolean | null }):
    { label: string; tone: "ok" | "warn" | "muted" } {
  const d = (w.agent_image_digest || "").replace(/^sha256:/, "").slice(0, 12);
  if (!d) return { label: "agent version unknown", tone: "muted" };
  if (w.agent_image_current === false) return { label: `agent outdated (${d})`, tone: "warn" };
  if (w.agent_image_current === true) return { label: `agent ${d}`, tone: "ok" };
  return { label: `agent ${d}`, tone: "muted" };
}

// The hover text says what brings the agent to the hub's image. The master's
// own agent (same-box: no advertise address) is refreshed with the stack, a
// node with `rzfz node-upgrade`. An agent from before version reporting sends
// no digest at all — one node-upgrade makes it report.
//
// #2559: a Mac worker (our agent in front of a loopback Ollama) runs no agent
// image; its digest is the Mac bundle's, and "Upgrade agent" in its drawer is
// what moves it — never `rzfz node-upgrade`. The master does not compare it.
export function agentImageTitle(
    w: { agent_image_digest?: string | null; agent_image_current?: boolean | null; advertise_addr?: string | null;
         engine?: string | null; agent_managed?: boolean | null; external?: boolean | null }): string {
  const master = !w.advertise_addr;
  const b = agentImageBadge(w);
  if ((w.engine || "").toLowerCase() === "ollama" && !!w.agent_managed && !w.external) {
    return w.agent_image_digest
      ? `Mac agent bundle ${w.agent_image_digest}. "Upgrade agent" in the worker's drawer moves it to the master's current bundle.`
      : "This Mac agent has not reported its bundle yet. \"Upgrade agent\" in the worker's drawer, or re-running the installer from Fleet → Add worker, makes it report.";
  }
  if (!w.agent_image_digest) {
    return master
      ? "The master's own agent has not reported its image yet; it does once it runs the image rzfz post-install --refresh publishes to the hub."
      : "This node's agent does not report its image yet. Run rzfz node-upgrade on the node once; from then on it does.";
  }
  if (b.tone === "warn") {
    return master
      ? "Not the hub's image. rzfz upgrade (or rzfz post-install --refresh) brings the master's agent to it."
      : "Not the hub's image. rzfz node-upgrade on the node brings it to the hub's image.";
  }
  if (w.agent_image_current == null) return `${w.agent_image_digest} (the hub's image could not be read)`;
  return w.agent_image_digest;
}

// #2897: the agent runs another release line than the master (the master decides;
// null = same line or unknown, which is never shown as a problem).
export function agentVersionSkewBadge(
    w: { agent_version_skew?: { worker: string; master: string; update: string } | null }):
    { label: string; title: string } | null {
  const k = w.agent_version_skew;
  if (!k) return null;
  return {
    label: `agent ${k.worker} \u2260 master ${k.master}`,
    title: `This agent is ${k.worker}, the master is ${k.master}. Update the agent with: ${k.update}`,
  };
}
