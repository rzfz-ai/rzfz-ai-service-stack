import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, endpoints, type CatalogEntry } from "../api/client";
import { deployWithFitConfirm } from "../lib/deployWithFitConfirm";
import { fleetFamilies, servableBy } from "../lib/hardwareFamily";
import { Badge, QueryState, TableSkeleton, toast } from "../components/ui";

// #289 Catalog — browse the curated deployable models + the central registry
// mirror, and deploy in one click (pulls weights from the HF source on the
// worker if absent — #287).
export function Catalog() {
  const qc = useQueryClient();
  const cat = useQuery({ queryKey: ["catalog"], queryFn: () => endpoints.catalog() });
  const reg = useQuery({ queryKey: ["registry-catalog"], queryFn: endpoints.registryCatalog });
  const [busy, setBusy] = useState<string | null>(null);
  // #361: entries no live node can serve get the reason, and their Deploy is
  // disabled — the server refuses the format/engine mismatch anyway (422); the
  // button just says it earlier.
  const workers = useQuery({ queryKey: ["workers"], queryFn: endpoints.workers });
  // #1518 (E5): match hardware FAMILIES, not literal labels — a worker
  // registers `cuda` while the entry lists `nvidia`, and the exact compare
  // below greyed out every card on a GB10-only fleet.
  const fleet = fleetFamilies(workers.data);
  const servable = (e: CatalogEntry) => servableBy(fleet, e.hardware);

  async function deploy(e: CatalogEntry) {
    if (!confirm(`Deploy ${e.display}?\nIf a worker doesn't already have the weights they're fetched from ${e.repo_id}.`)) return;
    setBusy(e.name);
    try {
      // #301: a refusal from the VRAM admission gate becomes a decision,
      // not a dead end — deployWithFitConfirm shows the gate's own numbers.
      const r = await deployWithFitConfirm({
        // #1256: a multimodal model needs its vision projector pulled ALONGSIDE
        // the weight quant, or it serves text-only. The filename is resolved by
        // the manager from the model manifest and handed over on the catalog
        // entry — never typed here.
        model_name: e.name, served_model: e.name,
        files: e.filename ? [e.filename, ...(e.mmproj ? [e.mmproj] : [])] : [],
        task: e.task, hf_repo: e.repo_id, params: e.params ?? {},
      });
      if (!r) return;   // operator declined the force — not an error
      toast(`Deploy scheduled: ${r.instance_id} on ${r.worker}`, "ok");
      qc.invalidateQueries({ queryKey: ["deployments"] });
    } catch (err) {
      toast(err instanceof ApiError ? err.message : String(err), "err");
    } finally { setBusy(null); }
  }

  return (
    <section className="page">
      <h1>Catalog</h1>
      <p className="lede">Curated, deployable models — GGUF / llama.cpp is the fleet standard (AMD Strix Halo,
        NVIDIA CUDA and CPU nodes; a Mac serves GGUF via Ollama). Deploy fetches the weights from the
        HuggingFace source if a worker doesn't already have them; the central registry mirrors them
        for offline / fleet distribution.</p>

      <h2 className="section">Curated models</h2>
      <QueryState q={cat} isEmpty={(d) => d.length === 0}
        loading={<TableSkeleton rows={5} cols={5} />}
        empty={<div className="empty">No catalog entries.</div>}>
        {(entries) => (
          <div className="table-wrap">
            <table className="rz">
              <thead><tr><th>Model</th><th>Mode</th><th>Source</th><th>Hardware</th><th></th></tr></thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.name}>
                    <td>
                      <strong>{e.name}</strong>{e.recommended ? <> <Badge kind="ok">★ recommended</Badge></> : null}
                      <div className="hint">{e.display} — {e.description}</div>
                    </td>
                    <td><Badge kind="info">{e.task}</Badge></td>
                    <td className="mono" style={{ fontSize: "0.72rem" }}>{e.repo_id}
                      <div className="muted">{e.filename}</div></td>
                    <td>{e.hardware.join(", ")}
                      {!servable(e) && <div><span className="badge warn">no matching node</span></div>}</td>
                    <td><button className="btn sm primary" disabled={busy === e.name || !servable(e)}
                      title={servable(e) ? undefined : "No worker with matching hardware — the engine cannot serve this artifact format (#361)"}
                      onClick={() => deploy(e)}>{busy === e.name ? "…" : "Deploy"}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>

      <h2 className="section" style={{ marginTop: 20 }}>Central registry <span className="hint">offline / fleet mirror (Zot)</span></h2>
      {!reg.data ? <TableSkeleton rows={2} cols={2} /> :
        !reg.data.available ? (
          <div className="empty">Central registry not reachable{reg.data.error ? ` (${reg.data.error})` : ""}. Models deploy directly from HuggingFace; the registry mirror is optional — for offline / air-gapped fleets.</div>
        ) : reg.data.repositories.length === 0 ? (
          <div className="empty">Registry is up but empty — no models mirrored yet. Today a deploy fetches from HuggingFace onto the worker; pushing models into the central registry for offline distribution is a tracked follow-up.</div>
        ) : (
          <div className="table-wrap">
            <table className="rz">
              <thead><tr><th>Repository</th><th>Tags</th></tr></thead>
              <tbody>
                {reg.data.repositories.map((r) => (
                  <tr key={r.repository}>
                    <td className="mono">{r.repository}</td>
                    <td>{r.tags.length ? r.tags.map((t) => <span key={t} className="chip">{t}</span>) : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

      <p className="muted" style={{ fontSize: "0.78rem", marginTop: 16 }}>
        Model tiers: <span className="mono">HuggingFace source</span> → fetched on deploy →
        <span className="mono"> per-worker volume</span>; optionally mirrored to the
        <span className="mono"> central registry</span> for offline / air-gapped fleets.
      </p>
    </section>
  );
}
