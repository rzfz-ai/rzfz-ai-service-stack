import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { endpoints, type CatalogEntry, type HfSearchResult } from "../api/client";
import { fleetFamilies, servableBy } from "../lib/hardwareFamily";
import { Badge, fmt, QueryState, TableSkeleton } from "../components/ui";
import { ModelEditor } from "../components/ModelEditor";

// #296 — full-page Deploy. Browse (Catalog · HuggingFace) → the unified
// ModelEditor for the chosen repo (quant table + live impact + params + README
// on ONE page). No intermediate repo-detail screen.

const SORTS = [
  { v: "trending", label: "🔥 Trending" },
  { v: "downloads", label: "Most downloads" },
  { v: "likes", label: "Most likes" },
  { v: "updated", label: "Recently updated" },
];
// #1518 (E5): the vLLM format chip is gone with the path itself. GGUF is the
// only model architecture the fleet serves — on AMD, on CPU and on NVIDIA,
// where the GPU's compute capability picks the CUDA llama.cpp runner
// (#1516/#1517). MLX stays browse-only.
const FORMAT_GGUF = { v: "gguf", label: "GGUF · llama.cpp / Mac" };
const FORMAT_MLX = { v: "mlx", label: "MLX · Mac" };
function formatsFor() {
  return [FORMAT_GGUF, FORMAT_MLX];
}
const TASKS = ["chat", "embed", "rerank", "vision"];

export type DeployTarget = {
  repoId: string; task: string;
  recommended?: Record<string, unknown>;
  recommendedLabel?: "card" | "house";
  preferFile?: string;
};

function HfBrowser({ onOpen }: { onOpen: (t: DeployTarget) => void }) {
  const FORMATS = formatsFor();
  const [term, setTerm] = useState("");
  const [sort, setSort] = useState("downloads");
  const [format, setFormat] = useState("gguf");
  const [task, setTask] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState({ term: "", sort: "downloads", format: "gguf", task: null as string | null });

  // no minimum length — empty box browses all for the sort + filters
  const q = useQuery({
    queryKey: ["hf-search", submitted],
    queryFn: () => endpoints.hfSearch(submitted.term, { sort: submitted.sort, format: submitted.format, task: submitted.task ?? undefined }),
  });
  const run = () => setSubmitted({ term, sort, format, task });

  return (
    <div>
      <div className="searchbar">
        <input className="input grow" placeholder="Search HuggingFace — or leave empty to browse the most-downloaded…"
          value={term} onChange={(e) => setTerm(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }} />
        <button className="btn primary" onClick={run}>Search</button>
      </div>
      <div className="filters">
        <div className="grp"><span className="eyebrow">Sort</span>
          <select className="input sm" value={sort} onChange={(e) => { setSort(e.target.value); setSubmitted((s) => ({ ...s, sort: e.target.value })); }}>
            {SORTS.map((s) => <option key={s.v} value={s.v}>{s.label}</option>)}
          </select></div>
        <div className="grp"><span className="eyebrow">Format</span>
          {FORMATS.map((f) => (
            <button key={f.v} className={`fchip ${format === f.v ? "on" : ""}`}
              onClick={() => { setFormat(f.v); setSubmitted((s) => ({ ...s, format: f.v })); }}>{f.label}</button>
          ))}</div>
        <div className="grp"><span className="eyebrow">Task</span>
          {TASKS.map((t) => (
            <button key={t} className={`fchip ${task === t ? "on" : ""}`}
              onClick={() => { const nt = task === t ? null : t; setTask(nt); setSubmitted((s) => ({ ...s, task: nt })); }}>{t}</button>
          ))}</div>
      </div>
      <div className="callout info" style={{ marginBottom: 14 }}>
        <b>Format → worker type.</b> <b>GGUF is the fleet standard</b>: it runs on the llama.cpp workers (AMD Strix Halo / NVIDIA CUDA / CPU) and on a Mac via Ollama.
        {" "}<b>MLX</b> needs an MLX runtime (mlx-lm / LM Studio) — <b>not in the fleet yet</b>, so MLX results are browse-only for now (Ollama on a Mac serves GGUF, not MLX-format weights).
      </div>
      <QueryState q={q} isEmpty={(d) => d.results.length === 0}
        loading={<div className="hf-grid">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="card repo skel" />)}</div>}
        empty={<div className="empty">No {submitted.format.toUpperCase()} repos{submitted.term ? ` match “${submitted.term}”` : ""}. Try another {submitted.term ? "term, " : ""}format, or sort.</div>}>
        {(d) => (
          <div className="hf-grid">
            {d.results.map((r: HfSearchResult) => (
              <button key={r.id} className="card repo" onClick={() => onOpen({ repoId: r.id, task: "chat" })}>
                <div className="name">{r.id}</div>
                <div className="badges">
                  {(r.library || submitted.format) && <span className="fmtbadge">{(r.library || submitted.format).toUpperCase()}</span>}
                  {r.pipeline_tag && <span className="fmtbadge">{r.pipeline_tag}</span>}
                  {r.gated && <span className="fmtbadge">🔒 gated</span>}
                </div>
                <div className="meta">
                  <span>↓ <b className="mono">{r.downloads != null ? fmt(r.downloads) : "—"}</b></span>
                  <span>♥ <b className="mono">{r.likes ?? "—"}</b></span>
                  {r.last_modified && <span className="muted">{new Date(r.last_modified).toLocaleDateString()}</span>}
                </div>
              </button>
            ))}
          </div>
        )}
      </QueryState>
    </div>
  );
}

