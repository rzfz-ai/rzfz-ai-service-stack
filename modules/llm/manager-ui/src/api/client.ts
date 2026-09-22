// Typed client for the LLM Manager API. All calls are same-origin (/api/*):
// the SPA is served at llm-manager.<domain> behind Caddy + Authentik, so XHRs
// ride the SSO session cookie and the manager's require_admin authorizes them.
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

// #346: one reload per window. An expired session makes every 5s poll fail at
// once — the first detection reloads (a full navigation runs the SSO round-trip),
// the rest must not queue further reloads behind it.
let authReloadAt = 0;

// CUI-3: FastAPI's RequestValidationError puts an ARRAY of {loc, msg, type}
// into `detail`; `String(detail)` on that renders "[object Object]", which is
// what every 422 in the console used to show the operator. Render the field
// path + message instead, and JSON-stringify any other non-string shape rather
// than falling through to Object#toString.
export function describeDetail(detail: unknown): string {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((e) => {
      if (typeof e === "string") return e;
      const it = e as { loc?: unknown; msg?: unknown };
      const msg = typeof it?.msg === "string" ? it.msg : JSON.stringify(e);
      const loc = Array.isArray(it?.loc)
        // drop the leading "body"/"query"/"path" frame — the operator cares
        // about the field, not which part of the request it arrived in
        ? (it.loc as unknown[]).slice(1).map(String).join(".")
        : "";
      return loc ? `${loc}: ${msg}` : msg;
    });
    return parts.filter(Boolean).join("; ");
  }
  const asObj = detail as { msg?: unknown; message?: unknown };
  if (typeof asObj.msg === "string") return asObj.msg;
  if (typeof asObj.message === "string") return asObj.message;
  try { return JSON.stringify(detail); } catch { return String(detail); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      const ct = res.headers.get("content-type") || "";
      // res.ok gate (review, #564): a followed SSO redirect always lands 200 —
      // a NON-ok HTML body is an error page (Caddy 502, backend down), and
      // reloading on those loops the console instead of surfacing the error.
      if (res.ok && (res.redirected || ct.includes("text/html"))) {
        // #346: the Authentik session lapsed. Caddy's forward_auth answers 302
        // to the login flow, fetch follows it transparently, and the login page
        // arrives as a 200 with HTML — JSON.parse threw on "<!doctype". Reload
        // so the browser runs the SSO round-trip and comes back authenticated.
        if (Date.now() - authReloadAt > 15_000) {
          authReloadAt = Date.now();
          window.location.reload();
        }
        throw new ApiError(401, "session expired — re-authenticating…");
      }
      // any other non-JSON body: surface it as the consistent type every page
      // already switches on, never as a raw SyntaxError
      throw new ApiError(res.status, `non-JSON response: ${text.slice(0, 200)}`);
    }
  }
  const d = data as { detail?: unknown; error?: { message?: unknown } } | null;
  if (!res.ok) {
    const detail = (d && (d.detail ?? d.error?.message)) ?? res.statusText;
    throw new ApiError(res.status, describeDetail(detail) || res.statusText);
  }
  return data as T;
}

// CUI-12: repo ids and HF repo_ids are third-party strings that carry a
// structural slash. Encode each segment (so `?`/`#`/space in a name cannot
// truncate the URL or eat the query string) while keeping the slash literal —
// the backend routes declare these as `:path` params.
export function encodePathSegments(p: string): string {
  return p.split("/").map(encodeURIComponent).join("/");
}

export const api = {
  get: <T>(path: string) => req<T>("GET", path),
  post: <T>(path: string, body?: unknown) => req<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => req<T>("PATCH", path, body),
  del: <T>(path: string) => req<T>("DELETE", path),
};

// --- shapes (mirror the FastAPI handlers) -----------------------------------
// #843 p1: mirrors app/authz.py::Role — kept a plain string union (not an
// enum) so an unrecognised value from a future server still type-checks and
// simply fails the `=== "none"` checks the SPA gates on (fail-closed, never a
// TS error hiding the real runtime shape).
export type Role = "none" | "user" | "admin" | "superadmin";

export interface Identity {
  username: string | null;
  groups: string[];
  // #843 p1: the resolved RBAC tier + the 9-key capability matrix from
  // app/authz.py::capabilities_for — lets the SPA gate its nav/actions off
  // ONE server-authoritative answer instead of re-deriving group membership.
  role: Role;
  capabilities: Record<string, boolean>;
  // #1518 (E5): `vllm_enabled` is gone — the vLLM path it gated no longer
  // exists. GGUF/llama.cpp is the only architecture the fleet serves.
}

export interface CostCenter {
  id: string;
  name: string;
  team: string | null;
}

export interface ApiKeyRow {
  id: string;
  key_prefix: string;
  cost_center_id: string;
  status: string;
  allowed_models: string[];
  rpm_limit: number | null;
  tpm_limit: number | null;
  max_budget_tokens: number | null;
  budget_duration_seconds: number | null;
  created_at: string | null;
  expires_at: string | null;
}

