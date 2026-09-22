import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, endpoints, type DeploymentRow, type HfQuant } from "../api/client";
import { deployWithFitConfirm } from "../lib/deployWithFitConfirm";
import { PARAM_SCHEMA, engineForWorker, resolveParams, paramsToSave, type FieldSource, type ParamField } from "../lib/paramSchema";
import { Badge, Markdown, toast } from "./ui";

// #296 — unified full-screen model editor. Deploy mode fetches the repo itself
// so the quant table, memory impact, params AND the README all live on ONE page:
// picking a quant updates the impact live (no back-and-forth). Shared by Edit.

type Arch = { layers: number; kv_heads: number; head_dim: number } | null;
type DeployProps = {
  mode: "deploy";
  repoId: string;
  task: string;
  recommended?: Record<string, unknown>;   // catalog curated params (else repo.recommended_params)
  recommendedLabel?: FieldSource;          // "house" for catalog, "card" default (HF card)
  preferFile?: string;                     // catalog filename → preselect that quant
  onCancel: () => void; onDone: () => void;
};
type EditProps = { mode: "edit"; deployment: DeploymentRow; onCancel: () => void; onDone: () => void };
type Props = DeployProps | EditProps;

const SRC_BADGE: Record<FieldSource, { txt: string; cls: string } | null> = {
  card: { txt: "card", cls: "src-card" },
  house: { txt: "house", cls: "src-house" },
  set: { txt: "set", cls: "src-set" },
  default: null,
};

function kvBytesPerElem(kvType: string): number {
  const t = (kvType || "").toLowerCase();
  if (t.includes("q8") || t.includes("fp8")) return 1.06;
  if (t.includes("q4")) return 0.56;
  return 2; // f16 / auto
}
const gb = (n: number) => (Math.round(n * 10) / 10).toFixed(1);

// Resident-footprint estimate (weights + KV-cache + compute, GB). Single source
// for the Memory-impact panel AND the est_gb sent for #227 admission control.
export function footprint(engine: string, sizeGb: number, arch: Arch, values: Record<string, unknown>) {
  const isLlama = engine === "llamacpp";
  const ctx = Number(isLlama ? values.ctx_size : values.max_model_len) || 8192;
  const parallel = isLlama ? (Number(values.n_parallel) || 1) : 1;
  const kvType = String((isLlama ? values.cache_type_k : values.kv_cache_dtype) ?? "f16");
  const weights = sizeGb || 0;
  // #1572: `ctx` is the TOTAL KV pool, not a per-slot value — llama.cpp DIVIDES
  // it across `--parallel` slots (core/llm/standard-models.yaml:49 says so in
  // as many words, and #1538 measured n_ctx_slot=262144 at --ctx-size=1048576
  // --parallel=4). Multiplying by `parallel` therefore counted each slot's
  // share as if it were the whole pool: 4x too much at the fleet default, on a
  // number that goes to the #227 admission gate as est_gb. A deploy that fits
  // was refused, and the hint below explained the refusal with the same error.
  const kv = isLlama && arch ? (2 * arch.layers * arch.kv_heads * arch.head_dim * ctx * kvBytesPerElem(kvType)) / 1e9 : 0;
  const compute = weights ? Math.max(1.2, weights * 0.06) : 1.2;
  return { isLlama, ctx, parallel, kvType, weights, kv, compute, total: weights + kv + compute };
}

type Budget = { id: string; name: string; mem_gb: number; source: string | null };

