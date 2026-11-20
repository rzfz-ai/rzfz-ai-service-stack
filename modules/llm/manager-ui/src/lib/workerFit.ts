// #2843 — does a model of `need_gb` fit on a worker, as the SERVER will judge it?
//
// The deploy dialog used to compare the estimate with the card's TOTAL memory
// (`vram_total_gb`), so on prod an 88.9 GB model showed a green dot on every
// worker while each already held a ~35 GB replica: the admission gate (#227 /
// #330) refuses that deploy with "would over-subscribe", the dialog said it fit.
//
// `/api/workers` reports the gate's own operands, computed by the manager with
// the functions `_budget_check` calls, so this mirrors the two legs exactly:
//   static:  need + committed <= budget        (admission_committed_gb / _budget_gb)
//   dynamic: need <= free_now                  (only with a fresh VRAM reading)
// The server stays the authority; this only stops the console contradicting it.

import type { WorkerRow } from "../api/client";

export type WorkerCapacity = {
  id: string;
  name: string;
  total_gb: number;               // card VRAM, else host RAM — the old (fallback) basis
  source: "vram" | "ram";
  budget_gb: number | null;       // admission_budget_gb — null on an older manager / inert gate
  committed_gb: number | null;    // admission_committed_gb — what the gate already charges here
  free_now_gb: number | null;     // admission_free_now_gb — null without a fresh reading
  placeable: boolean;
  placeable_reason: string | null;
  models: string[];               // what is placed there now (non-failed instances)
};

export type FitLevel = "ok" | "warn" | "err" | "muted";
export type Fit = { level: FitLevel; label: string; detail: string };

const gb = (n: number) => (Math.round(n * 10) / 10).toFixed(1);

/** The ready workers the editor can place on, with the admission numbers. */
export function workerCapacities(rows: WorkerRow[]): WorkerCapacity[] {
  return rows.filter((w) => w.status === "ready")
    .map((w) => ({
      id: w.id,
      name: w.display_name || w.name,
      total_gb: Number(w.vram_total_gb ?? w.mem_total_gb ?? 0),
      source: (w.vram_total_gb ? "vram" : "ram") as "vram" | "ram",
      budget_gb: w.admission_budget_gb ?? null,
      committed_gb: w.admission_committed_gb ?? null,
      free_now_gb: w.admission_free_now_gb ?? null,
      placeable: w.placeable !== false,
      placeable_reason: w.placeable_reason ?? null,
      models: (w.instances ?? []).filter((i) => i.status !== "failed").map((i) => i.model_name),
    }))
    .filter((w) => w.total_gb > 0 || w.budget_gb !== null);
}

/** What is free for a new placement right now: the tighter of the two legs. */
export function freeNow(w: WorkerCapacity): number | null {
  if (w.budget_gb === null) return null;
  const staticFree = w.committed_gb !== null ? Math.max(0, w.budget_gb - w.committed_gb) : null;
  if (staticFree === null) return w.free_now_gb;
  if (w.free_now_gb === null) return staticFree;
  return Math.min(staticFree, w.free_now_gb);
}

/** Green: fits now. Amber: fits only after freeing memory on that worker.
 *  Red: never fits there, or the worker takes no placements at all. */
export function workerFit(need_gb: number, w: WorkerCapacity): Fit {
  if (!w.placeable) {
    return { level: "err", label: "not placeable", detail: w.placeable_reason || "the manager refuses placements on this worker" };
  }
  const free = freeNow(w);
  if (!need_gb) {
    const cap = free !== null ? `${gb(free)} GB free now` : `${w.source === "vram" ? "VRAM" : "RAM"} ${gb(w.total_gb)} GB`;
    return { level: "muted", label: cap, detail: "" };
  }
  const need = `≈ ${gb(need_gb)} GB`;
  if (w.budget_gb !== null) {
    const budget = `budget ${gb(w.budget_gb)}`;
    if (need_gb > w.budget_gb) {
      return { level: "err", label: `${need} · never fits (${budget})`,
        detail: `needs ${gb(need_gb)} GB, more than this worker can ever give (${gb(w.budget_gb)} GB usable)` };
    }
    if (free === null) {
      // Budget known, current use not reported by this manager.
      return { level: w.models.length ? "warn" : "ok", label: `${need} · ${budget}, free now unknown`,
        detail: w.models.length ? `already serving: ${w.models.join(", ")}` : "" };
    }
    if (need_gb <= free) {
      return { level: "ok", label: `${need} · ${gb(free)} GB free now (${budget})`, detail: "" };
    }
    return { level: "warn", label: `${need} · ${gb(free)} GB free now (${budget})`,
      detail: `fits after freeing ${gb(need_gb - free)} GB` + (w.models.length ? ` — stop: ${w.models.join(", ")}` : "") };
  }
  // An older manager without admission fields: the total-memory heuristic,
  // labelled so nobody reads it as the admission verdict.
  const ok = need_gb <= w.total_gb * 0.92, tight = !ok && need_gb <= w.total_gb;
  return { level: ok ? "ok" : tight ? "warn" : "err",
    label: `${need} / ${gb(w.total_gb)} GB total`,
    detail: "total memory only — this worker reports no admission budget, so models already running are not counted" };
}

/** True when a deploy of `need_gb` has nowhere to go right now (Auto would be refused). */
export function noWorkerHasRoomNow(need_gb: number, ws: WorkerCapacity[]): boolean {
  if (!need_gb || ws.length === 0) return false;
  return !ws.some((w) => workerFit(need_gb, w).level === "ok");
}
