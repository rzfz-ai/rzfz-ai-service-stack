import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { endpoints, type UsageAnalytics } from "../api/client";
import { Bar, fmt, fmtCompact, QueryState, Skeleton, StatTile, StackedBarChart, TimeSeriesChart, type BarSegment, type ChartSeries } from "./ui";

// #991 — cost-control analytics. ONE fetch feeds every card below (the endpoint
// returns all six cuts of the same window), so the range/cost-centre controls
// cause one round-trip, not six.
//
// TOKENS, NOT CURRENCY. `usage_events` has no cost column by design, so there is
// no "$ spend" here and no invented price list; the headline is tokens and the
// metric toggle switches to requests. Pricing stays in the billing system —
// same line the rest of this page holds.
const RANGES: { label: string; days: number; bucket: "hour" | "day" }[] = [
  { label: "24h", days: 1, bucket: "hour" },
  { label: "7d", days: 7, bucket: "day" },
  { label: "30d", days: 30, bucket: "day" },
  { label: "90d", days: 90, bucket: "day" },
];

// Brand palette only (#CD1719 + mid-greys), matching the dashboard charts.
const INPUT_COLOR = "#8C8C8C";
const OUTPUT_COLOR = "#CD1719";
const REQ_COLOR = "#6E6E6E";

type Metric = "tokens" | "requests";

function Delta({ pct }: { pct: number | null }) {
  // A null delta is "no baseline", not 0% and not +100% — the previous window
  // had nothing to compare against, and printing a number there would read as
  // a measurement.
  if (pct == null) return <span className="muted">no baseline</span>;
  const up = pct >= 0;
  return (
    <span style={{ color: up ? "var(--error)" : "var(--success)" }}>
      {up ? "▲" : "▼"} {Math.abs(pct).toFixed(1)}% <span className="muted">vs previous</span>
    </span>
  );
}