function ImpactPanel({ engine, sizeGb, arch, values, workers, selected, onSelect }: {
  engine: string; sizeGb: number; arch: Arch;
  values: Record<string, unknown>; workers: Budget[];
  selected: string; onSelect: (id: string) => void;
}) {
  const { isLlama, ctx, parallel, kvType, weights, kv, compute, total } = footprint(engine, sizeGb, arch, values);
  const scale = Math.max(total, ...workers.map((w) => w.mem_gb), 1);
  const seg = (v: number, cls: string) => v > 0 ? <span className={cls} style={{ width: `${(v / scale) * 100}%` }} /> : null;

  return (
    <div className="impact-block">
      <h4 className="box-sub">Memory impact</h4>
      {!weights ? (
        <p className="sub">Pick a quant to see its footprint.</p>
      ) : (
        <>
          <div className="ibar">{seg(weights, "seg-w")}{seg(kv, "seg-k")}{seg(compute, "seg-o")}</div>
          <div className="legend">
            <span><i className="seg-w" />Weights <b className="mono">{gb(weights)}</b></span>
            {isLlama && <span><i className="seg-k" />KV-cache <b className="mono">{arch ? gb(kv) : "?"}</b></span>}
            <span><i className="seg-o" />Compute <b className="mono">{gb(compute)}</b></span>
            <span>Total <b className="mono">≈ {gb(total)} GB</b></span>
          </div>
          {isLlama && !arch && <div className="hint" style={{ marginTop: 6 }}>Model config unavailable — KV-cache not estimated (weights + compute only).</div>}
          {!isLlama && <div className="hint" style={{ marginTop: 6 }}>{engine} manages KV as a paged pool bounded by gpu_memory_utilization — not added here.</div>}
          {isLlama && arch && <p className="hint" style={{ marginTop: 8, marginBottom: 2 }}>KV = 2·{arch.layers}L·{arch.kv_heads}kv·{arch.head_dim}d·{ctx}ctx·{kvBytesPerElem(kvType)}B — the pool is shared by {parallel} slot(s) ({Math.floor(ctx / parallel)} ctx each); changes live with Context.</p>}
        </>
      )}
      {/* worker chooser — click a row to place there (replaces the combobox) */}
      <h4 className="box-sub" style={{ marginTop: 14 }}>Place on worker</h4>
      <button type="button" className={`wfit clickable${selected === "__cache__" ? " on" : ""}`} onClick={() => onSelect("__cache__")}>
        <span><span className={`rz-radio${selected === "__cache__" ? " on" : ""}`} aria-hidden />📦 Model cache <span className="muted">— store only, no worker</span></span>
        <span className="muted mono">{weights ? `${gb(weights)} GB` : ""}</span>
      </button>
      <button type="button" className={`wfit clickable${selected === "" ? " on" : ""}`} onClick={() => onSelect("")}>
        <span><span className={`rz-radio${selected === "" ? " on" : ""}`} aria-hidden />Auto <span className="muted">— first ready worker</span></span>
        <span className="muted">picks the engine</span>
      </button>
      {workers.length === 0 ? <div className="hint" style={{ marginTop: 6 }}>No worker reports a memory budget yet.</div> :
        workers.map((w) => {
          const ok = !weights || total <= w.mem_gb * 0.92, tight = !ok && total <= w.mem_gb;
          return (
            <button type="button" className={`wfit clickable${selected === w.id ? " on" : ""}`} key={w.id} onClick={() => onSelect(w.id)}>
              <span><span className={`rz-radio${selected === w.id ? " on" : ""}`} aria-hidden /><span className={`dot ${weights ? (ok ? "ok" : tight ? "warn" : "crit") : "muted"}`} /><b>{w.name}</b> <span className="muted">{w.source === "vram" ? "VRAM" : "RAM"} {w.mem_gb} GB</span></span>
              {weights ? <span className="mono">≈ {gb(total)} / {w.mem_gb} GB</span> : <span className="muted mono">{w.mem_gb} GB</span>}
            </button>
          );
        })}
    </div>
  );
}

// serve-mode guessed from the repo name (embedding / reranker families) — the
// operator can still override. GGUF cards rarely carry a pipeline tag.
function inferTask(repoId: string): "chat" | "embed" | "rerank" | null {
  const s = repoId.toLowerCase();
  if (/(rerank|reranker|cross-encoder)/.test(s)) return "rerank";
  if (/(embed|embedding|\bbge\b|\bgte\b|\be5\b|nomic|minilm|mxbai)/.test(s)) return "embed";
  return null;
}

function slug(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9._-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "");
}

