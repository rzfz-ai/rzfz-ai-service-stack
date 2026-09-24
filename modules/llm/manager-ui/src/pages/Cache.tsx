// #307 Model cache — the master registry (Zot). See what's cached, pull a model
// into the cache WITHOUT deploying (via the full Deploy browser → "Model cache"
// target), evict from the cache. Workers pull from here (LAN), not HuggingFace.
//
// #1179: Zot is content-addressed. The same model is routinely cached under TWO
// tags — `latest` (manager auto-mirror, #828) and `deployed` (worker-agent
// auto-cache on every deploy) — pointing at the SAME blobs. Listing one row per
// tag and summing per tag showed "6 cached · 84 GB" for 3 models / ~42 GB, which
// misleads capacity planning on small boxes. So: one row per repository, tags as
// chips (each its own delete), sizes over UNIQUE blob digests, and the delete
// confirm says that space is only freed once the last tag is gone.
import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints, type RegistryModel } from "../api/client";
import { QueryState, toast } from "../components/ui";

const gb = (b: number) => (b / 1e9).toFixed(2);

interface RepoGroup {
  repository: string;
  rows: RegistryModel[];        // one per tag, walk order
  unique_bytes: number;         // blobs counted once across the repo's tags
  tagged_bytes: number;         // naive per-tag sum (what the old view showed)
  unique_files: number;
}

// Group tag rows by repository and size each repo over its unique blobs
// (config + layer digests). A blob without a digest cannot be deduped and is
// counted — same rule as the backend's store total. Exported for reuse/tests.
export function groupByRepository(models: RegistryModel[]): RepoGroup[] {
  const order: string[] = [];
  const byRepo = new Map<string, RegistryModel[]>();
  for (const m of models) {
    if (!byRepo.has(m.repository)) { byRepo.set(m.repository, []); order.push(m.repository); }
    byRepo.get(m.repository)!.push(m);
  }
  return order.map((repository) => {
    const rows = byRepo.get(repository)!;
    const seen = new Set<string>();
    let unique_bytes = 0, tagged_bytes = 0;
    const fileDigests = new Set<string>();
    let undigestedFiles = 0;
    const count = (digest: string | null | undefined, size: number) => {
      if (digest && seen.has(digest)) return;
      if (digest) seen.add(digest);
      unique_bytes += size;
    };
    for (const r of rows) {
      tagged_bytes += r.size_bytes;
      count(r.config_digest, r.config_size ?? 0);
      for (const f of r.files) {
        count(f.digest, f.size);
        if (f.digest) fileDigests.add(f.digest); else undigestedFiles += 1;
      }
    }
    return { repository, rows, unique_bytes, tagged_bytes, unique_files: fileDigests.size + undigestedFiles };
  });
}

