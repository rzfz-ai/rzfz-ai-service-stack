import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { endpoints } from "../api/client";
import {
  appendSample, windowSlice, pct, clampPct, clockOffset, serverNow,
  LIVE_POLL_MS, LIVE_RANGES, LIVE_RETAIN_MS, type Sample,
} from "../lib/liveSamples";
import {
  Badge, Bar, fmt, fmtCompact, rowProps, Skeleton, StatTile, StatusBadge, StatusDot,
  ToggleChip, TimeSeriesChart, StackedBarChart, type ChartSeries, type BarSegment,
  Gauge,
} from "../components/ui";

// per-worker line colours — brand palette only: #CD1719 then distinct mid-greys
// (mid-tones so they read on BOTH light and dark themes; no blue/other hues).
const WORKER_COLORS = ["#CD1719", "#8C8C8C", "#B8B8B8", "#5C5C5C", "#A0A0A0", "#6E6E6E", "#C4C4C4"];
// Load-graph metrics — one line per (selected worker × metric), all as % (y-max
// 100). Brand palette only (#CD1719 + greys; no blue). GPU load = gpu_util;
// VRAM = vram_used/vram_total; CPU RAM = mem_used/mem_total; CPU load = load/ncpu.
const LOAD_METRICS = [
  { key: "gpu", label: "GPU load", color: "#CD1719" },
  { key: "vram", label: "VRAM", color: "#8C8C8C" },
  { key: "ram", label: "CPU RAM", color: "#B8B8B8" },
  { key: "cpu", label: "CPU load", color: "#5C5C5C" },
] as const;
type LoadKey = (typeof LOAD_METRICS)[number]["key"];
// CUI-6: with several workers on one chart, hue carries the WORKER (matching the
// filter chips) and the dash pattern carries the metric. One line per (worker ×
// metric) all sharing a metric colour, with only "worker #1 solid / everyone
// else dashed" to tell them apart, made worker #2/#3/#4 literally identical.
const LOAD_DASH: Record<LoadKey, string | undefined> = {
  gpu: undefined,        // solid
  vram: "6 3",           // dashed
  ram: "2 3",            // dotted
  cpu: "10 3 2 3",       // dash-dot
};
const RANGES: { label: string; days: number; bucket: "hour" | "day" }[] = [
  { label: "24h", days: 1, bucket: "hour" },
  { label: "7d", days: 7, bucket: "day" },
  { label: "30d", days: 30, bucket: "day" },
];
// #1598: there is no sample-COUNT window any more. `LOAD_WINDOW = 60` claimed
// "~5 min of 5s samples" and was wrong twice over: the console polled every 5 s
// but the nodes reported every 30 s, so sixty points were ten measurements and
// fifty re-draws. The window is now wall-clock time (`LIVE_RANGES`) and a point
// is appended only when the server's sample stamp moves — see lib/liveSamples.

// Zero-fill the usage series across the FULL selected window so the x-axis spans
// (now-range) → now with empty buckets shown as gaps — instead of collapsing to
// only the populated buckets (operator: "usage only shows 00:00–02:00"). Buckets
// are UTC-truncated to match the server's date_trunc(hour|day, ts).
function zeroFillUsage<T extends { bucket: string; input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number; events: number }>(
  data: T[], range: { days: number; bucket: "hour" | "day" },
): T[] {
  const stepMs = range.bucket === "hour" ? 3_600_000 : 86_400_000;
  const count = range.bucket === "hour" ? 24 : range.days;
  const trunc = (ms: number) => {
    const d = new Date(ms);
    if (range.bucket === "hour") d.setUTCMinutes(0, 0, 0); else d.setUTCHours(0, 0, 0, 0);
    return d.getTime();
  };
  const by = new Map(data.map((p) => [trunc(Date.parse(p.bucket)), p] as const));
  const start = trunc(Date.now()) - (count - 1) * stepMs;
  const out: T[] = [];
  for (let i = 0; i < count; i++) {
    const b = start + i * stepMs;
    out.push(by.get(b) ?? ({ bucket: new Date(b).toISOString(), input_tokens: 0, output_tokens: 0, cached_tokens: 0, total_tokens: 0, events: 0 } as T));
  }
  return out;
}