function Heatmap({ grid, metric }: { grid: UsageAnalytics["heatmap"]; metric: Metric }) {
  const cells = metric === "tokens" ? grid.tokens : grid.events;
  const max = metric === "tokens" ? grid.max_tokens : grid.max_events;
  // Same red-tint ramp the playground similarity heatmap uses; a linear ramp
  // makes a single busy hour flatten everything else, so the intensity is
  // square-rooted (still monotone — a busier cell is never paler).
  const tint = (v: number) => (max > 0 && v > 0 ? Math.sqrt(v / max) : 0);
  const label = metric === "tokens" ? "tokens" : "requests";
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="heatmap compact">
        <thead>
          <tr>
            <th />
            {Array.from({ length: 24 }, (_, h) => (
              <th key={h} className="lbl hr">{h % 3 === 0 ? String(h).padStart(2, "0") : ""}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.weekdays.map((d, i) => (
            <tr key={d}>
              <th className="lbl">{d}</th>
              {cells[i].map((v, h) => (
                <td
                  key={h}
                  className="cell"
                  style={{ background: `rgba(205,23,25,${tint(v).toFixed(3)})` }}
                  title={`${d} ${String(h).padStart(2, "0")}:00 UTC — ${fmt(v)} ${label}`}
                />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function CostAnalytics() {
  const [rangeIdx, setRangeIdx] = useState(2); // 30d
  const [metric, setMetric] = useState<Metric>("tokens");
  const [costCenter, setCostCenter] = useState("");
  const range = RANGES[rangeIdx];

  const centers = useQuery({ queryKey: ["costCenters"], queryFn: endpoints.costCenters, retry: false });
  const q = useQuery({
    queryKey: ["usageAnalytics", range.days, range.bucket, costCenter],
    queryFn: () => endpoints.usageAnalytics({
      days: range.days, bucket: range.bucket, top: 8,
      cost_center: costCenter || undefined,
    }),
    retry: false,
    refetchInterval: 60_000,
  });

  return (
    <>
      <h2 className="section" style={{ marginTop: 22 }}>
        Cost control
        <span className="muted" style={{ fontWeight: 400, fontSize: "0.72rem" }}> · buckets and the activity grid are UTC</span>
      </h2>
      <div className="spread">
        <div className="btn-row">
          <span className="muted">Range:</span>
          <div className="seg">
            {RANGES.map((r, i) => (
              <button key={r.label} className={i === rangeIdx ? "on" : ""} aria-pressed={i === rangeIdx}
                onClick={() => setRangeIdx(i)}>{r.label}</button>
            ))}
          </div>
          <span className="muted" style={{ marginLeft: 8 }}>Metric:</span>
          <div className="seg">
            <button className={metric === "tokens" ? "on" : ""} aria-pressed={metric === "tokens"}
              onClick={() => setMetric("tokens")}>Tokens</button>
            <button className={metric === "requests" ? "on" : ""} aria-pressed={metric === "requests"}
              onClick={() => setMetric("requests")}>Requests</button>
          </div>
        </div>
        <div className="btn-row">
          <span className="muted">Cost-center:</span>
          <select value={costCenter} onChange={(e) => setCostCenter(e.target.value)} style={{ width: 200 }}>
            <option value="">all</option>
            {(centers.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>{c.name}{c.team ? ` · ${c.team}` : ""}</option>
            ))}
          </select>
        </div>
      </div>

      <QueryState q={q} loading={<div className="cards"><Skeleton h={92} r={10} /><Skeleton h={92} r={10} /><Skeleton h={92} r={10} /><Skeleton h={92} r={10} /></div>}>
        {(d) => {
          const tokensPerReq = d.totals.events > 0 ? Math.round(d.totals.total_tokens / d.totals.events) : 0;
          const xLabels = d.series.map((p) =>
            new Date(p.bucket).toLocaleString(undefined, range.bucket === "hour"
              ? { day: "2-digit", month: "2-digit", hour: "2-digit" }
              : { day: "2-digit", month: "2-digit" }));
          const tokenBars: BarSegment[] = [
            { label: "Input", color: INPUT_COLOR, values: d.series.map((p) => p.input_tokens) },
            { label: "Output", color: OUTPUT_COLOR, values: d.series.map((p) => p.output_tokens) },
          ];
          const reqSeries: ChartSeries[] = [
            { label: "Requests", color: REQ_COLOR, values: d.series.map((p) => p.events), area: true },
          ];
          const rank = (r: { total_tokens: number; events: number }) => (metric === "tokens" ? r.total_tokens : r.events);
          const maxModel = Math.max(1, ...d.top_models.map(rank));

          return (
            <>
              <div className="cards">
                <StatTile k={`Tokens (${range.label})`} value={fmt(d.totals.total_tokens)}
                  sub={<Delta pct={d.delta_pct.total_tokens} />} />
                <StatTile k={`Requests (${range.label})`} value={fmt(d.totals.events)}
                  sub={<Delta pct={d.delta_pct.events} />} />
                <StatTile k="Tokens / request" value={fmt(tokensPerReq)}
                  sub={`${fmt(d.totals.cached_tokens)} cached`} />
                <StatTile k="Estimated rows" value={fmt(d.totals.estimated_events)}
                  sub={d.totals.events > 0
                    ? `${((d.totals.estimated_events / d.totals.events) * 100).toFixed(1)}% of requests · tokenizer backstop`
                    : "no requests in this window"} />
              </div>

              <div className="card" style={{ marginTop: 16 }}>
                <div className="ts-caption muted">
                  {metric === "tokens"
                    ? `Tokens per ${range.bucket} (input + output)`
                    : `Requests per ${range.bucket}`}
                </div>
                {metric === "tokens" ? (
                  <StackedBarChart segments={tokenBars} height={150} topFormat={fmtCompact} xLabels={xLabels}
                    empty="No usage recorded in this range." />
                ) : (
                  <TimeSeriesChart series={reqSeries} height={150} topFormat={fmt} xLabels={xLabels}
                    empty="No requests recorded in this range." />
                )}
              </div>

              <div className="dash-2col" style={{ marginTop: 16 }}>
                <div className="card">
                  <h2 className="section" style={{ marginTop: 0 }}>Top models</h2>
                  {d.top_models.length === 0 ? (
                    <p className="muted">No usage recorded in this range.</p>
                  ) : (
                    <table className="rz">
                      <tbody>
                        {d.top_models.map((m) => (
                          <tr key={m.model}>
                            <td style={{ width: "34%" }}>{m.model}</td>
                            <td><Bar value={rank(m)} max={maxModel} /></td>
                            <td className="right num" style={{ width: 92 }}>{fmt(rank(m))}</td>
                            <td className="right muted num" style={{ width: 62 }}>{m.share_pct.toFixed(1)}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>

                <div className="card">
                  <h2 className="section" style={{ marginTop: 0 }}>By cost-center</h2>
                  {d.by_cost_center.length === 0 ? (
                    <p className="muted">No usage recorded in this range.</p>
                  ) : (
                    <table className="rz">
                      <thead><tr><th>Cost-center</th><th className="right">Tokens</th><th className="right">Requests</th><th className="right">Share</th></tr></thead>
                      <tbody>
                        {d.by_cost_center.map((c) => (
                          <tr key={c.cost_center_id ?? c.name ?? "—"}>
                            <td>{c.name ?? <span className="mono muted">{c.cost_center_id?.slice(0, 8) ?? "—"}</span>}
                              {c.team && <span className="muted"> · {c.team}</span>}</td>
                            <td className="right num">{fmt(c.total_tokens)}</td>
                            <td className="right num">{fmt(c.events)}</td>
                            <td className="right muted num">{c.share_pct.toFixed(1)}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              </div>

              <div className="card" style={{ marginTop: 16 }}>
                <h2 className="section" style={{ marginTop: 0 }}>Per-key attribution</h2>
                {d.by_key.length === 0 ? (
                  <p className="muted">No usage recorded in this range.</p>
                ) : (
                  <div className="table-wrap">
                    <table className="rz">
                      <thead><tr><th>Key</th><th>Owner</th><th>Cost-center</th><th className="right">Tokens</th><th className="right">Requests</th><th className="right">Share</th></tr></thead>
                      <tbody>
                        {d.by_key.map((k) => (
                          <tr key={k.api_key_id ?? k.key_prefix ?? "—"}>
                            <td className="mono">{k.key_prefix ? `${k.key_prefix}…` : (k.api_key_id?.slice(0, 8) ?? "—")}</td>
                            <td>{k.owner_username ?? <span className="muted">—</span>}</td>
                            <td>{k.cost_center ?? <span className="muted">—</span>}</td>
                            <td className="right num">{fmt(k.total_tokens)}</td>
                            <td className="right num">{fmt(k.events)}</td>
                            <td className="right muted num">{k.share_pct.toFixed(1)}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="card" style={{ marginTop: 16 }}>
                <div className="spread" style={{ marginBottom: 6 }}>
                  <h2 className="section" style={{ marginTop: 0, border: "none", paddingBottom: 0 }}>Activity — weekday × hour</h2>
                  <span className="muted" style={{ fontSize: "0.72rem" }}>
                    darker = more {metric === "tokens" ? "tokens" : "requests"} · UTC · peak {fmt(metric === "tokens" ? d.heatmap.max_tokens : d.heatmap.max_events)}
                  </span>
                </div>
                <Heatmap grid={d.heatmap} metric={metric} />
              </div>
            </>
          );
        }}
      </QueryState>
    </>
  );
}
