// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
// #3403 — the deploy editor's runner picker and the "experimental runner" badge.
//
// The picker lists what the workers REPORT (`list_runner_images`), not a typed
// guess: a pin the node does not hold or cannot pull fails at engine start,
// far from the form. Two channel choices sit above the images: the node's
// default (stable — or what the catalogue declares for the model) and the
// experimental channel, which each node resolves to the experimental build of
// its own hardware line. Typing a reference stays available as "advanced".
//
// A pin must be registry-qualified (the manager refuses a bare name, #1214)
// and on the node's allowed registry (#549 R2). An image the node built
// locally is therefore offered as its hub twin `<registry>/runners/<name:tag>`
// — the place post-install publishes the same build (#1472).

export interface RunnerImageLike { image: string; origin?: string }
export interface RunnerInventoryLike { registry: string; runners: RunnerImageLike[] }
export interface RunnerOption { value: string; label: string; ref: string; experimental: boolean }

export const CHOICE_DEFAULT = "";
export const CHOICE_EXPERIMENTAL = "channel:experimental";
export const CHOICE_STABLE = "channel:stable";
export const CHOICE_ADVANCED = "advanced";

/** `name:tag` of a reference, without registry host, repository path or digest. */
export function imageLeaf(ref: string): string {
  const noDigest = String(ref || "").split("@", 1)[0];
  const leaf = noDigest.split("/").pop() || "";
  return leaf.includes(":") ? leaf : `${leaf}:latest`;
}

function tagOf(ref: string): string {
  const leaf = imageLeaf(ref);
  return leaf.slice(leaf.indexOf(":") + 1);
}

/** The reference a deployment may be PINNED to for an image a node reports. */
export function pinRef(registry: string, img: RunnerImageLike): string {
  const ref = String(img.image || "");
  const first = ref.split("/", 1)[0];
  const qualified = ref.includes("/") && (first.includes(".") || first.includes(":"));
  if (img.origin === "registry" && qualified) return ref;
  return `${registry.replace(/\/+$/, "")}/runners/${imageLeaf(ref)}`;
}

/** One option per distinct pin across the given inventories, experimental
 *  ones flagged (by the tags the manager declares for that channel). */
export function runnerOptions(inventories: RunnerInventoryLike[], experimentalTags: Iterable<string>): RunnerOption[] {
  const exp = new Set(experimentalTags);
  const seen = new Map<string, RunnerOption>();
  for (const inv of inventories) {
    for (const img of inv.runners || []) {
      if (!img || !img.image || img.image.startsWith("sha256:")) continue;
      const ref = pinRef(inv.registry, img);
      if (seen.has(ref)) continue;
      const experimental = exp.has(tagOf(ref));
      seen.set(ref, { value: `image:${ref}`, ref, experimental,
                      label: `${imageLeaf(ref)}${experimental ? " (experimental)" : ""}` });
    }
  }
  return [...seen.values()].sort((a, b) => a.label.localeCompare(b.label));
}

/** The experimental channel's image tags, from the manager's runner targets. */
export function experimentalTags(targets: { ref: string; channel?: string }[] | undefined): string[] {
  return (targets || []).filter((t) => t.channel === "experimental").map((t) => tagOf(t.ref));
}

/** What the deploy request carries for a picker choice. */
export function runnerPayload(choice: string, advanced = ""):
    { runner_image?: string; runner_channel?: string } {
  if (choice === CHOICE_EXPERIMENTAL) return { runner_channel: "experimental" };
  if (choice === CHOICE_STABLE) return { runner_channel: "stable" };
  if (choice.startsWith("image:")) return { runner_image: choice.slice("image:".length) };
  if (choice === CHOICE_ADVANCED) {
    const ref = advanced.trim();
    return ref ? { runner_image: ref } : {};
  }
  return {};
}

/** The picker's starting choice: a catalogue model that needs the experimental
 *  runner starts on that channel, so the operator SEES where it will run. */
export function initialChoice(catalogueRunner: string | null | undefined): string {
  return catalogueRunner === "experimental" ? CHOICE_EXPERIMENTAL : CHOICE_DEFAULT;
}

/** The "experimental runner" badge of a deployment, from the manager's verdict. */
export function runnerBadge(dep: { runner_experimental?: boolean | null; runner_channel?: string | null;
                                   runner_image?: string | null }):
    { label: string; title: string } | null {
  if (!dep.runner_experimental) return null;
  const how = dep.runner_image ? `pinned to ${dep.runner_image}`
    : dep.runner_channel === "experimental" ? "on the experimental channel"
    : "its engine reports the experimental image";
  return {
    label: "experimental runner",
    title: `Runs on the experimental llama.cpp runner (${how}). Experimental means no stability `
      + "promise. Rollback: set this deployment's runner back to the node default (stable).",
  };
}

/** #3403: the catalogue card's runner state. `off` = the model needs the
 *  experimental runner and this box has not opted in: the card says so and
 *  its deploy button is disabled (the manager would refuse it, 422). */
export function catalogueRunnerState(e: { runner?: string | null; runner_available?: boolean;
                                          runner_notice?: string | null }):
    { label: string; title: string; off: boolean } | null {
  if (e.runner !== "experimental") return null;
  if (e.runner_available === false) {
    return { label: "experimental runner — off on this box", off: true,
             title: e.runner_notice || "The experimental runner is off on this box." };
  }
  return { label: "experimental runner", off: false,
           title: "Served by the experimental llama.cpp runner: no stability promise." };
}

/** The PATCH that moves ONE deployment back to the node's default (stable)
 *  runner — the documented rollback. Clears a pin as well as the channel: a
 *  pin to the experimental image is the other way onto that runner. */
export function backToStablePatch(): { runner_image: null; runner_channel: null } {
  return { runner_image: null, runner_channel: null };
}