export interface NewKey {
  id: string;
  key: string; // plaintext — shown ONCE
  key_prefix: string;
  cost_center_id: string;
  status: string;
}

export interface WorkerInstance {
  id?: string;
  model_name: string;
  endpoint: string | null;
  container: string | null;
  status: string;
  detail?: string | null;   // #287 phase detail (pulling %, fail reason)
}
export interface WorkerRow {
  id: string;
  name: string;
  display_name: string | null;   // #284 manager-owned rename; null = show `name`
  address: string;
  hardware: string | null;
  engine: string | null;
  stack_version: string | null;
  // #1951: never null — the manager synthesises "not-reported" when the node
  // sent no source, so the console always has a state to name.
  stack_version_source: string | null;
  engine_version_why: string | null;
  engine_version: string | null;
  advertise_addr: string | null;
  mem_total_gb?: number | null;    // #295 host RAM (fits-check fallback)
  vram_total_gb?: number | null;   // #295 GPU VRAM (fits-check budget)
  // #328 which label funds the VRAM admission budget. null basis = admission is
  // silently INERT for this worker — the console must say so, not hide it.
  admission_basis?: string | null;
  admission_budget_gb?: number | null;
  // live utilization snapshot for the dashboard load graph (empty when a
  // worker doesn't report it — e.g. external Ollama endpoints).
  metrics?: { vram_used_gb?: number; load?: number; gpu_util?: number; mem_used_gb?: number; ncpu?: number };
  external?: boolean;
  status: string;
  last_heartbeat: string | null;
  instances: WorkerInstance[];
}

// #1598 live utilisation feed. `at` is the SERVER stamp for the sample, and the
// console appends a point only when it moves — that is what stops the chart
// from turning its own poll rate into data the fleet never reported.
export interface WorkerMetricRow {
  id: string;
  at: number | null;
  gpu_util?: number | null;
  vram_used_gb?: number | null;
  mem_used_gb?: number | null;
  load?: number | null;
  ncpu?: number | null;
  vram_total_gb?: number | null;
  mem_total_gb?: number | null;
}

export interface UsageSeriesPoint {
  bucket: string;          // ISO timestamp of the bucket start
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;    // input + output
  events: number;          // request count
}

// #991 cost-control analytics — ONE window, six cuts. Tokens/requests only:
// `usage_events` has no cost column by design, so nothing here is currency.
export interface UsageTotals {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  events: number;
  estimated_events: number;
}
export interface UsageRanked {
  total_tokens: number;
  events: number;
  share_pct: number;
}
export interface UsageAnalytics {
  window: {
    from: string; to: string; previous_from: string;
    days: number; bucket: "hour" | "day"; points: number; tz: string;
  };
  filters: { cost_center: string | null; model: string | null };
  totals: UsageTotals;
  previous: UsageTotals;
  // null = no baseline (the previous window had nothing) — NOT 0, NOT +100%.
  delta_pct: { total_tokens: number | null; events: number | null };
  series: UsageSeriesPoint[];      // zero-filled server-side across the window
  top_models: (UsageRanked & { model: string; input_tokens: number; output_tokens: number })[];
  by_cost_center: (UsageRanked & { cost_center_id: string | null; name: string | null; team: string | null })[];
  by_key: (UsageRanked & {
    api_key_id: string | null; key_prefix: string | null;
    owner_username: string | null; cost_center: string | null;
  })[];
  // weekday x hour-of-day grids, Monday-first rows, 24 columns, UTC.
  heatmap: {
    weekdays: string[];
    tokens: number[][];
    events: number[][];
    max_tokens: number;
    max_events: number;
  };
}

// #295 HuggingFace model browser
export interface HfSearchResult {
  id: string;
  downloads: number | null;
  likes: number | null;
  last_modified: string | null;
  gated: boolean;
  pipeline_tag: string | null;
  library?: string | null;
  trending_score?: number | null;
}
export interface HfFit { worker: string; mem_gb: number; source: string | null; ok: boolean; }
export interface HfQuant { label: string; files: string[]; size_gb: number; parts: number; fits: HfFit[]; }
export interface HfRepo {
  repo_id: string;
  downloads: number | null;
  likes: number | null;
  gguf: boolean;
  quants: HfQuant[];
  workers: { name: string; mem_gb: number; source: string | null }[];
  readme_md: string;
  recommended_params?: Record<string, unknown>;   // #296 scraped from the model card
  arch?: { layers: number; kv_heads: number; head_dim: number } | null;  // #296 KV-cache dims
  note?: string;
}

export interface RegistryModel {
  repository: string;
  tag: string;
  size_bytes: number;          // what THIS tag weighs (config + layers)
  digest?: string;
  files: { name: string; digest: string | null; size: number }[];
  config_digest?: string | null;  // #1179 — so the console can dedupe per repo
  config_size?: number;
}
export interface RegistryModels {
  base: string;
  available: boolean;
  models: RegistryModel[];
  total_bytes: number;           // #1179 — over UNIQUE blob digests (what Zot physically holds)
  total_bytes_tagged?: number;   // #1179 — naive per-tag sum, for transparency
  truncated?: boolean;
  truncated_reason?: string;
  error?: string;
}

