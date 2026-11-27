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
  optionLabels?: Record<string, string>;
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
      title: "Speculative decoding",
      fields: [
        { key: "spec_type", label: "Speculative decoding", type: "select", default: "off",
          options: ["off", "draft-mtp"], optionLabels: { off: "off", "draft-mtp": "MTP (draft-mtp)" } },
        { key: "spec_draft_n_max", label: "Draft tokens (n-max)", type: "number", default: 2, step: 1, min: 1, max: 4 },
      ],
    },
    {
      title: "Context & cache",
      fields: [
        { key: "ctx_size", label: "Context size", type: "number", default: 8192, step: 1024, min: 512, hint: "n_ctx — tokens" },
        { key: "n_parallel", label: "Parallel slots", type: "number", default: 1, step: 1, min: 1, hint: "concurrent requests" },
        { key: "cache_type_k", label: "KV cache (K)", type: "select", default: "f16", options: ["f16", "q8_0", "q4_0"] },
        { key: "cache_type_v", label: "KV cache (V)", type: "select", default: "f16", options: ["f16", "q8_0", "q4_0"] },
        { key: "flash_attn", label: "Flash attention", type: "bool", default: true },
        // #2520: per-model since 2026.09-ga.4. The default is the node driver's
        // own (0 — host prompt-cache containment), so a deployment that never
        // set it launches exactly as before. A hybrid/recurrent model (qwen3.6)
        // needs > 0 to reuse a prompt prefix at all; each engine with it on
        // costs ~1.7 GiB host RAM, which admission charges.
        { key: "ctx_checkpoints", label: "Context checkpoints", type: "number", default: 0, step: 1, min: 0, hint: "prefix reuse for hybrid models; +1.7 GiB host RAM" },
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

/**
 * #3498: the ONE stored spelling of a parameter — the llama.cpp flag name the
 * node emits (dashes, lowercase, no leading `--`), with the editor's old names
 * mapped onto the real flag. The schema's `key`s stay field ids; what is SENT
 * is always this. Identical to the manager's `app/param_keys.py` and the node's
 * `drivers/base.py::canonical_params` (a test compares the three tables).
 *
 * Why it matters: the editor wrote `n_parallel` next to the catalogue's
 * `parallel`; the node emitted both, llama.cpp kept the last one, and a
 * deployment stored `parallel: 2` + `n_parallel: 1` ran ONE slot.
 */
export const PARAM_ALIASES: Record<string, string> = {
  "n-parallel": "parallel",
  "temperature": "temp",
  "n-ctx": "ctx-size",
  // llama.cpp's short flags (`-c 8192`, `-np 4`, ...): the same option
  "c": "ctx-size",
  "np": "parallel",
  "ngl": "n-gpu-layers",
  "gpu-layers": "n-gpu-layers",
  "b": "batch-size",
  "ub": "ubatch-size",
  "fa": "flash-attn",
  "ctk": "cache-type-k",
  "ctv": "cache-type-v",
};

export function canonicalKey(k: string): string {
  const c = k.trim().replace(/^-+/, "").replace(/_/g, "-").toLowerCase();
  return PARAM_ALIASES[c] ?? c;
}

/** One key per flag, every key canonical; the canonical spelling wins. */
export function canonicalParams(bag: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const exact: Record<string, boolean> = {};
  for (const [k, v] of Object.entries(bag)) {
    const c = canonicalKey(k);
    if (!(c in out) || k === c || !exact[c]) { out[c] = v; exact[c] = k === c; }
  }
  return out;
}

/**
 * The value for `key`, whichever way the writer spelled it. When a row stores
 * two spellings of one flag, the CANONICAL one wins — the rule the manager and
 * the node apply — and an alias only counts when no canonical key exists
 * (among aliases only, the last one, as llama.cpp read the repeated flag).
 */
function lookup(bag: Record<string, unknown>, key: string): { hit: boolean; value?: unknown } {
  const want = canonicalKey(key);
  if (want in bag) return { hit: true, value: bag[want] };
  let hit: { hit: boolean; value?: unknown } = { hit: false };
  for (const k of Object.keys(bag)) {
    if (canonicalKey(k) === want) hit = { hit: true, value: bag[k] };
  }
  return hit;
}

/**
 * #3498: what the engine launches with — slots, the total context, and the
 * context EACH slot gets (llama.cpp divides `ctx-size` across `parallel`
 * unless `kv-unified` shares one pool). `null` = not set on the row: the
 * engine's own default applies, which the editor must not pretend to know.
 */
export function effectiveSlots(params: Record<string, unknown>): {
  parallel: number | null; ctx: number | null; perSlot: number | null; unified: boolean;
} {
  const num = (k: string) => {
    const r = lookup(params, k);
    const n = r.hit ? Number(r.value) : NaN;
    return Number.isFinite(n) && n > 0 ? Math.trunc(n) : null;
  };
  const parallel = num("parallel");
  const ctx = num("ctx-size");
  const u = lookup(params, "kv-unified");
  const unified = u.hit && (u.value === true || u.value === "true" || u.value === "on" || u.value === 1);
  const perSlot = ctx === null ? null : unified ? ctx : parallel ? Math.floor(ctx / parallel) : null;
  return { parallel, ctx, perSlot, unified };
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
 * is preserved verbatim.
 *
 * #3498 ("Edit only what you need"): with a `baseline` — the values the form
 * opened with — ONLY the fields the operator changed are written; an untouched
 * field keeps whatever the row stores, byte for byte, and a field the row never
 * set stays unset (the form's displayed default is NOT written). A written
 * field goes under its canonical key and replaces every other spelling of the
 * same flag, so the row cannot end up with `parallel` AND `n_parallel`.
 * Without a baseline (a new deploy) every value is written, canonically.
 */
export function paramsToSave(
  values: Record<string, unknown>,
  existing: Record<string, unknown> = {},
  baseline?: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...existing };
  const changed = (k: string) =>
    !baseline || !(k in baseline) || JSON.stringify(baseline[k]) !== JSON.stringify(values[k]);
  const write = (k: string, v: unknown) => {
    const c = canonicalKey(k);
    for (const e of Object.keys(out)) if (canonicalKey(e) === c) delete out[e];
    out[c] = v;
  };
  for (const [k, v] of Object.entries(values)) if (changed(k)) write(k, v);
  const specChanged = changed("spec_type") || changed("spec_draft_n_max");
  const spec = lookup(out, "spec-type");
  if (specChanged && spec.hit && spec.value === "off") {
    // An explicit disable must also remove the catalogue's hyphenated flags.
    for (const k of Object.keys(out)) {
      if (["spec-type", "spec-draft-n-max"].includes(canonicalKey(k))) delete out[k];
    }
  } else if (specChanged && spec.value === "draft-mtp") {
    const draft = lookup(out, "spec-draft-n-max");
    const n = draft.hit ? draft.value : 2;
    if (typeof n !== "number" || !Number.isInteger(n) || n < 1 || n > 4) {
      throw new Error("Draft tokens (n-max) must be an integer from 1 to 4.");
    }
    if (!draft.hit) write("spec-draft-n-max", n);
  }
  return out;
}

/**
 * #2536 item 33: what a DEPLOY sends. The schema's values plus, for the curated
 * catalogue ("house") only, every catalogue parameter the schema has no field
 * for. qwen3.6's measured `ubatch_size` 1024 (#2520) is such a key: without
 * this the console launched it at llama.cpp's default -ub 512 while the
 * manifest path used 1024. A Hugging Face card's extras are not carried: they
 * may name options llama.cpp has no flag for.
 */
export function deployParams(
  values: Record<string, unknown>,
  recommended: Record<string, unknown> = {},
  recommendedLabel: FieldSource = "card",
): Record<string, unknown> {
  // #3498: a new row starts with canonical keys only.
  return paramsToSave(values, recommendedLabel === "house" ? canonicalParams(recommended) : {});
}

/**
 * #2536: the recommendation a deploy editor resolves its fields from AND sends
 * from — one helper, so the two cannot disagree. A deploy takes the caller's
 * recommendation (the catalogue passes its own, labelled "house"), else the
 * Hugging Face card's, labelled "card". Edit mode has none.
 */
export function deployRecommendation(
  mode: "deploy" | "edit",
  given: Record<string, unknown> | undefined,
  card: Record<string, unknown> | undefined,
  givenLabel: FieldSource | undefined,
): { rec: Record<string, unknown>; label: FieldSource } {
  if (mode !== "deploy") return { rec: {}, label: "card" };
  return { rec: given ?? card ?? {}, label: givenLabel ?? "card" };
}
