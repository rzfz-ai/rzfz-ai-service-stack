import { useEffect, useRef, useState } from "react";
import { runCommand } from "../api/client";
import { Skeleton, relTime, toast } from "./ui";

// #296/#297 — one shared engine-log pane: live auto-refresh (pausable),
// auto-scroll to the newest line, and copy-the-whole-log. Used by both the
// Models drawer and the Fleet worker detail so they never drift apart.
// #1183: the FIRST answer is a full command round-trip (node claims on its
// next report cycle) — until it lands the pane shows a skeleton, and the
// manager hands back the node's last log chunk for this container at once
// (stale-while-revalidate) so the operator reads something immediately.
export function LogsPane({ container, workerId, onClose }:
  { container: string; workerId: string | null; onClose: () => void }) {
  // null = nothing to show yet (skeleton); `stale` = the last chunk, refresh in flight
  const [text, setText] = useState<string | null>(null);
  const [stale, setStale] = useState<{ asOf: string | null } | null>(null);
  const [live, setLive] = useState(true);
  const ref = useRef<HTMLPreElement | null>(null);
  const hasFresh = useRef(false);

  // #348: a tail_logs answer now legitimately waits up to a report cycle, so the
  // live poll must not stack a fresh enqueue on top of one still in flight.
  const inFlight = useRef(false);
  // CUI-17: a tail_logs poll runs on a wall-clock deadline of its own. Closing
  // the pane used to leave that loop running to completion and then write into
  // an unmounted component; the controller lets the effect cleanup stop it.
  const abort = useRef<AbortController | null>(null);
  async function fetchNow() {
    if (!workerId) { setText("no worker for this instance"); return; }
    if (inFlight.current) return;
    inFlight.current = true;
    const ctl = abort.current;
    try {
      const c = await runCommand(workerId, "tail_logs", { container, instance_id: container, tail: 400 }, {
        signal: ctl?.signal, revalidate: true, pollMs: 750,
        // only the very first paint uses the stale chunk — on later live polls
        // the screen already shows a fresher answer than any stale one
        onStale: (r, at) => { if (!ctl?.signal.aborted && !hasFresh.current) { setText(String((r as { logs?: string }).logs ?? "")); setStale({ asOf: at }); } },
      });
      if (ctl?.signal.aborted) return;
      if (c.status === "done") {
        hasFresh.current = true; setStale(null);
        setText(String((c.result as { logs?: string })?.logs ?? JSON.stringify(c.result)));
      } else if (!c.timed_out) {
        setStale(null); setText(String((c.result as { error?: string })?.error ?? JSON.stringify(c.result)));
      } else if (!hasFresh.current && text === null) {
        setText("node has not answered yet — it claims commands on its next report cycle");
      }
    } catch (e) { if (!ctl?.signal.aborted) setText(String(e)); } finally { inFlight.current = false; }
  }

  // initial fetch + live poll (cleared on pause / unmount / container change)
  useEffect(() => {
    const ctl = new AbortController();
    abort.current = ctl;
    hasFresh.current = false; setText(null); setStale(null);
    fetchNow().catch(() => undefined); /* eslint-disable-line */
    return () => { ctl.abort(); inFlight.current = false; };
  }, [container, workerId]);
  useEffect(() => {
    if (!live || !container) return;
    const t = setInterval(() => { fetchNow().catch(() => undefined); }, 2500);
    return () => clearInterval(t);
  }, [live, container, workerId]);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [text]);

  return (
    <div style={{ marginTop: 14 }}>
      <div className="logs-head">
        <strong className="mono">{container} — logs</strong>
        <div className="btn-row">
          {stale && <span className="badge muted" title={stale.asOf ? `Last chunk the node reported ${relTime(stale.asOf)}; a fresh tail is on its way` : "Last chunk the node reported; a fresh tail is on its way"}>last answer · refreshing…</span>}
          {live && <span className="logs-live" title="Refresh rides the node's command claim cycle (~30s per answer; faster while an instance is transitional)"><span className="pip" />live · per report cycle</span>}
          <button className="btn sm ghost" onClick={() => setLive((v) => !v)}>{live ? "pause" : "resume"}</button>
          <button className="btn sm ghost" disabled={text === null} onClick={() => navigator.clipboard?.writeText(text ?? "").then(
            () => toast("Logs copied", "ok"), () => toast("Copy failed", "err"))}>copy</button>
          <button className="btn sm ghost" onClick={onClose}>close</button>
        </div>
      </div>
      {text === null ? (
        // #1183 (1): skeleton lines while the first tail_logs round-trip is pending
        <div className="logs-pre logs-skel" aria-busy="true" aria-label="Loading logs…">
          {[72, 88, 55, 93, 64, 80, 47, 85].map((w, i) => <Skeleton key={i} w={`${w}%`} h={10} />)}
        </div>
      ) : (
        <pre className="logs-pre" ref={ref}>{text}</pre>
      )}
    </div>
  );
}
