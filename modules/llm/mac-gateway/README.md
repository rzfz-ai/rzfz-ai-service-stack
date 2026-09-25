<!-- SPDX-License-Identifier: Apache-2.0 -->
# Mac LLM gateway (`mac-llm` profile)

An **opt-in, off-by-default** in-stack [LiteLLM](https://github.com/BerriAI/litellm)
proxy that fronts one-or-more **Macs running Ollama** (Apple-Silicon / MLX) and
exposes them to the stack as a **second OpenAI-compatible endpoint** —
load-balanced and health-checked. **GPUStack is untouched:** this is a second
endpoint *alongside* `gpustack:9090/v1-openai`, never a replacement.

```
OWUI / Dify ──▶ gpustack:9090/v1-openai            (unchanged workhorse)
             └▶ llm-mac-gateway:4000/v1 ──┬▶ Mac1 :11434/v1   (Ollama / MLX)
                                          └▶ Mac2 :11434/v1   (same alias = LB group)
```

Design: internal docs ·
Plan: internal docs

> **Superseded-when-you-adopt-the-LLM-Manager (#254 P2-C1).** Once the
> **LLM Manager** (`llm-manager` profile) is in use, a Mac no longer needs this
> standalone LiteLLM gateway: register the Mac's Ollama endpoint directly with
> the manager (`POST /api/workers`, engine `ollama`, `hardware`
> `apple-silicon`, with the Mac's own upstream key) and it joins the fleet
> router as a load-balanced backend like any other node. This module stays
> shipped + supported for boxes not (yet) on the manager; GPUStack remains
> untouched either way. See `modules/llm/node-agent/app/registration.py::build_external_backend_registration`.

This directory is **P1 (Gateway MVP, declarative)**. The Config Portal "Mac LLM
backends" panel (add/remove Macs, live health, Tier-1 model management) is **P2**.

## Files

| File | Purpose |
|---|---|
| `compose.yml` | The `llm-mac-gateway` LiteLLM service (profile `mac-llm`, loopback-bound, master-key auth, healthcheck). |
| `config.example.yaml` | Template for the per-box `config.yaml`. Copy it on first enable. |
| `config.yaml` | **Per-box, gitignored.** The live LiteLLM config (your Macs + models). Must exist before enabling the profile. |
| `gen_config.py` | Pure generator: a list of Macs → the LiteLLM config. Source of truth for `config.yaml`'s shape; the P2 panel writes through it. |

## `.env` schema (Mac backends)

| Key | Default | Meaning |
|---|---|---|
| `MAC_GATEWAY_MASTER_KEY` | *(empty)* | **Required when enabled.** The master key stack components pass to authenticate to the gateway. Generate a strong random secret. |
| `MAC_GATEWAY_PORT` | `4000` | Loopback host-side port for operator debugging (`127.0.0.1:<port>:4000`). In-network consumers use `llm-mac-gateway:4000`. |
| `LITELLM_VERSION` | `main-v1.93.0-stable` | Pinned LiteLLM image tag (`ghcr.io/berriai/litellm:<tag>`). Repin to a reviewed tag / `@sha256` digest at ship (security review). |

The **Macs themselves are declared in `config.yaml`**, not in `.env` — a Mac is a
host `ip:port` plus one-or-more `{alias, ollama_tag}` models. Two Macs given the
same `alias` for the same model auto-form one load-balance group. Example (see
`config.example.yaml`):

```yaml
model_list:
  - model_name: mac-qwen3-coder          # alias shown in Open WebUI
    litellm_params:
      model: openai/qwen3-coder          # the Ollama tag, openai/ prefix
      api_base: http://192.0.2.194:11434/v1
      api_key: "ollama"
```

## Enable (P1, by hand)

1. On each Mac: run Ollama reachable on the LAN — `OLLAMA_HOST=0.0.0.0 ollama serve`
   (brew) or the Ollama.app equivalent. Pull the models you want.
2. In `.env`: set `MAC_GATEWAY_MASTER_KEY=<a strong secret>` and add `mac-llm` to
   `COMPOSE_PROFILES`.
3. `cp modules/llm/mac-gateway/config.example.yaml modules/llm/mac-gateway/config.yaml`
   and add your Macs + models (or `python3 gen_config.py < macs.json > config.yaml`).
4. `docker compose up -d llm-mac-gateway`.
5. Smoke it (loopback):
   ```bash
   curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $MAC_GATEWAY_MASTER_KEY"
   curl -s http://127.0.0.1:4000/health   -H "Authorization: Bearer $MAC_GATEWAY_MASTER_KEY"
   ```

## Wire Open WebUI to the Mac gateway

Open WebUI takes a **`;`-separated list** of OpenAI base URLs paired 1:1 with keys.
GPUStack is always endpoint #1; when `mac-llm` is enabled, append the gateway as
endpoint #2 by setting these in `.env` (the P2 Config Portal toggle writes them
for you):

```ini
OWUI_OPENAI_BASE_URLS=http://gpustack:9090/v1-openai;http://llm-mac-gateway:4000/v1
OWUI_OPENAI_KEYS=${GPUSTACK_API_KEY};${MAC_GATEWAY_MASTER_KEY}
```

Then `docker compose up -d --force-recreate openwebui`. The Mac aliases appear in
OWUI's model list next to the GPUStack models. (**Dify:** add
`http://llm-mac-gateway:4000/v1` + the master key as another OpenAI-compatible
model provider — no compose change.)

## Security

- **Internal-only in P1** — no Caddy route. Reached in-network as
  `llm-mac-gateway:4000`; host bind is loopback-only. An SSO-gated external route
  (`llm-mac.<domain>`) is deferred to P3.
- **Master-key auth** — components must present `MAC_GATEWAY_MASTER_KEY`.
- The container reaches each Mac over the **host LAN in plaintext** (Ollama has
  no auth). **Firewall every Mac to accept `:11434` only from this box's IP.**
- New compose service + new secret → a **security review is required at ship**
  (`scripts/pre-tag-check.sh` trips on it). Repin `LITELLM_VERSION` to a reviewed
  digest as part of that review.

## Needs a real Mac (not covered by P1 scaffold/CI)

End-to-end inference, the load-balance group across two Macs, and the
active-health flip when a Mac goes down all require a reachable Ollama host and
are validated on-box (design §12b proved the architecture against Mac `0.230`).
The committed P1 gate is scaffold + config-level validation only:
`docker compose config`, the `gen_config` unit tests, and the module contract
test (`tests/test-mac-gateway.sh`).

---
*razzfazz.ai GmbH — Member of SEQIS Group.*