export interface EnrollToken {
  worker_name: string;
  enroll_token: string;
  expires_at: number;
  manager_url: string;
  ca_fingerprint?: string | null;
  join_command: string;
  // #1059: the checkout-free one-liner for a BLANK box — no repo, no rzfz.
  // Optional because a master that predates #1059 does not return it, and the
  // console must render against an older manager rather than blank the drawer.
  install_command?: string;
}

export interface CatalogEntry {
  name: string;
  display: string;
  description: string;
  task: "chat" | "embed" | "rerank";
  repo_id: string;
  filename: string | null;  // #1518: always set now (GGUF); null was the retired vLLM repo-dir deploy
  // #1256 vision projector companion, RESOLVED by the manager from the model
  // manifest (the single declaration). null = this model has none. The console
  // never carries the filename itself — that copy is what diverged.
  mmproj: string | null;
  engine: string;
  hardware: string[];
  recommended: boolean;
  params?: Record<string, unknown>;   // #296 curated house params (pre-fill in the editor)
}

export interface RegistryCatalog {
  base: string;
  available: boolean;
  repositories: { repository: string; tags: string[] }[];
  error?: string;
}

export interface DeploymentInstanceRow {
  id: string;
  worker: string | null;
  worker_id: string | null;
  endpoint: string | null;
  container: string | null;
  status: string;
  detail?: string | null;   // #287 phase detail (pulling %, fail reason)
  started_at: string | null;
  params_effective: Record<string, unknown>;
  hardware?: string | null; // amd | nvidia | cpu | external (the worker's class)
  device?: string;          // GPU | CPU | — (display)
  arch?: string;            // Vulkan | CUDA | Ollama | CPU | vLLM (runtime family)
  external?: boolean;       // #318 external backend — no container/worker-agent to manage
}

// #549 R2 — node-side runner image inventory (result of list_runner_images)
export interface RunnerImageInfo {
  image: string;
  id: string;
  origin: "registry" | "local-build";
  size_bytes: number;
  size_gb: number;
}
export interface RunnerInventory {
  registry: string;
  // #1860 — WHICH source answered. `fallback` means neither
  // LLM_WORKER_RUNNER_REGISTRY nor LLM_HUB_DOMAIN is set on the node and it
  // landed on the compose-network name, which the host's docker daemon cannot
  // resolve: the node serves its models and can pull no runner at all.
  // Optional: a node on an older agent build reports no source, and "unknown"
  // must not be rendered as "broken".
  registry_source?: "explicit" | "hub-domain" | "fallback";
  runners: RunnerImageInfo[];
  count: number;
}

// #549 R3 — one runner-upgrade sequence (deploying → relaunching → done|failed|rolled_back)
export interface RunnerUpgradeRow {
  upgrade_id: string;
  worker_id: string;
  image: string;
  state: string;
  error: string | null;
  captured: unknown[];
  deadline: string | null;
}

export interface NodeCommandRow {
  id: string;
  worker_id: string;
  kind: string;
  args: Record<string, unknown>;
  status: string;
  result: Record<string, unknown> | null;
  created_at: string | null;
  // #1183 stale-while-revalidate (only when the enqueue asked `revalidate`):
  // the node's last completed answer for the same (worker, kind, args), to
  // render immediately while this row (the refresh) is on its way.
  stale?: boolean;
  stale_result?: Record<string, unknown> | null;
  stale_finished_at?: string | null;
  coalesced?: boolean;   // reused a refresh already in flight — nothing new was enqueued
}
export interface DeploymentRow {
  id: string;
  model_name: string;
  display_name: string | null;  // #284 console-only label; null = show model_name
  engine: string;
  task: string;                 // chat | embed | rerank (serve mode)
  status: string;               // desired state (active | pending | removing)
  health: string;               // #286 ACTUAL: ready|degraded|failed|loading|pending
  replicas: number;
  ready_instances: number;
  params: Record<string, unknown>;
  tags: string[];                       // #296 operator tags
  runner_image?: string | null;         // #549 R1 runner pin (null = node default)
  device?: string;                      // GPU | CPU | — (rolled up from placement)
  arch?: string;                        // Vulkan | CUDA | Ollama | CPU | vLLM
  instances: DeploymentInstanceRow[];
}

