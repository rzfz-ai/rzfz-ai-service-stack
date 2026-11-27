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
// #3394: what the operator confirms — the risk, nothing the box enforces
// (the notice above it recommends egress denied and names the box's
// AGENT_EGRESS_POLICY). docs/enterprise/how-to/llm-deployments.md quotes
// this text; tests/unit/llm-manager-ui/test_3394_egress_ack_gate.py keeps
// the two equal.
export const EGRESS_ACK_PROMPT = "I understand the risk flag and want to deploy this model.";

// #3394: the manager's notice before a deploy of a model that carries a risk
// flag (its catalogue entry or a risk family such as Qwen3.8). Not a refusal:
// the deploy goes through once the operator acknowledges it.
export function egressNotice(detail: unknown): string | null {
  if (detail && typeof detail === "object" && (detail as { code?: string }).code === "egress_ack_required") {
    const n = (detail as { notice?: unknown }).notice;
    return typeof n === "string" ? n : null;
  }
  return null;
}

// The acknowledgements a deploy can need: the request field that carries each
// and the question the one-click paths ask. A deploy can need both — the
// manager asks for the egress one first, the unverified one after it.
export type AckKind = "egress" | "unverified";
export const ACKS: Record<AckKind, { field: string; prompt: string; notice: (d: unknown) => string | null }> = {
  egress: { field: "acknowledge_egress_risk", prompt: EGRESS_ACK_PROMPT, notice: egressNotice },
  unverified: { field: "acknowledge_unverified", prompt: ACK_PROMPT, notice: unverifiedNotice },
};

// Which acknowledgement a refusal asks for, with its notice; null for any
// other error.
export function ackNeeded(detail: unknown): { kind: AckKind; notice: string } | null {
  for (const kind of Object.keys(ACKS) as AckKind[]) {
    const notice = ACKS[kind].notice(detail);
    if (notice !== null) return { kind, notice };
  }
  return null;
}

// #2394 / #3394: the one-click deploy paths (Catalog, Fleet "Deploy from
// cache") that have no editor to hold a checkbox. `send` is tried as is; each
// time the manager answers with a notice that needs an acknowledgement, the
// operator is shown it and the deploy is resubmitted with that
// acknowledgement added — only on an explicit yes (null = declined). A notice
// that was already acknowledged, or any other error, propagates unchanged.
export async function sendWithAcks<T>(
  send: (body: Record<string, unknown>) => Promise<T>,
  body: Record<string, unknown>,
  confirmFn: (msg: string) => boolean,
): Promise<T | null> {
  let cur: Record<string, unknown> = { ...body };
  for (;;) {
    try {
      return await send(cur);
    } catch (err) {
      const need = ackNeeded((err as { detail?: unknown } | null)?.detail);
      if (!need) throw err;
      const ack = ACKS[need.kind];
      if (cur[ack.field] === true) throw err;
      if (!confirmFn(`${need.notice}\n\n${ack.prompt}`)) return null;
      cur = { ...cur, [ack.field]: true };
    }
  }
}

// #2394: the original name of the one-click helper; both acknowledgements now.
export const sendWithUnverifiedAck = sendWithAcks;