// #296 tags: pick from the central catalog (chips) or type a new one; new tags
// are stored centrally when the deployment is saved (derived from all deployments).
function TagField({ value, onChange }: { value: string[]; onChange: (t: string[]) => void }) {
  const all = useQuery({ queryKey: ["tags"], queryFn: endpoints.tags });
  const [draft, setDraft] = useState("");
  const has = (t: string) => value.some((v) => v.toLowerCase() === t.toLowerCase());
  const add = (t: string) => { const s = t.trim(); if (s && !has(s)) onChange([...value, s]); setDraft(""); };
  const suggestions = (all.data ?? []).filter((t) => !has(t));
  return (
    <div className="tagfield">
      <div className="tagchips">
        {value.length === 0 && <span className="hint" style={{ margin: 0 }}>no tags yet</span>}
        {value.map((t) => (
          <span key={t} className="tagchip on">{t}
            <button type="button" aria-label={`remove ${t}`} onClick={() => onChange(value.filter((x) => x !== t))}>×</button>
          </span>
        ))}
      </div>
      <div className="tagadd">
        <input value={draft} placeholder="add a tag…" onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(draft); } }} />
        <button type="button" className="btn sm ghost" disabled={!draft.trim()} onClick={() => add(draft)}>+ add</button>
      </div>
      {suggestions.length > 0 && (
        <div className="tagsuggest">
          {suggestions.map((t) => <button type="button" key={t} className="tagchip" onClick={() => add(t)}>+ {t}</button>)}
        </div>
      )}
    </div>
  );
}