// #835 — GET /api/inventory: per-deployment registry/on-disk cache state
// (#307 S3), extended with everything a faithful "redeploy this cached model
// onto worker X" call needs (files/task/params/tags/est_gb/runner_image),
// plus a basename→provenance map for the Fleet drawer's on-disk cache view.
export interface FleetInventoryModel {
  deployment_id: string;
  model_name: string;
  served_model: string;
  hf_repo: string | null;
  files: string[];
  task: string;
  params: Record<string, unknown>;
  tags: string[];
  est_gb: number | null;
  runner_image: string | null;
  registry_repo: string;
  registry_tag: string;
  in_registry: boolean;
  workers: { worker_id: string; worker: string; cached: boolean }[];
}
// A basename maps to `{ambiguous:true}` ONLY when two DIFFERENT hf_repos both
// claim it — never a guessed model/repo (see `_file_provenance_map`, manager
// app/api/inventory.py). A basename absent from this map is simply unknown.
export type FleetFileProvenance = Record<
  string,
  { ambiguous: true } | {
    ambiguous: false;
    deployment_id: string;
    model_name: string;
    served_model: string;
    hf_repo: string | null;
  }
>;
// #837 item 3: cached weights on a worker that NO deployment row claims —
// undeployed-but-still-on-disk (the deployment row is dropped outright by
// DELETE /api/deployments/{id}), which is the primary "free disk space" case
// and was invisible in `models`. The manager resolves the join
// (`_unreferenced_cached`); the console reads it, it does not re-derive it.
export interface FleetUnreferencedCache {
  worker_id: string;
  worker: string;
  mount: string | null;
  files: { name: string; kind: string | null; size_gb: number | null }[];
  count: number;
  total_gb: number;
}
export interface FleetInventory {
  models: FleetInventoryModel[];
  file_provenance: FleetFileProvenance;
  unreferenced: FleetUnreferencedCache[];
}

export interface ModelStat { model: string; requests: number; avg_latency_ms: number; }
export interface ManagerStats {
  requests_total: number;
  avg_latency_ms: number;               // mean added proxy latency, ms (since start)
  failover_total: number;
  meter_rejected_total: number;
  entitlement_rejected_total: number;
  models: ModelStat[];
}

export interface UsageRow {
  api_key_id?: string;
  key_prefix?: string | null;
  model?: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  events: number;
  bucket?: string;
}

export interface EntitlementStatus {
  state: "active" | "expired" | "suspended" | "none";
  entitled: boolean;
  reason: string;
  plan: string | null;
  seats: number | null;
  valid_until: string | null;
  days_remaining: number | null;
  subscription_number: string | null;
  mode: "report" | "enforce";
  enforced: boolean;
}

export interface RollupTotal {
  cost_center: string | null;
  api_key: string | null;
  input: number;
  output: number;
  cached: number;
}
export interface Rollup {
  month: string;
  totals: RollupTotal[];
  signed: boolean;
  signature: string | null;
}

export interface ManagerSettings {
  metering_mode: string;
  metering_mode_env: string;
  metering_mode_source: "env" | "override";
  metering_modes: string[];
  node_registration_enabled: boolean;
  rollup_signing_enabled: boolean;
  litellm_base_url: string;
  key_prefix: string;
  router_config_path: string;
  admin_groups: string[];
  // #284 Phase 4: console-access tiers (read-only; the boundary is .env —
  // group membership is never editable through this console).
  superadmin_groups: string[];
  llm_admin_groups: string[];
  llm_user_groups: string[];
}

export interface RouterConfig {
  model_list?: { model_name: string; litellm_params?: Record<string, unknown> }[];
}

