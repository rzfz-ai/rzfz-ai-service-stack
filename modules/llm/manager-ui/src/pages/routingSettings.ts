// #2521 follow-up (K4): what the Settings page says about chat affinity and
// the router's load-balancing algorithm. Pure functions, no React/JSX, so
// tests/unit/llm-manager-ui/test_2521_k4_routing_settings_form.py can drive
// them through node (tests/lib/ts_probe.py) — the repo has no JS test runner.
import type { ManagerSettings } from "../api/client";

export interface StrategyOption {
  value: string;
  label: string;
  hint: string;
}

// Labels and hints for the values the server offers
// (router_config.ROUTING_STRATEGIES). The server's list is the authority: an
// unknown value still gets an option, labelled with its raw name.
const KNOWN: Record<string, { label: string; hint: string }> = {
  "latency-based-routing": {
    label: "Lowest latency",
    hint: "The replica that answered fastest recently takes the next request. The default, and what every box ran before this setting existed.",
  },
  "simple-shuffle": {
    label: "Random",
    hint: "Each request goes to a random replica. No shared state; the most even spread over time.",
  },
  "usage-based-routing-v2": {
    label: "Fewest tokens this minute",
    hint: "The replica that has processed the fewest tokens in the current minute. Counters live in Valkey and are shared across a router swap.",
  },
};

export function strategyOptions(s: Pick<ManagerSettings, "routing_strategies" | "routing_strategy_default">): StrategyOption[] {
  return s.routing_strategies.map((value) => {
    const k = KNOWN[value];
    const label = k ? k.label : value;
    return {
      value,
      label: value === s.routing_strategy_default ? `${label} (default)` : label,
      hint: k ? k.hint : "",
    };
  });
}

// The PATCH body for one change: exactly the field that changed, nothing else,
// so a click on one control can never re-send (and re-log) the other.
export function affinityPatch(on: boolean): { chat_affinity: boolean } {
  return { chat_affinity: on };
}

export function strategyPatch(value: string): { routing_strategy: string } {
  return { routing_strategy: value };
}

// What the page says after a save. `router_rebuild` is only present when the
// algorithm changed; a failed rebuild is not an error of the SAVE (the value
// is stored and the next rebuild applies it), but the operator must see it.
export function savedMessage(field: "affinity" | "strategy", s: ManagerSettings): { kind: "info" | "err"; text: string } {
  if (field === "affinity") {
    return {
      kind: "info",
      text: s.chat_affinity
        ? "Session affinity is on — a chat's turns stay on one replica from its next turn on."
        // #2521
        : "Session affinity is off — every turn is balanced on its own, as before.",
    };
  }
  const label = strategyOptions(s).find((o) => o.value === s.routing_strategy)?.label ?? s.routing_strategy;
  const rb = s.router_rebuild;
  if (rb && !rb.ok) {
    return {
      kind: "err",
      text: `Load balancing saved as “${label}”, but the router config could not be rebuilt (${rb.error ?? "unknown error"}). The next rebuild applies it.`,
    };
  }
  // Saved and written — NOT yet served: the router starts a second LiteLLM
  // with the new config and only switches once it serves (it may also never
  // serve). routerBadge() reports what actually happened.
  return {
    kind: "info",
    text: `Load balancing saved as “${label}”. The router switches once a new router process with it is serving; the status below shows when.`,
  };
}

// What the router ACTUALLY serves, from `router_config_state` — which the
// router entrypoint publishes when a new LiteLLM starts serving (K4 fix 1).
// `routing_strategy_running` is the algorithm of that served config.
export function routerBadge(
  s: Pick<ManagerSettings, "routing_strategy" | "routing_strategy_running" | "router_config_state">,
): { kind: "ok" | "warn" | "err" | "muted"; text: string } | null {
  const running = s.routing_strategy_running ?? "an unknown algorithm";
  switch (s.router_config_state) {
    case "running":
      return null;
    case "pending":
      return { kind: "warn", text: `router still runs ${running} — switching` };
    case "failed":
      return { kind: "err", text: `the router could not start with this config and still runs ${running}` };
    default:
      return { kind: "muted", text: "the router does not report what it runs (older router)" };
  }
}

// Poll while a switch is in flight, so the badge clears (or turns red) by itself.
export function pollWhileSwitching(s: Pick<ManagerSettings, "router_config_state"> | undefined): number | false {
  return s && s.router_config_state === "pending" ? 3000 : false;
}
