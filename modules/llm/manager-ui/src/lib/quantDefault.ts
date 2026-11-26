// #3402: which quantisation the deploy editor offers and pre-selects.
//
// A GGUF repo carries files next to its weights that are NOT a weight: the
// importance matrix (`imatrix_unsloth.gguf`, 0.01 GB, label "other"), an MTP
// head (`MTP/mtp-Qwen3.8-27B-Q4_0.gguf`) and the vision projector
// (`mmproj-…`). The default used to be "the smallest sized quant", so
// the 0.01 GB importance matrix won. Same rule as the manager's
// `hf.is_auxiliary_gguf`; the manager already leaves these out of the list it
// sends, and this side refuses them too, so an older manager cannot bring the
// wrong default back.
import type { HfQuant } from "../api/client";

type QuantLike = Pick<HfQuant, "label" | "files" | "size_gb">;

const QUANT_RE = /(?:^|[.\-_])((?:I?Q\d(?:_[0-9A-Za-z]+)*)|F16|FP16|BF16|F32)(?:[.\-_]|$)/i;

export function isAuxiliaryGguf(path: string): boolean {
  const base = (path.split("/").pop() || path);
  const low = base.toLowerCase();
  if (low.includes("mmproj") || /^mtp[.\-_]/i.test(base)) return true;
  // An importance matrix carries no quant label; a weight that only mentions
  // how it was made (`…-imatrix-Q4_K_M.gguf`) keeps its label and stays a weight.
  return low.includes("imatrix") && !QUANT_RE.test(base);
}

export function isAuxiliaryQuant(q: QuantLike): boolean {
  return q.files.length > 0 && q.files.some(isAuxiliaryGguf);
}

/** The quants an operator may pick as the model's weights. */
export function mainWeightQuants<Q extends QuantLike>(quants: ReadonlyArray<Q>): Q[] {
  return quants.filter((q) => !isAuxiliaryQuant(q));
}

/** Index of the quant to pre-select: the `preferFile` match, else the smallest
 * sized, else the first — never an auxiliary file. -1 when nothing qualifies. */
export function defaultQuantIndex(quants: ReadonlyArray<QuantLike>, preferFile?: string): number {
  const ok = (q: QuantLike) => !isAuxiliaryQuant(q);
  if (preferFile) {
    const i = quants.findIndex((q) => ok(q) && q.files.some((f) => f.endsWith(preferFile) || f === preferFile));
    if (i >= 0) return i;
  }
  const sized = quants.filter((q) => ok(q) && q.size_gb > 0);
  if (sized.length) {
    const smallest = sized.reduce((a, b) => (b.size_gb < a.size_gb ? b : a));
    return quants.indexOf(smallest);
  }
  return quants.findIndex(ok);
}
