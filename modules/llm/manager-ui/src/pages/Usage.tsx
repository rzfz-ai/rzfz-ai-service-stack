import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, endpoints, type Rollup, type UsageRow } from "../api/client";
import { Badge, Bar, Drawer, fmt, QueryState, rowProps, TableSkeleton } from "../components/ui";
import { CostAnalytics } from "../components/CostAnalytics";
import { useCapability } from "../components/Shell";

const WINDOWS = [
  { label: "24h", days: 1 },
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "All", days: 0 },
];

const pct = (part: number, whole: number) => (whole > 0 ? `${((part / whole) * 100).toFixed(1)}%` : "—");

// Client-side detail for one usage row — no per-row endpoint exists, so this
// is a computed breakdown (share of the visible window's totals) rather than
// a fresh fetch (#1a: rows must open a detail panel).
function UsageDrawer({
  row, label, group, totals, onClose,
}: {
  row: UsageRow;
  label: string;
  group: "key" | "model";
  totals: { i: number; o: number; c: number; e: number };
  onClose: () => void;
}) {
  const total = row.input_tokens + row.output_tokens;
  const maxComp = Math.max(1, row.input_tokens, row.output_tokens, row.cached_tokens);
  return (
    <Drawer title={<>{group === "model" ? "Model" : "Key"}: <span className="mono">{label}</span></>} onClose={onClose} storageKey="rzfz.drawer.usage" defaultWidth={460}>
      {row.bucket && <dl className="kv-list"><dt>Bucket</dt><dd className="mono">{row.bucket.slice(0, 10)}</dd></dl>}
      <dl className="kv-list">
        <dt>Input tokens</dt><dd className="num">{fmt(row.input_tokens)} <span className="muted">({pct(row.input_tokens, totals.i)} of window)</span></dd>
        <dt>Output tokens</dt><dd className="num">{fmt(row.output_tokens)} <span className="muted">({pct(row.output_tokens, totals.o)} of window)</span></dd>
        <dt>Cached tokens</dt><dd className="num">{fmt(row.cached_tokens)} <span className="muted">({pct(row.cached_tokens, totals.c)} of window)</span></dd>
        <dt>Total tokens</dt><dd className="num"><strong>{fmt(total)}</strong></dd>
        <dt>Requests</dt><dd className="num">{fmt(row.events)} <span className="muted">({pct(row.events, totals.e)} of window)</span></dd>
        <dt>Tokens / request</dt><dd className="num">{row.events > 0 ? fmt(Math.round(total / row.events)) : "—"}</dd>
      </dl>

      <div className="muted" style={{ fontSize: "0.75rem", margin: "6px 0 8px" }}>input / output / cached</div>
      <div className="stack" style={{ gap: 10 }}>
        <div className="spread" style={{ margin: 0 }}><span className="muted">input</span><Bar value={row.input_tokens} max={maxComp} /></div>
        <div className="spread" style={{ margin: 0 }}><span className="muted">output</span><Bar value={row.output_tokens} max={maxComp} /></div>
        <div className="spread" style={{ margin: 0 }}><span className="muted">cached</span><Bar value={row.cached_tokens} max={maxComp} /></div>
      </div>
    </Drawer>
  );
}

