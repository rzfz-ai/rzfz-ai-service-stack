<!-- SPDX-License-Identifier: BUSL-1.1 -->
# LLM Manager UI (`llm-manager-ui`)

Operator console for the razzfazz.ai **LLM Manager** (#254 Phase 3, epic #260).
Standalone **React + Vite + TypeScript** SPA, served by nginx at
`llm-manager.<domain>` behind Caddy + Authentik SSO. Its XHRs to `/api/*` ride
the Authentik session cookie; the manager's `require_admin` authorizes them.

Screens: Dashboard · API Keys & Cost-Centers · Fleet/Nodes · Models &
Deployments · Usage/Billing · Settings. Design:
`internal-docs`.

## Dev
```
npm install
npm run dev        # vite dev server; proxies /api,/v1 → 127.0.0.1:8091
npm run build      # tsc --noEmit + vite build → dist/
```
Ingress/auth (Caddy route + Authentik ProxyProvider for llm-manager.<domain>)
lands in S0b; screens fill in S1–S5.
