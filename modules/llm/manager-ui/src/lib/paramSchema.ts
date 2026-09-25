// #296 Phase C — engine-aware structured parameter schema for the model editor.
// The editor renders these grouped fields (no raw JSON required) and merges
// defaults: schema default ← house default ← model-card recommendation (new
// deploy) or the existing deployment's params (edit). Fields the operator most
// often touches — temperature, ctx size, kv-cache, penalties, min_p, parallel —
// are first-class; everything else stays reachable via "Advanced (raw JSON)".

export type ParamField = {
  key: string;
  label: string;
  type: "number" | "select" | "bool";
  default: number | string | boolean;
  hint?: string;
  options?: (string | number)[];
  step?: number;
  min?: number;
  max?: number;
};
export type ParamGroup = { title: string; note?: string; fields: ParamField[] };

const SAMPLING_LLAMA: ParamField[] = [
  { key: "temperature", label: "Temperature", type: "number", default: 0.7, step: 0.05, min: 0, max: 2 },
  { key: "top_p", label: "Top-p", type: "number", default: 0.9, step: 0.05, min: 0, max: 1 },
  { key: "top_k", label: "Top-k", type: "number", default: 40, step: 1, min: 0 },
  { key: "min_p", label: "Min-p", type: "number", default: 0.05, step: 0.01, min: 0, max: 1 },
  { key: "repeat_penalty", label: "Repeat penalty", type: "number", default: 1.1, step: 0.05, min: 0, max: 3 },
  { key: "presence_penalty", label: "Presence penalty", type: "number", default: 0, step: 0.1, min: -2, max: 2 },
  { key: "frequency_penalty", label: "Frequency penalty", type: "number", default: 0, step: 0.1, min: -2, max: 2 },
];

export const PARAM_SCHEMA: Record<string, ParamGroup[]> = {
  llamacpp: [
    { title: "Sampling", fields: SAMPLING_LLAMA },
    {
      title: "Context & cache",
      fields: [
        { key: "ctx_size", label: "Context size", type: "number", default: 8192, step: 1024, min: 512, hint: "n_ctx — tokens" },
        { key: "n_parallel", label: "Parallel slots", type: "number", default: 1, step: 1, min: 1, hint: "concurrent requests" },
        { key: "cache_type_k", label: "KV cache (K)", type: "select", default: "f16", options: ["f16", "q8_0", "q4_0"] },
        { key: "cache_type_v", label: "KV cache (V)", type: "select", default: "f16", options: ["f16", "q8_0", "q4_0"] },
        { key: "flash_attn", label: "Flash attention", type: "bool", default: true },
      ],
    },
    {
      title: "Engine",
      fields: [
        { key: "n_gpu_layers", label: "GPU layers (ngl)", type: "number", default: 99, step: 1, min: 0, hint: "99 = offload all" },
        { key: "batch_size", label: "Batch size", type: "number", default: 2048, step: 256, min: 1 },
      ],
    },
  ],
  vllm: [
    {
      title: "Sampling",
      fields: [
        { key: "temperature", label: "Temperature", type: "number", default: 0.7, step: 0.05, min: 0, max: 2 },
        { key: "top_p", label: "Top-p", type: "number", default: 0.9, step: 0.05, min: 0, max: 1 },
        { key: "top_k", label: "Top-k", type: "number", default: -1, step: 1, hint: "-1 = disabled" },
        { key: "min_p", label: "Min-p", type: "number", default: 0.0, step: 0.01, min: 0, max: 1 },
        { key: "presence_penalty", label: "Presence penalty", type: "number", default: 0, step: 0.1, min: -2, max: 2 },
        { key: "frequency_penalty", label: "Frequency penalty", type: "number", default: 0, step: 0.1, min: -2, max: 2 },
      ],
    },
    {
      title: "Context & scheduling",
      fields: [
        { key: "max_model_len", label: "Context size", type: "number", default: 8192, step: 1024, min: 512, hint: "max_model_len" },
        { key: "max_num_seqs", label: "Parallel sequences", type: "number", default: 256, step: 1, min: 1 },
        { key: "kv_cache_dtype", label: "KV cache dtype", type: "select", default: "auto", options: ["auto", "fp8", "fp8_e5m2"] },
      ],
    },
    {
      title: "Engine",
      fields: [
        { key: "gpu_memory_utilization", label: "GPU memory util", type: "number", default: 0.9, step: 0.05, min: 0.1, max: 1 },
        { key: "tensor_parallel_size", label: "Tensor parallel", type: "number", default: 1, step: 1, min: 1, hint: "GPUs" },
        { key: "dtype", label: "dtype", type: "select", default: "auto", options: ["auto", "bfloat16", "float16"] },
        { key: "enforce_eager", label: "Enforce eager", type: "bool", default: false },
      ],
    },
  ],
  mlx: [
    {
      title: "Sampling",
      fields: [
        { key: "temperature", label: "Temperature", type: "number", default: 0.7, step: 0.05, min: 0, max: 2 },
        { key: "top_p", label: "Top-p", type: "number", default: 0.9, step: 0.05, min: 0, max: 1 },
        { key: "min_p", label: "Min-p", type: "number", default: 0.0, step: 0.01, min: 0, max: 1 },
      ],
    },
    {
      title: "Context",
      fields: [{ key: "max_tokens", label: "Max tokens", type: "number", default: 4096, step: 256, min: 1 }],
    },
  ],
};

