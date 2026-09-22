import { Fragment, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, endpoints, runCommand, waitCommand, type EnrollToken, type FleetInventoryModel, type RunnerInventory, type RunnerUpgradeRow, type WorkerRow } from "../api/client";
import { deployWithFitConfirm } from "../lib/deployWithFitConfirm";
import { LogsPane } from "../components/LogsPane";
import { Drawer, QueryState, rowProps, StatusBadge, StatusDot, TableSkeleton, relTime, toast } from "../components/ui";

// #1951 — a version without its provenance answers nobody's question.
//
// The API states WHERE a reported version came from, and until now the console
// dropped it: "2026.09-rc1" read the same whether it came from the mounted repo
// (what is RUNNING) or from a possibly stale `.env`. Measured on box-175r
// (DevBox-Vuko, 2026-09-11) alongside two other workers, all three showing an
// identical-looking string with three different provenances.
//
// `not-reported` is the state the manager synthesises when the node sent no
// source at all — during a rollout the commonest one, and the ONLY one an
// operator fixes by updating the agent. It therefore carries that instruction.
//
// The word "unknown" is deliberately NOT used for any of these: this view used
// it for "no version at all", so reusing it would have merged the very states
// this exists to separate. A missing version now renders as an em dash here and
// in the fleet table, which previously disagreed with each other.
export function versionSourceLabel(source: string | null | undefined): string {
  switch (source) {
    case "git-describe": return "(from the mounted repo)";
    case "env":          return "(from RAZZFAZZ_VERSION)";
    case "image":        return "(stamped into the agent image)";
    case "unknown":      return "(the node could not establish one)";
    case "not-reported": return "(this agent predates the field \u2014 update the agent)";
    default:             return source ? `(${source})` : "";
  }
}

export function engineVersionLabel(why: string | null | undefined): string {
  switch (why) {
    case "probed":          return "";
    case "not-applicable":  return "(this engine reports no build string)";
    case "no-image":        return "(no engine image configured)";
    case "image-absent":    return "(the image is not on this node \u2014 pull it)";
    case "probe-failed":    return "(the probe failed \u2014 check the node)";
    case "no-version-line": return "(the engine printed nothing recognisable)";
    case "not-reported":    return "(this agent predates the field \u2014 update the agent)";
    default:                return why ? `(${why})` : "";
  }
}

// #1860 — a node that cannot pull runners says so, instead of looking healthy.
//
// The registry VALUE cannot be judged on its own: `llm-registry:5000` is a
// correct answer when an operator set it, and a dead end when the node fell
// back to it — the pull is performed by the host's docker daemon, which is not
// on the compose network and cannot resolve a compose service name. Measured on
// box-175r during the SCH3 run: `ready`, models served, every runner pull dead
// at DNS, and nothing said so until a switch was triggered.
//
// Returns null when there is nothing to warn about — including for a node whose
// agent predates the field. An unknown source is not a broken node, and warning
// on it would train the operator to ignore this line.
export function runnerRegistryWarning(
  inv?: { registry?: string; registry_source?: string } | null,
): string | null {
  if (!inv || inv.registry_source !== "fallback") return null;
  return `This node cannot pull runner images: neither LLM_WORKER_RUNNER_REGISTRY nor `
    + `LLM_HUB_DOMAIN is set, so it fell back to ${inv.registry ?? "llm-registry:5000"} — `
    + `a compose-network name the host's docker daemon cannot resolve. The node keeps `
    + `serving what it already has. Set LLM_HUB_DOMAIN (or LLM_WORKER_RUNNER_REGISTRY) `
    + `in the node's .env.node, run docker login against it, and restart the agent.`;
}

