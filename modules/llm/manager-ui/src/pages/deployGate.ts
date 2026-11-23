// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
// #2394 — the unverified-model gate, as pure functions (tested through node).

export function unverifiedNotice(detail: unknown): string | null {
  if (detail && typeof detail === "object" && (detail as { code?: string }).code === "unverified_ack_required") {
    const n = (detail as { notice?: unknown }).notice;
    return typeof n === "string" ? n : null;
  }
  return null;
}

export function canResubmit(notice: string | null, acknowledged: boolean): boolean {
  return notice !== null && acknowledged;
}

export function verificationBadge(v?: string): { label: string; tone: "ok" | "warn" } {
  return v === "verified" ? { label: "verified", tone: "ok" } : { label: "unverified", tone: "warn" };
}

// #2426 ask 3: the manager answers null when the box is online, false when it is
// offline and no worker holds the entry's files (the verdict G17 computes for
// verify-models: on-demand for this class and absent).
export function offlineBadge(v: boolean | null | undefined): string | null {
  return v === false ? "not available offline" : null;
}

// #2962: the catalogue's model risk flag. "denied-only" = run agents on this
// model only with egress denied (AGENT_EGRESS_POLICY=allowlist); nothing else
// is a flag today, and an absent value shows nothing.
export function egressBadge(v?: string | null): { label: string; title: string } | null {
  if (v !== "denied-only") return null;
  return {
    label: "egress denied only",
    title: "Risk flag from the model catalogue: this model was seen reaching for cloud storage and metadata addresses unasked. Run agents on it only with egress denied (AGENT_EGRESS_POLICY=allowlist).",
  };
}

export const ACK_PROMPT = "I understand and want to deploy this unverified model.";

// #2394: the one-click deploy paths (Catalog, Fleet "Deploy from cache") that
// have no editor to hold a checkbox. `send` is tried as is; when the manager
// answers with the unverified-model notice, the operator is shown it and the
// deploy is resubmitted with the acknowledgement only on an explicit yes
// (null = declined). Any other error propagates unchanged.
export async function sendWithUnverifiedAck<T>(
  send: (body: Record<string, unknown>) => Promise<T>,
  body: Record<string, unknown>,
  confirmFn: (msg: string) => boolean,
): Promise<T | null> {
  try {
    return await send(body);
  } catch (err) {
    const n = unverifiedNotice((err as { detail?: unknown } | null)?.detail);
    if (!n) throw err;
    if (!confirmFn(`${n}\n\n${ACK_PROMPT}`)) return null;
    return await send({ ...body, acknowledge_unverified: true });
  }
}