// --- endpoint helpers -------------------------------------------------------
export const endpoints = {
  me: () => api.get<Identity>("/api/me"),
  settings: () => api.get<ManagerSettings>("/api/settings"),
  patchSettings: (body: { metering_mode?: string }) =>
    api.patch<ManagerSettings>("/api/settings", body),

  costCenters: () => api.get<CostCenter[]>("/api/cost-centers"),
  createCostCenter: (body: { name: string; team?: string }) =>
    api.post<CostCenter>("/api/cost-centers", body),

  keys: () => api.get<ApiKeyRow[]>("/api/keys"),
  createKey: (body: Record<string, unknown>) => api.post<NewKey>("/api/keys", body),
  rotateKey: (id: string) => api.post<NewKey>(`/api/keys/${id}/rotate`),
  disableKey: (id: string) => api.post<{ id: string; status: string }>(`/api/keys/${id}/disable`),

  workers: () => api.get<WorkerRow[]>("/api/workers"),
  // #1598: the live chart's own feed — five numbers, two denominators and the
  // server stamp saying when the node measured them. Polled once a second,
  // which `/api/workers` (per-worker instance join, admission math twice) must
  // never be; see the route's docstring.
  workerMetrics: () => api.get<WorkerMetricRow[]>("/api/workers/metrics"),
  // #307 register a pre-existing OpenAI/Ollama endpoint as an external backend
  addExternalBackend: (body: Record<string, unknown>) =>
    api.post<{ name: string; endpoint: string; models: string[]; status: string }>("/api/workers/external", body),
  deployments: () => api.get<DeploymentRow[]>("/api/deployments"),
  // #835: reused (not a new route) for the Fleet drawer's on-disk cache
  // provenance + redeploy-from-cache shortcut — see FleetInventory above.
  inventory: () => api.get<FleetInventory>("/api/inventory"),
  stats: () => api.get<ManagerStats>("/api/stats"),
  tags: () => api.get<string[]>("/api/tags"),   // #296 central tag catalog
  // #307 master model cache
  registryModels: () => api.get<RegistryModels>("/api/registry/models"),
  mirrorModel: (body: Record<string, unknown>) =>
    api.post<{ command_id: string; worker: string; repo: string; tag: string; status: string }>("/api/registry/mirror", body),
  evictModel: (repo: string, tag: string) =>
    api.del<{ evicted: string; status: string }>(`/api/registry/models/${encodePathSegments(repo)}?tag=${encodeURIComponent(tag)}`),

  // #262 worker-add: mint a short-lived enrollment token + join command an
  // operator runs on the target box to self-join it as a worker.
  enrollToken: (body: { name: string; ttl_seconds?: number }) =>
    api.post<EnrollToken>("/api/workers/enroll-token", body),

  // #264 deployable-model catalog (curated). Optional task/hardware filter.
  catalog: (q: { task?: string; hardware?: string } = {}) => {
    const p = new URLSearchParams();
    if (q.task) p.set("task", q.task);
    if (q.hardware) p.set("hardware", q.hardware);
    const qs = p.toString();
    return api.get<CatalogEntry[]>(`/api/catalog${qs ? `?${qs}` : ""}`);
  },
  // #289 central Zot registry contents (what's mirrored for offline/fleet).
  registryCatalog: () => api.get<RegistryCatalog>("/api/registry/catalog"),

  // #295/#296 HuggingFace model browser (online/proxied only; 503 offline).
  hfSearch: (q: string, opts: { sort?: string; format?: string; task?: string } = {}) => {
    const p = new URLSearchParams({ q });
    if (opts.sort) p.set("sort", opts.sort);
    if (opts.format) p.set("format", opts.format);
    if (opts.task) p.set("task", opts.task);
    return api.get<{ query: string; sort: string; format: string; task: string | null; results: HfSearchResult[] }>(`/api/hf/search?${p}`);
  },
  // repo_id carries a slash (owner/name) — the backend route is a :path param,
  // so the slash stays structural and only the segments are encoded (CUI-12).
  hfRepo: (repoId: string) => api.get<HfRepo>(`/api/hf/repo/${encodePathSegments(repoId)}`),

  // #261 control channel + #263 deploy
  enqueueCommand: (workerId: string, body: { kind: string; args?: Record<string, unknown>; revalidate?: boolean }) =>
    api.post<NodeCommandRow>(`/api/workers/${workerId}/commands`, body),
  getCommand: (id: string) => api.get<NodeCommandRow>(`/api/commands/${id}`),
  // #261-C2 drain: manager-driven — no command kind involved
  drainWorker: (workerId: string) => api.post<{ status: string; instances_unloaded: number }>(`/api/workers/${workerId}/drain`),
  undrainWorker: (workerId: string) => api.post<{ status: string }>(`/api/workers/${workerId}/undrain`),
  // #594 retire: forget a drained/stale/external worker (refused for ready+fresh)
  removeWorker: (workerId: string) =>
    api.del<{ status: string; worker: string; instances_removed: number }>(`/api/workers/${workerId}`),
  // #284 rename: set a manager-owned display label (node registration keeps `name`)
  renameWorker: (workerId: string, displayName: string) =>
    api.patch<{ id: string; name: string; display_name: string | null }>(
      `/api/workers/${workerId}`, { display_name: displayName }),
  // #306 delete half: free disk by removing one cached weight file. Same
  // enqueue-then-poll shape as the runner routes below — the dedicated route
  // validates `name` synchronously (422 on a bad path) and enqueues; the node
  // does the actual delete and refuses (command status "failed", result.error)
  // when the file backs a currently-loaded deployment.
  deleteDiskModel: (workerId: string, name: string) =>
    api.post<NodeCommandRow>(`/api/workers/${workerId}/disk-models/delete`, { name }),
  // #549 R2 — these enqueue server-side and return the command row: poll with waitCommand()
  deployRunner: (workerId: string, image: string) => api.post<NodeCommandRow>(`/api/workers/${workerId}/runners`, { image }),
  removeRunner: (workerId: string, image: string) => api.post<NodeCommandRow>(`/api/workers/${workerId}/runners/remove`, { image }),
  listRunnerImages: (workerId: string, opts?: { revalidate?: boolean }) =>
    api.post<NodeCommandRow>(`/api/workers/${workerId}/runners/list`, opts?.revalidate ? { revalidate: true } : undefined),
  // #549 R3 — upgrade state machine (event-driven server-side; poll the status row)
  upgradeRunner: (workerId: string, image: string) =>
    api.post<{ upgrade_id: string; state: string; captured_deployments: number }>(`/api/workers/${workerId}/upgrade-runner`, { image }),
  latestUpgrade: (workerId: string) => api.get<RunnerUpgradeRow>(`/api/workers/${workerId}/upgrade-runner`),
  upgradeStatus: (upgradeId: string) => api.get<RunnerUpgradeRow>(`/api/runner-upgrades/${upgradeId}`),
  rollbackUpgrade: (upgradeId: string) => api.post<{ state: string }>(`/api/runner-upgrades/${upgradeId}/rollback`),
  deploy: (body: Record<string, unknown>) =>
    api.post<{ deployment_id: string; worker: string; instance_id: string; command_id: string; status: string }>(
      "/api/deployments", body),
  undeploy: (id: string) => api.del<{ status: string; unload_commands: number }>(`/api/deployments/${id}`),
  // #566 (C3): relaunch the running engines with the CURRENT desired state
  applyParams: (id: string) => api.post<{ status: string; relaunched: number }>(`/api/deployments/${id}/apply-params`),
  // #312 pause/resume without losing the deployment
  stopDeployment: (id: string) => api.post<{ status: string; unload_commands: number }>(`/api/deployments/${id}/stop`),
  startDeployment: (id: string) => api.post<{ status: string; scheduled_instances: number }>(`/api/deployments/${id}/start`),
  // #284: edit desired state (replicas / task / params / tags / display_name).
  // model_name itself is never offered here — it stays the immutable
  // client-facing LiteLLM routing id; display_name is the console-only alias
  // that registration never clobbers.
  patchDeployment: (id: string, body: Record<string, unknown>) =>
    api.patch<{ deployment_id: string; replicas: number; task: string; params: Record<string, unknown>; tags: string[]; display_name: string | null; status: string }>(
      `/api/deployments/${id}`, body),
  // #304: move a deployment's running instance to a DIFFERENT worker,
  // post-deploy — undoes the "worker is fixed at deploy time" limitation
  // without an undeploy/redeploy round-trip. `worker_id` is the ONLY field
  // ReassignRequest requires (app/api/inventory.py); instance_id/force are
  // left to their server-side defaults for the single-instance UI case.
  reassignDeployment: (deploymentId: string, workerId: string) =>
    api.post<{ deployment_id: string; worker_id: string; status: string }>(
      `/api/deployments/${deploymentId}/reassign`, { worker_id: workerId }),

  usage: (q: { since_days?: number; bucket?: string; group?: "key" | "model" } = {}) => {
    const p = new URLSearchParams();
    if (q.since_days != null) p.set("since_days", String(q.since_days));
    if (q.bucket) p.set("bucket", q.bucket);
    if (q.group) p.set("group", q.group);
    const qs = p.toString();
    return api.get<UsageRow[]>(`/api/usage${qs ? `?${qs}` : ""}`);
  },
  // time-bucketed token/request series for the dashboard usage graph.
  usageSeries: (q: { from?: string; to?: string; bucket?: string; model?: string } = {}) => {
    const p = new URLSearchParams();
    if (q.from) p.set("from", q.from);
    if (q.to) p.set("to", q.to);
    if (q.bucket) p.set("bucket", q.bucket);
    if (q.model) p.set("model", q.model);
    const qs = p.toString();
    return api.get<UsageSeriesPoint[]>(`/api/usage/series${qs ? `?${qs}` : ""}`);
  },
  // #991: the whole cost page in one round-trip (totals + previous window +
  // series + top models + per-cost-centre/per-key split + activity grid).
  usageAnalytics: (q: { days?: number; bucket?: "hour" | "day"; cost_center?: string; model?: string; top?: number } = {}) => {
    const p = new URLSearchParams();
    if (q.days != null) p.set("days", String(q.days));
    if (q.bucket) p.set("bucket", q.bucket);
    if (q.cost_center) p.set("cost_center", q.cost_center);
    if (q.model) p.set("model", q.model);
    if (q.top != null) p.set("top", String(q.top));
    const qs = p.toString();
    return api.get<UsageAnalytics>(`/api/usage/analytics${qs ? `?${qs}` : ""}`);
  },
  rollup: (month: string) =>
    api.get<Rollup>(`/api/entitlement/rollup?month=${encodeURIComponent(month)}`),
  entitlementStatus: () => api.get<EntitlementStatus>("/api/entitlement/status"),

  routerConfig: () => api.get<RouterConfig>("/api/router/config"),
  rebuildRouter: () => api.post<{ written: string; model_list_size: number }>("/api/router/rebuild"),

  // playground (SSO-gated; the manager injects the internal router key)
  playgroundChat: (body: {
    model: string;
    messages: { role: string; content: string }[];
    max_tokens?: number;
    temperature?: number;
    top_p?: number;
    presence_penalty?: number;
    frequency_penalty?: number;
  }) => api.post<any>("/api/playground/chat", body),
  // CUI-14: the wire contract accepts a single string OR a batch — the only
  // caller (Playground) sends an array, and used to cast it away with `as any`.
  playgroundEmbeddings: (body: { model: string; input: string | string[] }) =>
    api.post<any>("/api/playground/embeddings", body),
  playgroundRerank: (body: { model: string; query: string; documents: string[] }) =>
    api.post<any>("/api/playground/rerank", body),
};