export function ModelEditor(props: Props) {
  const isDeploy = props.mode === "deploy";
  // #304: edit mode's "move to worker" control needs the same worker/budget
  // list ImpactPanel uses for deploy — fetch it in BOTH modes now.
  const workers = useQuery({ queryKey: ["workers"], queryFn: endpoints.workers });
  // deploy mode fetches the repo → quants + README + arch on ONE page
  const repoQ = useQuery({
    queryKey: ["hf-repo", isDeploy ? props.repoId : ""],
    queryFn: () => endpoints.hfRepo((props as DeployProps).repoId),
    enabled: isDeploy,
  });
  const quants: HfQuant[] = (isDeploy && repoQ.data?.quants) || [];
  const arch: Arch = (isDeploy && repoQ.data?.arch) || null;
  // #307: which quants are already in the master cache (match by filename) → can
  // deploy with no external download (matters offline). Refreshes as models are pulled.
  const cacheQ = useQuery({ queryKey: ["registry-models"], queryFn: endpoints.registryModels, enabled: isDeploy });
  const cachedNames = useMemo(
    () => new Set((cacheQ.data?.models ?? []).flatMap((m) => m.files.map((f) => f.name))),
    [cacheQ.data]);
  const qCached = (q: HfQuant) => q.files.length > 0 && q.files.every((f) => cachedNames.has(f.split("/").pop() || f));

  // selected quant (default: preferFile match → smallest sized → first)
  const [selIdx, setSelIdx] = useState(0);
  const [selInit, setSelInit] = useState(false);
  useEffect(() => {
    if (selInit || quants.length === 0) return;
    const pf = (props as DeployProps).preferFile;
    let idx = pf ? quants.findIndex((q) => q.files.some((f) => f.endsWith(pf) || f === pf)) : -1;
    if (idx < 0) { const sized = quants.filter((q) => q.size_gb > 0); idx = sized.length ? quants.indexOf(sized[0]) : 0; }
    setSelIdx(Math.max(0, idx)); setSelInit(true);
  }, [quants, selInit, props]);
  const selQuant = quants[selIdx];

  const repoBase = isDeploy ? ((props.repoId.split("/").pop() || props.repoId).replace(/-?gguf$/i, "")) : "";
  const [name, setName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  useEffect(() => {  // auto-name from repo + quant until the operator edits it
    if (props.mode === "edit") { setName(props.deployment.model_name); return; }
    if (!nameEdited) setName(slug(selQuant ? `${repoBase}-${selQuant.label}` : repoBase));
  }, [selQuant, repoBase, nameEdited, props]);

  // serve mode: catalog entries carry a curated task; HF-browser deploys guess
  // from the repo name (embedding / reranker families). Operator can override.
  const inferredTask = isDeploy ? inferTask((props as DeployProps).repoId) : null;
  const catalogTask = isDeploy && (props as DeployProps).recommendedLabel === "house";
  const [task, setTask] = useState(
    isDeploy ? (catalogTask ? (props as DeployProps).task : (inferredTask ?? (props as DeployProps).task)) : props.deployment.task,
  );
  const [taskEdited, setTaskEdited] = useState(false);
  const taskGuessed = isDeploy && !catalogTask && !taskEdited && !!inferredTask && task === inferredTask;
  const [workerId, setWorkerId] = useState("");
  // #549 R1 — optional runner-image pin: this deployment launches on exactly
  // this runner build instead of the node's default. Empty = default.
  const [runnerImage, setRunnerImage] = useState("");
  // #284: deploy-time replica count — instances to place across workers in
  // one action. Edit mode already shows/changes replicas via PATCH's own
  // stepper (Models.tsx); this is deploy-mode only.
  const [replicas, setReplicas] = useState(1);
  const [tags, setTags] = useState<string[]>(props.mode === "edit" ? (props.deployment.tags ?? []) : []);

  const engine = useMemo(() => {
    if (props.mode === "edit") return props.deployment.engine || "llamacpp";
    const w = (workers.data ?? []).find((x) => x.id === workerId);
    return engineForWorker(w?.hardware, w?.engine);
  }, [props, workers.data, workerId]);

  // #1604: the params the deployment ALREADY has, needed twice — to resolve
  // what the fields show, and to carry through what the schema cannot show
  // when the editor saves. One definition, so the two can never disagree.
  const existing = useMemo(
    () => (props.mode === "edit" ? (props.deployment.params ?? {}) : {}) as Record<string, unknown>,
    [props]);

  const initial = useMemo(() => {
    const rec = props.mode === "deploy" ? (props.recommended ?? repoQ.data?.recommended_params ?? {}) : {};
    const recLabel: FieldSource = props.mode === "deploy" ? (props.recommendedLabel ?? "card") : "card";
    return resolveParams(engine, existing, rec as Record<string, unknown>, recLabel);
  }, [engine, existing, props, repoQ.data]);

  const [values, setValues] = useState<Record<string, unknown>>(initial.values);
  const [seen, setSeen] = useState(engine + "|" + Object.keys(initial.values).length + "|" + JSON.stringify((props as DeployProps).recommended ?? repoQ.data?.recommended_params ?? {}));
  const sig = engine + "|" + Object.keys(initial.values).length + "|" + JSON.stringify((props as DeployProps).recommended ?? repoQ.data?.recommended_params ?? {});
  if (sig !== seen) { setSeen(sig); setValues(initial.values); }  // re-resolve on engine switch / recommended arrival
  const [raw, setRaw] = useState(false);
  const [rawText, setRawText] = useState("");
  const [showReadme, setShowReadme] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // height-link the side panels to the middle column (3-col layout only):
  // README caps to the Parameters height; the Weights rail caps to Parameters +
  // Placement box height — both scroll internally. Measured live (group toggles,
  // worker rows). Disabled (natural height) below the stacked breakpoint.
  const paramsRef = useRef<HTMLDivElement>(null);
  const placeRef = useRef<HTMLDivElement>(null);
  const [caps, setCaps] = useState<{ p: number; pl: number }>({ p: 0, pl: 0 });
  useLayoutEffect(() => {
    if (!isDeploy) return;
    const wide = window.matchMedia("(min-width: 1081px)");
    const measure = () => {
      if (!wide.matches) { setCaps({ p: 0, pl: 0 }); return; }
      setCaps({ p: paramsRef.current?.offsetHeight ?? 0, pl: placeRef.current?.offsetHeight ?? 0 });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (paramsRef.current) ro.observe(paramsRef.current);
    if (placeRef.current) ro.observe(placeRef.current);
    wide.addEventListener("change", measure);
    return () => { ro.disconnect(); wide.removeEventListener("change", measure); };
  }, [isDeploy]);

  const groups = PARAM_SCHEMA[engine] || PARAM_SCHEMA.llamacpp;
  function set(k: string, v: unknown) { setValues((cur) => ({ ...cur, [k]: v })); }
  function coerce(f: ParamField, raw0: string): unknown {
    if (f.type === "number") { const n = Number(raw0); return Number.isNaN(n) ? raw0 : n; }
    return raw0;
  }
  function openRaw() { setRawText(JSON.stringify(values, null, 2)); setRaw(true); }
  function applyRaw() {
    try { setValues(JSON.parse(rawText)); setRaw(false); setErr(null); }
    catch { setErr("Advanced JSON is not valid."); }
  }

  const budgets: Budget[] = (workers.data ?? []).filter((w) => w.status === "ready")
    .map((w) => ({ id: w.id, name: w.name, mem_gb: (w.vram_total_gb ?? w.mem_total_gb ?? 0), source: w.vram_total_gb ? "vram" : "ram" }))
    .filter((w) => w.mem_gb > 0);

  const [applyNow, setApplyNow] = useState(false);

  // #304: reassign — move the running instance to a different worker. Only
  // meaningful in edit mode, and only when there is exactly ONE live (non-
  // failed) instance: 0 means nothing to move, >1 is ambiguous for this
  // single-target picker (the server itself 422s an ambiguous reassign
  // without an instance_id, which this control never sends).
  const liveInstances = props.mode === "edit"
    ? props.deployment.instances.filter((i) => i.status !== "failed") : [];
  const currentWorkerId = liveInstances.length === 1 ? liveInstances[0].worker_id : null;
  const [reassignTarget, setReassignTarget] = useState("");
  const [reassignBusy, setReassignBusy] = useState(false);
  const [reassignErr, setReassignErr] = useState<string | null>(null);
  async function reassign() {
    if (props.mode !== "edit" || !reassignTarget) return;
    setReassignBusy(true); setReassignErr(null);
    try {
      await endpoints.reassignDeployment(props.deployment.id, reassignTarget);
      toast("Reassign scheduled — moving to the new worker", "ok");
      props.onDone();
    } catch (e) {
      setReassignErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setReassignBusy(false);
    }
  }

  async function submit() {
    if (props.mode === "deploy" && !selQuant) { setErr("Pick a quant to deploy."); return; }
    setBusy(true); setErr(null);
    try {
      if (props.mode === "deploy" && workerId === "__cache__") {
        // #307: cache-only target — mirror HF → Zot, no worker deploy.
        const r = await endpoints.mirrorModel({
          hf_repo: props.repoId, files: selQuant.files, name: name.trim(), tag: selQuant.label,
        });
        toast(`Caching ${r.repo}:${r.tag} on ${r.worker} — runs in the background`, "ok");
      } else if (props.mode === "deploy") {
        // #227 send the footprint estimate so the manager can gate over-subscription.
        const est_gb = Math.round(footprint(engine, selQuant?.size_gb ?? 0, arch, values).total * 10) / 10;
        // #301: this is the path that carries est_gb, so it is the one the
        // gate refuses most often — offer the force decision with the gate's
        // numbers instead of only reporting the 409.
        // #1256: `selQuant.files` is a QUANT grouping — a repo-level vision
        // projector is not in it and cannot be. The manager completes the list
        // with the manifest's projector on POST /api/deployments, so this
        // editor deliberately sends the quant's files unchanged.
        const r = await deployWithFitConfirm({
          model_name: name.trim(), served_model: name.trim(), files: selQuant.files,
          hf_repo: props.repoId, task, worker_id: workerId || undefined, params: paramsToSave(values, existing), tags, est_gb,
          runner_image: runnerImage.trim() || undefined, replicas,
        });
        if (!r) { setBusy(false); return; }   // declined — leave the editor open
        toast(`Deploy scheduled: ${r.instance_id} on ${r.worker}`, "ok");
      } else {
        // #1604: PATCH replaces `params` wholesale, so send what the schema does
        // NOT cover as well — otherwise an unchanged save deletes it.
        await endpoints.patchDeployment(props.deployment.id, { params: paramsToSave(values, existing), task, tags });
        if (applyNow) {
          // #566 (C3): the redeploy IS this call — per instance: unload the old
          // engine, enqueue a fresh load from the saved desired state. A
          // failure HERE must not read as a failed save (the PATCH landed).
          try {
            const r = await endpoints.applyParams(props.deployment.id);
            toast(`Saved — relaunching ${r.relaunched} engine(s) with the new params`, "ok");
          } catch (e) {
            toast(`Saved, but the relaunch was not started: ${e instanceof ApiError ? e.message : String(e)} — params apply on the next (re)deploy`, "warn");
          }
        } else {
          toast("Saved — applies on the next (re)deploy of the engine", "ok");
        }
      }
      props.onDone();
    } catch (e) { setErr(e instanceof ApiError ? e.message : String(e)); } finally { setBusy(false); }
  }

  const readme = isDeploy ? repoQ.data?.readme_md : "";
  const SERVE_MODES: [string, string][] = [["chat", "Chat"], ["embed", "Embeddings"], ["rerank", "Rerank"]];
  const serveModes = (
    <div className="segmented">
      {SERVE_MODES.map(([v, l]) => (
        <button key={v} type="button" className={`seg${task === v ? " on" : ""}`} onClick={() => { setTask(v); setTaskEdited(true); }}>{l}</button>
      ))}
    </div>
  );
  const paramsInner = (
    <>
      <div className="spread"><h3 style={{ margin: 0 }}>Parameters</h3>
        <button className="btn sm ghost" onClick={raw ? () => setRaw(false) : openRaw}>{raw ? "⌃ structured" : "⌄ Advanced (raw JSON)"}</button></div>
      <p className="sub">Pre-filled from the model card (<span className="src-badge src-card">card</span>), house defaults (<span className="src-badge src-house">house</span>), else the engine default. Edit only what you need.</p>
      {raw ? (
        <>
          <textarea className="mono raw-json" rows={16} value={rawText} onChange={(e) => setRawText(e.target.value)} />
          <button className="btn sm" style={{ marginTop: 8 }} onClick={applyRaw}>Apply JSON</button>
        </>
      ) : (
        groups.map((g) => (
          <details key={g.title} className="pgroup" open>
            <summary>{g.title}</summary>
            <div className="pgrid">
              {g.fields.map((f) => {
                const badge = SRC_BADGE[initial.source[f.key]];
                return (
                  <div className="field" key={f.key}>
                    <label>{f.label}{badge && <span className={`src-badge ${badge.cls}`}>{badge.txt}</span>}{f.hint && <span className="hint">{f.hint}</span>}</label>
                    {f.type === "bool" ? (
                      <select value={String(values[f.key])} onChange={(e) => set(f.key, e.target.value === "true")}>
                        <option value="true">on</option><option value="false">off</option>
                      </select>
                    ) : f.type === "select" ? (
                      <select value={String(values[f.key])} onChange={(e) => set(f.key, e.target.value)}>
                        {f.options!.map((o) => <option key={String(o)} value={String(o)}>{o}</option>)}
                      </select>
                    ) : (
                      <input className="mono" type="number" step={f.step} min={f.min} max={f.max}
                        value={String(values[f.key] ?? "")} onChange={(e) => set(f.key, coerce(f, e.target.value))} />
                    )}
                  </div>
                );
              })}
            </div>
          </details>
        ))
      )}
    </>
  );

  return (
    <section className="page editor-page">
      <button className="btn ghost sm back-top" onClick={props.onCancel}>← back</button>
      <h1 className="editor-h1">{props.mode === "deploy" ? "Deploy a model" : `Edit ${props.deployment.model_name}`}</h1>

      {isDeploy ? (
        <div className="editor-grid deploy3">
          {/* col 1, rows 1-2 — Weights (quant selector); caps to Params + Placement height */}
          <div className="card panel gc-weights" style={caps.p && caps.pl ? { maxHeight: caps.p + caps.pl + 16 } : undefined}>
            <h3>Weights</h3>
            <p className="sub"><span className="mono" style={{ fontSize: 12 }}>{props.repoId}</span> — pick a quant</p>
            {repoQ.isLoading ? <div className="loading">Loading quants…</div>
              : repoQ.data?.note ? <div className="callout warn">{repoQ.data.note}</div>
              : quants.length === 0 ? <div className="empty">No GGUF quants in this repo.</div>
              : (
                <div className="table-wrap grow-scroll">
                  <table className="rz">
                    <thead><tr><th>Quant</th><th>Size</th><th>Fits</th></tr></thead>
                    <tbody>
                      {/* CUI-16: a shared `name` makes these ONE radiogroup —
                          arrow keys move between quants and the table is a
                          single tab stop, instead of a dozen. `onChange` (not a
                          readOnly radio + row click alone) is what the native
                          keyboard behaviour drives; the row click stays for
                          pointer users. */}
                      {quants.map((q, i) => (
                        <tr key={q.label + q.files[0]} aria-selected={i === selIdx}
                          onClick={() => setSelIdx(i)} style={{ cursor: "pointer" }}>
                          <td><input type="radio" name="quant-select" value={q.label}
                            checked={i === selIdx} onChange={() => setSelIdx(i)}
                            aria-label={`Quant ${q.label}`} style={{ width: "auto", marginRight: 8 }} />
                            <b>{q.label}</b>{q.parts > 1 ? <span className="muted"> · {q.parts} parts</span> : null}
                              {qCached(q) && <span className="badge ok" style={{ marginLeft: 6 }} title="already in the model cache — deploys with no external download">✓ cached</span>}</td>
                          <td className="num mono">{q.size_gb ? `${q.size_gb} GB` : "—"}</td>
                          <td>{q.fits.length === 0 ? <span className="muted">—</span> :
                            q.fits.map((ft) => <span key={ft.worker} title={`${ft.worker}: ${ft.mem_gb} GB`} style={{ marginRight: 4 }}>
                              <Badge kind={ft.ok ? "ok" : "err"}>{ft.ok ? "✓" : "✗"} {ft.worker}</Badge></span>)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
          </div>

          {/* col 2, row 1 — Parameters (drives the height of the row) */}
          <div className="card panel gc-params" ref={paramsRef}>{paramsInner}</div>

          {/* col 3, row 1 — Model card; caps to the Parameters height, scrolls */}
          <div className="card panel gc-readme" style={caps.p ? { maxHeight: caps.p } : undefined}>
            <div className="spread"><h3 style={{ margin: 0 }}>Model card</h3>
              {readme && <button className="btn sm ghost" onClick={() => setShowReadme((v) => !v)}>{showReadme ? "Hide" : "Show"}</button>}</div>
            {repoQ.isLoading ? <div className="loading">Loading…</div>
              : !readme ? <p className="sub">No model card.</p>
              : showReadme ? <div className="readme editor-readme grow-scroll"><Markdown md={readme} /></div> : null}
          </div>

          {/* row 2, cols 2-3 — Placement & deploy: fields left, memory + worker choice right */}
          <div className="card panel deploy-box gc-deploy" ref={placeRef}>
            <h3>Placement &amp; deploy</h3>
            <div className="deploy-cols">
              <div className="dc-left">
                <div className="field"><label>Model name <span className="hint">what /v1 exposes</span></label>
                  <input value={name} onChange={(e) => { setName(e.target.value); setNameEdited(true); }} /></div>
                <div className="field"><label>Serve mode {taskGuessed && <span className="src-badge src-card">from name</span>}</label>
                  {serveModes}</div>
                <div className="field"><label>Replicas <span className="hint">instances to deploy across workers</span></label>
                  <div className="stepper">
                    <button type="button" onClick={() => setReplicas((n) => Math.max(1, n - 1))} disabled={replicas <= 1} aria-label="decrease">−</button>
                    <input className="n" value={replicas} readOnly aria-label="replica count" />
                    <button type="button" onClick={() => setReplicas((n) => n + 1)} aria-label="increase">+</button>
                  </div></div>
                <div className="field"><label>Tags <span className="hint">group / find models</span></label>
                  <TagField value={tags} onChange={setTags} /></div>
                {workerId !== "__cache__" && (
                  <div className="field"><label>Runner image <span className="hint">optional pin — exact runner build (#549)</span></label>
                    <input className="mono" value={runnerImage} onChange={(e) => setRunnerImage(e.target.value)}
                      placeholder="node default" /></div>
                )}
                <div className="hint">Engine: <b className="mono">{engine}</b> — set by the chosen worker.</div>
              </div>
              <div className="dc-right">
                <ImpactPanel engine={engine} sizeGb={selQuant?.size_gb ?? 0} arch={arch} values={values} workers={budgets} selected={workerId} onSelect={setWorkerId} />
              </div>
            </div>
            {err && <div className="callout err" style={{ marginTop: 10 }}>{err}</div>}
            <div className="btn-row" style={{ marginTop: 14, justifyContent: "flex-end" }}>
              <button className="btn ghost" onClick={props.onCancel}>Cancel</button>
              <button className="btn primary big" disabled={busy || !name.trim() || !selQuant} onClick={submit}>{busy ? "…" : workerId === "__cache__" ? "Pull into cache →" : "Deploy →"}</button>
            </div>
          </div>
        </div>
      ) : (
        /* EDIT mode — compact: deployment info + save on the left, params on the right */
        <div className="editor-grid edit2">
          <div className="col">
            <div className="card panel">
              <h3>Deployment</h3>
              <dl className="kv-list">
                <dt>Model</dt><dd><b>{props.deployment.model_name}</b></dd>
                <dt>Engine</dt><dd>{props.deployment.engine}</dd>
                <dt>Ready</dt><dd><Badge kind={props.deployment.ready_instances > 0 ? "ok" : "warn"}>{props.deployment.ready_instances}/{props.deployment.replicas}</Badge></dd>
              </dl>
            </div>
            {/* #304 — move the running instance to a different worker without
                an undeploy/redeploy round-trip. */}
            <div className="card panel">
              <h3>Move to worker</h3>
              {liveInstances.length === 0 ? (
                <p className="hint">No live instance to move — deploy or start the engine first.</p>
              ) : liveInstances.length > 1 ? (
                <p className="hint">{liveInstances.length} live instances — reassign a specific one from the Fleet view.</p>
              ) : (
                <>
                  <div className="field">
                    <label>Target worker</label>
                    <select value={reassignTarget} onChange={(e) => setReassignTarget(e.target.value)}>
                      <option value="">— choose a worker —</option>
                      {budgets.filter((w) => w.id !== currentWorkerId).map((w) => (
                        <option key={w.id} value={w.id}>{w.name} — {w.mem_gb} GB</option>
                      ))}
                    </select>
                  </div>
                  {reassignErr && <div className="callout err" style={{ marginTop: 8 }}>{reassignErr}</div>}
                  <div className="btn-row" style={{ marginTop: 12 }}>
                    <button className="btn" disabled={reassignBusy || !reassignTarget || reassignTarget === currentWorkerId} onClick={reassign}>
                      {reassignBusy ? "…" : "Move →"}
                    </button>
                  </div>
                </>
              )}
            </div>
            <div className="card panel deploy-box">
              <h3>Save changes</h3>
              <div className="field"><label>Serve mode</label>{serveModes}</div>
              <div className="field"><label>Tags <span className="hint">group / find models</span></label>
                <TagField value={tags} onChange={setTags} /></div>
              <label className="hint" style={{ display: "flex", gap: 6, alignItems: "center", cursor: "pointer" }}>
                <input type="checkbox" checked={applyNow} onChange={(e) => setApplyNow(e.target.checked)} />
                Apply now — restart the running engine(s) with the new params (#566)
              </label>
              {!applyNow && <p className="hint">Otherwise changes apply on the next (re)deploy of the engine.</p>}
              {err && <div className="callout err" style={{ marginTop: 8 }}>{err}</div>}
              <div className="btn-row" style={{ marginTop: 12 }}>
                <button className="btn primary big" disabled={busy || !name.trim()} onClick={submit}>{busy ? "…" : "Save"}</button>
                <button className="btn ghost" onClick={props.onCancel}>Cancel</button>
              </div>
            </div>
          </div>
          <div className="card panel" ref={paramsRef}>{paramsInner}</div>
        </div>
      )}
    </section>
  );
}
