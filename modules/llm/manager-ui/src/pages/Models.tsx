import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, endpoints, runCommand, type DeploymentRow } from "../api/client";
import { Badge, Drawer, Params, QueryState, rowProps, StatusBadge, StatusDot, TableSkeleton, relTime, toast } from "../components/ui";
import { LogsPane } from "../components/LogsPane";

// #284 — edit desired state via PATCH /api/deployments/{id}: replicas, backend
// params, tags, display_name. model_name itself stays immutable (the
// client-facing LiteLLM routing id) — display_name is the console-only alias
// node registration never clobbers.
function ModelEditAffordances({ dep, onChanged }: { dep: DeploymentRow; onChanged: () => void }) {
  const [replicas, setReplicas] = useState(dep.replicas);
  const [nameDraft, setNameDraft] = useState(dep.display_name ?? "");
  const [paramsText, setParamsText] = useState(
    JSON.stringify(Object.fromEntries(Object.entries(dep.params ?? {}).filter(([k]) => k !== "tags")), null, 2));
  const tags0 = (dep.params as any)?.tags;
  const [tagsText, setTagsText] = useState(Array.isArray(tags0) ? tags0.join(", ") : "");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // #1542: WHERE the next replica goes. "" = the auto-placement this endpoint
  // has always done; a worker id names the target.
  //
  // The API half landed in #1543 and nothing sent it — so on a heterogeneous
  // fleet the operator still could not put the second replica where the load
  // belongs, which is the whole point of the issue. `_pick_worker` sorts by
  // NAME, so auto-placement hands out `box-175r` before `gb10-191`: for RAG
  // load that is measurably the slower box (1.7-1.9x on prefill, 1.8x on batch
  // embedding — .gsd/reports/2026.09-3box-perf-comparison…).
  const [target, setTarget] = useState<string>("");
  const workers = useQuery({ queryKey: ["workers"], queryFn: endpoints.workers, retry: false });

  // #1038/CUI-4: the drawer re-polls `deployments` every 5s but `replicas` is a
  // local mirror seeded once — so a REJECTED change (403 from require_role, 409
  // from the admission gate, 422, transport failure) used to leave the stepper
  // showing a number the server never accepted, for the whole life of the open
  // drawer. `onFail` restores the last server-accepted value.
  async function save(kind: string, body: Record<string, unknown>, onFail?: () => void) {
    setErr(null); setBusy(kind);
    try { await endpoints.patchDeployment(dep.id, body); toast("Saved", "ok"); onChanged(); }
    catch (e) { onFail?.(); setErr(e instanceof ApiError ? e.message : String(e)); }
    finally { setBusy(null); }
  }
  async function saveName() { await save("name", { display_name: nameDraft.trim() }); }
  async function saveReplicas(n: number) {
    if (n < 1) return;
    const prev = replicas;
    setReplicas(n);
    // Only a scale-UP carries the target: the endpoint 422s a `worker_id` that
    // places nothing, rather than accepting it as a quiet no-op (#1542).
    const body: Record<string, unknown> = { replicas: n };
    if (target && n > prev) body.worker_id = target;
    await save("replicas", body, () => setReplicas(prev));
  }
  async function saveParams() {
    let params: Record<string, unknown>;
    try { params = paramsText.trim() ? JSON.parse(paramsText) : {}; } catch { setErr("params must be valid JSON"); return; }
    await save("params", { params });
  }
  async function saveTags() { await save("tags", { tags: tagsText.split(",").map((s) => s.trim()).filter(Boolean) }); }

  return (
    <div className="affordance">
      <div className="hint" style={{ margin: "4px 0 10px" }}>
        Edit desired state — replicas, backend params, tags, display name. Params &amp; task apply on the next (re)deploy.
      </div>

      <div className="field" style={{ marginBottom: 10 }}>
        <label>Display name <span className="hint">console label; clients still call <span className="mono">{dep.model_name}</span></span></label>
        <div className="inline-edit">
          <input value={nameDraft} onChange={(e) => setNameDraft(e.target.value)}
            placeholder={dep.model_name} maxLength={128} style={{ width: 240 }} disabled={busy !== null} />
          <button className="btn sm primary" onClick={saveName}
            disabled={busy !== null || nameDraft === (dep.display_name ?? "")}>
            {busy === "name" ? "Saving…" : "Rename"}</button>
        </div>
      </div>

      <div className="field" style={{ marginBottom: 10 }}>
        <label>Replicas <span className="hint">desired instance count</span></label>
        <div className="stepper">
          <button onClick={() => saveReplicas(replicas - 1)} disabled={busy !== null || replicas <= 1} aria-label="decrease">−</button>
          <input className="n" value={replicas} readOnly aria-label="replica count" />
          <button onClick={() => saveReplicas(replicas + 1)} disabled={busy !== null} aria-label="increase">+</button>
          <select value={target} aria-label="target worker for new replicas"
            onChange={(e) => setTarget(e.target.value)} disabled={busy !== null}
            style={{ marginLeft: 8, maxWidth: 220 }}>
            <option value="">auto-place</option>
            {(workers.data ?? []).map((w) => (
              <option key={w.id} value={w.id}>
                {w.display_name || w.name}{w.vram_total_gb ? ` — ${w.vram_total_gb} GB` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          {target
            ? "The next replica goes on that worker — or the call fails saying why. It is never placed somewhere else instead."
            : "Auto-placement prefers a worker that does not already host this deployment; among equals it picks by name."}
        </div>
      </div>

      <div className="field" style={{ marginBottom: 10 }}>
        <label>Backend params <span className="hint">JSON — sent to the engine</span></label>
        <textarea rows={4} value={paramsText} onChange={(e) => setParamsText(e.target.value)} className="mono" style={{ fontSize: "0.72rem" }} />
        <button className="btn sm" style={{ marginTop: 6 }} onClick={saveParams} disabled={busy !== null}>{busy === "params" ? "Saving…" : "Save params"}</button>
      </div>

      <div className="field" style={{ marginBottom: 4 }}>
        <label>Tags <span className="hint">comma-separated</span></label>
        <div className="inline-edit">
          <input value={tagsText} onChange={(e) => setTagsText(e.target.value)} placeholder="e.g. chat, default, rag" style={{ width: 280 }} />
          <button className="btn sm" onClick={saveTags} disabled={busy !== null}>{busy === "tags" ? "Saving…" : "Save"}</button>
        </div>
      </div>
      {err && <div className="callout err" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  );
}

function ModelDrawer({ dep, onClose, onChanged, autoLogs }: { dep: DeploymentRow; onClose: () => void; onChanged: () => void; autoLogs?: boolean }) {
  const nav = useNavigate();
  const [busy, setBusy] = useState<string | null>(null);
  // #1603: the log pane is `components/LogsPane`, the same one Fleet uses. It
  // used to be a SECOND implementation right here, and the two had drifted:
  // this one painted the string "loading…" and asked for a plain command,
  // while LogsPane asks with `revalidate` and paints the node's last chunk at
  // once (#1183, stale-while-revalidate). Opening the same log twice showed
  // "loading…" both times although the answer was already in the manager.
  // LogsPane's own header claimed it was "used by both the Models drawer and
  // the Fleet worker detail so they never drift apart" — this makes that true.
  const [logs, setLogs] = useState<{ container: string; worker_id: string | null } | null>(null);

  // #313: opened via a row's "logs" button → auto-open the first instance's logs.
  useEffect(() => {
    if (!autoLogs) return;
    const i = dep.instances.find((x) => x.container && x.worker_id);
    if (i && i.container) act("tail_logs", i.container, i.worker_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function act(kind: string, container: string, worker_id: string | null) {
    if (!worker_id) { toast("no worker for this instance", "err"); return; }
    if ((kind === "restart_engine" || kind === "unload_engine") && !confirm(`${kind.replace("_engine", "")} ${container}?`)) return;
    setBusy(`${kind}:${container}`);
    try {
      if (kind === "tail_logs") {
        // No fetch here: LogsPane owns the round trip, including the immediate
        // paint of the last known chunk. Setting state and ALSO fetching was
        // how the two implementations came to exist.
        setLogs({ container, worker_id });
      } else {
        const c = await runCommand(worker_id, kind, { container, instance_id: container, tail: 300 });
        // #348: timed_out = unconfirmed (node still working), not failed.
        toast(c.timed_out ? `${kind} not confirmed yet — node still working` : `${kind} ${c.status}`,
          c.status === "done" ? "ok" : c.timed_out ? "warn" : "err"); onChanged();
      }
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }
  async function undeploy() {
    if (!confirm(`Undeploy ${dep.model_name}? This stops all its engines.`)) return;
    setBusy("undeploy");
    try { await endpoints.undeploy(dep.id); toast(`Undeploying ${dep.model_name}`, "ok"); onChanged(); onClose(); }
    catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }
  // #312 pause/resume — stop the engines (free VRAM) but keep the deployment.
  async function pauseResume() {
    const stopping = dep.status !== "stopped";
    if (stopping && !confirm(`Stop ${dep.model_name}? The engines unload (frees VRAM); the deployment — params, tags, replicas — is kept so you can resume it later.`)) return;
    setBusy("pause");
    try {
      if (stopping) { await endpoints.stopDeployment(dep.id); toast(`Stopping ${dep.model_name}`, "ok"); }
      else { await endpoints.startDeployment(dep.id); toast(`Resuming ${dep.model_name}`, "ok"); }
      onChanged();
    } catch (e) { toast(String(e), "err"); } finally { setBusy(null); }
  }

  return (
    <Drawer title={<><StatusDot status={dep.ready_instances > 0 ? "ready" : "pending"} />{dep.display_name || dep.model_name}</>} onClose={onClose}
      storageKey="rzfz.drawer.model" defaultWidth={640}>
      <dl className="kv-list">
        <dt>Engine</dt><dd>{dep.engine}</dd>
        <dt>Runner</dt><dd className="mono" style={{ fontSize: "0.72rem" }}
          title="Pinned at deploy time. Changing it goes through the worker's 'Upgrade & relaunch' (Fleet drawer) — there is deliberately no per-deployment re-pin.">
          {dep.runner_image ?? <span className="muted">node default</span>}</dd>
        <dt>Status</dt><dd><StatusBadge status={dep.health} /></dd>
        <dt>Ready</dt><dd><Badge kind={dep.ready_instances > 0 ? "ok" : "warn"}>{dep.ready_instances}/{dep.replicas}</Badge></dd>
      </dl>

      <div className="btn-row" style={{ margin: "0 0 12px" }}>
        <button className="btn sm primary" onClick={() => nav(`/models/${dep.id}/edit`)}>Edit parameters →</button>
        <button className="btn sm" disabled={dep.ready_instances < 1}
          onClick={() => nav(`/playground?model=${encodeURIComponent(dep.model_name)}&tab=${dep.task === "embed" ? "embeddings" : dep.task === "rerank" ? "rerank" : "chat"}`)}
          title={dep.ready_instances < 1 ? "no ready instance yet" : `open the ${dep.task} playground`}>▷ Playground</button>
      </div>

      <ModelEditAffordances dep={dep} onChanged={onChanged} />

      {Object.keys(dep.params ?? {}).length > 0 && (
        <><div className="muted" style={{ fontSize: "0.75rem", marginBottom: 4 }}>deployment params</div><div style={{ marginBottom: 14 }}><Params params={dep.params} /></div></>
      )}

      <h2 className="section" style={{ marginTop: 4 }}>Instances ({dep.instances.length})</h2>
      {dep.instances.length === 0 ? <p className="muted">No running instances.</p> : (
        <div className="table-wrap">
          <table className="rz">
            <thead><tr><th>Worker</th><th>Status</th><th>Started</th><th></th></tr></thead>
            <tbody>
              {dep.instances.map((i) => (
                <tr key={i.id}>
                  <td><StatusDot status={i.status} />{i.worker ?? "—"}</td>
                  <td><StatusBadge status={i.status} />{i.detail ? <div className="hint mono" style={{ marginTop: 2 }}>{i.detail}</div> : null}</td>
                  <td className="muted">{relTime(i.started_at)}</td>
                  <td>{i.external ? (
                    <span className="hint">external endpoint — lifecycle &amp; logs live on <span className="mono">{i.worker}</span>, not orchestrated here</span>
                  ) : (
                    <div className="btn-row">
                      <button className="btn sm" disabled={!i.container || !!busy} onClick={() => i.container && act("tail_logs", i.container, i.worker_id)}>Logs</button>
                      <button className="btn sm" disabled={!i.container || !!busy} onClick={() => i.container && act("restart_engine", i.container, i.worker_id)}>Restart</button>
                      <button className="btn sm danger" disabled={!i.container || !!busy} onClick={() => i.container && act("unload_engine", i.container, i.worker_id)}>Unload</button>
                    </div>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {logs && (
        <LogsPane container={logs.container} workerId={logs.worker_id}
                  onClose={() => setLogs(null)} />
      )}
      <div className="btn-row" style={{ marginTop: 18 }}>
        <button className="btn" disabled={!!busy} onClick={pauseResume}>
          {dep.status === "stopped" ? "▶ Resume" : "⏸ Stop"}</button>
        <button className="btn danger" disabled={!!busy} onClick={undeploy}>Undeploy model</button>
      </div>
    </Drawer>
  );
}

// #313: the common controls inline in each row (stopPropagation so they don't
// open the drawer). Undeploy / edit / per-instance stay in the drawer.
function RowActions({ dep, onChanged, onLogs }: { dep: DeploymentRow; onChanged: () => void; onLogs: () => void }) {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const stopped = dep.status === "stopped";
  // #318 external backends have no container/worker-agent — restart & logs can't work.
  const external = dep.instances.length > 0 && dep.instances.every((i) => i.external);
  const containers = dep.instances.filter((i) => i.container && i.worker_id);
  const pgTab = dep.task === "embed" ? "embeddings" : dep.task === "rerank" ? "rerank" : "chat";
  async function toggle(e: React.MouseEvent) {
    e.stopPropagation(); setBusy(true);
    try {
      if (stopped) { await endpoints.startDeployment(dep.id); toast(`Resuming ${dep.model_name}`, "ok"); }
      else { await endpoints.stopDeployment(dep.id); toast(`Stopping ${dep.model_name}`, "ok"); }
      onChanged();
    } catch (err) { toast(String(err), "err"); } finally { setBusy(false); }
  }
  async function restart(e: React.MouseEvent) {
    e.stopPropagation();
    if (!confirm(`Restart ${dep.model_name}? (${containers.length} engine${containers.length === 1 ? "" : "s"})`)) return;
    setBusy(true);
    try {
      for (const i of containers) await runCommand(i.worker_id!, "restart_engine", { container: i.container, instance_id: i.container, tail: 300 });
      toast(`Restarted ${dep.model_name}`, "ok"); onChanged();
    } catch (err) { toast(String(err), "err"); } finally { setBusy(false); }
  }
  // #319: undeploy straight from the overview row (no need to open the drawer) —
  // stops every engine + removes the desired-state deployment.
  async function undeploy(e: React.MouseEvent) {
    e.stopPropagation();
    if (!confirm(`Undeploy ${dep.model_name}? This stops all its engines and removes the deployment.`)) return;
    setBusy(true);
    try { await endpoints.undeploy(dep.id); toast(`Undeploying ${dep.model_name}`, "ok"); onChanged(); }
    catch (err) { toast(String(err), "err"); } finally { setBusy(false); }
  }
  return (
    <div className="row-actions" onClick={(e) => e.stopPropagation()}>
      <button className="btn xs" disabled={busy} title={stopped ? "Resume" : "Stop"} onClick={toggle}>{stopped ? "▶" : "⏸"}</button>
      <button className="btn xs" disabled={busy || stopped || external || containers.length === 0} title={external ? "external backend — no container" : "Restart engines"} onClick={restart}>↻</button>
      <button className="btn xs" disabled={external || containers.length === 0} title={external ? "external backend — logs live on the remote host" : "Show logs"} onClick={(e) => { e.stopPropagation(); onLogs(); }}>▤</button>
      <button className="btn xs" disabled={dep.ready_instances < 1} title={dep.ready_instances < 1 ? "no ready instance" : `open the ${dep.task} playground`}
        onClick={(e) => { e.stopPropagation(); nav(`/playground?model=${encodeURIComponent(dep.model_name)}&tab=${pgTab}`); }}>▷</button>
      <button className="btn xs danger" style={{ color: "var(--razz-red)" }} disabled={busy} title={`Undeploy ${dep.model_name}`} onClick={undeploy} aria-label={`Undeploy ${dep.model_name}`}>
        {/* inline SVG (not the 🗑 emoji — emoji ignore `color`, so it rendered grey); stroke=currentColor → brand red */}
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M3 6h18" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /><path d="M10 11v6M14 11v6" />
        </svg>
      </button>
    </div>
  );
}

// #315: with several workers the flat list gets long. Group it by worker /
// runtime (Vulkan·CUDA·Ollama) / task / tag so it stays scannable. A deployment
// can carry multiple tags → it appears under each of its tag groups.
type GroupBy = "none" | "worker" | "runtime" | "task" | "tag";
const GROUP_OPTS: { key: GroupBy; label: string }[] = [
  { key: "none", label: "None" },
  { key: "worker", label: "Worker" },
  { key: "runtime", label: "Runtime" },
  { key: "task", label: "Task" },
  { key: "tag", label: "Tag" },
];

function groupsFor(deps: DeploymentRow[], by: GroupBy): { label: string; items: DeploymentRow[] }[] {
  if (by === "none") return [{ label: "", items: deps }];
  const map = new Map<string, DeploymentRow[]>();
  const push = (k: string, d: DeploymentRow) => {
    const cur = map.get(k); if (cur) cur.push(d); else map.set(k, [d]);
  };
  for (const d of deps) {
    if (by === "worker") push(d.instances[0]?.worker ?? "unplaced", d);
    else if (by === "runtime") push(d.arch || d.engine || "—", d);
    else if (by === "task") push(d.task || "chat", d);
    else { const ts = d.tags?.length ? d.tags : ["untagged"]; for (const t of ts) push(t, d); }
  }
  return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([label, items]) => ({ label, items }));
}

// device + runtime family, e.g. "GPU · Vulkan" / "GPU · Ollama" / "CPU · CPU".
function RuntimeCell({ dep }: { dep: DeploymentRow }) {
  const dev = dep.device || "—";
  const arch = dep.arch || dep.engine || "—";
  return (
    <span className="runtime">
      <span className={"rt-dev " + (dev === "GPU" ? "gpu" : dev === "CPU" ? "cpu" : "na")}>{dev}</span>
      <span className="rt-arch">{arch}</span>
    </span>
  );
}

export function Models() {
  const qc = useQueryClient();
  const nav = useNavigate();
  // #286/#288: poll so loading→ready/failed transitions show without a manual refresh.
  const q = useQuery({ queryKey: ["deployments"], queryFn: endpoints.deployments, refetchInterval: 5000 });
  const [openId, setOpenId] = useState<string | null>(null);
  const [autoLogs, setAutoLogs] = useState(false);
  const [groupBy, setGroupBy] = useState<GroupBy>(() => (localStorage.getItem("rzfz.deployments.groupby") as GroupBy) || "none");
  const open = q.data?.find((d) => d.id === openId) || null;
  // #319 deep-link: /models?model=<name> (e.g. from a worker's model-instance
  // row) opens that deployment's detail once its row loads.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    const want = params.get("model");
    if (!want || !q.data) return;
    const hit = q.data.find((d) => d.model_name === want);
    if (hit) {
      setOpenId(hit.id);
      params.delete("model");
      setParams(params, { replace: true });
    }
  }, [q.data, params, setParams]);
  const refresh = () => qc.invalidateQueries({ queryKey: ["deployments"] });
  const closeDrawer = () => { setOpenId(null); setAutoLogs(false); };
  const setGroup = (g: GroupBy) => { setGroupBy(g); localStorage.setItem("rzfz.deployments.groupby", g); };

  const renderRow = (d: DeploymentRow) => (
    <tr key={d.id} {...rowProps(() => setOpenId(d.id), d.id === openId)}>
      <td><StatusDot status={d.health} /><strong>{d.display_name || d.model_name}</strong>
        {d.tags && d.tags.length > 0 && (
          <div className="row-tags" style={{ marginTop: 4 }}>
            {d.tags.map((t) => <span key={t} className="tagchip ro">{t}</span>)}
          </div>)}</td>
      <td><RuntimeCell dep={d} /></td>
      <td>{d.engine}</td>
      <td>{(() => {
        if (d.status === "stopped") return <span className="badge muted">stopped</span>;
        const dt = d.instances.find((i) => i.detail)?.detail || "";
        const pct = dt.match(/(\d+)%/);
        // #296: while pulling, the % lives IN the chip ("pulling 23%").
        if (pct && (d.health === "pulling" || d.health === "loading")) return <span className="badge warn">pulling {pct[1]}%</span>;
        return <><StatusBadge status={d.health} />{dt && !pct ? <div className="hint mono" style={{ marginTop: 2 }}>{dt}</div> : null}</>;
      })()}</td>
      <td><Badge kind={d.health === "failed" ? "err" : d.ready_instances >= d.replicas ? "ok" : "warn"}>{d.ready_instances}/{d.replicas}</Badge></td>
      <td className="num">{d.instances.length}</td>
      <td><RowActions dep={d} onChanged={refresh} onLogs={() => { setOpenId(d.id); setAutoLogs(true); }} /></td>
    </tr>
  );

  const head = (
    <thead><tr><th>Model</th><th>Runtime</th><th>Engine</th><th>Status</th><th>Ready</th><th>Instances</th><th>Actions</th></tr></thead>
  );

  return (
    <section className="page">
      <h1>Deployments</h1>
      <p className="lede">Client-facing model names and their engine instances across workers. Click a row for detail + actions.</p>
      <div className="spread">
        <div className="groupby">
          <span className="gb-label">Group by</span>
          {GROUP_OPTS.map((o) => (
            <button key={o.key} className={"gb-btn" + (groupBy === o.key ? " on" : "")} onClick={() => setGroup(o.key)}>{o.label}</button>
          ))}
        </div>
        <div className="btn-row">
          <span className="muted">{q.data ? `${q.data.length} deployment${q.data.length === 1 ? "" : "s"}` : ""}</span>
          <button className="btn ghost sm" onClick={refresh}>Refresh</button>
          <button className="btn primary" onClick={() => nav("/deploy")}>+ Deploy model</button>
        </div>
      </div>
      <QueryState q={q} isEmpty={(d) => d.length === 0}
        loading={<TableSkeleton rows={4} cols={7} />}
        empty={<div className="empty">No deployments yet. Use “+ Deploy model” to schedule one onto a worker.</div>}>
        {(deps) => (
          <div className="stack-groups">
            {groupsFor(deps, groupBy).map((g) => (
              <div key={g.label || "all"} className="dgroup">
                {groupBy !== "none" && (
                  <div className="group-head"><span>{g.label}</span><span className="group-count">{g.items.length}</span></div>
                )}
                <div className="table-wrap">
                  <table className="rz">{head}<tbody>{g.items.map(renderRow)}</tbody></table>
                </div>
              </div>
            ))}
          </div>
        )}
      </QueryState>
      {open && <ModelDrawer dep={open} onClose={closeDrawer} onChanged={refresh} autoLogs={autoLogs} />}
    </section>
  );
}