// #348 — command execution is asynchronous on the node side: a node claims
// commands once per report cycle (LLM_WORKER_REPORT_INTERVAL, default 30s; 4s only
// while an instance is transitional). A poll therefore needs a wall-clock
// deadline of at least two full cycles plus execution time; the old fixed
// 30x1s loop returned before the node had even seen the command, and the
// caller could not tell "not claimed yet" from "failed".
//
// Long-running kinds get their own budget. deploy_runner is an image pull
// (minutes on first pull); mirror_model / pull_artifact are #364 async kinds
// whose result arrives whenever the transfer ends.
const KIND_TIMEOUT_MS: Record<string, number> = {
  deploy_runner: 15 * 60_000,
  remove_runner: 3 * 60_000,
  list_runner_images: 2 * 60_000,
  mirror_model: 30 * 60_000,
  pull_artifact: 30 * 60_000,
};
// Two report cycles (worst-case claim latency after a just-missed cycle) plus
// execution headroom. Pinned against the node's interval default by
// tests/unit/consistency/test_ui_command_polling.py.
export const DEFAULT_COMMAND_TIMEOUT_MS = 90_000;

export interface RunCommandResult extends NodeCommandRow {
  // Deadline passed with the command still pending/claimed. NOT a failure:
  // the node may yet complete it — say "unconfirmed", not "failed".
  timed_out?: boolean;
}