export function Cache() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["registry-models"], queryFn: endpoints.registryModels, refetchInterval: 5000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["registry-models"] });
  const groups = useMemo(() => groupByRepository(q.data?.models ?? []), [q.data]);

  // Delete ONE tag. Tags of the same repo share their blobs, so the registry's
  // GC only reclaims space once the LAST referencing tag is gone — say so.
  async function evict(g: RepoGroup, tag: string) {
    const me = g.rows.find((r) => r.tag === tag);
    // siblings that point at a DIFFERENT manifest are the only ones that keep the blobs alive
    const others = g.rows.filter((r) => r.tag !== tag && !(me?.digest && r.digest === me.digest)).map((r) => r.tag);
    const why = others.length
      ? `Tags of the same model share their blobs — space is freed only when the last tag is gone, and ${others.map((t) => `“${t}”`).join(", ")} still reference${others.length === 1 ? "s" : ""} them.`
      : `This is the last tag — the registry's garbage collector will reclaim its ${gb(g.unique_bytes)} GB.`;
    if (!confirm(`Delete ${g.repository}:${tag} from the cache?\n\n${why}\n\nWorkers keep any copy they already pulled.`)) return;
    try { await endpoints.evictModel(g.repository, tag); toast(`Evicted ${g.repository}:${tag}`, "ok"); refresh(); }
    catch (e) { toast(String(e), "err"); }
  }

  // Delete EVERY tag of a repo — the one action that actually frees its space.
  // rzfz review #1204: the registry deletes by MANIFEST digest and the pusher's
  // manifest is deterministic, so `latest` + `deployed` of one model usually
  // share ONE manifest — deleting the first tag removes both, the second 404s.
  // Delete once per distinct manifest digest and treat 404 as "already gone".
  async function evictAll(g: RepoGroup) {
    const tags = g.rows.map((r) => r.tag);
    if (!confirm(`Delete ${g.repository} (${tags.map((t) => `“${t}”`).join(", ")}) from the cache?\n\nAll ${tags.length} tags go — the registry's garbage collector then reclaims ${gb(g.unique_bytes)} GB.\n\nWorkers keep any copy they already pulled.`)) return;
    const seenManifests = new Set<string>();
    const failed: string[] = [];
    for (const r of g.rows) {
      if (r.digest && seenManifests.has(r.digest)) continue;   // same manifest already deleted
      if (r.digest) seenManifests.add(r.digest);
      try { await endpoints.evictModel(g.repository, r.tag); }
      catch (e) {
        const msg = String(e);
        if (/\b404\b|not in cache/i.test(msg)) continue;          // gone with a sibling tag — fine
        failed.push(`${r.tag}: ${msg}`);
      }
    }
    if (failed.length) toast(`Some tags were not evicted — ${failed.join("; ")}`, "err");
    else toast(`Evicted ${g.repository} (${tags.length} tag${tags.length === 1 ? "" : "s"})`, "ok");
    refresh();
  }

  const d = q.data;
  const tagCount = d?.models.length ?? 0;
  const tagged = d?.total_bytes_tagged ?? d?.total_bytes ?? 0;
  const showsTagged = d != null && tagged > d.total_bytes;
  // The header total dedupes blobs ACROSS repos; the per-repo column dedupes
  // within a repo — two models sharing a blob make Σ(column) > header. Intended.
  return (
    <section className="page">
      <h1>Model cache</h1>
      <p className="lede">The master model store — models cached for the fleet. Pull a model here once; workers get it from the master over the LAN, not HuggingFace. Deploying a model caches it automatically.</p>
      <div className="spread">
        <span className="muted">
          {d?.available ? (
            <>
              {groups.length} cached · {tagCount} tag{tagCount === 1 ? "" : "s"} · {gb(d.total_bytes)} GB on disk
              {showsTagged && (
                <span title="Tags of the same model point at the same blobs. The registry stores each blob once, so the tagged sum overstates what is actually on disk.">
                  {" "}({gb(tagged)} GB as tagged — shared blobs counted once)
                </span>
              )}
              {d.truncated && <span className="badge warn" style={{ marginLeft: 8 }} title={d.truncated_reason}>partial view</span>}
            </>
          ) : ""}
        </span>
        <div className="btn-row">
          <button className="btn ghost sm" onClick={refresh}>Refresh</button>
          {/* reuse the full HF / curated-catalog browser — pick "Model cache" as the target there */}
          <button className="btn primary" onClick={() => nav("/deploy")}>+ Pull a model into cache</button>
        </div>
      </div>
      <QueryState q={q} isEmpty={(dd) => !dd.available || dd.models.length === 0}
        loading={<div className="loading">Loading cache…</div>}
        empty={<div className="empty">{q.data && !q.data.available
          ? `Model store not reachable${q.data.error ? ` (${q.data.error})` : ""}.`
          : "Nothing cached yet. Use “+ Pull a model into cache” — browse HuggingFace or the catalog, then choose “Model cache” as the target."}</div>}>
        {() => (
          <div className="table-wrap">
            <table className="rz">
              <thead><tr><th>Model</th><th>Tags</th><th>Files</th><th>Size on disk</th><th>Actions</th></tr></thead>
              <tbody>
                {groups.map((g) => (
                  <tr key={g.repository}>
                    <td><strong>{g.repository}</strong></td>
                    <td>
                      {g.rows.map((r) => (
                        <span key={r.tag} className="chip tag-chip" title={`${g.repository}:${r.tag} · ${gb(r.size_bytes)} GB as tagged`}>
                          {r.tag}
                          <button type="button" className="chip-x" aria-label={`Delete tag ${r.tag}`}
                            title={g.rows.length > 1 ? "Delete this tag — space is freed only when the last tag is gone" : "Delete this tag (last one — frees the space)"}
                            onClick={() => evict(g, r.tag)}>×</button>
                        </span>
                      ))}
                    </td>
                    <td className="num">{g.unique_files}</td>
                    <td className="num mono" title={g.rows.length > 1 ? `${gb(g.tagged_bytes)} GB as tagged — ${g.rows.length} tags share the same blobs` : undefined}>
                      {gb(g.unique_bytes)} GB
                    </td>
                    <td>
                      <button className="btn xs danger" onClick={() => evictAll(g)}>
                        {g.rows.length > 1 ? "Delete all tags" : "Delete"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
    </section>
  );
}