export function Usage() {
  // CUI-9: the nav shows this page to both usage tiers, but the routes behind
  // it are not equally reachable. `/api/usage*` is router-gated at ADMIN
  // (app/api/keys.py:145) and the entitlement status + signed rollup are gated
  // at SUPER-ADMIN (app/api/entitlement.py:235). `view_own_usage` has no
  // per-user endpoint yet. Gate each section on the capability that actually
  // opens it, and skip the queries that would only 403.
  const canViewAll = useCapability("view_all_usage");
  // the super-admin tier marker in the #843 matrix — the same tier the
  // entitlement router requires.
  const isSuper = useCapability("global_settings");
  const [win, setWin] = useState(7);
  const [group, setGroup] = useState<"key" | "model">("key");
  const [bucket, setBucket] = useState<string>("");

  const q = useQuery({
    queryKey: ["usage", win, group, bucket],
    queryFn: () => endpoints.usage({ since_days: win || undefined, group, bucket: bucket || undefined }),
    enabled: canViewAll,
  });
  const ent = useQuery({ queryKey: ["entitlement"], queryFn: endpoints.entitlementStatus, enabled: isSuper });
  const rows = q.data ?? [];
  const totals = rows.reduce(
    (a, r) => ({ i: a.i + r.input_tokens, o: a.o + r.output_tokens, c: a.c + r.cached_tokens, e: a.e + r.events }),
    { i: 0, o: 0, c: 0, e: 0 },
  );
  const labelOf = (r: UsageRow) =>
    group === "model" ? (r.model ?? "—") : (r.key_prefix ? `${r.key_prefix}…` : (r.api_key_id?.slice(0, 8) ?? "—"));
  const keyOf = (r: UsageRow) => `${r.bucket ?? ""}|${labelOf(r)}`;
  const [openKey, setOpenKey] = useState<string | null>(null);
  const openRow = rows.find((r) => keyOf(r) === openKey) || null;

  // --- signed monthly rollup ---
  const [month, setMonth] = useState(() => "");
  const [rollup, setRollup] = useState<Rollup | null>(null);
  const [rErr, setRErr] = useState<string | null>(null);
  const fetchRollup = useMutation({
    mutationFn: () => endpoints.rollup(month),
    onSuccess: (r) => { setRollup(r); setRErr(null); },
    onError: (e) => { setRollup(null); setRErr(e instanceof ApiError ? e.message : String(e)); },
  });
  // CUI-13: the anchor must be IN the document and the object URL must outlive
  // the click. Revoking on the next statement off a detached anchor happens to
  // work in Chrome (the download starts synchronously) and intermittently drops
  // the file in Firefox/Safari — a silent no-op for the entitlement-reporting
  // artifact. Append → click → remove, and revoke on a later turn.
  const download = () => {
    if (!rollup) return;
    const blob = new Blob([JSON.stringify(rollup, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `rzfz-usage-rollup-${rollup.month}.json`;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  if (!canViewAll) {
    return (
      <section className="page">
        <h1>Usage</h1>
        <p className="lede">Token counts only — input / output / cached. No currency; pricing lives in your billing system.</p>
        <div className="callout">
          <strong>Per-user usage isn&rsquo;t available yet.</strong>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            The usage report covers every key on this box and is an administrator capability.
            Ask an LLM Manager administrator for your cost-center&rsquo;s figures.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="page">
      <h1>Usage</h1>
      <p className="lede">Token counts only — input / output / cached. No currency; pricing lives in your billing system.</p>

      {ent.data && (() => {
        const e = ent.data;
        const kind = e.state === "active" ? "ok" : e.state === "none" ? "muted" : "err";
        return (
          <div className={`callout ${kind === "err" ? "err" : ""}`} style={{ marginBottom: 12 }}>
            <div className="spread" style={{ margin: 0 }}>
              <div>
                <strong>Subscription:</strong> <Badge kind={kind}>{e.state}</Badge>
                {e.plan && <span className="muted"> · {e.plan}{e.seats != null ? ` · ${e.seats} seats` : ""}</span>}
                {e.valid_until && <span className="muted"> · valid until {e.valid_until.slice(0, 10)}{e.days_remaining != null ? ` (${e.days_remaining}d)` : ""}</span>}
              </div>
              <span className="muted mono">enforcement: {e.mode}{e.mode === "enforce" && !e.entitled ? " · BLOCKING" : ""}</span>
            </div>
            {e.mode === "enforce" && !e.entitled && (
              <div className="hint" style={{ marginTop: 4 }}>Requests are being refused (402) — {e.reason}. Contact your razzfazz.ai support contact.</div>
            )}
          </div>
        );
      })()}

      {/* #991: the cost-control surface — headline totals + "vs previous"
          delta, the spend/requests series, top models, the per-cost-centre and
          per-key split, and the weekday x hour activity grid. One fetch of
          /api/usage/analytics feeds all of it; the raw per-row table below
          stays as the drill-down. */}
      <CostAnalytics />

      <h2 className="section" style={{ marginTop: 22 }}>Usage detail</h2>
      <div className="spread">
        <div className="btn-row">
          <span className="muted">Window:</span>
          <div className="seg">
            {WINDOWS.map((w) => (
              <button key={w.label} className={win === w.days ? "on" : ""} onClick={() => setWin(w.days)}>{w.label}</button>
            ))}
          </div>
          <span className="muted" style={{ marginLeft: 8 }}>Group by:</span>
          <div className="seg">
            <button className={group === "key" ? "on" : ""} onClick={() => setGroup("key")}>Key</button>
            <button className={group === "model" ? "on" : ""} onClick={() => setGroup("model")}>Model</button>
          </div>
        </div>
        <div className="btn-row">
          <span className="muted">Bucket:</span>
          <select value={bucket} onChange={(e) => setBucket(e.target.value)} style={{ width: 120 }}>
            <option value="">none</option>
            <option value="day">day</option>
            <option value="month">month</option>
          </select>
        </div>
      </div>

      <QueryState q={q} isEmpty={(d) => d.length === 0}
        loading={<TableSkeleton rows={5} cols={bucket ? 6 : 5} />}
        empty={<div className="empty">No token usage recorded in this window.</div>}>
        {() => (
          <div className="table-wrap">
            <table className="rz">
              <thead>
                <tr>
                  {bucket && <th>Bucket</th>}
                  <th>{group === "model" ? "Model" : "Key"}</th>
                  <th className="right">Input</th><th className="right">Output</th>
                  <th className="right">Cached</th><th className="right">Requests</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, idx) => (
                  <tr key={idx} {...rowProps(() => setOpenKey(keyOf(r)), keyOf(r) === openKey)}>
                    {bucket && <td className="muted">{r.bucket?.slice(0, 10)}</td>}
                    <td className={group === "model" ? "" : "mono"}>{labelOf(r)}</td>
                    <td className="right num">{fmt(r.input_tokens)}</td>
                    <td className="right num">{fmt(r.output_tokens)}</td>
                    <td className="right num">{fmt(r.cached_tokens)}</td>
                    <td className="right num">{fmt(r.events)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr style={{ fontWeight: 700 }}>
                  {bucket && <td></td>}
                  <td>Total</td>
                  <td className="right num">{fmt(totals.i)}</td>
                  <td className="right num">{fmt(totals.o)}</td>
                  <td className="right num">{fmt(totals.c)}</td>
                  <td className="right num">{fmt(totals.e)}</td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </QueryState>

      {/* --- signed monthly rollup (CUI-9: SUPER-ADMIN only, matching the
           entitlement router's own gate — an admin-tier operator would only
           get a 403 out of this form) --- */}
      {isSuper && (<>
      <h2 className="section">Signed monthly rollup</h2>
      <p className="muted" style={{ marginTop: -4 }}>Per cost-center / key totals for a month, optionally HMAC-signed for entitlement reporting.</p>
      <div className="btn-row">
        <input placeholder="YYYY-MM (e.g. 2026-08)" value={month} onChange={(e) => setMonth(e.target.value)} style={{ width: 200 }} />
        <button className="btn primary" disabled={!/^\d{4}-\d{2}$/.test(month) || fetchRollup.isPending} onClick={() => fetchRollup.mutate()}>
          {fetchRollup.isPending ? "Fetching…" : "Fetch rollup"}
        </button>
        {rollup && <button className="btn ghost" onClick={download}>Download JSON</button>}
      </div>
      {rErr && <div className="callout err" style={{ marginTop: 10 }}>{rErr}</div>}
      {rollup && (
        <div style={{ marginTop: 12 }}>
          <div className="btn-row" style={{ marginBottom: 8 }}>
            <strong>{rollup.month}</strong>
            {rollup.signed ? <Badge kind="ok">signed</Badge> : <Badge kind="muted">unsigned</Badge>}
          </div>
          <div className="table-wrap">
            <table className="rz">
              <thead><tr><th>Cost-center</th><th>Key</th><th className="right">Input</th><th className="right">Output</th><th className="right">Cached</th></tr></thead>
              <tbody>
                {rollup.totals.map((t, i) => (
                  <tr key={i}>
                    <td>{t.cost_center ?? "—"}</td>
                    <td className="mono muted">{t.api_key ? `${t.api_key.slice(0, 8)}…` : "—"}</td>
                    <td className="right num">{fmt(t.input)}</td>
                    <td className="right num">{fmt(t.output)}</td>
                    <td className="right num">{fmt(t.cached)}</td>
                  </tr>
                ))}
                {rollup.totals.length === 0 && <tr><td colSpan={5} className="empty">No usage in {rollup.month}.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      )}
      </>)}

      {openRow && <UsageDrawer row={openRow} label={labelOf(openRow)} group={group} totals={totals} onClose={() => setOpenKey(null)} />}
    </section>
  );
}