export interface WaitCommandOpts {
  timeoutMs?: number;
  pollMs?: number;
  onUpdate?: (c: NodeCommandRow) => void; // fires on every poll — progress surfaces
  // #1183 (runCommand only): ask the manager for the node's last completed
  // answer for the same (worker, kind, args) — delivered through `onStale`
  // BEFORE polling starts — and let it reuse a refresh already in flight.
  revalidate?: boolean;
  onStale?: (result: Record<string, unknown>, finishedAt: string | null) => void;
  // CUI-17: cancellation channel. Without it a closed drawer / unmounted log
  // pane kept polling /api/commands/{id} until its (up to 30 min) deadline and
  // then wrote state into an unmounted component — React 18 warns about none of
  // that, so it accumulated silently. Callers pass an AbortController aborted
  // from their effect cleanup.
  signal?: AbortSignal;
}

// Poll an ALREADY-ENQUEUED command (e.g. from the /runners routes, which
// enqueue server-side and return the command row) until done/failed/deadline.
export async function waitCommand(
  cmd: NodeCommandRow,
  opts: WaitCommandOpts = {},
): Promise<RunCommandResult> {
  const timeoutMs = opts.timeoutMs ?? KIND_TIMEOUT_MS[cmd.kind] ?? DEFAULT_COMMAND_TIMEOUT_MS;
  // #1183: the status read is one cheap row lookup, and the node answers a
  // claimed command within a second or two of claiming it — poll every 1 s
  // (was 2 s), with the FIRST read early so a node mid-cycle is not made to
  // wait a whole interval for the console to notice.
  const pollMs = opts.pollMs ?? 1_000;
  let wait = Math.min(pollMs, 300);
  const deadline = Date.now() + timeoutMs;
  let last: NodeCommandRow = cmd;
  if (last.status === "done" || last.status === "failed") return last;
  while (Date.now() < deadline) {
    if (opts.signal?.aborted) return { ...last, timed_out: true };
    await new Promise((r) => setTimeout(r, Math.min(wait, Math.max(0, deadline - Date.now()))));
    wait = pollMs;
    if (opts.signal?.aborted) return { ...last, timed_out: true };
    try {
      last = await endpoints.getCommand(cmd.id);
    } catch (e) {
      // A DEFINITIVE 404 means the command row is gone (manager redeploy
      // mid-poll) — no amount of retrying brings it back; spinning to a 30min
      // deadline would hide that. Transient/connection errors keep polling.
      if (e instanceof ApiError && e.status === 404) return { ...last, timed_out: true };
      continue;
    }
    opts.onUpdate?.(last);
    if (last.status === "done" || last.status === "failed") return last;
  }
  return { ...last, timed_out: true };
}

// Enqueue a node command via the generic channel and poll to completion.
export async function runCommand(
  workerId: string,
  kind: string,
  args: Record<string, unknown> = {},
  opts: WaitCommandOpts = {},
): Promise<RunCommandResult> {
  const cmd = await endpoints.enqueueCommand(workerId, { kind, args, ...(opts.revalidate ? { revalidate: true } : {}) });
  // #1183: the stale answer arrives on the enqueue itself — hand it over
  // before the (possibly long) poll so the caller can paint it now.
  if (cmd.stale_result && opts.onStale) opts.onStale(cmd.stale_result, cmd.stale_finished_at ?? null);
  return waitCommand(cmd, opts);
}

