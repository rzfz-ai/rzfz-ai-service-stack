import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { whoami, type Identity } from "../api/client";
import { NoAccess } from "../pages";
import { Toaster } from "./ui";

// CUI-9: #843's capability matrix used to be consumed in exactly ONE place —
// the nav filter below, which the file itself calls "cosmetic only". Two nav
// entries are deliberately visible to EITHER of two tiers (Keys, Usage), so the
// weaker tier landed on the full admin page and was shown controls the server
// is guaranteed to 403. The identity resolved here is now published so pages
// can gate at the CONTROL level too. This is still not the security boundary —
// `require_role` on the server is — it just stops offering what cannot work.
const IdentityCtx = createContext<Identity | null>(null);

export function useIdentity(): Identity | null {
  return useContext(IdentityCtx);
}

/** True only when the resolved identity actually carries the capability. */
export function useCapability(cap: string): boolean {
  return !!useContext(IdentityCtx)?.capabilities?.[cap];
}

// #843 p1: `cap` names the capability (from app/authz.py::capabilities_for's
// 9-key matrix) that reveals this item. An array means "any of these" — Keys
// and Usage are visible to BOTH tiers that can do something there, just not
// the same thing. No `cap` = always visible (Dashboard). This is cosmetic
// only: the real gate is the server's `require_role` 403 on the underlying
// routes — a mutation here can hide/show a link, never grant/revoke access.
type NavItem =
  | { to: string; label: string; ico: string; cap?: string | string[] }
  | { divider: true };

const NAV: NavItem[] = [
  { to: "/dashboard", label: "Dashboard", ico: "▚" },
  { to: "/deploy", label: "Deploy a model", ico: "＋", cap: "deploy_models" },
  { to: "/models", label: "Deployments", ico: "◆", cap: "deploy_models" },
  { to: "/cache", label: "Model cache", ico: "▦", cap: "deploy_models" },
  { divider: true },
  { to: "/workers", label: "Workers", ico: "🖧", cap: "manage_workers" },
  { to: "/playground", label: "Playground", ico: "▶", cap: "playground" },
  { to: "/keys", label: "API Keys", ico: "🔑", cap: ["self_issue_key", "issue_keys_for_anyone"] },
  { to: "/usage", label: "Usage", ico: "▤", cap: ["view_own_usage", "view_all_usage"] },
  { to: "/settings", label: "Settings", ico: "⚙", cap: "global_settings" },
];

function hasCap(capabilities: Record<string, boolean>, cap: string | string[] | undefined): boolean {
  if (!cap) return true; // no capability tag = always visible
  const wanted = Array.isArray(cap) ? cap : [cap];
  return wanted.some((c) => capabilities[c]);
}

function crumbFor(path: string): string {
  const n = NAV.find((x): x is Extract<NavItem, { to: string }> => "to" in x && path.startsWith(x.to));
  return n ? n.label : "LLM Manager";
}

// CUI-2: auto-retry budget for the identity probe. 5 attempts at 0.5/1/2/4/8s
// covers a Caddy reload or a manager container restart without the operator
// touching anything; after that the Retry button drives it.
const WHOAMI_TRIES = 5;
const backoffMs = (attempt: number) => Math.min(8000, 500 * 2 ** attempt);

export default function Shell() {
  // #843 p1: `null` is a distinct THIRD state from a resolved identity — it
  // means "whoami() hasn't answered yet", so the loading branch below can
  // render a neutral placeholder instead of flashing the full sidebar+nav
  // (or the no-access screen) before the real role is known.
  const [id, setId] = useState<Identity | null>(null);
  // CUI-2: a FOURTH state — "/api/me could not be reached at all". Distinct
  // from role "none": we do not know the role, so the not-entitled screen
  // would be a lie (and, being terminal, would strand the operator there).
  const [unreachable, setUnreachable] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const loc = useLocation();
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    whoami().then(
      (v) => { if (alive) { setUnreachable(null); setId(v); } },
      (e: unknown) => {
        if (!alive) return;
        setUnreachable(e instanceof Error ? e.message : String(e));
        if (attempt + 1 < WHOAMI_TRIES) {
          timer = setTimeout(() => { if (alive) setAttempt((a) => a + 1); }, backoffMs(attempt));
        }
      },
    );
    return () => { alive = false; if (timer !== undefined) clearTimeout(timer); };
  }, [attempt]);

  if (id === null && unreachable !== null) {
    const retrying = attempt + 1 < WHOAMI_TRIES;
    return (
      <div className="content" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
        <div className="card" style={{ maxWidth: 480, textAlign: "center" }} role="alert">
          <h1 style={{ marginTop: 0 }}>Can&rsquo;t reach the LLM Manager</h1>
          <p className="lede" style={{ margin: "0 auto 16px" }}>
            The console could not load your identity from <code>/api/me</code>. This is a
            connection problem, not a permissions one — the manager may still be starting.
          </p>
          <p className="muted" style={{ marginBottom: 16 }}>{unreachable}</p>
          <button
            className="btn primary"
            onClick={() => { setUnreachable(null); setAttempt((a) => a + 1); }}
          >
            {retrying ? "Retrying…" : "Retry"}
          </button>
        </div>
      </div>
    );
  }

  if (id === null) {
    // Neutral — no sidebar, no nav, no "no access" flash either way.
    return <div className="content" aria-busy="true" aria-label="Loading…" />;
  }

  // #843 p1: the whole point of #314's Role.NONE — a signed-in identity whose
  // groups match no LLM-Manager tier gets an explicit answer instead of the
  // full shell (which would then 403 panel-by-panel and read as broken).
  if (id.role === "none") {
    return <NoAccess identity={id} />;
  }

  const initial = (id.username || "?").trim().charAt(0).toUpperCase();
  const nav = NAV.filter((n) => "divider" in n || hasCap(id.capabilities, n.cap));

  return (
    <IdentityCtx.Provider value={id}>
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          {/* the razzfazz.ai spark — the red lightning bolt from the brand favicon */}
          <svg className="brand-spark" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path d="M13.6 1.4a.7.7 0 0 0-1.24-.5L3.3 12.3a.7.7 0 0 0 .55 1.14h5.02l-1.3 8.2a.7.7 0 0 0 1.25.52l9.06-11.4a.7.7 0 0 0-.55-1.14h-5.02l1.29-8.22Z" />
          </svg>
          <span className="brand-name">rzfz.ai</span>
        </div>
        <div className="brand-sub">LLM Manager</div>
        <nav>
          {nav.map((n, i) => ("divider" in n ? (
            <div key={`div-${i}`} className="nav-divider" aria-hidden />
          ) : (
            <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "nav active" : "nav")}>
              <span className="nav-ico" aria-hidden>{n.ico}</span>
              <span>{n.label}</span>
            </NavLink>
          )))}
        </nav>
        <div className="sidebar-foot">
          Sovereign LLM control plane.
          <br />
          <a href="/docs">API reference →</a>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="crumb">{crumbFor(loc.pathname)}</span>
          <span className="who">
            {id.username ? (
              <>
                <span className="avatar">{initial}</span>
                {id.username}
              </>
            ) : (
              "SSO session"
            )}
          </span>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
      <Toaster />
    </div>
    </IdentityCtx.Provider>
  );
}