// #262 worker-add: mint an enrollment token, then show the operator the
// copy-paste join command to run on the TARGET box. The manager never reaches
// out — the box self-joins by exchanging the token for its node key.
// #1059 P2.2: the box may be BLANK — no repo, no rzfz. The primary command is
// therefore the master-served `curl … | bash`, with the repo-based
// `rzfz worker-join` kept as the secondary for a box that already has a
// checkout. The freshly-minted token travels in the command (this drawer is
// SSO'd); the script the box fetches carries no secret at all.
function AddWorkerDrawer({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [hardware, setHardware] = useState<"cpu" | "amd" | "nvidia">("cpu");
  const [busy, setBusy] = useState(false);
  const [minted, setMinted] = useState<EnrollToken | null>(null);

  // The manager already returns a ready-made one-liner; this only appends the
  // operator's --hardware choice, which is a UI-side selection made after the
  // mint. An older master returns no install_command — fall back to the join
  // command rather than rendering an empty box.
  function installCommand(m: EnrollToken): string | null {
    if (!m.install_command) return null;
    return hardware ? `${m.install_command} --hardware ${hardware}` : m.install_command;
  }

  async function mint() {
    const n = name.trim();
    if (!n) { toast("Enter a worker name", "err"); return; }
    setBusy(true);
    try {
      setMinted(await endpoints.enrollToken({ name: n }));
    } catch (e) { toast(String(e), "err"); } finally { setBusy(false); }
  }
  function copy(text: string) {
    navigator.clipboard?.writeText(text).then(() => toast("Copied", "ok"), () => {});
  }

  return (
    <Drawer title="Add a worker" onClose={onClose} storageKey="rzfz.drawer.addworker" defaultWidth={620}>
      <p className="lede">Enroll another machine into the fleet. Name it, mint a one-time token, then run the command on that box — it self-registers, no SSH from here. The box needs nothing pre-installed.</p>
      <div className="inline-edit" style={{ marginTop: 6 }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. worker-gpu-2"
          aria-label="Worker name" style={{ width: 260 }} disabled={busy || !!minted} />
        <select value={hardware} aria-label="Hardware class" disabled={busy}
          onChange={(e) => setHardware(e.target.value as "cpu" | "amd" | "nvidia")}>
          <option value="cpu">CPU</option>
          <option value="amd">AMD</option>
          <option value="nvidia">NVIDIA</option>
        </select>
        {!minted && <button className="btn sm primary" onClick={mint} disabled={busy}>{busy ? "…" : "Mint token"}</button>}
      </div>

      {minted && (
        <div style={{ marginTop: 16 }}>
          <h2 className="section">Run this on <span className="mono">{minted.worker_name}</span></h2>
          {installCommand(minted) && (
            <>
              <div className="spread"><strong className="mono">Install command</strong>
                <button className="btn sm ghost" onClick={() => copy(installCommand(minted)!)}>copy</button></div>
              <pre className="logs-pre">{installCommand(minted)}</pre>
              <div className="hint" style={{ marginTop: 4 }}>
                Blank box — no repo, no rzfz. The master serves the installer; it installs Docker,
                enrols this worker and starts it as a systemd service.
              </div>
            </>
          )}
          <div className="spread" style={{ marginTop: 12 }}><strong className="mono">Join command (box with the repo)</strong>
            <button className="btn sm ghost" onClick={() => copy(minted.join_command)}>copy</button></div>
          <pre className="logs-pre">{minted.join_command}</pre>
          <dl className="kv-list" style={{ marginTop: 8 }}>
            <dt>Manager URL</dt><dd className="mono">{minted.manager_url || "(set LLM_MANAGER_ADVERTISE_URL)"}</dd>
            <dt>Token expires</dt><dd className="muted">{new Date(minted.expires_at * 1000).toLocaleString()}</dd>
          </dl>
          <div className="hint" style={{ marginTop: 4 }}>The token is single-purpose (initial join only) and short-lived. The worker appears here once its agent reports in.</div>
          <div style={{ marginTop: 10 }}>
            <button className="btn sm" onClick={() => { setMinted(null); setName(""); }}>Add another</button>
          </div>
        </div>
      )}
    </Drawer>
  );
}

type DiskCache = { mount: string; count: number; total_gb: number; files: { name: string; size_gb: number }[]; error?: string };

// #1183: one shape for the drawer's command-backed panels. `data` may be the
// node's LAST answer (`stale`, with `asOf`) while `loading` says a refresh is
// in flight — stale-while-revalidate, so the panel paints at once instead of
// showing nothing for a full claim round-trip. `note` is a soft caveat that
// leaves `data` on screen (e.g. the refresh was not confirmed in time).
type PanelState<T> = { loading: boolean; stale?: boolean; asOf?: string | null; err?: string; note?: string; data?: T };

function StaleNote({ st, what }: { st: PanelState<unknown>; what: string }) {
  if (st.loading && st.stale) return <span className="badge muted" title={st.asOf ? `Last answer from the node ${relTime(st.asOf)}; a fresh ${what} is on its way` : undefined}>refreshing… <span className="muted">(as of {relTime(st.asOf)})</span></span>;
  if (st.note) return <span className="badge warn" title={st.note}>{st.note}</span>;
  return null;
}

function WorkerDrawer({ worker, onClose, onChanged }: { worker: WorkerRow; onClose: () => void; onChanged: () => void }) {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const [logsFor, setLogsFor] = useState<string | null>(null);
  const [nameDraft, setNameDraft] = useState(worker.display_name ?? "");
  // #306 on-disk model cache — what weights this worker physically holds. External
  // backends (Ollama Mac/box) have no worker-agent/mount: their weights live remotely.
  const external = worker.hardware === "external" || worker.engine === "ollama";
  const [cache, setCache] = useState<PanelState<DiskCache>>({ loading: !external });
  // shared loader — the mount effect below AND the post-delete refresh both
  // go through this one function, never a second fetch path.
  // CUI-17: list_disk_models is a node command polled on a wall-clock deadline.
  // Closing the drawer used to leave that poll running (and re-opening started
  // another), so the controller is aborted from the effect cleanup and by the
  // next loadCache() — a drawer opened and closed repeatedly no longer stacks
  // background pollers against the manager API.
  // #1183: stale-while-revalidate — the manager answers the enqueue with the
  // node's last listing (painted at once, marked stale) and reuses a refresh
  // already in flight; the fresh listing replaces it when the node answers.
  const cacheAbort = useRef<AbortController | null>(null);
  // `fresh`: after a confirmed WRITE (delete) the node's last answer is the
  // pre-mutation listing — serving it stale-while-revalidate would resurrect
  // the just-deleted file for a whole claim round-trip (rzfz review #1206,
  // the #835 class). A post-mutation reload therefore drops the stale copy
  // and waits for the fresh answer.
  function loadCache(fresh = false) {
    if (external) return;
    cacheAbort.current?.abort();
    const ctl = new AbortController();
    cacheAbort.current = ctl;
    setCache((prev) => (fresh ? { loading: true } : { ...prev, loading: true, err: undefined, note: undefined }));
    runCommand(worker.id, "list_disk_models", {}, {
      signal: ctl.signal, revalidate: !fresh, pollMs: 750,
      onStale: (r, at) => { if (!ctl.signal.aborted) setCache({ loading: true, stale: true, asOf: at, data: r as unknown as DiskCache }); },
    })
      .then((c) => {
        if (ctl.signal.aborted) return;
        if (c.status === "done") setCache({ loading: false, data: c.result as unknown as DiskCache });
        else if (c.timed_out) setCache((prev) => prev.data
          ? { ...prev, loading: false, note: "refresh not confirmed yet — showing the node's last answer" }
          : { loading: false, err: "node has not answered yet — it claims commands on its next report cycle" });
        else setCache((prev) => ({ ...prev, loading: false, err: String((c.result as { error?: string } | null)?.error ?? JSON.stringify(c.result)) }));
      })
      .catch((e) => { if (!ctl.signal.aborted) setCache((prev) => ({ ...prev, loading: false, err: String(e) })); });
  }
  useEffect(() => {
    // eslint-disable-next-line react-hooks/exhaustive-deps
    loadCache();
    return () => cacheAbort.current?.abort();
  }, [worker.id, external]);

  // #835: provenance for the on-disk cache view — which deployment (model +
  // HF repo) each cached FILE belongs to, plus everything a faithful
  // "redeploy this cached model onto worker X" call needs. Reuses the
  // EXISTING `GET /api/inventory` (#307 S3, already admin-gated) rather than
  // a new route; the manager already resolved the basename→provenance join
  // (DB-authoritative, ambiguity-safe — see `_file_provenance_map`) so this
  // is a dumb lookup, no guessing here.
  const inv = useQuery({ queryKey: ["inventory"], queryFn: endpoints.inventory, enabled: !external });
  const provenance = inv.data?.file_provenance ?? {};
  const modelsById = new Map((inv.data?.models ?? []).map((m) => [m.deployment_id, m]));
  // #837 item 3: how much of this worker's cache no deployment claims —
  // undeployed weights that are pure reclaimable disk. Read from the manager's
  // own join (`_unreferenced_cached`) rather than re-derived here, the same
  // posture as `file_provenance` above: one authority, not two.
  const unclaimed = (inv.data?.unreferenced ?? []).find((u) => u.worker_id === worker.id);

  function basenameOf(name: string): string {
    return name.split(/[\\/]/).pop() || name;
  }

  const [deployBusy, setDeployBusy] = useState<string | null>(null);
  async function deployFromCache(model: FleetInventoryModel) {
    if (!confirm(`Deploy ${model.model_name} onto ${worker.display_name || worker.name} from its on-disk cache? The already-present weight files are not re-downloaded.`)) return;
    setDeployBusy(model.deployment_id);
    try {
      const body: Record<string, unknown> = {
        model_name: model.model_name,
        served_model: model.served_model,
        worker_id: worker.id,
        files: model.files,
        hf_repo: model.hf_repo,
        params: model.params,
        task: model.task,
        tags: model.tags,
      };
      if (model.est_gb != null) body.est_gb = model.est_gb;
      const r = await deployWithFitConfirm(body);
      if (r) { toast(`Deploying ${model.model_name} on ${worker.display_name || worker.name} from cache`, "ok"); onChanged(); }
    } catch (e) {
      toast(e instanceof ApiError ? e.message : String(e), "err");
    } finally {
      setDeployBusy(null);
    }
  }

  // #306 delete half: free disk by removing one cached weight file. The
  // dedicated route validates `name` and enqueues synchronously (422 on a
  // malformed path, surfaced as an ApiError below); the node does the actual
  // delete asynchronously and refuses (command status "failed", result.error)
  // when the file backs a currently-loaded deployment — surface that too,
  // never crash or silently swallow it.
  const [cacheBusy, setCacheBusy] = useState<string | null>(null);
  async function deleteCached(f: { name: string; size_gb: number }) {
    if (!confirm(`Delete ${f.name}? Frees ${f.size_gb} GB.`)) return;
    setCacheBusy(f.name);
    try {
      const cmd = await endpoints.deleteDiskModel(worker.id, f.name);
      const r = await waitCommand(cmd);
      if (r.status === "done") {
        const freed = (r.result as { freed_gb?: number } | null)?.freed_gb ?? f.size_gb;
        toast(`Deleted ${f.name} — ${freed} GB freed`, "ok");
        loadCache(true);
        // #835: the two halves of THIS table must agree. The row's sibling
        // "Deploy from cache" button reads the `["inventory"]` query
        // (provenance + the per-worker `cached` flag), which is cached
        // (App.tsx: staleTime 10s, no refetch-on-focus) and is not remounted
        // by a delete — so refreshing only the disk listing left the console
        // still offering a no-pull redeploy of weights that are gone, which
        // would silently re-fetch from HuggingFace or, on an offline box,
        // simply fail. Invalidate it alongside the listing.
        qc.invalidateQueries({ queryKey: ["inventory"] });
      } else if (r.timed_out) {
        toast(`Delete of ${f.name} not confirmed yet — node still working; check command history`, "warn");
      } else {
        toast(`Delete failed: ${String((r.result as { error?: string } | null)?.error ?? JSON.stringify(r.result))}`, "err");
      }
    } catch (e) {
      toast(e instanceof ApiError ? e.message : String(e), "err");
    } finally {
      setCacheBusy(null);
    }
  }

  async function restart(container: string) {
    setBusy(`restart:${container}`);
    try {
      const c = await runCommand(worker.id, "restart_engine", { container });
      // #348: timed_out means the node has not confirmed yet — not a failure.
      toast(c.status === "done" ? `Restarted ${container}`
        : c.timed_out ? `Restart of ${container} not confirmed yet — node still working; check command history`
        : `Restart failed: ${JSON.stringify(c.result)}`,
        c.status === "done" ? "ok" : c.timed_out ? "warn" : "err");
      onChanged();
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }

  return (
    <Drawer title={<><StatusDot status={worker.status} />{worker.display_name || worker.name}</>} onClose={onClose}
      storageKey="rzfz.drawer.worker" defaultWidth={640}>
      {/* #284 — rename a worker (manager-owned display label; node keeps its
          registered name). */}
      <div className="affordance">
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Display name <span className="hint">shown in the console; the node keeps <span className="mono">{worker.name}</span></span></label>
          <div className="inline-edit">
            <input value={nameDraft} onChange={(e) => setNameDraft(e.target.value)}
              placeholder={worker.name} aria-label="Worker display name" style={{ width: 240 }}
              maxLength={128} disabled={busy === "rename"} />
            <button className="btn sm primary" disabled={busy === "rename" || nameDraft === (worker.display_name ?? "")}
              onClick={async () => {
                setBusy("rename");
                try {
                  await endpoints.renameWorker(worker.id, nameDraft.trim());
                  toast(nameDraft.trim() ? `Renamed to ${nameDraft.trim()}` : "Reset to the registered name", "ok");
                  onChanged();
                } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
              }}>{busy === "rename" ? "…" : "Rename"}</button>
          </div>
        </div>
      </div>

      {/* #261-C2 drain / undrain — manager-driven, no command kind involved */}
      {!external && worker.status !== "pending" && (
        <div className="btn-row" style={{ marginBottom: 10 }}>
          {worker.status === "draining" ? (
            <button className="btn sm primary" disabled={busy === "undrain"}
              onClick={async () => {
                setBusy("undrain");
                try { await endpoints.undrainWorker(worker.id); toast(`${worker.name} back in service`, "ok"); onChanged(); }
                catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
              }}>{busy === "undrain" ? "…" : "Undrain — return to service"}</button>
          ) : (
            <button className="btn sm" disabled={busy === "drain"}
              onClick={async () => {
                if (!confirm(`Drain ${worker.name}? All ${worker.instances.length} model instance(s) unload and placement skips this worker until undrained.`)) return;
                setBusy("drain");
                try { const r = await endpoints.drainWorker(worker.id); toast(`Draining ${worker.name} — ${r.instances_unloaded} instance(s) unloading`, "ok"); onChanged(); }
                catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
              }}>{busy === "drain" ? "…" : "Drain"}</button>
          )}
        </div>
      )}

      {/* #594 retire: forget a worker that has left the fleet. Mirrors the
          server gate — offered only when the worker is NOT actively serving
          (a "ready" (fresh) non-external worker must be drained first; the
          Drain button above covers it). Type-to-confirm: destructive + the
          worker is gone from the fleet after. */}
      {(worker.status !== "ready" || external) && (
        <div className="btn-row" style={{ marginBottom: 10 }}>
          <button className="btn sm danger" disabled={busy === "remove"}
            onClick={async () => {
              const typed = prompt(
                `Remove ${worker.name} from the fleet? This forgets the worker — ` +
                `it must re-enroll with a fresh join command to return.\n\n` +
                `Type the worker name to confirm:`);
              if (typed !== worker.name) {
                if (typed !== null) toast("Name did not match — not removed", "err");
                return;
              }
              setBusy("remove");
              try {
                const r = await endpoints.removeWorker(worker.id);
                toast(`Removed ${r.worker} (${r.instances_removed} instance row(s) cleared)`, "ok");
                onChanged();
              } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
            }}>{busy === "remove" ? "…" : "Remove from fleet"}</button>
        </div>
      )}

      <dl className="kv-list">
        <dt>Status</dt><dd><StatusBadge status={worker.status} /></dd>
        <dt>Address</dt><dd className="mono">{worker.address}</dd>
        <dt>Hardware</dt><dd>{worker.hardware ?? "—"}</dd>
        <dt>Engine</dt><dd>{worker.engine ?? "—"}</dd>
        <dt>Advertise addr</dt><dd className="mono">{worker.advertise_addr ?? "— (same-box)"}</dd>
        <dt>Stack version</dt><dd className="mono">{worker.stack_version ?? "\u2014"}{" "}
          <span className="muted">{versionSourceLabel(worker.stack_version_source)}</span></dd>
        <dt>Engine build</dt><dd className="mono">{worker.engine_version ?? "\u2014"}{" "}
          <span className="muted">{engineVersionLabel(worker.engine_version_why)}</span></dd>
        <dt>Last seen</dt><dd className="muted">{relTime(worker.last_heartbeat)}</dd>
        {!external && (<>
          <dt>VRAM admission</dt>
          <dd>{worker.admission_basis
            ? <>{worker.admission_budget_gb} GB <span className="muted">(from {worker.admission_basis}, incl. headroom)</span></>
            : <span className="badge warn" title="No usable capacity label — every deploy is admitted unchecked. Fix the worker's vram_total_gb/mem_total_gb labels.">inert — no capacity label</span>}
          </dd>
        </>)}
      </dl>

      <h2 className="section" style={{ marginTop: 8 }}>Model instances ({worker.instances.length})</h2>
      {worker.instances.length === 0 ? (
        <p className="muted">Idle — no model instances running on this worker.</p>
      ) : (
        <div className="table-wrap">
          <table className="rz">
            <thead><tr><th>Model</th><th>Status</th><th>Endpoint</th><th></th></tr></thead>
            <tbody>
              {worker.instances.map((i, idx) => (
                <Fragment key={idx}>
                  <tr>
                    <td><StatusDot status={i.status} />{i.model_name}</td>
                    <td><StatusBadge status={i.status} />{i.detail ? <div className="hint mono" style={{ marginTop: 2 }}>{i.detail}</div> : null}</td>
                    <td className="mono muted" style={{ fontSize: "0.7rem" }}>{i.endpoint}</td>
                    <td>
                      <div className="btn-row">
                        <button className={`btn sm${logsFor && logsFor === i.container ? " on" : ""}`} disabled={!i.container}
                          aria-expanded={!!logsFor && logsFor === i.container}
                          onClick={() => i.container && setLogsFor(logsFor === i.container ? null : i.container)}>
                          {logsFor && logsFor === i.container ? "Hide logs" : "Logs"}
                        </button>
                        <button className="btn sm primary" disabled={!i.container || !!busy}
                          onClick={() => i.container && confirm(`Restart ${i.container}?`) && restart(i.container)}>
                          {busy === `restart:${i.container}` ? "…" : "Restart"}
                        </button>
                      </div>
                      {/* #319: jump to this model's deployment detail on the Deployments page */}
                      <button className="linklike" style={{ marginTop: 4 }}
                        onClick={() => nav(`/models?model=${encodeURIComponent(i.model_name)}`)}>
                        See deployment details →
                      </button>
                    </td>
                  </tr>
                  {/* #1183 (3): the log pane opens INLINE, directly under the
                      clicked instance — it used to render below the two long
                      tables further down, under the fold on a normal screen,
                      so "Logs" looked like it did nothing. Inline also keeps
                      the context (WHICH instance) on screen. */}
                  {logsFor && logsFor === i.container && (
                    <tr className="inline-logs">
                      <td colSpan={4}>
                        <LogsPane container={logsFor} workerId={worker.id} onClose={() => setLogsFor(null)} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="section" style={{ marginTop: 12 }}>On-disk model cache</h2>
      {external ? (
        <p className="muted">External backend — weights live on the remote host, not managed here.</p>
      ) : cache.loading && !cache.data ? (
        // #1183 (1): a skeleton, not a blank panel, for the claim round-trip
        <>
          <p className="muted" style={{ marginBottom: 6 }}>Scanning the worker's model mount…</p>
          <TableSkeleton rows={3} cols={4} />
        </>
      ) : cache.err && !cache.data ? (
        <p className="hint">Could not read the cache: <span className="mono">{cache.err}</span>{" "}
          <button className="btn sm ghost" onClick={() => loadCache()}>retry</button></p>
      ) : !cache.data?.files?.length ? (
        <p className="muted">No model weights on disk{cache.data?.mount ? <> under <span className="mono">{cache.data.mount}</span></> : ""}. <StaleNote st={cache} what="listing" /></p>
      ) : (
        <>
          <div className="spread"><span className="muted">{cache.data.count} file{cache.data.count === 1 ? "" : "s"}
            {unclaimed ? <span title="Cached weights no deployment claims — deleting them frees disk without touching any model the console can still deploy (#837)"> · {unclaimed.count} unclaimed, {unclaimed.total_gb} GB reclaimable</span> : null}
            {" "}<StaleNote st={cache} what="listing" /></span>
            <span className="muted mono">{cache.data.total_gb} GB · {cache.data.mount}</span></div>
          <div className="table-wrap">
            <table className="rz">
              <thead><tr><th>File</th><th>Model</th><th className="right">Size</th><th></th></tr></thead>
              <tbody>
                {cache.data.files.map((f) => {
                  // #835: provenance is an authoritative DB join the manager
                  // already resolved (`_file_provenance_map`) — this is a
                  // plain lookup, never a guess. Absent or `ambiguous` both
                  // read as unknown: no redeploy is ever offered off a guess.
                  const prov = provenance[basenameOf(f.name)];
                  const known = prov && prov.ambiguous === false ? prov : null;
                  const model = known ? modelsById.get(known.deployment_id) : undefined;
                  // "fully cached on THIS worker" reuses the SAME per-worker
                  // `cached` flag `_worker_has_files` computes for #307 S3 —
                  // true only when EVERY file this deployment needs (not just
                  // this row) is already on this worker's disk, so the
                  // redeploy is a genuine no-pull relaunch.
                  const fullyCached = model?.workers.some((w) => w.worker_id === worker.id && w.cached) ?? false;
                  const alreadyServing = model ? worker.instances.some((i) => i.model_name === model.model_name) : false;
                  return (
                    <tr key={f.name}><td className="mono" style={{ fontSize: "0.72rem" }}>{f.name}</td>
                      <td>
                        {known ? (
                          <span title={known.hf_repo ?? undefined}>{known.model_name}</span>
                        ) : prov?.ambiguous ? (
                          <span className="muted" title="Multiple deployments claim this file from different HF repos — provenance is ambiguous, no redeploy offered">ambiguous</span>
                        ) : (
                          <span className="muted">unknown</span>
                        )}
                      </td>
                      <td className="right num">{f.size_gb} GB</td>
                      <td>
                        <div className="btn-row">
                          {model && fullyCached && !alreadyServing && (
                            <button className="btn sm primary" disabled={!!cacheBusy || !!deployBusy}
                              onClick={() => deployFromCache(model)}>
                              {deployBusy === model.deployment_id ? "…" : "Deploy from cache"}
                            </button>
                          )}
                          {model && alreadyServing && (
                            <span className="muted" title="Already running on this worker">running here</span>
                          )}
                          <button className="btn sm danger" disabled={!!cacheBusy || !!deployBusy}
                            onClick={() => deleteCached(f)}>
                            {cacheBusy === f.name ? "…" : "Delete"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      {!external && <RunnersSection worker={worker} onChanged={onChanged} />}
    </Drawer>
  );
}

// #549 R2/R4 — runner images on this node: inventory, pull, remove; and the R3
// upgrade sequence (drain → pull → repin → relaunch) with live state + rollback.
function RunnersSection({ worker, onChanged }: { worker: WorkerRow; onChanged: () => void }) {
  const [inv, setInv] = useState<PanelState<RunnerInventory>>({ loading: true });
  const [image, setImage] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [up, setUp] = useState<RunnerUpgradeRow | null>(null);
  const upActive = !!up && (up.state === "deploying" || up.state === "relaunching");

  // CUI-17: same cancellation channel as the disk-cache poll above.
  // #1183: same stale-while-revalidate as the disk cache — the enqueue answers
  // with the node's last inventory (painted at once), the fresh one replaces it.
  const invAbort = useRef<AbortController | null>(null);
  // `fresh`: same rule as loadCache — after Pull/Remove/Upgrade the stale
  // listing lacks the new image or still shows the removed one.
  async function refresh(fresh = false) {
    invAbort.current?.abort();
    const ctl = new AbortController();
    invAbort.current = ctl;
    setInv((prev) => (fresh ? { loading: true } : { ...prev, loading: true, err: undefined, note: undefined }));
    try {
      const cmd = await endpoints.listRunnerImages(worker.id, { revalidate: !fresh });
      if (ctl.signal.aborted) return;
      if (cmd.stale_result) setInv({ loading: true, stale: true, asOf: cmd.stale_finished_at ?? null, data: cmd.stale_result as unknown as RunnerInventory });
      const c = await waitCommand(cmd, { signal: ctl.signal, pollMs: 750 });
      if (ctl.signal.aborted) return;
      if (c.status === "done") setInv({ loading: false, data: c.result as unknown as RunnerInventory });
      else if (c.timed_out) setInv((prev) => prev.data
        ? { ...prev, loading: false, note: "refresh not confirmed yet — showing the node's last answer" }
        : { loading: false, err: "node has not answered yet — it claims commands on its next report cycle" });
      else setInv((prev) => ({ ...prev, loading: false, err: String((c.result as { error?: string })?.error ?? JSON.stringify(c.result)) }));
    } catch (e) { if (!ctl.signal.aborted) setInv((prev) => ({ ...prev, loading: false, err: String(e) })); }
  }
  useEffect(() => {
    refresh();
    endpoints.latestUpgrade(worker.id).then(setUp).catch(() => setUp(null));
    return () => invAbort.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [worker.id]);
  // live-poll an active upgrade; the status read runs the manager's lazy
  // timeout check, so a stuck upgrade turns failed here, not never. Failures
  // are bounded (agent-seqis on #563): if the row vanishes (manager redeploy —
  // which a runner upgrade makes likely on a single-box master), re-resolve via
  // the by-worker read instead of spinning on a dead id forever.
  const pollFails = useRef(0);
  useEffect(() => {
    if (!upActive || !up) return;
    pollFails.current = 0;
    const t = setInterval(() => {
      endpoints.upgradeStatus(up.upgrade_id)
        .then((r) => { pollFails.current = 0; setUp(r); if (r.state === "done" || r.state === "failed") { onChanged(); refresh(true); } })
        .catch(() => {
          pollFails.current += 1;
          if (pollFails.current >= 5) {
            pollFails.current = 0;
            endpoints.latestUpgrade(worker.id).then(setUp).catch(() => setUp(null));
          }
        });
    }, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [upActive, up?.upgrade_id]);

  async function deploy() {
    const ref = image.trim();
    if (!ref) { toast("Enter a registry-qualified image ref", "err"); return; }
    setBusy("deploy"); setNote("enqueued — the node claims on its next report cycle…");
    try {
      const c = await waitCommand(await endpoints.deployRunner(worker.id, ref),
        { onUpdate: (u) => setNote(`pull ${u.status}…`) });
      if (c.status === "done") { toast(`Pulled ${ref}`, "ok"); setImage(""); refresh(true); }
      else toast(c.timed_out ? "Pull not confirmed yet — node still working; refresh later"
        : `Pull failed: ${String((c.result as { error?: string })?.error ?? JSON.stringify(c.result))}`, "err");
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); setNote(null); }
  }
  async function remove(ref: string) {
    if (!confirm(`Remove ${ref} from ${worker.name}? The node refuses if a container still uses it.`)) return;
    setBusy(`rm:${ref}`);
    try {
      const c = await waitCommand(await endpoints.removeRunner(worker.id, ref));
      if (c.status === "done") { toast(`Removed ${ref}`, "ok"); refresh(true); }
      else toast(c.timed_out ? "Removal not confirmed yet — node still working"
        : `Remove failed: ${String((c.result as { error?: string })?.error ?? JSON.stringify(c.result))}`, "err");
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }
  async function upgrade() {
    const ref = image.trim();
    if (!ref) { toast("Enter a registry-qualified image ref", "err"); return; }
    if (!confirm(`Upgrade ${worker.name} to ${ref}?

This DRAINS the worker (all instances unload), pulls the image, repins its deployments and relaunches them on the new runner. Rollback is available until it completes.`)) return;
    setBusy("upgrade");
    try {
      const r = await endpoints.upgradeRunner(worker.id, ref);
      toast(`Upgrade started — ${r.captured_deployments} deployment(s) captured`, "ok");
      setUp(await endpoints.latestUpgrade(worker.id)); setImage(""); onChanged();
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }
  async function rollback() {
    if (!up) return;
    if (!confirm(`Roll back the upgrade to ${up.image}? Prior runner pins are restored and the captured deployments relaunch.`)) return;
    setBusy("rollback");
    try {
      await endpoints.rollbackUpgrade(up.upgrade_id);
      toast("Rolled back — worker returning to service", "ok");
      setUp(await endpoints.latestUpgrade(worker.id)); onChanged(); refresh(true);
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }

  return (
    <>
      <h2 className="section" style={{ marginTop: 12 }}>Runner images</h2>
      {up && (upActive || up.state === "failed") && (
        <div className={`callout ${up.state === "failed" ? "err" : "info"}`}>
          <div className="spread">
            <span><b>Upgrade {up.state}</b> → <span className="mono">{up.image}</span></span>
            <button className="btn sm" disabled={busy === "rollback"} onClick={rollback}>
              {busy === "rollback" ? "…" : "Roll back"}</button>
          </div>
          {up.state === "deploying" && <div className="hint" style={{ marginTop: 4 }}>Worker drained; node is pulling the image. Deployments relaunch on the new runner once the pull reports done.</div>}
          {up.state === "relaunching" && <div className="hint" style={{ marginTop: 4 }}>Image pulled; captured deployments repinned and relaunching — done when their instances report ready.</div>}
          {up.error && <div className="hint mono" style={{ marginTop: 4 }}>{up.error}</div>}
        </div>
      )}
      {runnerRegistryWarning(inv.data) && (
        <div className="hint" role="alert" style={{ marginTop: 4, marginBottom: 6 }}>
          ⚠ {runnerRegistryWarning(inv.data)}
        </div>
      )}
      {inv.loading && !inv.data ? (
        // #1183 (1): skeleton for the claim round-trip
        <>
          <p className="muted" style={{ marginBottom: 6 }}>Asking the node for its runner inventory…</p>
          <TableSkeleton rows={2} cols={4} />
        </>
      ) : inv.err && !inv.data ? (
        <p className="hint">Inventory unavailable: <span className="mono">{inv.err}</span>{" "}
          <button className="btn sm ghost" onClick={() => refresh()}>retry</button></p>
      ) : !inv.data?.runners.length ? (
        <p className="muted">No runner images on this node yet — pull one from <span className="mono">{inv.data?.registry}</span> below. <StaleNote st={inv} what="inventory" /></p>
      ) : (
        <div className="table-wrap">
          {(inv.stale && inv.loading) || inv.note ? <div className="spread" style={{ marginBottom: 4 }}><StaleNote st={inv} what="inventory" /><span /></div> : null}
          <table className="rz">
            <thead><tr><th>Image</th><th>Origin</th><th className="right">Size</th><th></th></tr></thead>
            <tbody>
              {inv.data.runners.map((r) => (
                <tr key={r.image}>
                  <td className="mono" style={{ fontSize: "0.72rem" }}>{r.image}</td>
                  <td><span className="chip">{r.origin}</span></td>
                  <td className="right num">{r.size_gb} GB</td>
                  <td><button className="btn sm" disabled={!!busy || r.origin !== "registry"}
                    title={r.origin !== "registry" ? "init-built runner — managed by the box's own upgrade, not this channel" : "Remove from node"}
                    onClick={() => remove(r.image)}>{busy === `rm:${r.image}` ? "…" : "Remove"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="inline-edit" style={{ marginTop: 8 }}>
        <input className="mono" value={image} onChange={(e) => setImage(e.target.value)}
          placeholder={`${inv.data?.registry ?? "llm-registry:5000"}/runners/llama-vulkan-runner:bXXXX`}
          aria-label="Runner image ref" style={{ width: 340 }} disabled={!!busy || upActive} />
        <button className="btn sm" disabled={!!busy || upActive || !image.trim()} onClick={deploy}>
          {busy === "deploy" ? "…" : "Pull onto node"}</button>
        <button className="btn sm primary" disabled={!!busy || upActive || !image.trim()} onClick={upgrade}>
          {busy === "upgrade" ? "…" : "Upgrade & relaunch"}</button>
      </div>
      {note && <div className="hint" style={{ marginTop: 4 }}>{note}</div>}
      <div className="hint" style={{ marginTop: 4 }}>Pull just fetches the image (deployments untouched). Upgrade drains the worker, pins its deployments to the new runner and relaunches them — with rollback until done. Refs must come from the allowed registry.</div>
    </>
  );
}

// #307: register a pre-existing OpenAI/Ollama endpoint as an external backend —
// no agent, no container; the manager just folds its models into the router.
function ExternalBackendDrawer({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [models, setModels] = useState("");
  const [busy, setBusy] = useState(false);
  async function add() {
    const n = name.trim(), ep = endpoint.trim();
    if (!n || !ep) { toast("Name and endpoint are required", "err"); return; }
    setBusy(true);
    try {
      const body: Record<string, unknown> = { name: n, endpoint: ep };
      const ms = models.split(",").map((m) => m.trim()).filter(Boolean);
      if (ms.length) body.models = ms;
      const r = await endpoints.addExternalBackend(body);
      toast(`Registered ${r.name} — ${r.models.length} model(s)`, "ok");
      onChanged(); onClose();
    } catch (e) { toast(String(e), "err"); } finally { setBusy(false); }
  }
  return (
    <Drawer title="Add external backend" onClose={onClose} storageKey="rzfz.drawer.extbackend" defaultWidth={620}>
      <p className="lede">Register a pre-existing OpenAI-compatible endpoint (e.g. a Mac or box running Ollama) as a backend. The manager folds its models into the fleet router — no agent, no container; the endpoint keeps running its own server, untouched.</p>
      <div className="field"><label>Name</label>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. my-mac-worker" /></div>
      <div className="field"><label>Endpoint <span className="hint">OpenAI-compatible base — Ollama is …:11434/v1</span></label>
        <input className="mono" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="http://mac-worker.lan:11434/v1" /></div>
      <div className="field"><label>Models <span className="hint">optional, comma-separated — blank auto-discovers from /v1/models</span></label>
        <input className="mono" value={models} onChange={(e) => setModels(e.target.value)} placeholder="(auto-discover)" /></div>
      <div className="callout info" style={{ marginTop: 8 }}>The manager <b>and its router</b> must be able to reach this endpoint over the network.</div>
      <div className="btn-row" style={{ marginTop: 12 }}>
        <button className="btn primary" disabled={busy || !name.trim() || !endpoint.trim()} onClick={add}>{busy ? "…" : "Register backend"}</button>
        <button className="btn ghost" onClick={onClose}>Cancel</button>
      </div>
    </Drawer>
  );
}

export function Fleet() {
  const qc = useQueryClient();
  // #286/#288: poll so engine loading→ready/failed transitions show live.
  const q = useQuery({ queryKey: ["workers"], queryFn: endpoints.workers, refetchInterval: 5000 });
  const [openId, setOpenId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [extOpen, setExtOpen] = useState(false);
  const open = q.data?.find((w) => w.id === openId) || null;

  return (
    <section className="page">
      <h1>Workers</h1>
      <p className="lede">Machines serving the fleet. Each worker exposes its engines (llama.cpp / vLLM / Ollama / CPU) to the router. Click a row for detail + actions.</p>
      <div className="spread">
        <span className="muted">{q.data ? `${q.data.length} worker${q.data.length === 1 ? "" : "s"}` : ""}</span>
        <div className="btn-row">
          <button className="btn ghost sm" onClick={() => qc.invalidateQueries({ queryKey: ["workers"] })}>Refresh</button>
          <button className="btn sm" onClick={() => setExtOpen(true)}>+ Add external backend</button>
          <button className="btn sm primary" onClick={() => setAddOpen(true)}>+ Add worker</button>
        </div>
      </div>
      <QueryState q={q} isEmpty={(d) => d.length === 0}
        loading={<TableSkeleton rows={4} cols={6} />}
        empty={<div className="empty">No workers registered yet. Nodes self-register once their agent starts and reports served models.</div>}>
        {(nodes) => (
          <div className="table-wrap">
            <table className="rz">
              <thead>
                <tr><th>Worker</th><th>Hardware</th><th>Stack</th><th>Status</th><th>Last seen</th><th>Serving</th></tr>
              </thead>
              <tbody>
                {nodes.map((w) => (
                  <tr key={w.id} {...rowProps(() => setOpenId(w.id), w.id === openId)}>
                    <td><StatusDot status={w.status} /><strong>{w.display_name || w.name}</strong></td>
                    <td>{w.hardware ?? "—"}{w.hardware !== "external" && !w.admission_basis &&
                      <span className="badge warn" style={{ marginLeft: 6 }}
                        title="#328: no usable capacity label — VRAM admission is inert; deploys are admitted unchecked">no admission</span>}</td>
                    <td className="mono">{w.stack_version ?? "—"}</td>
                    <td><StatusBadge status={w.status} /></td>
                    <td className="muted">{relTime(w.last_heartbeat)}</td>
                    <td>
                      {w.instances.length === 0 ? <span className="muted">idle</span>
                        : w.instances.map((i, idx) => <span key={idx} className="chip">{i.model_name}</span>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
      {open && <WorkerDrawer worker={open} onClose={() => setOpenId(null)}
        onChanged={() => qc.invalidateQueries({ queryKey: ["workers"] })} />}
      {addOpen && <AddWorkerDrawer onClose={() => setAddOpen(false)} />}
      {extOpen && <ExternalBackendDrawer onClose={() => setExtOpen(false)}
        onChanged={() => qc.invalidateQueries({ queryKey: ["workers"] })} />}
    </section>
  );
}
