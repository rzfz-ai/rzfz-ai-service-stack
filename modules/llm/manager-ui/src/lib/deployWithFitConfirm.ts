import { ApiError, endpoints } from "../api/client";

/** The marker the admission gate puts in its 409 detail (#227/#335/#330).
 *  Kept in ONE place and pinned against the server wording by
 *  tests/unit/llm-manager/test_301_fit_confirm.py — if the backend rewords its
 *  refusal, that test fails instead of this dialog silently disappearing. */
export const OVERSUBSCRIBE_MARKER = "over-subscribe";

export type DeployResult = Awaited<ReturnType<typeof endpoints.deploy>>;

/**
 * Deploy, and turn the admission gate's refusal into an informed decision (#301).
 *
 * Before the gate existed, an over-sized model deployed silently (a 397 GB Q1
 * onto a 30 GB worker). The gate now refuses with 409 and a message that names
 * the remedy — "…or re-deploy with force enabled" — but the console offered no
 * way to do that, so the operator read an error and hit a dead end.
 *
 * On a 409 over-subscription we show the gate's OWN numbers (requested /
 * committed / usable, or the measured free-now figure from the #330 dynamic
 * leg) and let the operator decide. Anything else propagates unchanged.
 *
 * Returns `null` when the operator declines — callers stay quiet in that case;
 * a deliberate "no" is not an error worth a red toast.
 */
export async function deployWithFitConfirm(
  body: Record<string, unknown>,
  confirmFn: (msg: string) => boolean = (m) => window.confirm(m),
): Promise<DeployResult | null> {
  try {
    return await endpoints.deploy(body);
  } catch (err) {
    const oversubscribed =
      err instanceof ApiError &&
      err.status === 409 &&
      err.message.toLowerCase().includes(OVERSUBSCRIBE_MARKER);
    if (!oversubscribed) throw err;

    const proceed = confirmFn(
      `${(err as ApiError).message}\n\n` +
        "Deploy anyway? Forcing past the VRAM budget usually ends in a failed " +
        "engine load or weights spilling into host RAM, which slows the whole box.",
    );
    if (!proceed) return null;
    return await endpoints.deploy({ ...body, force: true });
  }
}