// Identity for the shell. #843 p1: the fallback is FAIL-CLOSED — role "none"
// + no capabilities — so an *answered* non-2xx from /api/me renders the
// explicit no-access screen instead of a silently-blank console with a
// partial nav.
//
// CUI-2: but only a DEFINITIVE answer may do that. /api/me is
// `require_authenticated`: it answers 200 + role "none" for a signed-in but
// un-entitled operator, and 4xx only when the identity itself is rejected
// (no Caddy headers / not through the ingress). A 5xx or a transport failure
// (fetch rejects with a TypeError — DNS, TLS, container restarting) says
// NOTHING about entitlement; swallowing those stranded the operator on the
// full-screen "No access" page forever after one blip, because Shell calls
// this exactly once with no retry. Those rethrow so the caller can retry.
export async function whoami(): Promise<Identity> {
  try {
    return await endpoints.me();
  } catch (e) {
    if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
      return { username: null, groups: [], role: "none", capabilities: {} };
    }
    throw e;
  }
}

// --- #1184 playground chat streaming (SSE) ----------------------------------
// The chat path streams so a long generation is never cut by a TOTAL timeout —
// the backend bounds only the gap between chunks. Parsed here, not in the
// page, so the session-expiry (#346) and error-envelope rules `req()` applies
// hold for the streamed request too. `onDelta` receives the accumulated
// assistant text after every chunk; `signal` (an AbortController) is the Stop
// button — aborting closes the upstream generation on the manager side.
export interface PlaygroundStreamResult {
  content: string;
  reasoning: string;                                   // delta.reasoning_content, when the engine splits it
  usage: { prompt_tokens?: number; completion_tokens?: number } | null;
  finish_reason: string | null;
  aborted: boolean;                                    // Stop was pressed; `content` is partial
  error: string | null;                                // in-band failure after the stream started; `content` is partial
}

export async function streamPlaygroundChat(
  body: Parameters<typeof endpoints.playgroundChat>[0],
  onDelta: (content: string, reasoning: string) => void,
  signal: AbortSignal,
): Promise<PlaygroundStreamResult> {
  const res = await fetch("/api/playground/chat", {
    method: "POST",
    credentials: "same-origin",
    signal,
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({ ...body, stream: true }),
  });
  const ct = res.headers.get("content-type") || "";
  if (!ct.includes("text/event-stream")) {
    // Not a stream: a gate refusal (402/429/503), a router error handed back
    // with its status (#323), or the SSO login page (#346) — same rules as req().
    const text = await res.text();
    let data: { detail?: unknown; error?: { message?: unknown }; choices?: any[]; usage?: any } | null = null;
    if (text) {
      try { data = JSON.parse(text); } catch {
        if (res.ok && (res.redirected || ct.includes("text/html"))) {
          if (Date.now() - authReloadAt > 15_000) { authReloadAt = Date.now(); window.location.reload(); }
          throw new ApiError(401, "session expired — re-authenticating…");
        }
        throw new ApiError(res.status, `non-JSON response: ${text.slice(0, 200)}`);
      }
    }
    if (!res.ok) {
      const detail = (data && (data.detail ?? data.error?.message)) ?? res.statusText;
      throw new ApiError(res.status, describeDetail(detail) || res.statusText);
    }
    // a 200 JSON body on the stream path (an engine that ignored `stream`)
    const content: string = data?.choices?.[0]?.message?.content ?? "";
    onDelta(content, "");
    return { content, reasoning: "", usage: data?.usage ?? null,
             finish_reason: data?.choices?.[0]?.finish_reason ?? null, aborted: false, error: null };
  }
  if (!res.body) throw new ApiError(res.status, "this browser cannot read a streamed response");

  const out: PlaygroundStreamResult = { content: "", reasoning: "", usage: null, finish_reason: null, aborted: false, error: null };
  const handle = (payload: string) => {
    let obj: any;
    try { obj = JSON.parse(payload); } catch { return; }
    if (!obj || typeof obj !== "object") return;
    if (obj.error) {
      // the manager's in-band failure event (idle engine, crashed runner) or a
      // router error object emitted mid-stream — keep what we have, say why
      out.error = describeDetail(obj.error?.message ?? obj.error) || "stream interrupted";
      return;
    }
    if (obj.usage) out.usage = obj.usage;
    for (const ch of obj.choices ?? []) {
      const d = ch?.delta ?? {};
      if (typeof d.content === "string") out.content += d.content;
      if (typeof d.reasoning_content === "string") out.reasoning += d.reasoning_content;
      if (ch?.finish_reason) out.finish_reason = ch.finish_reason;
    }
    onDelta(out.content, out.reasoning);
  };

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const ev = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of ev.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const p = line.slice(5).trim();
          if (p && p !== "[DONE]") handle(p);
        }
      }
    }
  } catch (e) {
    if (signal.aborted || (e as { name?: string })?.name === "AbortError") out.aborted = true;
    else throw e;
  } finally {
    void reader.cancel().catch(() => undefined);
  }
  return out;
}
