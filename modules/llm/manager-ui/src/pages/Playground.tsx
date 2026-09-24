import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, endpoints, streamPlaygroundChat } from "../api/client";
import { fmt } from "../components/ui";

type Tab = "chat" | "embeddings" | "rerank";

// serve-task that each Playground tab needs, so the model list only offers
// deployments that can actually handle the request.
const TAB_TASK: Record<Tab, string> = { chat: "chat", embeddings: "embed", rerank: "rerank" };

function ModelPicker({ value, onChange, task }: { value: string; onChange: (v: string) => void; task: string }) {
  const q = useQuery({ queryKey: ["deployments"], queryFn: endpoints.deployments });
  const models = (q.data ?? []).filter((d) => (d.task || "chat") === task).map((d) => d.model_name);
  // pick the first matching model when none is chosen OR the current one isn't
  // valid for this tab (e.g. after switching chat → embeddings).
  useEffect(() => {
    if (models.length === 0) return;
    if (!value || !models.includes(value)) onChange(models[0]);
  }, [value, models.join(","), task]);
  return (
    <select value={models.includes(value) ? value : ""} onChange={(e) => onChange(e.target.value)} style={{ width: 240 }}>
      {models.length === 0 && <option value="">no {task} models served</option>}
      {models.map((m) => <option key={m} value={m}>{m}</option>)}
    </select>
  );
}

function errMsg(e: unknown) { return e instanceof ApiError ? `${e.status}: ${e.message}` : String(e); }

// Reasoning models (qwen3, …) emit <think>…</think>. When the engine doesn't
// split it into reasoning_content it leaks into the chat — strip it (and stray
// tags) so the transcript shows the answer, not the scratchpad. #1184: while a
// stream is in flight the block may still be OPEN — cut the unclosed tail too,
// so the scratchpad never flashes through before its closing tag arrives.
function stripThink(t: string): string {
  return t.replace(/<think>[\s\S]*?<\/think>/gi, "").replace(/<think>[\s\S]*$/i, "")
    .replace(/<\/?think>/gi, "").replace(/\n{3,}/g, "\n\n").trim();
}

// One completion parameter: slider + numeric input, kept in sync (#2a).
function ParamControl({
  label, value, set, min, max, step,
}: {
  label: string; value: number; set: (n: number) => void; min: number; max: number; step: number;
}) {
  const clamp = (n: number) => Math.max(min, Math.min(max, n));
  const decimals = step < 1 ? String(step).split(".")[1]?.length ?? 2 : 0;
  // exact fill % (value within [min,max]) — drives the track gradient so 0 sits
  // fully left and max fully right regardless of the (narrow) slider width.
  const pct = max > min ? Math.round(((clamp(value) - min) / (max - min)) * 100) : 0;
  return (
    <div className="param-row">
      <label title={label}>{label}</label>
      <input type="range" min={min} max={max} step={step} value={value}
        style={{ ["--pct"]: `${pct}%` } as React.CSSProperties}
        onChange={(e) => set(+e.target.value)} aria-label={`${label} slider`} />
      <input className="val" type="number" min={min} max={max} step={step}
        value={Number.isInteger(value) ? value : value.toFixed(decimals)}
        onChange={(e) => { const n = parseFloat(e.target.value); if (!Number.isNaN(n)) set(clamp(n)); }}
        aria-label={`${label} value`} />
    </div>
  );
}

const CHAT_DEFAULTS = { temperature: 0.7, topP: 1.0, maxTokens: 512, presencePenalty: 0, frequencyPenalty: 0 };

// --- Chat with inline params ------------------------------------------------
// #1184: the chat STREAMS. The old non-streaming call sat behind a 120 s total
// timeout, so a long answer (8192 tokens at ~49 tok/s ≈ 170 s) was killed by
// the console itself while the engine was fine. Tokens now render as they
// arrive, there is no total timeout (only the backend's idle timeout), and
// Send turns into Stop while a generation is in flight.
type ChatMsg = { role: string; content: string; stopped?: boolean; partial?: boolean };

