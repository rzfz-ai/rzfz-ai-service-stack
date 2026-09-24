// Small shared presentational primitives + formatters. Hand-rolled (no
// component kit / chart lib) to keep the bundle lean + npm-audit surface small.
import { useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";

// Locale-free by design (EXO-15): follow the viewer's browser, exactly like
// Fleet.tsx / Deploy.tsx do. The console ships to any on-prem customer, so a
// hardcoded presentation locale here would contradict the rest of the app.
export function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function fixedLocale(n: number, digits: number): string {
  return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtCompact(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return n.toLocaleString();
  if (n < 1_000_000) return fixedLocale(n / 1000, n < 10_000 ? 1 : 0) + "k";
  return fixedLocale(n / 1_000_000, 1) + "M";
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "—";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 0) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// Map a free-form status string to a UI severity kind.
export function statusKind(status: string | null | undefined): "ok" | "warn" | "err" | "muted" {
  const s = (status || "").toLowerCase();
  if (["ready", "active", "healthy", "up", "registered"].includes(s)) return "ok";
  if (["scheduled", "pending", "starting", "loading", "pulling", "restarting", "backing_off", "unknown", "draining"].includes(s)) return "warn";
  // #1455 SCH3: an instance row whose worker stopped heartbeating is `lost`.
  if (["failed", "error", "revoked", "down", "dead", "unreachable", "lost"].includes(s)) return "err";
  return "muted";
}

// Actively-transitional states — the dot pulses so the console reads as "live,
// still working on it" vs a settled ready/failed (#288).
const TRANSITIONAL = new Set(["scheduled", "starting", "loading", "pulling", "restarting", "backing_off"]);

export function StatusDot({ status }: { status: string | null | undefined }) {
  const s = (status || "").toLowerCase();
  return <span className={`dot ${statusKind(status)}${TRANSITIONAL.has(s) ? " pulse" : ""}`} title={status || ""} />;
}

export function Badge({ kind, children }: { kind: "ok" | "warn" | "err" | "info" | "muted"; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>;
}

export function StatusBadge({ status }: { status: string }) {
  const kind = statusKind(status);
  return <Badge kind={kind === "muted" ? "muted" : kind}>{status}</Badge>;
}

export function StatTile({ k, value, sub }: { k: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="k">{k}</div>
      <div className="v num">{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  );
}

// Read-only key/value chips for engine params (editing = deferred control plane).
export function Params({ params }: { params: Record<string, unknown> }) {
  const entries = Object.entries(params ?? {});
  if (entries.length === 0) return <span className="muted">—</span>;
  return (
    <>
      {entries.map(([k, v]) => (
        <span key={k} className="chip">{k}={typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
      ))}
    </>
  );
}

export function Bar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="bar-track" title={`${fmt(value)} of ${fmt(max)}`}>
      <div className="bar-fill" style={{ width: `${pct}%` }} />
    </div>
  );
}

// --- toggle chip (worker filters on the dashboard) --------------------------
export function ToggleChip({ label, color, active, onClick }: {
  label: string; color?: string; active: boolean; onClick: () => void;
}) {
  return (
    <button type="button" className={`tchip${active ? " on" : ""}`} onClick={onClick} aria-pressed={active}>
      {color && <span className="tchip-dot" style={{ background: active ? color : "transparent", borderColor: color }} />}
      {label}
    </button>
  );
}

// --- hand-rolled time-series chart (no chart lib; #290/#311 dashboard) -------
// Multi-series line/area over a shared x. The SVG stretches to fill width
// (preserveAspectRatio none) with non-scaling strokes so lines stay crisp; axis
// text is HTML overlay so it never distorts. Theme-aware via CSS vars; honours
// prefers-reduced-motion (no transition) through the .ts-* classes.
// `id` is the React key when present — two workers may share a display name, in
// which case keying by `label` would collide (CUI-6).
// `dash` is an explicit SVG stroke-dasharray — it lets a caller use the dash
// pattern as a second identity axis (CUI-6); `dashed` stays as the boolean
// shorthand for the plain two-state case.
export interface ChartSeries {
  id?: string; label: string; color: string; values: number[];
  area?: boolean; dashed?: boolean; dash?: string;
  // #1598: when the readings were taken (epoch ms, one per value, ascending).
  // With it the point lands where its TIMESTAMP says, not where its index in
  // the array says — which is the whole difference between a chart that shows
  // how often the fleet reports and one that hides it behind the poll rate.
  // Series without it keep the even spacing every other caller relies on.
  at?: number[];
}

// --- #1598: animated utilisation gauges -------------------------------------
//
// The operator asked for GPUStack's four readings, "not with gauges, more
// modern, but also animated": a ring that runs up from 0 to the real value
// when the page loads, and afterwards eases from the PREVIOUS value to the new
// one. From 0 on every update would be a flicker, not an animation — the
// distinction is in `animatedValue(from, …)` plus the ref that holds the
// previous value.
//
// The two pure parts are exported so a test can run them; the component itself
// needs a browser.
export function easeOutCubic(t: number): number {
  const x = Math.min(1, Math.max(0, t));
  return 1 - Math.pow(1 - x, 3);
}

/** Value at `elapsed` ms of an eased run from `from` to `to` over `ms`. */
export function animatedValue(from: number, to: number, elapsed: number, ms: number): number {
  if (ms <= 0) return to;
  return from + (to - from) * easeOutCubic(elapsed / ms);
}

/** SVG dash offset for a ring of circumference `c` showing `pct` of 100. */
export function ringOffset(pct: number, c: number): number {
  return c * (1 - Math.min(100, Math.max(0, pct)) / 100);
}

/** Does this viewer ask for reduced motion? Read at call time, never cached —
 *  the setting can change while the page is open. Guarded: `matchMedia` is
 *  absent in a non-browser render. */
export function prefersReducedMotion(): boolean {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

export function Gauge({ label, pct, sub, color, ms = 900 }:
  { label: string; pct: number | null; sub?: ReactNode; color?: string; ms?: number }) {
  const target = pct == null ? 0 : Math.min(100, Math.max(0, pct));
  const [shown, setShown] = useState(0);
  // The value this run starts FROM: 0 on the first paint (the requested
  // run-up), the last shown value afterwards.
  const from = useRef(0);
  const raf = useRef<number | null>(null);
  useEffect(() => {
    // #1598 review: the reduced-motion preference has to be honoured HERE.
    // The CSS rule that used to sit next to `.gauge-arc` disabled a
    // `transition` — and this arc is not moved by one, it is driven by
    // requestAnimationFrame (deliberately: the run-up starts at 0 only on the
    // first paint and from the previous value afterwards, which a transition
    // cannot tell apart). So the rule read as "handled" and did nothing, which
    // is worse than no rule: it ends the inspection.
    if (prefersReducedMotion()) {
      from.current = target;
      setShown(target);
      return;
    }
    const start = from.current;
    const t0 = performance.now();
    const step = () => {
      const e = performance.now() - t0;
      setShown(animatedValue(start, target, e, ms));
      if (e < ms) { raf.current = requestAnimationFrame(step); }
      else { from.current = target; setShown(target); raf.current = null; }
    };
    raf.current = requestAnimationFrame(step);
    return () => { if (raf.current != null) cancelAnimationFrame(raf.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, ms]);

  const R = 26, C = 2 * Math.PI * R;
  return (
    <div className="gauge" title={pct == null ? `${label}: not reported` : `${label}: ${pct.toFixed(0)}%`}>
      <svg viewBox="0 0 64 64" className="gauge-svg" aria-hidden>
        <circle cx="32" cy="32" r={R} className="gauge-track" />
        <circle cx="32" cy="32" r={R} className="gauge-arc"
          style={{ stroke: color, strokeDasharray: C, strokeDashoffset: ringOffset(shown, C) }} />
      </svg>
      <div className="gauge-val num">{pct == null ? "—" : `${Math.round(shown)}%`}</div>
      <div className="gauge-lbl">{label}</div>
      {sub && <div className="gauge-sub muted">{sub}</div>}
    </div>
  );
}

// --- #1598: smooth lines, without lying about the data ----------------------
//
// The operator asked for GPUStack's curvy lines instead of the polyline this
// chart drew. The obvious spline (Catmull-Rom) is the wrong one here: it
// OVERSHOOTS between points, so a run of 98%, 100%, 98% bulges above 100 and a
// dip to 0 undercuts the axis. On a percentage chart that is a curve showing a
// value the fleet never reported.
//
// Monotone cubic (Fritsch-Carlson) is the interpolant that cannot do that: it
// passes through every sample and stays within the interval of its neighbours,
// so a smoothed line never claims a reading between two samples that is
// outside them. That property is asserted by SAMPLING the emitted curve, not
// by trusting this comment (tests/unit/llm-manager/test_1598_smooth_and_gauges.py).
//
// Exported because it is the only part of the chart a test can run.
export function monotoneTangents(xs: number[], ys: number[]): number[] {
  const n = xs.length;
  if (n < 2) return new Array(n).fill(0);
  const dx: number[] = [], dy: number[] = [], slope: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(xs[i + 1] - xs[i]);
    dy.push(ys[i + 1] - ys[i]);
    slope.push(dx[i] === 0 ? 0 : dy[i] / dx[i]);
  }
  const m: number[] = new Array(n);
  m[0] = slope[0];
  m[n - 1] = slope[n - 2];
  for (let i = 1; i < n - 1; i++) {
    // A local extremum gets a FLAT tangent — that is what keeps the curve from
    // sailing past the peak it is drawing.
    m[i] = slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2;
  }
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / slope[i], b = m[i + 1] / slope[i];
    const h = Math.hypot(a, b);
    if (h > 3) { m[i] = (3 * a / h) * slope[i]; m[i + 1] = (3 * b / h) * slope[i]; }
  }
  return m;
}

export function smoothPath(xs: number[], ys: number[]): string {
  const n = xs.length;
  if (n === 0) return "";
  if (n < 3) {
    // Two points have no curvature to describe; a straight segment is the
    // honest drawing, and it is also what the old code produced.
    return xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  }
  const m = monotoneTangents(xs, ys);
  let d = `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = (xs[i + 1] - xs[i]) / 3;
    d += ` C${(xs[i] + h).toFixed(2)},${(ys[i] + m[i] * h).toFixed(2)}` +
         ` ${(xs[i + 1] - h).toFixed(2)},${(ys[i + 1] - m[i + 1] * h).toFixed(2)}` +
         ` ${xs[i + 1].toFixed(1)},${ys[i + 1].toFixed(1)}`;
  }
  return d;
}

export function TimeSeriesChart({
  series, height = 150, max, topFormat = fmt, xLabels, empty = "No data yet.",
  smooth = false, xDomain,
}: {
  series: ChartSeries[];
  height?: number;
  max?: number;                       // fixed y-top (e.g. 100 for a percentage)
  topFormat?: (n: number) => string;  // formats the y-top label
  xLabels?: string[];                 // first + last shown under the plot
  empty?: ReactNode;
  // #1598: monotone-cubic instead of straight segments. An OPTION, not a new
  // default: the usage chart next to it plots hourly buckets, where a curve
  // between two buckets would suggest readings that were never taken.
  smooth?: boolean;
  // #1598: [from, to] in epoch ms for series that carry `at`. The window the
  // operator CHOSE, not the extent of the data — three samples in a five-minute
  // window belong at the right-hand edge, marching left, not stretched across
  // the whole plot as if they spanned it.
  xDomain?: [number, number];
}) {
  const W = 640, H = height, padT = 6, padB = 2;
  const n = Math.max(0, ...series.map((s) => s.values.length));
  const hasData = series.some((s) => s.values.length > 0);
  // #1598 (agent-seqis): ONE sample used to fall into the empty state, so a
  // freshly-opened page said "collecting…" while holding a real reading. A
  // line needs two points; a measurement does not — it is drawn as a dot.
  if (n < 1 || !hasData) return <div className="chart-empty muted">{empty}</div>;
  const top = max ?? Math.max(1, ...series.flatMap((s) => s.values.filter((v) => Number.isFinite(v))));
  // Time axis when the series say WHEN; index axis otherwise (every pre-#1598
  // caller). The domain is the chosen window if one was given, else the extent
  // of the stamps — never a mix, so a point cannot land in two places.
  const stamps = series.flatMap((s) => s.at ?? []);
  const timed = stamps.length > 0;
  const [t0, t1] = xDomain ?? [Math.min(...stamps), Math.max(...stamps)];
  const span = t1 - t0;
  const X = (i: number) => (i / (n - 1)) * W;
  const Xt = (t: number) => (span > 0 ? ((t - t0) / span) * W : W);
  const Y = (v: number) => padT + (1 - Math.min(Math.max(v, 0), top) / top) * (H - padT - padB);
  const xs = (s: ChartSeries) => (timed && s.at
    ? s.values.map((_, i) => Xt(s.at![i]))
    : s.values.map((_, i) => X(i)));
  const path = (s: ChartSeries) => {
    const px = xs(s), py = s.values.map((v) => Y(v ?? 0));
    return smooth
      ? smoothPath(px, py)
      : px.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${py[i].toFixed(1)}`).join(" ");
  };
  const area = (s: ChartSeries) => {
    const px = xs(s);
    const right = px.length ? px[px.length - 1] : W, left = px.length ? px[0] : 0;
    return `${path(s)} L${right.toFixed(1)},${H - padB} L${left.toFixed(1)},${H - padB} Z`;
  };
  return (
    <div className="chart">
      <div className="chart-plot" style={{ height: H }}>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="ts-svg" aria-hidden>
          {[0, 0.5, 1].map((g) => {
            const gy = padT + (1 - g) * (H - padT - padB);
            return <line key={g} x1={0} x2={W} y1={gy} y2={gy} className="ts-grid" vectorEffect="non-scaling-stroke" />;
          })}
          {series.map((s, si) => (
            <g key={s.id ?? `${s.label}#${si}`}>
              {s.area && s.values.length > 1 && <path d={area(s)} className="ts-area" style={{ fill: s.color }} />}
              {s.values.length > 1 && (
                <path d={path(s)} className="ts-line" style={{ stroke: s.color }}
                  strokeDasharray={s.dash ?? (s.dashed ? "5 3" : undefined)} vectorEffect="non-scaling-stroke" />
              )}
              {s.values.length === 1 && (
                <circle cx={xs(s)[0]} cy={Y(s.values[0] ?? 0)} r={3}
                  className="ts-dot" style={{ fill: s.color }} vectorEffect="non-scaling-stroke" />
              )}
            </g>
          ))}
        </svg>
        {/* value labels at each gridline (top / mid / 0) so the axis is readable */}
        {[1, 0.5, 0].map((g) => {
          const gy = padT + (1 - g) * (H - padT - padB);
          return <span key={g} className="ts-ylabel num" style={{ top: `${gy}px` }}>{topFormat(top * g)}</span>;
        })}
      </div>
      {xLabels && xLabels.length >= 2 && (
        <div className="ts-x"><span>{xLabels[0]}</span><span>{xLabels[xLabels.length - 1]}</span></div>
      )}
    </div>
  );
}

// --- hand-rolled stacked bar chart (GPUStack-style usage-per-bucket) ---------
// One bar per bucket; each bar stacks its segments bottom-up (e.g. input then
// output tokens). Same visual language as TimeSeriesChart (grid + y-top label +
// x labels), theme-aware via CSS vars. Colours come from the segment defs.
export interface BarSegment { label: string; color: string; values: number[]; }

export function StackedBarChart({
  segments, height = 150, topFormat = fmtCompact, xLabels, empty = "No data yet.",
}: {
  segments: BarSegment[];
  height?: number;
  topFormat?: (n: number) => string;
  xLabels?: string[];
  empty?: ReactNode;
}) {
  const W = 640, H = height, padT = 6, padB = 2;
  const n = Math.max(0, ...segments.map((s) => s.values.length));
  const totals = Array.from({ length: n }, (_, i) => segments.reduce((a, s) => a + (s.values[i] || 0), 0));
  const hasData = totals.some((t) => t > 0);
  if (n < 1 || !hasData) return <div className="chart-empty muted">{empty}</div>;
  const top = Math.max(1, ...totals);
  const slot = W / n;
  const bw = Math.min(slot * 0.72, 40);
  const scaleH = (v: number) => (Math.max(v, 0) / top) * (H - padT - padB);
  return (
    <div className="chart">
      <div className="chart-plot" style={{ height: H }}>
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="ts-svg" aria-hidden>
          {[0, 0.5, 1].map((g) => {
            const gy = padT + (1 - g) * (H - padT - padB);
            return <line key={g} x1={0} x2={W} y1={gy} y2={gy} className="ts-grid" vectorEffect="non-scaling-stroke" />;
          })}
          {totals.map((_, i) => {
            const x = i * slot + (slot - bw) / 2;
            let y = H - padB;
            return (
              <g key={i}>
                {segments.map((s) => {
                  const h = scaleH(s.values[i] || 0);
                  if (h <= 0) return null;
                  y -= h;
                  return <rect key={s.label} x={x} y={y} width={bw} height={h} fill={s.color} />;
                })}
              </g>
            );
          })}
        </svg>
        {[1, 0.5, 0].map((g) => {
          const gy = padT + (1 - g) * (H - padT - padB);
          return <span key={g} className="ts-ylabel num" style={{ top: `${gy}px` }}>{topFormat(top * g)}</span>;
        })}
      </div>
      {xLabels && xLabels.length >= 2 && (
        <div className="ts-x"><span>{xLabels[0]}</span><span>{xLabels[xLabels.length - 1]}</span></div>
      )}
      <div className="ts-legend">
        {segments.map((s) => (
          <span key={s.label} className="ts-leg"><span className="ts-leg-dot" style={{ background: s.color }} />{s.label}</span>
        ))}
      </div>
    </div>
  );
}

// Row-activation props: makes a table <tr> behave like a button — pointer +
// keyboard (Enter / Space) + focus ring — so the WHOLE row opens the detail
// drawer, not just a "Details" button (operator complaint #1a).
export function rowProps(onOpen: () => void, selected = false) {
  return {
    className: `clickable${selected ? " selected" : ""}`,
    tabIndex: 0,
    "aria-selected": selected,
    onClick: onOpen,
    onKeyDown: (e: ReactKeyboardEvent) => {
      // Only act when the ROW itself is focused — not a button/input inside it
      // (otherwise Enter on an in-row action would also open the drawer).
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpen();
      }
    },
  };
}

// --- loading skeletons (shimmer) --------------------------------------------
export function Skeleton({ w = "100%", h = 14, r }: { w?: number | string; h?: number | string; r?: number | string }) {
  return <span className="skl" style={{ width: w, height: h, borderRadius: r }} aria-hidden />;
}

// A table-shaped loading placeholder — reads as "a table is coming", not a
// bare spinner. Pass the column count so the shimmer matches the real table.
export function TableSkeleton({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="table-wrap" aria-busy="true" aria-label="Loading…">
      <table className="rz">
        <tbody>
          {Array.from({ length: rows }).map((_, r) => (
            <tr key={r}>
              {Array.from({ length: cols }).map((__, c) => (
                <td key={c}><Skeleton w={c === 0 ? "60%" : `${40 + ((r + c) % 3) * 15}%`} /></td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --- detail drawer (right slide-in; click a row to open) --------------------
// Resizable: drag (or arrow-key) the left edge to widen — the log pane in the
// worker detail was too narrow and wrapped badly (operator complaint #1c). The
// chosen width persists per storageKey in localStorage.
const DRAWER_MIN = 380;
function drawerMax() {
  return Math.round((typeof window !== "undefined" ? window.innerWidth : 1200) * 0.96);
}

export function Drawer({
  title,
  onClose,
  children,
  storageKey = "rzfz.drawer.width",
  defaultWidth = 560,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  storageKey?: string;
  defaultWidth?: number;
}) {
  const [width, setWidth] = useState<number>(() => {
    try {
      const saved = Number(localStorage.getItem(storageKey));
      if (Number.isFinite(saved) && saved >= DRAWER_MIN) return Math.min(saved, drawerMax());
    } catch { /* ignore */ }
    return defaultWidth;
  });
  const dragging = useRef(false);
  const widthRef = useRef(width);
  widthRef.current = width;
  const panelRef = useRef<HTMLElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);

  // CUI-7: aria-modal="true" tells AT that everything outside the dialog does
  // not exist — so focus has to actually live inside it. Move focus in on
  // mount, cycle Tab within the panel, restore the opener on unmount.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>(".drawer-x");
    (first ?? panel)?.focus();
    return () => {
      if (opener && typeof opener.focus === "function" && document.contains(opener)) opener.focus();
    };
  }, []);

  function trapTab(e: ReactKeyboardEvent) {
    if (e.key !== "Tab") return;
    const panel = panelRef.current;
    if (!panel) return;
    const focusables = Array.from(
      panel.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (focusables.length === 0) { e.preventDefault(); panel.focus(); return; }
    const firstEl = focusables[0], lastEl = focusables[focusables.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (e.shiftKey && (active === firstEl || active === panel)) { e.preventDefault(); lastEl.focus(); }
    else if (!e.shiftKey && active === lastEl) { e.preventDefault(); firstEl.focus(); }
  }

  useEffect(() => {
    function onMove(e: PointerEvent) {
      if (!dragging.current) return;
      const w = Math.round(window.innerWidth - e.clientX);
      setWidth(Math.max(DRAWER_MIN, Math.min(w, drawerMax())));
    }
    function onUp() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
      try { localStorage.setItem(storageKey, String(widthRef.current)); } catch { /* ignore */ }
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [storageKey]);

  function startDrag(e: ReactPointerEvent) {
    e.preventDefault();
    dragging.current = true;
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  }
  function keyResize(e: ReactKeyboardEvent) {
    const step = e.shiftKey ? 48 : 16;
    if (e.key === "ArrowLeft") { e.preventDefault(); setWidth((w) => Math.min(drawerMax(), w + step)); }
    else if (e.key === "ArrowRight") { e.preventDefault(); setWidth((w) => Math.max(DRAWER_MIN, w - step)); }
  }
  useEffect(() => {
    // persist arrow-key resizes too (no pointerup to hook)
    const id = setTimeout(() => { try { localStorage.setItem(storageKey, String(width)); } catch { /* ignore */ } }, 250);
    return () => clearTimeout(id);
  }, [width, storageKey]);

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={panelRef}
        onKeyDown={trapTab}
        style={{ width, maxWidth: "96vw" }}
      >
        <div
          className="drawer-resize"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panel"
          tabIndex={0}
          onPointerDown={startDrag}
          onKeyDown={keyResize}
          title="Drag to resize (← → to nudge)"
        />
        <div className="drawer-head">
          <h2 id={titleId}>{title}</h2>
          <button className="drawer-x" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}

// --- toasts (lightweight module pub-sub; <Toaster/> mounted in the shell) ----
export interface Toast { id: number; msg: string; kind: "ok" | "err" | "info" | "warn" }
let _tid = 0;
let _toasts: Toast[] = [];
const _subs = new Set<(t: Toast[]) => void>();
export function toast(msg: string, kind: Toast["kind"] = "info") {
  const t: Toast = { id: ++_tid, msg, kind };
  _toasts = [..._toasts, t];
  _subs.forEach((f) => f(_toasts));
  setTimeout(() => {
    _toasts = _toasts.filter((x) => x.id !== t.id);
    _subs.forEach((f) => f(_toasts));
  }, 4500);
}
export function Toaster() {
  const [ts, setTs] = useState<Toast[]>(_toasts);
  useEffect(() => { _subs.add(setTs); return () => { _subs.delete(setTs); }; }, []);
  if (ts.length === 0) return null;
  return (
    <div className="toast-wrap">
      {ts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.msg}</div>)}
    </div>
  );
}

// Query-state wrapper: shows loading / error / empty, else renders children.
export function QueryState<T>({
  q,
  isEmpty,
  loading,
  empty,
  children,
}: {
  q: UseQueryResult<T>;
  isEmpty?: (d: T) => boolean;
  loading?: ReactNode;   // custom loading view (e.g. <TableSkeleton/>)
  empty?: ReactNode;     // custom empty view
  children: (data: T) => ReactNode;
}) {
  if (q.isLoading) return <>{loading ?? <div className="loading">Loading…</div>}</>;
  if (q.isError) {
    const msg = q.error instanceof Error ? q.error.message : "request failed";
    return <div className="callout err">Could not load: {msg}</div>;
  }
  if (q.data == null) return <div className="empty">No data.</div>;
  if (isEmpty && isEmpty(q.data)) return <>{empty ?? <div className="empty">Nothing here yet.</div>}</>;
  return <>{children(q.data)}</>;
}

// --- minimal, XSS-safe markdown (README preview, #295) ----------------------
// No markdown dependency (keeps the bundle lean + audit surface small). Code
// fences are extracted first, ALL content is HTML-escaped, then a small subset
// (headings, bold, inline code, links, paragraphs) is re-applied — so nothing
// in the source README can inject markup. Links are http(s) with a quote-free
// URL, so a crafted model card can't break out of the href attribute.
function renderMarkdown(md: string): string {
  const esc = (t: string) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // HF cards are full of raw HTML (badge rows, <div> wrappers, screenshots). We
  // don't render arbitrary HTML — instead convert <img> to a (CSS-constrained)
  // markdown image, drop <a>/<br>, and strip every other tag to its text, so the
  // card reads as prose + bounded images instead of dumping <div style=…> literally.
  // CUI-10 (#1038): pull fenced code blocks out FIRST — before any tag
  // stripping — and put a placeholder line in their place. Model cards routinely
  // fence HTML/XML/JSX, chat templates (<|im_start|>, <tool_call>) and engine
  // invocations with <…> placeholders; running the tag-strippers over the whole
  // document gutted all of it. The fence bodies are restored verbatim (and still
  // esc()'d) when the placeholder is rendered, so nothing about the XSS posture
  // changes: fenced content never reaches the inline() markup pass.
  const FENCE_MARK = "\u0000";
  const fences: string[] = [];
  const base = (md || "").replace(/\r\n/g, "\n").replace(/\u0000/g, "")
    .replace(/^---\n[\s\S]*?\n---\n/, "");
  const held: string[] = [];
  let fenceBuf: string[] | null = null;
  const closeFence = () => {
    fences.push((fenceBuf as string[]).join("\n"));
    held.push(`${FENCE_MARK}${fences.length - 1}${FENCE_MARK}`);
    fenceBuf = null;
  };
  for (const raw of base.split("\n")) {
    const isFence = /^\s*```/.test(raw);
    if (fenceBuf !== null) {
      if (isFence) closeFence(); else fenceBuf.push(raw);
      continue;
    }
    if (isFence) { fenceBuf = []; continue; }
    held.push(raw);
  }
  if (fenceBuf !== null) closeFence();   // unterminated fence: render what we have
  const src = held.join("\n")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, "")
    .replace(/<img\b[^>]*?\bsrc=["']?(https?:\/\/[^"'\s>]+)[^>]*>/gi, "\n![]($1)\n")
    .replace(/<\/?a\b[^>]*>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "");
  // inline: image STUBS + http(s) links (quote-free URL so no href break-out) + bold/em/code.
  // #347: no remote <img>. A real src gave any HF author a tracking beacon inside
  // an SSO-gated admin console (operator IP + which model they evaluate) and hung
  // on air-gapped boxes; the site CSP (img-src 'self' data:) now blocks such loads
  // anyway, so render an explicit stub instead of a broken-image icon. The alt
  // text goes into element TEXT only — esc() does not escape quotes, so it must
  // never land in an attribute.
  const inline = (t: string) => esc(t)
    .replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s"'<>]+)\)/g,
      (_m, alt) => `<span class="md-imgstub">${alt || "image"}</span>`)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s"'<>]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
  const out: string[] = [];
  let para: string[] = [], list: string[] = [];
  const FENCE_REF = /^\u0000(\d+)\u0000$/;
  const flushP = () => { if (para.length) { out.push(`<p>${para.map(inline).join(" ")}</p>`); para = []; } };
  const flushL = () => { if (list.length) { out.push(`<ul>${list.map((li) => `<li>${inline(li)}</li>`).join("")}</ul>`); list = []; } };
  // GFM table helpers: a header row with a pipe followed by a --- separator row.
  const isTableSep = (s: string) => /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/.test(s) && s.includes("-");
  const cells = (s: string) => s.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const lines = src.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = raw.replace(/\s+$/, "");
    let m: RegExpMatchArray | null;
    // a fence placeholder stands alone on its line — restore the block verbatim
    if ((m = raw.match(FENCE_REF))) {
      flushP(); flushL();
      out.push(`<pre class="md-code">${esc(fences[Number(m[1])] ?? "")}</pre>`);
      continue;
    }
    if (!line.trim()) { flushP(); flushL(); continue; }
    // GFM table: current line has a pipe AND the next line is a --- separator.
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushP(); flushL();
      const head = cells(line);
      const body: string[][] = [];
      i += 2; // consume header + separator
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) { body.push(cells(lines[i])); i++; }
      i--; // the for-loop will ++ back
      const th = head.map((c) => `<th>${inline(c)}</th>`).join("");
      const rows = body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("");
      out.push(`<div class="md-table-wrap"><table class="md-table"><thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table></div>`);
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) { flushP(); flushL(); out.push("<hr/>"); continue; }
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushP(); flushL();
      const lvl = Math.min(m[1].length + 1, 6);
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`); continue;
    }
    if ((m = line.match(/^\s*(?:[-*+]|\d+\.)\s+(.*)$/))) { flushP(); list.push(m[1]); continue; }
    if ((m = line.match(/^>\s?(.*)$/))) { flushP(); flushL(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    para.push(line);
  }
  flushP(); flushL();
  return out.join("\n");
}

export function Markdown({ md }: { md: string }) {
  return <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(md) }} />;
}
