// #315: with several workers the flat list gets long. Group it by worker /
// runtime (Vulkan·CUDA·Ollama) / task / tag so it stays scannable. A deployment
// can carry multiple tags → it appears under each of its tag groups.
//
// Extracted out of Models.tsx (#2513) so it can be driven by a plain node
// harness without dragging React/Vite into the test — this function has no
// JSX and no hooks, it only ever touches its two arguments.
import type { DeploymentRow } from "../api/client";

export type GroupBy = "none" | "worker" | "runtime" | "task" | "tag";

export function groupsFor(deps: DeploymentRow[], by: GroupBy): { label: string; items: DeploymentRow[] }[] {
  if (by === "none") return [{ label: "", items: deps }];
  const map = new Map<string, DeploymentRow[]>();
  const push = (k: string, d: DeploymentRow) => {
    const cur = map.get(k); if (cur) cur.push(d); else map.set(k, [d]);
  };
  for (const d of deps) {
    if (by === "worker") {
      // #2513: list this deployment under EVERY worker that holds one of its
      // instances, not just `instances[0]`'s worker — a 4-replica deployment
      // spread across 3 boxes used to show under one worker's group only, so
      // the other two looked like they carried nothing.
      const ws = Array.from(new Set(d.instances.map((i) => i.worker).filter((w): w is string => !!w))).sort();
      if (ws.length === 0) push("unplaced", d); else ws.forEach((w) => push(w, d));
    }
    else if (by === "runtime") push(d.arch || d.engine || "—", d);
    else if (by === "task") push(d.task || "chat", d);
    else {
      // #2513: `standard-set` is post-install's own internal marker, not an
      // operator tag — it used to form its own group in the Tag view. It is
      // surfaced instead as a "managed" badge on the row (Models.tsx).
      const real = (d.tags ?? []).filter((t) => t !== "standard-set");
      const ts = real.length ? real : ["untagged"];
      for (const t of ts) push(t, d);
    }
  }
  return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([label, items]) => ({ label, items }));
}
