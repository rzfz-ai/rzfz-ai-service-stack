import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, endpoints, type ApiKeyRow, type NewKey } from "../api/client";
import { Badge, Drawer, fmt, relTime, rowProps, statusKind, TableSkeleton } from "../components/ui";
import { useCapability } from "../components/Shell";

function CopyRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="keyval">
      <span>{value}</span>
      <button
        className="btn sm"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            setCopied(false);
          }
        }}
      >
        {copied ? "Copied ✓" : `Copy ${label}`}
      </button>
    </div>
  );
}

// Full detail for one API key — opened by clicking its row (#1a).
function KeyDrawer({
  k, ccName, dur, onClose, onRotate, onDisable, busy, canManage,
}: {
  k: ApiKeyRow;
  ccName: (id: string) => string;
  dur: (s: number | null) => string;
  onClose: () => void;
  onRotate: (id: string) => void;
  onDisable: (id: string) => void;
  busy: boolean;
  canManage: boolean;
}) {
  const kind = statusKind(k.status);
  return (
    <Drawer title={<><span className="mono">{k.key_prefix}…</span></>} onClose={onClose} storageKey="rzfz.drawer.key" defaultWidth={480}>
      <dl className="kv-list">
        <dt>Status</dt><dd><Badge kind={kind === "muted" ? "muted" : kind}>{k.status}</Badge></dd>
        <dt>Cost-center</dt><dd>{ccName(k.cost_center_id)}</dd>
        <dt>Key prefix</dt><dd className="mono">{k.key_prefix}…</dd>
        <dt>RPM limit</dt><dd className="num">{k.rpm_limit ?? "—"}</dd>
        <dt>TPM limit</dt><dd className="num">{k.tpm_limit ?? "—"}</dd>
        <dt>Max budget</dt><dd className="num">{k.max_budget_tokens != null ? `${fmt(k.max_budget_tokens)} tokens` : "—"}</dd>
        <dt>Budget window</dt><dd>{dur(k.budget_duration_seconds)}</dd>
        <dt>Created</dt><dd className="muted">{relTime(k.created_at)}</dd>
        <dt>Expires</dt><dd className="muted">{k.expires_at ? relTime(k.expires_at) : "never"}</dd>
      </dl>

      <div className="muted" style={{ fontSize: "0.75rem", marginBottom: 4 }}>allowed models</div>
      <div style={{ marginBottom: 16 }}>
        {k.allowed_models.length ? k.allowed_models.map((m) => <span key={m} className="chip">{m}</span>) : <span className="muted">all models</span>}
      </div>

      {canManage && (
        <div className="btn-row">
          <button className="btn" disabled={busy || k.status === "revoked"} onClick={() => onRotate(k.id)}>Rotate</button>
          <button className="btn danger" disabled={busy || k.status === "revoked"}
            onClick={() => { if (confirm(`Revoke key ${k.key_prefix}…? This is immediate.`)) onDisable(k.id); }}>Revoke</button>
        </div>
      )}
    </Drawer>
  );
}