function CatalogBrowser({ onOpen }: { onOpen: (t: DeployTarget) => void }) {
  const cat = useQuery({ queryKey: ["catalog"], queryFn: () => endpoints.catalog() });
  // #361: the fleet's live hardware classes — entries no node can serve are
  // greyed WITH the reason, not hidden (hiding reads as a broken catalog).
  const workers = useQuery({ queryKey: ["workers"], queryFn: endpoints.workers });
  // #1518 (E5): compare hardware FAMILIES, not literal labels. A worker
  // registers `cuda` (or, until it re-registers, `cuda-gb10`) while an entry
  // lists `nvidia` — the exact compare below used to grey out every card on a
  // GB10-only fleet, i.e. the console showed a box with no deployable model at
  // all. Same rule the manager uses for placement (`_hardware_family`).
  const fleet = fleetFamilies(workers.data);
  const servable = (e: CatalogEntry) => servableBy(fleet, e.hardware);
  return (
    <QueryState q={cat} isEmpty={(d) => d.length === 0}
      loading={<TableSkeleton rows={5} cols={4} />} empty={<div className="empty">No catalog entries.</div>}>
      {(entries) => (
        <>
          <div className="hf-grid">
            {entries.map((e: CatalogEntry) => {
              const ok = servable(e);
              return (
                <div key={e.name} className="card repo" style={ok ? undefined : { opacity: 0.55 }}>
                  <div className="name" style={{ fontFamily: "inherit", fontSize: 15 }}>{e.name} {e.recommended && <Badge kind="ok">★</Badge>}</div>
                  <div className="muted" style={{ fontSize: 12.5 }}>{e.description}</div>
                  <div className="badges"><span className="fmtbadge">{e.task}</span><span className="fmtbadge">{e.hardware.join("/")}</span>
                    {!ok && <span className="badge warn">no {e.hardware.join("/")} node in this fleet</span>}</div>
                  <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)" }}>{e.repo_id}</div>
                  {/* #1518 (E5): every catalog entry is GGUF now — the
                      repo-directory deploy branch went with vLLM. */}
                  <button className="btn sm primary" style={{ alignSelf: "flex-start" }} disabled={!ok}
                    title={ok ? undefined : "No worker with matching hardware — the engine cannot serve this artifact format"}
                    onClick={() => onOpen({
                      repoId: e.repo_id, task: e.task, recommended: e.params ?? {}, recommendedLabel: "house", preferFile: e.filename ?? undefined,
                    })}>Configure &amp; deploy</button>
                </div>
              );
            })}
          </div>
        </>
      )}
    </QueryState>
  );
}

export function Deploy() {
  const [tab, setTab] = useState<"hf" | "cat">("hf");
  const [target, setTarget] = useState<DeployTarget | null>(null);
  const qc = useQueryClient();
  const nav = useNavigate();
  const done = () => { qc.invalidateQueries({ queryKey: ["deployments"] }); nav("/models"); };

  if (target) {
    return <ModelEditor mode="deploy" repoId={target.repoId} task={target.task}
      recommended={target.recommended} recommendedLabel={target.recommendedLabel}
      preferFile={target.preferFile} onCancel={() => setTarget(null)} onDone={done} />;
  }

  return (
    <section className="page">
      <h1>Deploy a model</h1>
      <p className="lede">Pick a model to run on a worker. Start from the curated catalog, or browse HuggingFace — the browser filters to what your workers can actually serve.</p>
      <div className="tabs">
        <button className={`tab ${tab === "hf" ? "active" : ""}`} onClick={() => setTab("hf")}>HuggingFace</button>
        <button className={`tab ${tab === "cat" ? "active" : ""}`} onClick={() => setTab("cat")}>Catalog</button>
      </div>
      {tab === "hf" ? <HfBrowser onOpen={setTarget} /> : <CatalogBrowser onOpen={setTarget} />}
    </section>
  );
}
