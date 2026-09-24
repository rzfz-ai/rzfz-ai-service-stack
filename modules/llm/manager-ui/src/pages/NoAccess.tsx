// #843 p1: the explicit "you don't have access" screen. Before this, a
// signed-in identity whose groups matched no LLM-Manager tier (Role.NONE)
// still got the full shell — every panel then 403'd against the manager API
// one call at a time, and the console read as silently broken rather than
// "you're not entitled". Shell.tsx renders THIS instead of the sidebar +
// <Outlet/> whenever `role === "none"` (see Shell.tsx's gate, #843 p1).
//
// Cosmetic only: the real gate is server-side (`require_role` 403s on every
// management route + `require_authenticated`'s Role.NONE for /api/me). This
// page exists purely so the human sees an answer instead of a blank console.
import type { Identity } from "../api/client";

export function NoAccess({ identity }: { identity: Identity }) {
  return (
    <div className="content" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
      <div className="card" style={{ maxWidth: 480, textAlign: "center" }}>
        <h1 style={{ marginTop: 0 }}>No access to the LLM Manager</h1>
        <p className="lede" style={{ margin: "0 auto 16px" }}>
          {identity.username ? (
            <>Signed in as <strong>{identity.username}</strong>, but this account isn&rsquo;t a member of an LLM Manager group yet.</>
          ) : (
            "You're signed in, but this account isn't a member of an LLM Manager group yet."
          )}
        </p>
        <p className="muted">
          Ask an administrator to add you to <span className="chip">razzfazz.ai LLM Users</span> (playground,
          your own usage, self-issued API keys) or <span className="chip">razzfazz.ai LLM Admins</span> (deploy
          models, issue keys for anyone, view all usage) in Authentik, then reload this page.
        </p>
      </div>
    </div>
  );
}