export function Dashboard() {
  const navigate = useNavigate();
  // #290 live: fleet + deployments poll every 5s so the dashboard reflects
  // engines coming up / failing without a manual refresh.
  const workers = useQuery({ queryKey: ["workers"], queryFn: endpoints.workers, retry: false, refetchInterval: 5000 });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: endpoints.deployments, retry: false, refetchInterval: 5000 });
  const keys = useQuery({ queryKey: ["keys"], queryFn: endpoints.keys, retry: false });
  const usageToday = useQuery({
    queryKey: ["usage", "today", "model"],
    queryFn: () => endpoints.usage({ since_days: 1, group: "model" }),
    retry: false, refetchInterval: 30000,
  });
  // #311 live performance snapshot (requests, latency, failover, rejects, per-model).
  const stats = useQuery({ queryKey: ["stats"], queryFn: endpoints.stats, retry: false, refetchInterval: 5000 });

  // --- advanced dashboard: worker filter chips + live load graph ------------
  // Which workers are plotted on the load graph (all by default). Deselect a
  // chip to hide its line. null = "not initialised yet" (select-all on first data).
  const [sel, setSel] = useState<Set<string> | null>(null);
  // #1598 which slice of the collected history the live chart shows. The buffer
  // always keeps LIVE_RETAIN_MS, so switching 5 min → 1 h shows the hour that
  // was already there instead of starting to collect again.
  const [liveRangeIdx, setLiveRangeIdx] = useState(0);
  const liveRange = LIVE_RANGES[liveRangeIdx];

  // #1598 the live feed: five numbers per worker plus the SERVER stamp saying
  // when the node measured them, polled once a second on its own lean route.
  // The fleet table, the deployments and the stats keep their 5 s poll on the
  // heavy routes — this one exists precisely so the chart does not drag them
  // along at its own rate.
  const live = useQuery({
    queryKey: ["workerMetrics"], queryFn: endpoints.workerMetrics,
    retry: false, refetchInterval: LIVE_POLL_MS,
  });

  // Per-worker, per-metric ring buffers of {t, v}. A point is a MEASUREMENT:
  // `appendSample` drops anything whose stamp has not moved, so polling faster
  // than the nodes report adds nothing to the picture — which is the whole
  // point, because the x-axis is time and a slow node must LOOK slow.
  const loadHist = useRef<Map<string, Record<LoadKey, Sample[]>>>(new Map());
  // #1598 (agent-seqis' finding): the axis takes its clock FROM THE DATA. A
  // point carries the manager's stamp; if `Date.now()` drove the window as
  // well, a box whose clock differs by more than the window — an air-gapped
  // one has no time source at all (#184) — would show an empty chart while
  // samples kept arriving. Measured: server 6 min behind, 10 samples in the
  // buffer, 0 drawn.
  const clockSkew = useRef(0);
  const [, bumpLoad] = useState(0);
  useEffect(() => {
    if (!live.data) return;
    const hist = loadHist.current;
    const seen = new Set<string>();
    const newest = Math.max(0, ...live.data.map((w) => (w.at ?? 0) * 1000));
    if (newest > 0) clockSkew.current = clockOffset(newest, Date.now());
    const now = serverNow(Date.now(), clockSkew.current);
    let appended = false;
    for (const w of live.data) {
      seen.add(w.id);
      // No stamp → an agent older than #1598 that never says when it measured.
      // Skipped rather than stamped here: inventing a time on arrival would
      // draw the poll rate again, in the one place built to stop that.
      if (w.at == null) continue;
      const t = w.at * 1000;   // server seconds → epoch ms
      const pcts: Record<LoadKey, number | null> = {
        gpu: clampPct(w.gpu_util),
        vram: pct(w.vram_used_gb, w.vram_total_gb),
        ram: pct(w.mem_used_gb, w.mem_total_gb),
        cpu: w.ncpu ? pct(w.load, w.ncpu) : null,
      };
      const buf = hist.get(w.id) ?? { gpu: [], vram: [], ram: [], cpu: [] };
      for (const mk of LOAD_METRICS) {
        const v = pcts[mk.key];
        if (v == null) continue;   // absent metric: no line, not a zero line
        const before = buf[mk.key].length;
        appendSample(buf[mk.key], t, v, now, LIVE_RETAIN_MS);
        if (buf[mk.key].length !== before) appended = true;
      }
      hist.set(w.id, buf);
    }
    for (const id of [...hist.keys()]) if (!seen.has(id)) hist.delete(id);
    if (appended) bumpLoad((n) => n + 1);
  }, [live.dataUpdatedAt]); // eslint-disable-line react-hooks/exhaustive-deps

  // first fleet data → select every worker
  useEffect(() => {
    if (!workers.data) return;
    setSel((cur) => cur ?? new Set((workers.data ?? []).map((w) => w.id)));
  }, [workers.dataUpdatedAt]); // eslint-disable-line react-hooks/exhaustive-deps

  // #1598: the CURRENT reading per metric, averaged over the workers the chips
  // have selected. Missing readings are left OUT of the average rather than
  // counted as 0 — a worker that reports no GPU is not a worker with an idle
  // GPU, and averaging the two together is how a fleet gauge starts lying.
  // Fed from the live route as well, so the rings move with the chart rather
  // than lagging it by up to five seconds.
  const { gaugeNow, gaugeWorkers } = useMemo(() => {
    const chosen = (live.data ?? []).filter((w) => !sel || sel.has(w.id));
    const acc: Record<LoadKey, number[]> = { gpu: [], vram: [], ram: [], cpu: [] };
    for (const w of chosen) {
      const push = (k: LoadKey, v: number | null) => { if (v != null) acc[k].push(v); };
      push("gpu", clampPct(w.gpu_util));
      push("vram", pct(w.vram_used_gb, w.vram_total_gb));
      push("ram", pct(w.mem_used_gb, w.mem_total_gb));
      push("cpu", w.ncpu ? pct(w.load, w.ncpu) : null);
    }
    const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
    return {
      gaugeNow: { gpu: avg(acc.gpu), vram: avg(acc.vram), ram: avg(acc.ram), cpu: avg(acc.cpu) } as Record<LoadKey, number | null>,
      gaugeWorkers: chosen.length,
    };
  }, [live.dataUpdatedAt, sel]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- advanced dashboard: usage-over-time graph (historical, date-range) ---
  const [rangeIdx, setRangeIdx] = useState(0);
  const range = RANGES[rangeIdx];
  const usageSeries = useQuery({
    queryKey: ["usageSeries", range.days],
    queryFn: () => endpoints.usageSeries({
      from: new Date(Date.now() - range.days * 86_400_000).toISOString(),
      bucket: range.bucket,
    }),
    retry: false, refetchInterval: 60_000,
  });

  const nodes = workers.data ?? [];
  const readyNodes = nodes.filter((w) => w.status === "ready").length;
  const deps = deployments.data ?? [];
  const readyInstances = deps.reduce((a, d) => a + d.ready_instances, 0);
  // #290 engine-phase rollup + what needs attention
  const insts = deps.flatMap((d) => d.instances);
  const engReady = insts.filter((i) => i.status === "ready").length;
  const engFailed = insts.filter((i) => i.status === "failed").length;
  const engBusy = insts.filter((i) => ["loading", "pulling", "restarting", "backing_off"].includes(i.status)).length;
  const attention = deps.filter((d) => d.health === "failed" || d.health === "degraded");
  const activeKeys = (keys.data ?? []).filter((k) => k.status === "active").length;
  const totalKeys = keys.data?.length ?? 0;
  const today = usageToday.data ?? [];
  const tokensToday = today.reduce((a, r) => a + r.input_tokens + r.output_tokens, 0);
  const topModels = [...today]
    .map((r) => ({ model: r.model ?? "—", tokens: r.input_tokens + r.output_tokens }))
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, 6);
  const maxTok = Math.max(1, ...topModels.map((m) => m.tokens));
  // #311 performance rollup + per-model perf (requests + latency merged with 24h tokens)
  const st = stats.data;
  const rejected = (st?.meter_rejected_total ?? 0) + (st?.entitlement_rejected_total ?? 0);
  const tokBy: Record<string, number> = {};
  for (const r of today) if (r.model) tokBy[r.model] = (tokBy[r.model] ?? 0) + r.input_tokens + r.output_tokens;
  const modelPerf = (st?.models ?? []).map((m) => ({ ...m, tokens: tokBy[m.model] ?? 0 })).slice(0, 8);

  // worker role + colour assignment (stable by index) for chips + load lines.
  const workerRole = (w: typeof nodes[number]) =>
    w.name === "master" ? "master" : w.external || w.hardware === "external" ? "external" : "worker";
  const colorFor = useMemo(() => {
    const m = new Map<string, string>();
    nodes.forEach((w, i) => m.set(w.id, WORKER_COLORS[i % WORKER_COLORS.length]));
    return m;
  }, [nodes.map((w) => w.id).join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  const selected = sel ?? new Set(nodes.map((w) => w.id));
  const selNodes = nodes.filter((w) => selected.has(w.id));
  const multiWorker = selNodes.length > 1;
  // which metrics this worker currently reports (drop lines a worker can't provide,
  // e.g. an external Mac backend has none; a GPU box has all four)
  // #1598: whether a worker HAS a metric is now answered by the buffer — if
  // samples were collected, there is a line; if none were, there is nothing to
  // draw. Reading it off the current fleet row instead would drop a whole
  // worker's history the moment one reading went missing.
  // Server time, not browser time — see clockSkew above. The two must be the
  // same clock as the stamps in the buffer, or the window selects nothing.
  const liveNow = liveRange ? serverNow(Date.now(), clockSkew.current) : 0;
  const xFrom = liveNow - liveRange.ms;
  const loadSeries: ChartSeries[] = selNodes.flatMap((w) => {
    const buf = loadHist.current.get(w.id);
    if (!buf) return [] as ChartSeries[];
    return LOAD_METRICS.map((mk) => {
      const pts = windowSlice(buf[mk.key] ?? [], liveRange.ms, liveNow);
      return {
        // CUI-6: key on the worker id — two workers may share a display name.
        id: `${w.id}:${mk.key}`,
        label: multiWorker ? `${w.name} · ${mk.label}` : mk.label,
        // exactly ONE axis carries hue: with several workers it is the worker
        // (so the chart agrees with the chip dots) and the dash pattern is the
        // metric; with a single worker there is no worker axis, so hue goes back
        // to the metric and every line is solid.
        color: multiWorker ? (colorFor.get(w.id) ?? mk.color) : mk.color,
        values: pts.map((p) => p.v),
        at: pts.map((p) => p.t),
        dash: multiWorker ? LOAD_DASH[mk.key] : undefined,
      };
    });
  }).filter((s) => s.values.length > 0);

  // usage series → input+output tokens as a stacked bar per bucket (GPUStack-
  // style) + requests (line). EXO-15: no explicit locale — the x labels follow
  // the viewer's browser, like Fleet.tsx / Deploy.tsx.
  const usagePts = zeroFillUsage(usageSeries.data ?? [], range);
  const xLabels = usagePts.map((p) =>
    new Date(p.bucket).toLocaleString(undefined, range.bucket === "hour"
      ? { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { day: "2-digit", month: "2-digit" }));
  // input (blue) stacked under output (brand red) — same split GPUStack shows.
  const tokenBars: BarSegment[] = [
    { label: "Input", color: "#8C8C8C", values: usagePts.map((p) => p.input_tokens) },   // grey
    { label: "Output", color: "#CD1719", values: usagePts.map((p) => p.output_tokens) }, // brand red
  ];
  const reqSeries: ChartSeries[] = [{ label: "Requests", color: "#6E6E6E", values: usagePts.map((p) => p.events) }];

  return (
    <section className="page">
      <h1>Dashboard</h1>
      <p className="lede">Sovereign LLM control plane — fleet health, served models, and token usage at a glance.</p>

      <div className="cards">
        <StatTile
          k="Nodes"
          value={workers.isLoading ? "…" : fmt(nodes.length)}
          sub={workers.isError ? "unavailable" : `${readyNodes} ready`}
        />
        <StatTile
          k="Models served"
          value={deployments.isLoading ? "…" : fmt(deps.length)}
          sub={deployments.isError ? "unavailable" : `${readyInstances} ready instance${readyInstances === 1 ? "" : "s"}`}
        />
        <StatTile
          k="Instances"
          value={deployments.isLoading ? "…" : fmt(insts.length)}
          sub={deployments.isError ? "unavailable"
            : <>{engReady} ready{engBusy ? ` · ${engBusy} busy` : ""}{engFailed ? <> · <span style={{ color: "var(--error)" }}>{engFailed} failed</span></> : ""}</>}
        />
        <StatTile
          k="Tokens (24h)"
          value={usageToday.isLoading ? "…" : fmt(tokensToday)}
          sub={usageToday.isError ? "unavailable" : "input + output"}
        />
        <StatTile
          k="API keys"
          value={keys.isLoading ? "…" : fmt(activeKeys)}
          sub={keys.isError ? "unavailable" : `${totalKeys} total`}
        />
      </div>

      {attention.length > 0 && (
        <div className="card" style={{ marginTop: 16, borderLeft: "3px solid var(--error)" }}>
          <h2 className="section" style={{ marginTop: 0 }}>Needs attention ({attention.length})</h2>
          <table className="rz">
            <thead><tr><th>Model</th><th>Health</th><th>Detail</th><th></th></tr></thead>
            <tbody>
              {attention.map((d) => (
                <tr key={d.id} {...rowProps(() => navigate("/models"))}>
                  <td><StatusDot status={d.health} /><strong>{d.model_name}</strong></td>
                  <td><StatusBadge status={d.health} /></td>
                  <td className="hint mono">{d.instances.find((i) => i.detail)?.detail ?? `${d.ready_instances}/${d.replicas} ready`}</td>
                  <td><Badge kind="muted">{d.engine}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="section" style={{ marginTop: 22 }}>Performance <span className="muted" style={{ fontWeight: 400, fontSize: "0.72rem" }}>· since manager start</span></h2>
      <div className="cards">
        <StatTile k="Requests" value={stats.isLoading ? "…" : fmt(st?.requests_total ?? 0)}
          sub={stats.isError ? "unavailable" : "proxied /v1 calls"} />
        <StatTile k="Avg latency" value={stats.isLoading ? "…" : `${fmt(st?.avg_latency_ms ?? 0)} ms`}
          sub={stats.isError ? "unavailable" : "manager-added"} />
        <StatTile k="Failovers" value={stats.isLoading ? "…" : fmt(st?.failover_total ?? 0)}
          sub={stats.isError ? "unavailable" : "upstream retries"} />
        <StatTile k="Rejected" value={stats.isLoading ? "…" : fmt(rejected)}
          sub={stats.isError ? "unavailable" : <>{st?.meter_rejected_total ?? 0} meter · {st?.entitlement_rejected_total ?? 0} entitle</>} />
      </div>

      {/* --- advanced: live load graph (worker-chip filtered) + usage over time --- */}
      <h2 className="section" style={{ marginTop: 22 }}>Load &amp; usage</h2>
      <div className="dash-2col">
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <h2 className="section" style={{ marginTop: 0, border: "none", paddingBottom: 0 }}>Utilization</h2>
            <div className="chip-row" style={{ margin: 0 }}>
              {LIVE_RANGES.map((r, i) => (
                <ToggleChip key={r.label} label={r.label} active={i === liveRangeIdx}
                  onClick={() => setLiveRangeIdx(i)} />
              ))}
            </div>
          </div>
          <div className="chip-row">
            <span className="chip-lbl">Workers</span>
            {nodes.length === 0 && <span className="muted">none registered</span>}
            {nodes.map((w) => (
              <ToggleChip
                key={w.id}
                label={`${w.name}${workerRole(w) !== "worker" ? ` · ${workerRole(w)}` : ""}`}
                color={colorFor.get(w.id)}
                active={selected.has(w.id)}
                onClick={() => setSel((cur) => {
                  const next = new Set(cur ?? nodes.map((x) => x.id));
                  next.has(w.id) ? next.delete(w.id) : next.add(w.id);
                  return next;
                })}
              />
            ))}
          </div>
          {/* #1598: the four current readings, as rings that run up from 0 on
              the first paint and ease from the previous value afterwards. The
              CHART behind them is the history; these are "right now".
              Averaged over the SELECTED workers and labelled with how many —
              a single number over heterogeneous boxes means nothing unless it
              says what it covers. */}
          <div className="gauge-row">
            {LOAD_METRICS.map((mk) => (
              <Gauge key={mk.key} label={mk.label} color={mk.color}
                pct={gaugeNow[mk.key]}
                sub={gaugeWorkers === 0 ? "no workers"
                     : gaugeWorkers === 1 ? "1 worker" : `Ø ${gaugeWorkers} workers`} />
            ))}
          </div>
          {/* #1598: the x-axis is the CHOSEN window, not the extent of the
              data. Three samples into a five-minute view they sit at the right
              edge and march left, which is what "last 5 minutes" means; the
              alternative stretches three points across the plot and shows a
              busy history that was never collected. */}
          <TimeSeriesChart series={loadSeries} height={220} max={100} smooth
            xDomain={[xFrom, liveNow]}
            xLabels={[`-${liveRange.label}`, "now"]}
            topFormat={(n) => `${Math.round(n)}%`}
            empty="Collecting live samples… (GPU load · VRAM · CPU RAM · CPU load, from local workers)" />
          {loadSeries.length > 0 && (
            <div className="chart-legend">
              {/* CUI-6: the legend states whichever axis currently carries hue.
                  Multi-worker → colour = worker (same swatch as the chips
                  above), dash = metric. Single worker → colour = metric. */}
              {multiWorker && <span className="lg-group">colour = worker · pattern = metric</span>}
              {multiWorker && selNodes.map((w) => (
                <span className="lg" key={w.id}>
                  <span className="lg-swatch" style={{ background: colorFor.get(w.id) }} />{w.name}
                </span>
              ))}
              {LOAD_METRICS.map((mk) => (
                <span className="lg" key={mk.key}>
                  {multiWorker ? (
                    <svg className="lg-dash" width={18} height={4} aria-hidden>
                      <line x1={0} y1={2} x2={18} y2={2} stroke="currentColor" strokeWidth={2}
                        strokeDasharray={LOAD_DASH[mk.key]} />
                    </svg>
                  ) : (
                    <span className="lg-swatch" style={{ background: mk.color }} />
                  )}
                  {mk.label}
                </span>
              ))}
              {!multiWorker && selNodes[0] && (() => {
                const w = selNodes[0]; const m = w.metrics ?? {};
                const parts: string[] = [];
                if (m.gpu_util != null) parts.push(`GPU ${Math.round(m.gpu_util)}%`);
                if (m.vram_used_gb != null && w.vram_total_gb) parts.push(`VRAM ${fmt(m.vram_used_gb)}/${fmt(w.vram_total_gb)} GB`);
                if (m.mem_used_gb != null && w.mem_total_gb) parts.push(`RAM ${fmt(m.mem_used_gb)}/${fmt(w.mem_total_gb)} GB`);
                if (m.load != null) parts.push(`load ${m.load}${m.ncpu ? `/${m.ncpu}` : ""}`);
                return parts.length ? <span className="lg muted">{w.name}: {parts.join(" · ")}</span> : null;
              })()}
            </div>
          )}
        </div>

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <h2 className="section" style={{ marginTop: 0, border: "none", paddingBottom: 0 }}>Usage over time</h2>
            <div className="seg">
              {RANGES.map((r, i) => (
                <button key={r.label} className={`seg-btn${i === rangeIdx ? " on" : ""}`}
                  onClick={() => setRangeIdx(i)} aria-pressed={i === rangeIdx}>{r.label}</button>
              ))}
            </div>
          </div>
          {/* CUI-11: `usagePts` is zero-filled to a fixed bucket count, so it is
              never empty and the old `usagePts.length === 0` branch was dead
              code — the intended copy now goes through the charts' own `empty`
              prop. The isLoading branch matters too: without it the first paint
              was a fully zero-filled chart reading "No data yet." until the
              fetch landed, i.e. "no usage" for a real range. */}
          {usageSeries.isError ? (
            <div className="chart-empty muted">Usage unavailable.</div>
          ) : usageSeries.isLoading ? (
            <div style={{ padding: "6px 0" }}>
              <Skeleton h={120} r={6} />
              <div style={{ height: 10 }} />
              <Skeleton h={60} r={6} />
            </div>
          ) : (
            <>
              <div className="ts-caption muted">Tokens per {range.bucket === "hour" ? "hour" : "day"} (input + output)</div>
              <StackedBarChart segments={tokenBars} height={120} topFormat={fmtCompact} xLabels={xLabels}
                empty="No usage recorded in this range." />
              <div style={{ height: 10 }} />
              <div className="ts-caption muted">Requests per {range.bucket === "hour" ? "hour" : "day"}</div>
              <TimeSeriesChart series={reqSeries} height={60} topFormat={fmt}
                empty="No requests recorded in this range." />
            </>
          )}
        </div>
      </div>

      <div className="dash-2col" style={{ marginTop: 20 }}>
        <div className="card">
          <h2 className="section" style={{ marginTop: 0 }}>Fleet</h2>
          {nodes.length === 0 ? (
            <p className="muted">No nodes registered.</p>
          ) : (
            <table className="rz">
              <thead>
                <tr><th>Node</th><th>Hardware</th><th>Serving</th><th></th></tr>
              </thead>
              <tbody>
                {nodes.slice(0, 6).map((w) => (
                  <tr key={w.id} {...rowProps(() => navigate("/workers"))}>
                    <td><StatusDot status={w.status} />{w.name}</td>
                    <td className="muted">{w.hardware ?? "—"}</td>
                    <td className="num">{w.instances.length}</td>
                    <td className="muted mono">{w.address}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <h2 className="section" style={{ marginTop: 0 }}>Top models — last 24h</h2>
          {topModels.length === 0 ? (
            <p className="muted">No usage recorded in the last 24h.</p>
          ) : (
            <table className="rz">
              <tbody>
                {topModels.map((m) => (
                  <tr key={m.model} {...rowProps(() => navigate("/models"))}>
                    <td style={{ width: "38%" }}>{m.model}</td>
                    <td><Bar value={m.tokens} max={maxTok} /></td>
                    <td className="right num" style={{ width: 90 }}>{fmt(m.tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section" style={{ marginTop: 0 }}>Model performance</h2>
        {modelPerf.length === 0 ? (
          <p className="muted">No requests since the manager started.</p>
        ) : (
          <table className="rz">
            <thead><tr><th>Model</th><th className="right">Requests</th><th className="right">Avg latency</th><th className="right">Tokens 24h</th></tr></thead>
            <tbody>
              {modelPerf.map((m) => (
                <tr key={m.model} {...rowProps(() => navigate("/models"))}>
                  <td>{m.model}</td>
                  <td className="right num">{fmt(m.requests)}</td>
                  <td className="right num">{fmt(m.avg_latency_ms)} ms</td>
                  <td className="right num">{fmt(m.tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