function ChatTab({ model }: { model: string }) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [usage, setUsage] = useState("");
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const [temperature, setTemperature] = useState(CHAT_DEFAULTS.temperature);
  const [topP, setTopP] = useState(CHAT_DEFAULTS.topP);
  const [maxTokens, setMaxTokens] = useState(CHAT_DEFAULTS.maxTokens);
  const [presencePenalty, setPresencePenalty] = useState(CHAT_DEFAULTS.presencePenalty);
  const [frequencyPenalty, setFrequencyPenalty] = useState(CHAT_DEFAULTS.frequencyPenalty);

  // replace the assistant turn being generated (always the last message)
  const setLast = (patch: (last: ChatMsg) => ChatMsg) =>
    setMsgs((m) => (m.length ? [...m.slice(0, -1), patch(m[m.length - 1])] : m));

  async function send(history: ChatMsg[]) {
    const ac = new AbortController();
    abortRef.current = ac;
    setStreaming(true); setErr(null); setUsage("");
    setMsgs([...history, { role: "assistant", content: "" }]);
    try {
      const r = await streamPlaygroundChat(
        {
          model, messages: history.map(({ role, content }) => ({ role, content })),
          max_tokens: maxTokens, temperature, top_p: topP,
          presence_penalty: presencePenalty, frequency_penalty: frequencyPenalty,
        },
        (content) => setLast((l) => ({ ...l, content })),
        ac.signal,
      );
      setLast((l) => ({
        ...l,
        content: r.content || (r.aborted || r.error ? r.content : "(no content)"),
        stopped: r.aborted,
        partial: !!r.error,
      }));
      if (r.error) setErr(r.error);
      if (r.usage) {
        const u = r.usage;
        setUsage(`${fmt(u.prompt_tokens)} in / ${fmt(u.completion_tokens)} out` +
                 (r.finish_reason === "length" ? " · hit max_tokens" : ""));
      }
    } catch (e) {
      setErr(errMsg(e));
      // nothing arrived — drop the empty placeholder turn
      setMsgs((m) => (m.length && m[m.length - 1].role === "assistant" && !m[m.length - 1].content ? m.slice(0, -1) : m));
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }
  const stop = () => abortRef.current?.abort();
  const submit = () => {
    if (streaming || !input.trim() || !model) return;
    const next = [...msgs, { role: "user", content: input }];
    setInput(""); void send(next);
  };
  // leaving the page mid-generation must not leave the engine generating
  useEffect(() => () => abortRef.current?.abort(), []);
  const last = msgs[msgs.length - 1];
  const waiting = streaming && !!last && last.role === "assistant" && !stripThink(last.content);
  const resetParams = () => {
    setTemperature(CHAT_DEFAULTS.temperature); setTopP(CHAT_DEFAULTS.topP); setMaxTokens(CHAT_DEFAULTS.maxTokens);
    setPresencePenalty(CHAT_DEFAULTS.presencePenalty); setFrequencyPenalty(CHAT_DEFAULTS.frequencyPenalty);
  };

  return (
    <div className="pg-chat">
      {/* chat on the left, parameters on the right */}
      <div className="pg-main">
        <div className="table-wrap" style={{ padding: 12, minHeight: 260, marginBottom: 10 }}>
          {msgs.length === 0 && <p className="muted">Say something to test the model.</p>}
          {msgs.map((m, i) => {
            const live = streaming && i === msgs.length - 1 && m.role === "assistant";
            const shown = m.role === "assistant" ? stripThink(m.content) : m.content;
            if (live && !shown) return null;   // "…thinking" below stands in until the first visible token
            return (
              <p key={i} style={{ margin: "6px 0" }}>
                <strong style={{ color: m.role === "user" ? "var(--text)" : "var(--razz-red)" }}>{m.role}&gt;</strong>{" "}
                <span style={{ whiteSpace: "pre-wrap" }}>{shown}</span>
                {live && <span className="pg-cursor" aria-hidden="true" />}
                {m.stopped && <span className="muted"> (stopped)</span>}
                {m.partial && <span className="muted"> (interrupted)</span>}
              </p>
            );
          })}
          {waiting && <p className="muted">…thinking</p>}
        </div>
        {err && <div className="callout err">{err}</div>}
        <div className="btn-row">
          <input placeholder="message" value={input} onChange={(e) => setInput(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") submit(); }} />
          {streaming
            ? <button className="btn danger" onClick={stop} title="Abort the generation — the partial answer stays">Stop</button>
            : <button className="btn primary" disabled={!model || !input.trim()} onClick={submit}>Send</button>}
          <button className="btn ghost" disabled={!msgs.length || streaming} onClick={() => { setMsgs([]); setUsage(""); }}>Clear</button>
          {usage && <span className="muted">{usage}</span>}
        </div>
      </div>
      <aside className="pg-params">
        <div className="params-panel">
          <div style={{ fontWeight: 600, fontSize: "0.8125rem", marginBottom: 4 }}>Parameters</div>
          <ParamControl label="temperature" value={temperature} set={setTemperature} min={0} max={2} step={0.05} />
          <ParamControl label="top_p" value={topP} set={setTopP} min={0} max={1} step={0.05} />
          <ParamControl label="max_tokens" value={maxTokens} set={setMaxTokens} min={16} max={8192} step={16} />
          <ParamControl label="presence_penalty" value={presencePenalty} set={setPresencePenalty} min={-2} max={2} step={0.1} />
          <ParamControl label="frequency_penalty" value={frequencyPenalty} set={setFrequencyPenalty} min={-2} max={2} step={0.1} />
          <div className="param-reset"><button className="btn ghost sm" onClick={resetParams}>Reset to defaults</button></div>
        </div>
      </aside>
    </div>
  );
}

// --- Embeddings: similarity heatmap of sample sentences ---------------------
const SAMPLE = `The cat sits on the mat.
A feline rests quietly on the rug.
Stock markets fell sharply today.
Investors sold shares amid the downturn.
I love hiking in the mountains.`;

function cosine(a: number[], b: number[]): number {
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
  return na && nb ? dot / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

// Hand-rolled 2-component PCA (no chart/ML lib). Uses the DUAL form: build the
// n×n Gram matrix of the centered vectors (cheap — n is a handful of sentences,
// never the embedding dim D) and extract the top-2 eigenpairs by power
// iteration + deflation. PCA scores are sqrt(λ_c)·v_c — the 2D coordinates.
function topEigen(M: number[][], iters = 200): { vec: number[]; val: number } {
  const n = M.length;
  let v = Array.from({ length: n }, (_, i) => Math.sin(i + 1) + 0.5); // deterministic seed
  let val = 0;
  for (let it = 0; it < iters; it++) {
    const w = new Array(n).fill(0);
    for (let i = 0; i < n; i++) { let s = 0; for (let j = 0; j < n; j++) s += M[i][j] * v[j]; w[i] = s; }
    const norm = Math.sqrt(w.reduce((a, x) => a + x * x, 0)) || 1;
    for (let i = 0; i < n; i++) w[i] /= norm;
    val = norm;
    v = w;
  }
  return { vec: v, val };
}

function pca2d(vecs: number[][]): { x: number; y: number }[] {
  const n = vecs.length;
  const d = vecs[0]?.length ?? 0;
  if (n === 0 || d === 0) return [];
  if (n === 1) return [{ x: 0, y: 0 }];
  const mean = new Array(d).fill(0);
  for (const v of vecs) for (let k = 0; k < d; k++) mean[k] += v[k] / n;
  const B = vecs.map((v) => v.map((x, k) => x - mean[k]));
  const G: number[][] = Array.from({ length: n }, () => new Array(n).fill(0));
  for (let i = 0; i < n; i++)
    for (let j = i; j < n; j++) {
      let s = 0; for (let k = 0; k < d; k++) s += B[i][k] * B[j][k];
      G[i][j] = G[j][i] = s;
    }
  const e1 = topEigen(G);
  const G2 = G.map((row, i) => row.map((x, j) => x - e1.val * e1.vec[i] * e1.vec[j]));
  const e2 = topEigen(G2);
  const s1 = Math.sqrt(Math.max(0, e1.val));
  const s2 = Math.sqrt(Math.max(0, e2.val));
  return vecs.map((_, i) => ({ x: e1.vec[i] * s1, y: e2.vec[i] * s2 }));
}

// 2D scatter of the PCA projection — clustering shows as spatial proximity.
function PcaScatter({ sents, coords }: { sents: string[]; coords: { x: number; y: number }[] }) {
  const W = 420, H = 300, m = 26;
  const xs = coords.map((p) => p.x), ys = coords.map((p) => p.y);
  const [minX, maxX] = [Math.min(...xs), Math.max(...xs)];
  const [minY, maxY] = [Math.min(...ys), Math.max(...ys)];
  const sx = (x: number) => (maxX === minX ? W / 2 : m + ((x - minX) / (maxX - minX)) * (W - 2 * m));
  const sy = (y: number) => (maxY === minY ? H / 2 : H - m - ((y - minY) / (maxY - minY)) * (H - 2 * m));
  return (
    <div className="pca-wrap">
      <svg className="pca-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="2D PCA projection of the sentence embeddings">
        <line x1={m / 2} y1={H / 2} x2={W - m / 2} y2={H / 2} stroke="var(--border)" strokeDasharray="3 3" />
        <line x1={W / 2} y1={m / 2} x2={W / 2} y2={H - m / 2} stroke="var(--border)" strokeDasharray="3 3" />
        {coords.map((p, i) => (
          <g key={i}>
            <title>{`${i + 1}. ${sents[i]}`}</title>
            <circle className="pca-dot" cx={sx(p.x)} cy={sy(p.y)} r={13} />
            <text className="pca-num" x={sx(p.x)} y={sy(p.y)}>{i + 1}</text>
          </g>
        ))}
      </svg>
      <ol className="pca-legend">
        {sents.map((s, i) => (
          <li key={i} className="li"><b>{i + 1}.</b><span title={s}>{s}</span></li>
        ))}
      </ol>
    </div>
  );
}

function EmbeddingsTab({ model }: { model: string }) {
  const [text, setText] = useState(SAMPLE);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<{ sents: string[]; sim: number[][]; dims: number; coords: { x: number; y: number }[] } | null>(null);
  const run = useMutation({
    mutationFn: async () => {
      const sents = text.split("\n").map((s) => s.trim()).filter(Boolean);
      const res = await endpoints.playgroundEmbeddings({ model, input: sents });
      const vecs: number[][] = (res?.data ?? []).map((d: any) => d.embedding);
      const sim = sents.map((_, i) => sents.map((__, j) => cosine(vecs[i] || [], vecs[j] || [])));
      // keep index alignment with `sents` (do not drop rows)
      const coords = vecs.every((v) => Array.isArray(v) && v.length > 0) ? pca2d(vecs) : [];
      return { sents, sim, dims: vecs[0]?.length ?? 0, coords };
    },
    onSuccess: (d) => { setErr(null); setData(d); },
    onError: (e) => { setData(null); setErr(errMsg(e)); },
  });
  const cellColor = (v: number) => `rgba(205,23,25,${Math.max(0, Math.min(1, v)).toFixed(3)})`;

  return (
    <div className="stack">
      <p className="muted" style={{ marginTop: 0 }}>Embed several sentences and see how the model groups them — brighter = more similar (cosine). Related sentences cluster; unrelated stay pale.</p>
      <textarea rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder="one sentence per line" />
      <div className="btn-row">
        <button className="btn primary" disabled={run.isPending || !model || !text.trim()} onClick={() => run.mutate()}>
          {run.isPending ? "Embedding…" : "Embed + compare"}
        </button>
      </div>
      {err && <div className="callout err">{err}</div>}
      {data && data.coords.length >= 2 && (
        <div className="card">
          <div className="muted" style={{ marginBottom: 10 }}>2D projection (PCA) — closer dots = more similar embeddings</div>
          <PcaScatter sents={data.sents} coords={data.coords} />
        </div>
      )}
      {data && (
        <div className="card" style={{ overflowX: "auto" }}>
          <div className="muted" style={{ marginBottom: 10 }}>{data.sents.length} sentences · {data.dims} dims · cosine similarity</div>
          <table className="heatmap">
            <thead>
              <tr><th></th>{data.sents.map((s, j) => <th key={j} className="lbl col" title={s}>{j + 1}. {s}</th>)}</tr>
            </thead>
            <tbody>
              {data.sents.map((s, i) => (
                <tr key={i}>
                  <th className="lbl" title={s}>{i + 1}. {s}</th>
                  {data.sim[i].map((v, j) => (
                    <td key={j} className="cell" style={{ background: cellColor(v), color: v > 0.55 ? "#fff" : "inherit" }} title={`${v.toFixed(3)}`}>{v.toFixed(2)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// --- Rerank: visual score bars ----------------------------------------------
const RR_DOCS = `Cats are small domesticated carnivores.
The stock market closed lower on Friday.
Kittens are baby cats and love to play.
Bond yields rose as investors reacted.`;

function RerankTab({ model }: { model: string }) {
  const [query, setQuery] = useState("Tell me about cats");
  const [docs, setDocs] = useState(RR_DOCS);
  const [err, setErr] = useState<string | null>(null);
  const [results, setResults] = useState<{ doc: string; score: number }[] | null>(null);
  const documents = docs.split("\n").map((d) => d.trim()).filter(Boolean);
  const run = useMutation({
    mutationFn: () => endpoints.playgroundRerank({ model, query, documents }),
    onSuccess: (d) => {
      setErr(null);
      const rows = (d?.results ?? []).map((r: any) => ({ doc: documents[r.index] ?? `#${r.index}`, score: r.relevance_score ?? r.score ?? 0 }));
      rows.sort((a: any, b: any) => b.score - a.score);
      setResults(rows);
    },
    onError: (e) => { setResults(null); setErr(errMsg(e)); },
  });
  const max = results && results.length ? Math.max(...results.map((r) => r.score)) : 1;

  return (
    <div className="stack">
      <p className="muted" style={{ marginTop: 0 }}>Rank documents by relevance to the query — bar length + rank show how the model scores each. Relevant docs rise to the top.</p>
      <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="query" />
      <textarea rows={5} value={docs} onChange={(e) => setDocs(e.target.value)} placeholder="one document per line" />
      <div className="btn-row">
        <button className="btn primary" disabled={run.isPending || !model || !query.trim() || !documents.length} onClick={() => run.mutate()}>
          {run.isPending ? "Reranking…" : "Rerank"}
        </button>
        <span className="muted">{documents.length} document(s)</span>
      </div>
      {err && <div className="callout err">{err}</div>}
      {results && (
        <div className="card">
          {results.map((r, i) => (
            <div key={i} className="rr-row">
              <span className="rr-rank">{i + 1}</span>
              <div>
                <div className="rr-doc">{r.doc}</div>
                <div className="rr-bar"><div className="rr-fill" style={{ width: `${max > 0 ? Math.max(3, (r.score / max) * 100) : 0}%` }} /></div>
              </div>
              <span className="num" style={{ textAlign: "right" }}>{r.score.toFixed(4)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Playground() {
  // #296 deep-link: from a deployment, /playground?model=<name>&tab=<chat|embeddings|rerank>
  // opens the right tab pre-selected to that model.
  const params = new URLSearchParams(window.location.search);
  const qtab = params.get("tab");
  const [tab, setTab] = useState<Tab>(
    qtab === "embeddings" || qtab === "rerank" || qtab === "chat" ? (qtab as Tab) : "chat");
  const [model, setModel] = useState(params.get("model") || "");
  // #1024 (operator decision 2026-09-02, reversing #501): in enforce mode with
  // a lapsed subscription this page is refused with 402 exactly like the
  // metered API — the playground spends the same fleet, so it takes the same
  // gates. Every request would fail; say why BEFORE the operator sends one,
  // rather than leaving them to read it out of an error toast.
  const ent = useQuery({ queryKey: ["entitlement"], queryFn: endpoints.entitlementStatus,
    refetchInterval: 60_000 });
  const lapsed = !!ent.data && ent.data.enforced && !ent.data.entitled;
  return (
    <section className="page">
      <h1>Playground</h1>
      <p className="lede">Try the fleet directly — chat, embeddings, and rerank. Runs through the manager with the internal key, under the same concurrency cap, entitlement gate and metering as the <span className="mono">/v1</span> API.</p>
      {lapsed && (
        <div className="callout key">
          <b>Subscription {ent.data!.state}</b> — inference is refused with 402 in enforce mode, on
          this page as well as on the metered API. The playground spends the same fleet, so it takes
          the same gates; renew the subscription to use it again.
          {ent.data!.reason ? <> Reason: <span className="mono">{ent.data!.reason}</span>.</> : null}
        </div>
      )}
      <div className="spread">
        <div className="seg">
          {(["chat", "embeddings", "rerank"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
          ))}
        </div>
        <div className="btn-row"><span className="muted">model:</span><ModelPicker value={model} onChange={setModel} task={TAB_TASK[tab]} /></div>
      </div>
      {tab === "chat" && <ChatTab model={model} />}
      {tab === "embeddings" && <EmbeddingsTab model={model} />}
      {tab === "rerank" && <RerankTab model={model} />}
    </section>
  );
}
