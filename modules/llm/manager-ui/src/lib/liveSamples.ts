// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
//
// #1598 — the live chart's sample buffer, kept out of the component so it can
// be RUN in a test instead of read.
//
// The rule this file exists to enforce: A POINT IS A MEASUREMENT. The console
// polls faster than the nodes report — deliberately, so a new reading is on
// screen within a second of arriving — and the buffer must not turn that poll
// rate into data. Before this, `Dashboard.tsx` appended once per refetch: at a
// 5 s poll against a 30 s report interval, five of every six points were the
// previous reading drawn again, and the line looked like a live measurement of
// a fleet that had not been measured. That is the failure mode the issue calls
// "an animation without information", and it is invisible in the picture.
//
// So: the server stamps each sample (`metrics_at`), and `appendSample` refuses
// anything that is not NEWER than what it already holds. The x-axis is time,
// not sample index, so ten points across five minutes are drawn ten points
// apart — the picture then shows the real reporting rate instead of hiding it.

/** How often the console asks for new samples. Not how often they arrive —
 *  that is the node's `LLM_WORKER_FAST_BEAT_INTERVAL` (#1619, default 3 s). */
export const LIVE_POLL_MS = 1000;

/** Ranges offered for the live chart. First entry is the default (#1598). */
export const LIVE_RANGES: { label: string; ms: number }[] = [
  { label: "5 min", ms: 5 * 60_000 },
  { label: "15 min", ms: 15 * 60_000 },
  { label: "1 h", ms: 60 * 60_000 },
];

/** Kept in memory regardless of the selected range, so switching 5 min → 1 h
 *  shows the hour that was already collected instead of starting over. */
export const LIVE_RETAIN_MS = Math.max(...LIVE_RANGES.map((r) => r.ms));

export interface Sample { t: number; v: number }

/**
 * Append `v` measured at `t` — but only if `t` is newer than the last sample.
 *
 * Returns the SAME array reference when nothing was appended, so a caller can
 * cheaply tell "there was no new measurement" from "there was one" without
 * comparing contents. Mutates in place otherwise (this is a ring buffer on a
 * ref, called several times a second).
 *
 * Out-of-order and repeated stamps are both dropped rather than reordered: a
 * sample that arrives late is a sample whose place on the axis has already
 * been passed, and inserting it would redraw history under the operator.
 */
export function appendSample(buf: Sample[], t: number, v: number,
                             now: number = t, retainMs: number = LIVE_RETAIN_MS): Sample[] {
  if (!Number.isFinite(t) || !Number.isFinite(v)) return buf;
  const last = buf.length ? buf[buf.length - 1] : null;
  if (last && t <= last.t) return buf;
  buf.push({ t, v });
  const cutoff = now - retainMs;
  let drop = 0;
  while (drop < buf.length && buf[drop].t < cutoff) drop++;
  if (drop) buf.splice(0, drop);
  return buf;
}

/**
 * How far the SERVER's clock is behind the browser's, in ms.
 *
 * agent-seqis' finding, and it is the sharper half of this whole change: a
 * point carries the manager's stamp, while `Date.now()` drives the axis, the
 * window and the buffer's expiry. Let the two clocks drift apart and the
 * picture does not shift — it VANISHES. Measured against this module: with the
 * server six minutes behind, ten real samples sat in the buffer and the
 * five-minute chart drew none of them; past the retention span the expiry ate
 * the buffer as fast as it filled. Both silent, both while measurements kept
 * arriving every few seconds — the exact defect class this file was written
 * against, one level up.
 *
 * It is not an exotic case. An air-gapped box (#184) has no time source by
 * definition, and an RTC drifts into minutes over a few weeks.
 *
 * So the axis takes its clock FROM THE DATA: the newest stamp the console has
 * seen defines "now", and the browser clock only measures the seconds that
 * have passed since it arrived. Whichever clock the server keeps, point and
 * axis then share it.
 */
export function clockOffset(newestSampleT: number, receivedAtClient: number): number {
  if (!Number.isFinite(newestSampleT) || !Number.isFinite(receivedAtClient)) return 0;
  return receivedAtClient - newestSampleT;
}

/** "Now" on the SERVER's clock: the browser's clock, less the measured offset.
 *  With no sample yet the offset is 0 and this is just `nowClient` — the chart
 *  is empty at that point anyway. */
export function serverNow(nowClient: number, offsetMs: number): number {
  return nowClient - offsetMs;
}

/** The samples inside `[now - windowMs, now]`. Never copies more than it must. */
export function windowSlice(buf: Sample[], windowMs: number, now: number): Sample[] {
  const from = now - windowMs;
  let i = 0;
  while (i < buf.length && buf[i].t < from) i++;
  return i === 0 ? buf : buf.slice(i);
}

/** A reading as a percentage of its denominator, clamped, or null when either
 *  side is missing. A worker that reports no GPU is NOT a worker with an idle
 *  GPU — the difference has to survive all the way to the gauge. */
export function pct(used: number | null | undefined,
                    total: number | null | undefined): number | null {
  if (used == null || !total) return null;
  return Math.max(0, Math.min(100, (used / total) * 100));
}

/** Clamp a value that is already a percentage (gpu_util arrives as one). */
export function clampPct(v: number | null | undefined): number | null {
  return v == null ? null : Math.max(0, Math.min(100, v));
}