export function Keys() {
  const qc = useQueryClient();
  // CUI-9: the nav shows this page to EITHER key tier, but every route it uses
  // — POST/GET /api/cost-centers, POST/GET /api/keys, rotate, disable — sits
  // behind one router-level `require_role(ADMIN)` (app/api/keys.py:145). The
  // USER tier's `self_issue_key` is a DOCUMENTED FUTURE capability: the
  // self-service mint is deferred to #265 and no endpoint exists yet. So for a
  // user-tier operator every control here is guaranteed to 403 — don't render
  // them, and don't fire the queries that would 403 either.
  const canIssueForAnyone = useCapability("issue_keys_for_anyone");
  const [openId, setOpenId] = useState<string | null>(null);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["keys"] });
  const keys = useQuery({ queryKey: ["keys"], queryFn: endpoints.keys, enabled: canIssueForAnyone });
  const centers = useQuery({ queryKey: ["cost-centers"], queryFn: endpoints.costCenters, enabled: canIssueForAnyone });
  const ccName = (id: string) => centers.data?.find((c) => c.id === id)?.name ?? id.slice(0, 8);
  const open = keys.data?.find((k) => k.id === openId) || null;

  const [reveal, setReveal] = useState<{ plaintext: string; note: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // --- cost-center create ---
  const [ccForm, setCcForm] = useState({ name: "", team: "" });
  const createCC = useMutation({
    mutationFn: () => endpoints.createCostCenter({ name: ccForm.name, team: ccForm.team || undefined }),
    onSuccess: () => {
      setCcForm({ name: "", team: "" });
      qc.invalidateQueries({ queryKey: ["cost-centers"] });
    },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });

  // --- key mint ---
  const [form, setForm] = useState({
    cost_center_id: "",
    allowed_models: "",
    rpm_limit: "",
    tpm_limit: "",
    max_budget_tokens: "",
    budget_duration_seconds: "",
    expires_at: "",
  });
  const num = (s: string) => (s.trim() === "" ? undefined : Number(s));
  const createKey = useMutation({
    mutationFn: () =>
      endpoints.createKey({
        cost_center_id: form.cost_center_id,
        allowed_models: form.allowed_models.split(",").map((s) => s.trim()).filter(Boolean),
        rpm_limit: num(form.rpm_limit),
        tpm_limit: num(form.tpm_limit),
        max_budget_tokens: num(form.max_budget_tokens),
        budget_duration_seconds: num(form.budget_duration_seconds),
        expires_at: form.expires_at || undefined,
      }),
    onSuccess: (k: NewKey) => {
      setErr(null);
      setReveal({ plaintext: k.key, note: `New key for cost-center “${ccName(k.cost_center_id)}”.` });
      setForm({ ...form, allowed_models: "", rpm_limit: "", tpm_limit: "", max_budget_tokens: "", budget_duration_seconds: "", expires_at: "" });
      invalidate();
    },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });

  const rotate = useMutation({
    mutationFn: (id: string) => endpoints.rotateKey(id),
    onSuccess: (k: NewKey) => {
      setReveal({ plaintext: k.key, note: "Rotated key — the previous secret is now invalid." });
      invalidate();
    },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });
  const disable = useMutation({
    mutationFn: (id: string) => endpoints.disableKey(id),
    onSuccess: invalidate,
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });

  const dur = (s: number | null) => {
    if (s == null) return "lifetime";
    if (s % 86400 === 0) return `${s / 86400}d`;
    if (s % 3600 === 0) return `${s / 3600}h`;
    return `${s}s`;
  };

  if (!canIssueForAnyone) {
    return (
      <section className="page">
        <h1>API Keys</h1>
        <p className="lede">Mint, rotate and revoke <code>rzfz-sk</code> keys. Budgets are TOKEN caps — no currency.</p>
        <div className="callout">
          <strong>Self-service key issuing isn&rsquo;t available yet.</strong>
          <p className="muted" style={{ margin: "6px 0 0" }}>
            Key management — cost-centers, minting, rotate and revoke — is an administrator
            capability on this box. Ask an LLM Manager administrator to mint a key for you.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="page">
      <h1>API Keys &amp; Cost-Centers</h1>
      <p className="lede">Mint, rotate and revoke <code>rzfz-sk</code> keys. Budgets are TOKEN caps — no currency.</p>

      {err && <div className="callout err">{err} <button className="btn sm" onClick={() => setErr(null)}>dismiss</button></div>}

      {reveal && (
        <div className="callout key">
          <strong>Copy this key now — it is shown only once.</strong>
          <div className="muted" style={{ fontSize: "0.8125rem", margin: "4px 0" }}>{reveal.note}</div>
          <CopyRow label="key" value={reveal.plaintext} />
          <div style={{ marginTop: 8 }}>
            <button className="btn sm" onClick={() => setReveal(null)}>I’ve stored it — dismiss</button>
          </div>
        </div>
      )}

      {/* --- cost centers --- */}
      <h2 className="section">Cost-centers</h2>
      <div className="spread">
        <div className="btn-row">
          {(centers.data ?? []).map((c) => (
            <span key={c.id} className="chip" title={c.team ?? ""}>{c.name}</span>
          ))}
          {centers.data?.length === 0 && <span className="empty-hint">None yet — create one to mint keys against it.</span>}
        </div>
        <form
          className="btn-row"
          onSubmit={(e) => { e.preventDefault(); if (ccForm.name.trim()) createCC.mutate(); }}
        >
          <input placeholder="name (e.g. team-a)" value={ccForm.name} onChange={(e) => setCcForm({ ...ccForm, name: e.target.value })} style={{ width: 160 }} />
          <input placeholder="team (optional)" value={ccForm.team} onChange={(e) => setCcForm({ ...ccForm, team: e.target.value })} style={{ width: 140 }} />
          <button className="btn" disabled={createCC.isPending || !ccForm.name.trim()}>Add</button>
        </form>
      </div>

      {/* --- mint a key --- */}
      <h2 className="section">Mint a key</h2>
      <form
        className="card"
        onSubmit={(e) => { e.preventDefault(); if (form.cost_center_id) createKey.mutate(); }}
      >
        <div className="form-grid">
          <div className="field">
            <label>Cost-center</label>
            <select value={form.cost_center_id} onChange={(e) => setForm({ ...form, cost_center_id: e.target.value })}>
              <option value="">— select —</option>
              {(centers.data ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label>Allowed models <span className="hint">comma-separated; empty = all</span></label>
            <input placeholder="qwen3.6, rzfz-chat" value={form.allowed_models} onChange={(e) => setForm({ ...form, allowed_models: e.target.value })} />
          </div>
          <div className="field"><label>RPM limit</label><input type="number" min="0" value={form.rpm_limit} onChange={(e) => setForm({ ...form, rpm_limit: e.target.value })} /></div>
          <div className="field"><label>TPM limit</label><input type="number" min="0" value={form.tpm_limit} onChange={(e) => setForm({ ...form, tpm_limit: e.target.value })} /></div>
          <div className="field"><label>Max budget <span className="hint">tokens</span></label><input type="number" min="0" value={form.max_budget_tokens} onChange={(e) => setForm({ ...form, max_budget_tokens: e.target.value })} /></div>
          <div className="field"><label>Budget window <span className="hint">seconds; empty = lifetime</span></label><input type="number" min="0" placeholder="2592000 = 30d" value={form.budget_duration_seconds} onChange={(e) => setForm({ ...form, budget_duration_seconds: e.target.value })} /></div>
          <div className="field"><label>Expires <span className="hint">ISO-8601; optional</span></label><input placeholder="2027-01-01T00:00:00" value={form.expires_at} onChange={(e) => setForm({ ...form, expires_at: e.target.value })} /></div>
        </div>
        <div className="btn-row">
          <button className="btn primary" disabled={createKey.isPending || !form.cost_center_id}>
            {createKey.isPending ? "Minting…" : "Mint key"}
          </button>
          {!form.cost_center_id && <span className="muted">Select a cost-center first.</span>}
        </div>
      </form>

      {/* --- keys table --- */}
      <h2 className="section">Keys</h2>
      <p className="muted" style={{ marginTop: -4, fontSize: "0.75rem" }}>Click a row for full detail. Rotate / Revoke are also available inline.</p>
      {keys.isLoading ? (
        <TableSkeleton rows={4} cols={9} />
      ) : keys.isError ? (
        <div className="callout err">Could not load keys.</div>
      ) : (
        <div className="table-wrap">
          <table className="rz">
            <thead>
              <tr>
                <th>Key</th><th>Cost-center</th><th>Status</th><th>Models</th>
                <th>RPM</th><th>TPM</th><th>Budget</th><th>Window</th><th></th>
              </tr>
            </thead>
            <tbody>
              {(keys.data ?? []).map((k) => (
                <tr key={k.id} {...rowProps(() => setOpenId(k.id), k.id === openId)}>
                  <td className="mono">{k.key_prefix}…</td>
                  <td>{ccName(k.cost_center_id)}</td>
                  <td><Badge kind={statusKind(k.status) === "muted" ? "muted" : statusKind(k.status)}>{k.status}</Badge></td>
                  <td>{k.allowed_models.length ? k.allowed_models.map((m) => <span key={m} className="chip">{m}</span>) : <span className="muted">all</span>}</td>
                  <td className="num">{k.rpm_limit ?? "—"}</td>
                  <td className="num">{k.tpm_limit ?? "—"}</td>
                  <td className="num">{k.max_budget_tokens != null ? fmt(k.max_budget_tokens) : "—"}</td>
                  <td>{dur(k.budget_duration_seconds)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <div className="btn-row">
                      <button className="btn sm" disabled={rotate.isPending || k.status === "revoked"} onClick={() => rotate.mutate(k.id)}>Rotate</button>
                      <button className="btn sm danger" disabled={disable.isPending || k.status === "revoked"} onClick={() => { if (confirm(`Revoke key ${k.key_prefix}…? This is immediate.`)) disable.mutate(k.id); }}>Revoke</button>
                    </div>
                  </td>
                </tr>
              ))}
              {keys.data?.length === 0 && <tr><td colSpan={9} className="empty">No keys yet — mint one above.</td></tr>}
            </tbody>
          </table>
        </div>
      )}

      {open && <KeyDrawer k={open} ccName={ccName} dur={dur} canManage={canIssueForAnyone} busy={rotate.isPending || disable.isPending}
        onClose={() => setOpenId(null)} onRotate={(id) => rotate.mutate(id)} onDisable={(id) => disable.mutate(id)} />}
    </section>
  );
}