// House nudges over the raw schema default, keyed by engine (kept small — the
// model card usually knows best; these are fallbacks when it doesn't).
export const HOUSE_DEFAULTS: Record<string, Record<string, number | string | boolean>> = {
  llamacpp: { repeat_penalty: 1.05, cache_type_k: "q8_0", cache_type_v: "q8_0" },
  vllm: {},
  mlx: {},
};

export function engineForWorker(hardware?: string | null, engine?: string | null): string {
  const e = (engine || "").toLowerCase();
  // `vllm` stays RECOGNIZED (a pre-#1518 deployment row can still name it, and
  // its editor must render rather than crash) but is never INFERRED any more.
  if (e === "vllm" || e === "mlx" || e === "llamacpp") return e;
  const h = (hardware || "").toLowerCase();
  // #1518 (E5): CUDA serves GGUF through llama.cpp — the compute capability
  // picks sm_120 vs 121a (#1517), not a different engine.
  if (h === "mac" || h === "metal") return "mlx";
  return "llamacpp";
}

export type FieldSource = "card" | "house" | "default" | "set";

// Resolve the effective value + its provenance for each field, merging in order
// schema-default → house → (recommended | existing). `existing` wins (it's what
// the operator already chose / the card recommends).
/**
 * #1604: one spelling of a parameter name.
 *
 * The catalog writes what the CLI wrote — `cli/lib-llm-manager-deploy.sh`
 * strips the two leading dashes and nothing else, so a deployment made from it
 * carries `ctx-size`. The console's schema declares `ctx_size`. The ENGINE
 * never noticed: `drivers/base.py::normalize_flag` pulls `_` → `-`, so both
 * land as the same CLI option — only the console could not see the hyphen form,
 * and `"ctx_size" in {"ctx-size": 8192}` is false.
 *
 * Both spellings are real and live side by side on a box today (measured on
 * 0.91), so this normalises rather than picking a winner.
 */
export function paramKey(k: string): string {
  return k.replace(/-/g, "_");
}

/** The value for `key`, whichever way the writer spelled it. */
function lookup(bag: Record<string, unknown>, key: string): { hit: boolean; value?: unknown } {
  if (key in bag) return { hit: true, value: bag[key] };
  const want = paramKey(key);
  for (const k of Object.keys(bag)) {
    if (paramKey(k) === want) return { hit: true, value: bag[k] };
  }
  return { hit: false };
}

export function resolveParams(
  engine: string,
  existing: Record<string, unknown> = {},
  recommended: Record<string, unknown> = {},
  recommendedLabel: FieldSource = "card",   // HF card → "card"; curated catalog → "house"
): { values: Record<string, unknown>; source: Record<string, FieldSource> } {
  const groups = PARAM_SCHEMA[engine] || PARAM_SCHEMA.llamacpp;
  const house = HOUSE_DEFAULTS[engine] || {};
  const values: Record<string, unknown> = {};
  const source: Record<string, FieldSource> = {};
  for (const g of groups) {
    for (const f of g.fields) {
      const set = lookup(existing, f.key);
      const rec = lookup(recommended, f.key);
      if (set.hit) { values[f.key] = set.value; source[f.key] = "set"; }
      else if (rec.hit) { values[f.key] = rec.value; source[f.key] = recommendedLabel; }
      else if (f.key in house) { values[f.key] = house[f.key]; source[f.key] = "house"; }
      else { values[f.key] = f.default; source[f.key] = "default"; }
    }
  }
  return { values, source };
}

/**
 * #1604: what to SEND when the editor saves.
 *
 * `PATCH /api/deployments/{id}` replaces `params` wholesale — deliberately, so
 * a caller can remove one. The console was sending only the keys its schema
 * knows, which turned an unchanged "Save" into silent data loss: every backend
 * parameter without a schema field was gone. After #1599 that meant
 * `--spec-type draft-mtp` and `--spec-draft-n-max 2` disappeared from a
 * deployment whose operator had changed nothing — MTP off, the model still
 * running, nobody the wiser. The very failure mode #1583 had just fixed,
 * triggered by a click instead of a flag.
 *
 * So the editor carries the rest through: everything the schema does not cover
 * is preserved verbatim; everything it does cover is written in the SCHEMA's
 * spelling, so a saved deployment ends up with one name per parameter instead
 * of both.
 */
export function paramsToSave(
  values: Record<string, unknown>,
  existing: Record<string, unknown> = {},
): Record<string, unknown> {
  const known = new Set(Object.keys(values).map(paramKey));
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(existing)) {
    if (!known.has(paramKey(k))) out[k] = v;   // no schema field — keep it as it is
  }
  return { ...out, ...values };
}
