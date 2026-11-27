// #3267: a model's exact origin — HF repo (linked), files/shards, quant, size,
// mmproj — as the manager states it (`origin` on deployments, inventory and
// cache rows; app/model_origin.py). Nothing here is derived from a model name.
//
// CUI-12: the repo id and file names are third-party strings. They are rendered
// as React text only. A link is used only when the manager built one AND it
// points at huggingface.co; otherwise the id is plain text. Air-gapped boxes:
// the link is information, nothing is fetched to render it.
import type { ModelOrigin } from "../api/client";

const HF = "https://huggingface.co/";

/** The manager's URL, if it is a huggingface.co link; else null. */
export function hfHref(url: string | null | undefined): string | null {
  return typeof url === "string" && url.startsWith(HF) ? url : null;
}

function gbText(n: number | null | undefined): string | null {
  return n == null ? null : `${n.toFixed(n >= 100 ? 0 : 1)} GB`;
}

/** One-line summary pieces after the repo: quant · shards · size · mmproj. */
export function originFacts(o: ModelOrigin): string[] {
  const out: string[] = [];
  if (o.quant) out.push(o.quant);
  if (o.shards > 1) out.push(`${o.shards} shards`);
  const size = gbText(o.size_gb);
  if (size) out.push(size);
  if (o.mmproj) out.push("+ mmproj");
  return out;
}

function Ext({ href, children, title }: { href: string | null; children: string; title?: string }) {
  if (!href) return <span className="mono" title={title}>{children}</span>;
  return (
    <a className="mono origin-link" href={href} target="_blank" rel="noopener noreferrer nofollow" title={title}
      onClick={(e) => e.stopPropagation()}>{children}</a>
  );
}

/** Compact origin line for a table cell. `linked={false}` (#3518): plain text,
 *  for a clickable row — the link took the row's click to Hugging Face. */
export function OriginLine({ origin, linked = true }: { origin?: ModelOrigin | null; linked?: boolean }) {
  if (!origin || origin.state === "unknown") {
    return <span className="origin-line muted" title={origin?.reason ?? "the manager states no origin for this model"}>origin unknown</span>;
  }
  if (origin.state === "ambiguous") {
    return <span className="origin-line badge warn" title={origin.reason ?? "two repositories claim the same file name"}>origin ambiguous</span>;
  }
  const facts = originFacts(origin);
  return (
    <span className="origin-line">
      <Ext href={linked ? hfHref(origin.repo_url) : null} title="Hugging Face repository">{origin.repo_id ?? ""}</Ext>
      {facts.length > 0 && <span className="muted"> · {facts.join(" · ")}</span>}
    </span>
  );
}

/** Full origin block for a detail drawer: repo, every file with its size and link. */
export function OriginBlock({ origin }: { origin?: ModelOrigin | null }) {
  if (!origin || origin.state !== "known") return <OriginLine origin={origin} />;
  return (
    <div className="origin-block">
      <div><span className="mono">{origin.repo_id ?? ""}</span>
        {/* #3518: the repository link lives here, labelled, not in the row */}
        {hfHref(origin.repo_url) && (
          <a className="origin-link" style={{ marginLeft: 8 }} href={hfHref(origin.repo_url)!}
            target="_blank" rel="noopener noreferrer nofollow">Open on Hugging Face ↗</a>)}</div>
      <div className="muted">
        {[origin.quant ? `quant ${origin.quant}` : null,
          origin.shards > 1 ? `${origin.shards} shards` : origin.shards === 1 ? "single file" : null,
          gbText(origin.size_gb) ? `${gbText(origin.size_gb)} on disk` : "size on disk unknown"]
          .filter(Boolean).join(" · ")}
      </div>
      <ul className="origin-files">
        {origin.files.map((f) => (
          <li key={f.name}>
            <Ext href={hfHref(f.url)} title={f.mmproj ? "vision projector (mmproj)" : "weight file"}>{f.name}</Ext>
            {f.mmproj && <span className="badge muted" style={{ marginLeft: 6 }}>mmproj</span>}
            <span className="muted"> {gbText(f.size_gb) ?? "—"}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
