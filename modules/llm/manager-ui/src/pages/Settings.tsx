import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, endpoints } from "../api/client";
import { Badge } from "../components/ui";

export function Settings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: endpoints.settings });
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const setMode = useMutation({
    mutationFn: (mode: string) => endpoints.patchSettings({ metering_mode: mode }),
    onSuccess: (s) => {
      setErr(null);
      setMsg(`Metering mode is now “${s.metering_mode}”.`);
      qc.setQueryData(["settings"], s);
    },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });
  const rebuild = useMutation({
    mutationFn: () => endpoints.rebuildRouter(),
    onSuccess: (r) => { setErr(null); setMsg(`Router config rebuilt — ${r.model_list_size} model(s).`); },
    onError: (e) => setErr(e instanceof ApiError ? e.message : String(e)),
  });

  const s = q.data;

  return (
    <section className="page">
      <h1>Settings</h1>
      <p className="lede">Runtime knobs the operator controls. Secrets (node key, rollup key) rotate via <code>.env</code> + restart — never here.</p>

      {msg && <div className="callout info">{msg} <button className="btn sm" onClick={() => setMsg(null)}>ok</button></div>}
      {err && <div className="callout err">{err} <button className="btn sm" onClick={() => setErr(null)}>dismiss</button></div>}

      {q.isLoading ? (
        <div className="loading">Loading…</div>
      ) : q.isError || !s ? (
        <div className="callout err">Could not load settings.</div>
      ) : (
        <div className="stack">
          {/* --- metering mode --- */}
          <div className="card">
            <h2 className="section" style={{ marginTop: 0 }}>Billing-meter behaviour</h2>
            <p className="muted" style={{ marginTop: -4 }}>
              What the proxy does when the usage store is unreachable. <strong>available</strong>: keep serving,
              meter best-effort (availability-first). <strong>strict</strong>: refuse un-metered requests (503) to
              guarantee no un-billed tokens (consistency-first). Takes effect within seconds — no restart.
            </p>
            <div className="btn-row">
              <div className="seg">
                {s.metering_modes.map((m) => (
                  <button key={m} className={s.metering_mode === m ? "on" : ""} disabled={setMode.isPending} onClick={() => setMode.mutate(m)}>
                    {m}
                  </button>
                ))}
              </div>
              {s.metering_mode_source === "override"
                ? <Badge kind="info">override (env default: {s.metering_mode_env})</Badge>
                : <Badge kind="muted">from .env</Badge>}
            </div>
          </div>

          {/* --- router --- */}
          <div className="card">
            <h2 className="section" style={{ marginTop: 0 }}>Router</h2>
            <p className="muted" style={{ marginTop: -4 }}>
              The LiteLLM router config is regenerated from fleet state on every node (de)registration. Force a rebuild if a backend looks stale.
            </p>
            <div className="form-grid">
              <Kv k="LiteLLM base URL" v={<span className="mono">{s.litellm_base_url}</span>} />
              <Kv k="Config path" v={<span className="mono">{s.router_config_path}</span>} />
            </div>
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button className="btn ghost" disabled={rebuild.isPending} onClick={() => rebuild.mutate()}>
                {rebuild.isPending ? "Rebuilding…" : "Rebuild router config"}
              </button>
            </div>
          </div>

          {/* --- readiness / info --- */}
          <div className="card">
            <h2 className="section" style={{ marginTop: 0 }}>Platform</h2>
            <div className="form-grid">
              <Kv k="Node registration" v={s.node_registration_enabled ? <Badge kind="ok">enabled</Badge> : <Badge kind="warn">no node key</Badge>} />
              <Kv k="Rollup signing" v={s.rollup_signing_enabled ? <Badge kind="ok">signed</Badge> : <Badge kind="muted">unsigned</Badge>} />
              <Kv k="Key prefix" v={<span className="mono">{s.key_prefix}</span>} />
              <Kv k="Admin groups" v={s.admin_groups.map((g) => <span key={g} className="chip">{g}</span>)} />
            </div>
          </div>

          {/* --- console access (read-only; boundary is .env, #284) --- */}
          <div className="card">
            <h2 className="section" style={{ marginTop: 0 }}>Console access</h2>
            <p className="muted" style={{ marginTop: -4 }}>
              Authentik groups that grant console access, by tier. Membership grants use with no extra login.
              These are set in <code>.env</code> and shown read-only here — the access boundary is not runtime-editable.
            </p>
            <div className="form-grid">
              <Kv k="Super-admin groups" v={s.superadmin_groups.map((g) => <span key={g} className="chip">{g}</span>)} />
              <Kv k="Admin groups" v={s.llm_admin_groups.map((g) => <span key={g} className="chip">{g}</span>)} />
              <Kv k="User groups" v={s.llm_user_groups.map((g) => <span key={g} className="chip">{g}</span>)} />
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function Kv({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="field">
      <label>{k}</label>
      <div>{v}</div>
    </div>
  );
}
