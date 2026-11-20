"""
title: Message Loading Indicator
description: Adds a Message Loading Indicator setting under Settings > Interface, so each user picks how a reply shows it is loading: Open WebUI's default, the blinking cursor, the pulsing dot, the legacy loading bars, a terminal block cursor, typing dots, an equalizer, a spinner, or shimmering skeleton lines. Published into /static/loader.js through the Shared Assets Protocol, so it coexists with Theme Designer Pro and any other plugin using it.
author: @G30
author_url: https://openwebui.com/u/g30
funding_url: https://buymeacoffee.com/iamg30
version: 1.1.3
license: MIT
required_open_webui_version: 0.11.0
"""

import hashlib
import json as _json

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


import asyncio
import logging
import uuid
from typing import Any, Optional

from open_webui.utils.json_codec import JSONCodec

ASSET_KEY = "message-loading-indicator"  # fixed, used for asset_register too

# ===========================================================================
# Live reload broadcast
# ---------------------------------------------------------------------------
# Each load publishes over Redis; a peer whose cached code differs from the
# database re-executes it. register(app) is yours and must be idempotent; gate
# each producer on reload_active(app).
# ===========================================================================
RELOAD_STATE_ATTR = "_owui_live_reload"  # {key: {active, exec_id, listener}}
RELOAD_EXEC_ID = uuid.uuid4().hex  # new on every exec of this source
# Open WebUI executes each function as module "function_<id>", or on newer
# builds "function_<id>_<32 hex digits>"; the id is what is left of either.
RELOAD_FUNCTION_ID = __name__.removeprefix("function_")
if (
    len(RELOAD_FUNCTION_ID) > 33
    and RELOAD_FUNCTION_ID[-33] == "_"
    and all(c in "0123456789abcdef" for c in RELOAD_FUNCTION_ID[-32:])
):
    RELOAD_FUNCTION_ID = RELOAD_FUNCTION_ID[:-33]

reload_log = logging.getLogger("owui-live-reload")
# asyncio only keeps weak references to tasks.
reload_tasks: set = set()


def reload_state(app: Any) -> dict:
    registry = getattr(app.state, RELOAD_STATE_ATTR, None)
    if not isinstance(registry, dict):
        registry = {}
        app.state.__setattr__(RELOAD_STATE_ATTR, registry)
    return registry.setdefault(ASSET_KEY, {})


def reload_active(app: Any) -> bool:
    return bool(reload_state(app).get("active"))


def reload_channel() -> str:
    from open_webui.env import REDIS_KEY_PREFIX

    return f"{REDIS_KEY_PREFIX}:live-reload:{ASSET_KEY}"


def reload_mark_loaded(app: Any) -> None:
    register(app)
    state = reload_state(app)
    state["active"] = True
    state["exec_id"] = RELOAD_EXEC_ID


async def reload_publish(app: Any, active: bool) -> None:
    redis = getattr(app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.publish(reload_channel(), JSONCodec.dumps({"active": active}))
    except Exception as e:
        reload_log.warning("[%s] publish failed: %s", ASSET_KEY, type(e).__name__)


async def reload_ensure_listener(app: Any) -> None:
    state = reload_state(app)
    if state.get("listener") is not None:
        return
    redis = getattr(app.state, "redis", None)
    if redis is None:
        return

    from types import SimpleNamespace

    from open_webui.models.functions import Functions
    from open_webui.utils.plugin import (
        get_function_module_from_cache,
        get_functions_cache,
    )

    # Mark before awaiting so a concurrent event() can't double-subscribe.
    state["listener"] = True
    try:
        pubsub = redis.pubsub()
        await pubsub.subscribe(reload_channel())
    except Exception as e:
        state["listener"] = None
        reload_log.warning("[%s] subscribe failed: %s", ASSET_KEY, type(e).__name__)
        return

    async def listen():
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                if not JSONCodec.loads(message["data"])["active"]:
                    state["active"] = False
                    continue
                was_active = state.get("active")
                # Enable broadcasts arrive before is_active commits; bootstrap trusts this flag.
                state["active"] = True
                context = SimpleNamespace(app=app)
                if not was_active:
                    # Force a fresh exec so its bootstrap registers again.
                    get_functions_cache(context).pop(RELOAD_FUNCTION_ID, None)
                try:
                    function_module, _, _ = await get_function_module_from_cache(
                        context, RELOAD_FUNCTION_ID
                    )
                    # Peers never see a valves save; re-read them like Open WebUI's dispatch does.
                    if hasattr(function_module, "Valves"):
                        valves = await Functions.get_function_valves_by_id(
                            RELOAD_FUNCTION_ID
                        )
                        function_module.valves = function_module.Valves(
                            **(valves or {})
                        )
                except Exception as e:
                    reload_log.warning(
                        "[%s] reload from db failed: %s", ASSET_KEY, type(e).__name__
                    )
        except Exception as e:
            reload_log.warning("[%s] listener stopped: %s", ASSET_KEY, type(e).__name__)

    state["listener"] = asyncio.create_task(listen())


def reload_bootstrap() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def bootstrap():
        try:
            from open_webui.main import app
            from open_webui.models.functions import Functions

            own = await Functions.get_function_by_id(RELOAD_FUNCTION_ID)
            # Loading a disabled function (e.g. for its valves) must not switch it on.
            if own is None or not (own.is_active or reload_active(app)):
                return
            reload_mark_loaded(app)
            await reload_ensure_listener(app)
            await reload_publish(app, True)
        except Exception as e:
            reload_log.warning("[%s] bootstrap failed: %s", ASSET_KEY, type(e).__name__)

    task = loop.create_task(bootstrap())
    reload_tasks.add(task)
    task.add_done_callback(reload_tasks.discard)


async def reload_on_event(
    app: Any, event: Optional[dict], event_name: Optional[str]
) -> None:
    await reload_ensure_listener(app)
    state = reload_state(app)

    is_own = ((event or {}).get("subject") or {}).get("id") == RELOAD_FUNCTION_ID
    if is_own and event_name == "function.disable_started":
        state["active"] = False
        await reload_publish(app, False)
        return
    if is_own and event_name == "function.valves_updated":
        await reload_publish(app, True)

    current = state.get("active") and state.get("exec_id") == RELOAD_EXEC_ID
    if current and event_name != "system.startup.completed":
        return
    reload_mark_loaded(app)
    await reload_publish(app, True)


# =========================== end live reload block =========================


import hashlib
from pathlib import Path
from typing import Any

# ===========================================================================
# Shared static-asset registry
# --- KEEP BYTE-IDENTICAL IN EVERY PLUGIN THAT USES IT ----------------------
# ---------------------------------------------------------------------------
# Each plugin publishes a fragment into one app.state registry and the route
# composes them per request, so load order is irrelevant. Per-process: each
# container serves what it has loaded.
#
# Contract:
#   * ASSET_REGISTRY_ATTR, ASSET_ROUTE_ATTR, the entry shape and the paths are
#     the interop surface. The route owner's copy serves everyone.
#   * `key` must be a module-level constant, or a re-exec adds a second entry.
#   * Lower `order` composes first (default 0). Never encode priority in the key.
#   * Producers run on the event loop for every request, 304s included: return
#     a prebuilt string, no I/O, no locks.
#   * Return "" to withdraw. A deleted plugin never sees its own deletion, so
#     disable it first.
#   * Inline fragments only: <script src> / @import lands after hydration.
# ===========================================================================
LOADER_PATH = "/static/loader.js"
CUSTOM_CSS_PATH = "/static/custom.css"
SHARED_ASSET_TYPES = {
    LOADER_PATH: "application/javascript; charset=utf-8",
    CUSTOM_CSS_PATH: "text/css; charset=utf-8",
}
ASSET_REGISTRY_ATTR = "_owui_static_fragments"  # {path: {key: entry}}
ASSET_ROUTE_ATTR = "_owui_shared_asset"  # set to the path the route serves
ASSET_IMPL_ATTR = "_owui_shared_asset_impl"  # implementation version of the route
# Bump on any behaviour change: the newest copy takes over the route.
ASSET_IMPL_VERSION = 4

# Warn once per (path, key, exception type): compose runs on every page load.
_ASSET_WARNED: set = set()


def asset_fragments(app: Any, path: str) -> dict:
    registry = getattr(app.state, ASSET_REGISTRY_ATTR, None)
    if not isinstance(registry, dict):
        registry = {}
        app.state.__setattr__(ASSET_REGISTRY_ATTR, registry)
    bucket = registry.get(path)
    if not isinstance(bucket, dict):
        bucket = {}
        registry[path] = bucket
    return bucket


def asset_sort_key(item):
    """(order, key); a non-int order falls back to 0 so it cannot break sorted()."""
    key, entry = item
    try:
        order = int(entry.get("order", 0))
    except (TypeError, ValueError):
        order = 0
    return (order, key)


def asset_strip_block(content: str, start_marker: str, end_marker: str) -> str:
    """Remove every marker-wrapped block, leaving other content untouched."""
    while start_marker in content:
        start = content.find(start_marker)
        end = content.find(end_marker, start)
        if end == -1:
            # No end marker: the block was appended last, so drop to EOF.
            content = content[:start]
            break
        end += len(end_marker)
        if content[end : end + 1] == "\n":
            end += 1
        content = content[:start] + content[end:]
    return content


def asset_compose(app: Any, path: str) -> str:
    """Disk file plus every registered fragment, in (order, key) order."""
    import logging

    try:
        from open_webui.env import STATIC_DIR

        target = Path(STATIC_DIR) / path.rsplit("/", 1)[-1]
        body = (
            ""
            if (target.is_symlink() or not target.is_file())
            else target.read_text(encoding="utf-8")
        )
    except Exception:
        body = ""

    ordered = sorted(asset_fragments(app, path).items(), key=asset_sort_key)
    # Strip first: an older file-writing build may have left a block on disk.
    for _key, entry in ordered:
        body = asset_strip_block(body, entry["start"], entry["end"])
    body = body.rstrip()

    for key, entry in ordered:
        try:
            block = (entry["js"]() or "").strip()
        except Exception as exc:
            mark = (path, key, type(exc).__name__)
            if mark not in _ASSET_WARNED:
                if len(_ASSET_WARNED) > 256:
                    _ASSET_WARNED.clear()
                _ASSET_WARNED.add(mark)
                logging.getLogger("owui-shared-assets").warning(
                    "fragment %r failed for %s - it will be omitted",
                    key,
                    path,
                    exc_info=True,
                )
            continue
        if block:
            body = (body + "\n\n" if body else "") + block
    return body + "\n" if body else ""


def asset_register(
    app: Any, path: str, key: str, start: str, end: str, producer, order: int = 0
) -> None:
    """Publish a fragment and ensure the route exists. Idempotent."""
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    asset_fragments(app, path)[key] = {
        "start": start,
        "end": end,
        "js": producer,
        "order": order,
    }

    for existing in app.routes:
        if getattr(existing, ASSET_ROUTE_ATTR, None) != path:
            continue
        if getattr(existing, ASSET_IMPL_ATTR, 0) >= ASSET_IMPL_VERSION:
            return  # an equal or newer implementation already owns the route
        break  # ours is newer - fall through and replace it

    # Fragments live on app.state, so replacing the route loses nothing.
    app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != path]
    media_type = SHARED_ASSET_TYPES.get(path, "text/plain; charset=utf-8")

    async def serve_asset(request):
        content = asset_compose(app, path)
        etag = (
            '"owui-'
            # Cache validator only; a bare md5() raises on FIPS hosts.
            + hashlib.md5(
                (path + "\x00" + content).encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            + '"'
        )
        # no-cache, not no-store: no-store would disable the 304 below.
        headers = {
            "Cache-Control": "no-cache, must-revalidate, private",
            "ETag": etag,
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        # Starlette only adds charset for text/*.
        return Response(content, media_type=media_type, headers=headers)

    insert_at = len(app.routes)
    for position, existing in enumerate(app.routes):
        if isinstance(existing, Mount) and getattr(existing, "name", "") == "static":
            insert_at = position
            break
    shared = Route(path, serve_asset, methods=["GET"])
    setattr(shared, ASSET_ROUTE_ATTR, path)
    setattr(shared, ASSET_IMPL_ATTR, ASSET_IMPL_VERSION)
    app.routes.insert(insert_at, shared)


# =========================== end shared asset block ========================


STYLE_NAMES = {
    "default": "Default",
    "cursor": "Blinking Cursor",
    "dot": "Pulsing Dot",
    "bars": "Legacy Bars",
    "block": "Terminal Block",
    "typing": "Typing Dots",
    "wave": "Equalizer",
    "spinner": "Spinner",
    "shimmer": "Shimmer Lines",
}

OSK_CSS = r"""
.osk,.osk>span{display:none}
html[data-osk="bars"] #response-content-container>div:not(:first-child)>.osk-host[data-osk-kind="cursor"],
html[data-osk="dot"] #response-content-container>div:not(:first-child)>.osk-host[data-osk-kind="cursor"],
html[data-osk="shimmer"] #response-content-container>div:not(:first-child)>.osk-host[data-osk-kind="cursor"]{display:none!important}
html[data-osk="bars"] .osk-host,
html[data-osk="block"] .osk-host,
html[data-osk="typing"] .osk-host,
html[data-osk="wave"] .osk-host,
html[data-osk="spinner"] .osk-host,
html[data-osk="shimmer"] .osk-host,
html[data-osk="dot"] .osk-host[data-osk-kind="cursor"],
html[data-osk="cursor"] .osk-host[data-osk-kind="dot"]{background:none!important;animation:none!important;opacity:1!important;margin:0!important;width:auto!important;height:auto!important;vertical-align:baseline!important}
html[data-osk="bars"] .osk-host,html[data-osk="shimmer"] .osk-host{display:block!important;width:100%!important}
html[data-osk="block"] .osk-host,html[data-osk="typing"] .osk-host,html[data-osk="wave"] .osk-host,html[data-osk="spinner"] .osk-host{display:inline-block!important}
html[data-osk="dot"] .osk-host[data-osk-kind="cursor"]{display:inline-flex!important}
html[data-osk="cursor"] .osk-host[data-osk-kind="dot"]{display:inline-block!important}
html[data-osk="bars"] .osk-host[data-osk-kind="dot"]>span:not(.osk),
html[data-osk="cursor"] .osk-host[data-osk-kind="dot"]>span:not(.osk),
html[data-osk="block"] .osk-host[data-osk-kind="dot"]>span:not(.osk),
html[data-osk="typing"] .osk-host[data-osk-kind="dot"]>span:not(.osk),
html[data-osk="wave"] .osk-host[data-osk-kind="dot"]>span:not(.osk),
html[data-osk="spinner"] .osk-host[data-osk-kind="dot"]>span:not(.osk),
html[data-osk="shimmer"] .osk-host[data-osk-kind="dot"]>span:not(.osk){display:none!important}
html[data-osk="bars"] .osk-host>.osk,html[data-osk="bars"] .osk>.osk-bars{display:block}
html[data-osk="shimmer"] .osk-host>.osk,html[data-osk="shimmer"] .osk>.osk-shimmer{display:block}
html[data-osk="block"] .osk-host>.osk,html[data-osk="block"] .osk>.osk-block{display:inline-block}
html[data-osk="spinner"] .osk-host>.osk,html[data-osk="spinner"] .osk>.osk-spinner{display:inline-block}
html[data-osk="typing"] .osk-host>.osk,html[data-osk="wave"] .osk-host>.osk{display:inline-block}
html[data-osk="typing"] .osk>.osk-typing,html[data-osk="wave"] .osk>.osk-wave{display:inline-flex}
html[data-osk="dot"] .osk-host[data-osk-kind="cursor"]>.osk,html[data-osk="dot"] .osk>.osk-dot{display:inline-flex}
html[data-osk="cursor"] .osk-host[data-osk-kind="dot"]>.osk,html[data-osk="cursor"] .osk>.osk-cursor{display:inline-block}
.osk-bars{width:100%;margin:8px 0}
.osk-bars-in{display:flex;flex-direction:column;gap:8px;animation:osk-pulse 2s cubic-bezier(.4,0,.6,1) infinite}
.osk-b{display:block;height:8px;border-radius:4px;background:var(--color-gray-200,#e5e5e5)}
html.dark .osk-b{background:var(--color-gray-600,#525252)}
.osk-r1{margin-right:3.5rem}
.osk-g3,.osk-g4{display:grid;gap:1rem}
.osk-g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.osk-g4{grid-template-columns:repeat(4,minmax(0,1fr))}
.osk-s2{grid-column:span 2/span 2}
.osk-mr4{margin-right:1rem}
.osk-dot{position:relative;width:12px;height:12px;margin:8px 4px}
.osk-dot-ring{position:absolute;inset:0;border-radius:9999px;background:var(--color-gray-700,#404040);opacity:.75;animation:osk-pulse 2s cubic-bezier(.4,0,.6,1) infinite}
html.dark .osk-dot-ring{background:var(--color-gray-200,#e5e5e5)}
.osk-dot-core{position:relative;display:inline-flex;width:12px;height:12px;border-radius:9999px;background:#000;animation:osk-size 1.5s ease-in-out infinite}
html.dark .osk-dot-core{background:#fff}
.osk-cursor{width:2px;height:14px;margin-left:2px;vertical-align:text-bottom;background:var(--color-gray-400,#a3a3a3);animation:osk-pulse 2s cubic-bezier(.4,0,.6,1) infinite}
html.dark .osk-cursor{background:var(--color-gray-500,#737373)}
.osk-block{width:7px;height:14px;margin-left:2px;vertical-align:text-bottom;background:var(--color-gray-400,#a3a3a3);animation:osk-blink 1s steps(1,end) infinite}
html.dark .osk-block{background:var(--color-gray-500,#737373)}
.osk-typing{align-items:center;gap:3px;height:14px;margin-left:4px;vertical-align:text-bottom}
.osk-typing>span{display:block;width:5px;height:5px;border-radius:9999px;background:var(--color-gray-400,#a3a3a3);animation:osk-bounce 1.2s ease-in-out infinite}
html.dark .osk-typing>span{background:var(--color-gray-500,#737373)}
.osk-typing>span:nth-child(2){animation-delay:.15s}
.osk-typing>span:nth-child(3){animation-delay:.3s}
.osk-wave{align-items:center;gap:2px;height:14px;margin-left:4px;vertical-align:text-bottom}
.osk-wave>span{display:block;width:2px;height:100%;border-radius:1px;background:var(--color-gray-400,#a3a3a3);animation:osk-wave 1s ease-in-out infinite}
html.dark .osk-wave>span{background:var(--color-gray-500,#737373)}
.osk-wave>span:nth-child(2){animation-delay:.15s}
.osk-wave>span:nth-child(3){animation-delay:.3s}
.osk-wave>span:nth-child(4){animation-delay:.45s}
.osk-spinner{box-sizing:border-box;width:12px;height:12px;margin-left:4px;vertical-align:text-bottom;border:2px solid var(--color-gray-200,#e5e5e5);border-top-color:var(--color-gray-500,#737373);border-radius:9999px;animation:osk-spin .8s linear infinite}
html.dark .osk-spinner{border-color:var(--color-gray-700,#404040);border-top-color:var(--color-gray-300,#d4d4d4)}
.osk-shimmer{width:100%;margin:8px 0}
.osk-shimmer-in{display:flex;flex-direction:column;gap:8px}
.osk-sh{display:block;height:8px;border-radius:4px;background:linear-gradient(90deg,var(--color-gray-200,#e5e5e5) 30%,var(--color-gray-50,#fafafa) 50%,var(--color-gray-200,#e5e5e5) 70%);background-size:200% 100%;animation:osk-shimmer 1.6s linear infinite}
html.dark .osk-sh{background-image:linear-gradient(90deg,var(--color-gray-700,#404040) 30%,var(--color-gray-500,#737373) 50%,var(--color-gray-700,#404040) 70%)}
.osk-sh:nth-child(2){width:85%}
.osk-sh:nth-child(3){width:60%}
.osk-pv{display:inline-flex;flex-shrink:0;align-items:center;width:56px;height:22px}
.osk-pv>span{display:none}
.osk-pv[data-pv="cursor"]>.osk-cursor,.osk-pv[data-pv="block"]>.osk-block,.osk-pv[data-pv="spinner"]>.osk-spinner{display:inline-block}
.osk-pv[data-pv="dot"]>.osk-dot,.osk-pv[data-pv="typing"]>.osk-typing,.osk-pv[data-pv="wave"]>.osk-wave{display:inline-flex}
.osk-pv[data-pv="bars"]>.osk-bars,.osk-pv[data-pv="shimmer"]>.osk-shimmer{display:block}
.osk-pv .osk-bars,.osk-pv .osk-shimmer{margin:0}
.osk-pv .osk-bars-in,.osk-pv .osk-shimmer-in{gap:3px}
.osk-pv .osk-b,.osk-pv .osk-sh{height:3px;border-radius:2px}
.osk-pv .osk-r1{margin-right:1rem}
.osk-pv .osk-g3,.osk-pv .osk-g4{gap:4px}
.osk-pv .osk-mr4{margin-right:4px}
.osk-pv .osk-dot{margin:0 4px}
.osk-pv .osk-cursor,.osk-pv .osk-block,.osk-pv .osk-typing,.osk-pv .osk-wave,.osk-pv .osk-spinner{margin-left:4px}
.osk-trigger{display:flex;align-items:center;gap:6px}
.osk-chev{width:12px;height:12px;flex-shrink:0}
.osk-menu{position:fixed;z-index:10000;min-width:13rem;max-height:min(24rem,calc(100dvh - 2rem));overflow-y:auto;padding:2px;border:1px solid var(--color-gray-100,#f5f5f5);border-radius:.75rem;background:#fff;color:var(--color-gray-900,#171717);box-shadow:0 10px 15px -3px rgb(0 0 0/.1),0 4px 6px -4px rgb(0 0 0/.1);outline:none}
html.dark .osk-menu{border-color:var(--color-gray-800,#262626);background:var(--color-gray-850,#1c1c1c);color:#fff}
.osk-opt{display:flex;align-items:center;gap:8px;min-height:1.875rem;padding:0 8px;border-radius:.75rem;font-size:.8125rem;cursor:pointer;outline:none}
.osk-opt:hover,.osk-opt:focus-visible{background:color-mix(in oklab,var(--color-gray-100,#f5f5f5) 70%,transparent)}
html.dark .osk-opt:hover,html.dark .osk-opt:focus-visible{background:color-mix(in oklab,var(--color-gray-800,#262626) 60%,transparent)}
.osk-opt-name{flex:1;min-width:0;white-space:nowrap}
.osk-opt-check{width:14px;height:14px;flex-shrink:0;visibility:hidden}
.osk-opt[aria-selected="true"] .osk-opt-check{visibility:visible}
@keyframes osk-pulse{50%{opacity:.5}}
@keyframes osk-size{0%,100%{transform:scale(1)}50%{transform:scale(1.25)}}
@keyframes osk-blink{50%{opacity:0}}
@keyframes osk-bounce{0%,60%,100%{transform:translateY(0);opacity:.5}30%{transform:translateY(-3px);opacity:1}}
@keyframes osk-wave{0%,100%{transform:scaleY(.35)}50%{transform:scaleY(1)}}
@keyframes osk-spin{to{transform:rotate(360deg)}}
@keyframes osk-shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
"""

LOADER_SCRIPT = r"""
(function () {
  'use strict';
  if (window.__owuiMessageIndicator) return;
  window.__owuiMessageIndicator = true;

  var CFG = __CONFIG__;
  var CSS = __CSS__;
  var KEY = 'owui-message-loading-indicator';
  var NAMES = CFG.names;
  var ORDER = Object.keys(NAMES);
  var MARKUP =
    '<span class="osk-bars"><span class="osk-bars-in">' +
    '<span class="osk-b osk-r1"></span>' +
    '<span class="osk-g3"><span class="osk-b osk-s2"></span><span class="osk-b"></span></span>' +
    '<span class="osk-g4"><span class="osk-b"></span><span class="osk-b osk-s2"></span><span class="osk-b osk-mr4"></span></span>' +
    '<span class="osk-b"></span>' +
    '</span></span>' +
    '<span class="osk-dot"><span class="osk-dot-ring"></span><span class="osk-dot-core"></span></span>' +
    '<span class="osk-cursor"></span>' +
    '<span class="osk-block"></span>' +
    '<span class="osk-typing"><span></span><span></span><span></span></span>' +
    '<span class="osk-wave"><span></span><span></span><span></span><span></span></span>' +
    '<span class="osk-spinner"></span>' +
    '<span class="osk-shimmer"><span class="osk-shimmer-in">' +
    '<span class="osk-sh"></span><span class="osk-sh"></span><span class="osk-sh"></span>' +
    '</span></span>';
  var CHEVRON =
    '<svg class="osk-chev" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor" aria-hidden="true">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5"/></svg>';
  var CHECK =
    '<svg class="osk-opt-check" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">' +
    '<path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5"/></svg>';
  var scheduled = false;
  var trigger = null;
  var menu = null;

  function choice() {
    var value = null;
    try { value = localStorage.getItem(KEY); } catch (e) {}
    return ORDER.indexOf(value) === -1 ? 'default' : value;
  }

  function effective() {
    var value = choice();
    return value === 'default' ? CFG.fallback : value;
  }

  function apply() {
    var value = effective();
    if (value === 'default') document.documentElement.removeAttribute('data-osk');
    else document.documentElement.setAttribute('data-osk', value);
  }

  function label(value) {
    if (value !== 'default' || CFG.fallback === 'default') return NAMES[value];
    return NAMES.default + ' (' + NAMES[CFG.fallback] + ')';
  }

  function ensureStyle() {
    if (document.getElementById('owui-osk-style')) return;
    var style = document.createElement('style');
    style.id = 'owui-osk-style';
    style.textContent = CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  function decorate(host, kind) {
    if (host.classList.contains('osk-host')) return;
    host.classList.add('osk-host');
    host.setAttribute('data-osk-kind', kind);
    var wrap = document.createElement('span');
    wrap.className = 'osk';
    wrap.setAttribute('aria-hidden', 'true');
    wrap.innerHTML = MARKUP;
    host.appendChild(wrap);
  }

  function preview(value) {
    var shown = value === 'default' ? CFG.fallback : value;
    var el = document.createElement('span');
    el.className = 'osk-pv';
    el.setAttribute('aria-hidden', 'true');
    if (shown !== 'default') {
      el.setAttribute('data-pv', shown);
      el.innerHTML = MARKUP;
    }
    return el;
  }

  function renderTrigger() {
    if (!trigger) return;
    var value = choice();
    var mode = document.createElement('span');
    mode.id = 'osk-setting-mode';
    mode.textContent = label(value);
    trigger.replaceChildren(preview(value), mode);
    trigger.insertAdjacentHTML('beforeend', CHEVRON);
  }

  function placeMenu() {
    if (!menu || !trigger) return;
    var rect = trigger.getBoundingClientRect();
    var gap = 4;
    var pad = 16;
    var height = menu.offsetHeight;
    var width = menu.offsetWidth;
    var below = window.innerHeight - rect.bottom - gap;
    var above = rect.top - gap;
    var top = below < height && above > below ? rect.top - gap - height : rect.bottom + gap;
    var left = Math.min(rect.right - width, window.innerWidth - width - pad);
    menu.style.top = Math.max(pad, top) + 'px';
    menu.style.left = Math.max(pad, left) + 'px';
  }

  function closeMenu(refocus) {
    if (!menu) return;
    menu.remove();
    menu = null;
    window.removeEventListener('keydown', onMenuKey, true);
    document.removeEventListener('pointerdown', onOutside, true);
    window.removeEventListener('resize', onResize);
    window.removeEventListener('scroll', placeMenu, true);
    if (!trigger) return;
    trigger.setAttribute('aria-expanded', 'false');
    if (refocus && trigger.isConnected) trigger.focus();
  }

  function pick(value) {
    try { localStorage.setItem(KEY, value); } catch (e) {}
    apply();
    renderTrigger();
    closeMenu(true);
  }

  function onOutside(evt) {
    if (menu && !menu.contains(evt.target) && !(trigger && trigger.contains(evt.target))) closeMenu(false);
  }

  function onResize() {
    closeMenu(false);
  }

  function onMenuKey(evt) {
    var items = menu ? Array.prototype.slice.call(menu.querySelectorAll('[role="option"]')) : [];
    var at = items.indexOf(document.activeElement);
    var to = -1;
    if (evt.key === 'Escape') closeMenu(true);
    else if (evt.key === 'Tab') { closeMenu(true); return; }
    else if (evt.key === 'ArrowDown') to = at < 0 ? 0 : Math.min(at + 1, items.length - 1);
    else if (evt.key === 'ArrowUp') to = at < 0 ? items.length - 1 : Math.max(at - 1, 0);
    else if (evt.key === 'Home') to = 0;
    else if (evt.key === 'End') to = items.length - 1;
    else if ((evt.key === 'Enter' || evt.key === ' ') && at >= 0) pick(items[at].getAttribute('data-value'));
    else return;
    evt.preventDefault();
    evt.stopPropagation();
    if (to >= 0) items[to].focus();
  }

  function openMenu() {
    if (menu || !trigger) return;
    var current = choice();
    var selected = null;
    menu = document.createElement('div');
    menu.id = 'osk-menu';
    menu.className = 'osk-menu';
    menu.tabIndex = -1;
    menu.setAttribute('role', 'listbox');
    menu.setAttribute('aria-labelledby', 'osk-setting-label');
    ORDER.forEach(function (value) {
      var item = document.createElement('div');
      item.className = 'osk-opt';
      item.tabIndex = -1;
      item.setAttribute('role', 'option');
      item.setAttribute('data-value', value);
      item.setAttribute('aria-selected', value === current ? 'true' : 'false');
      var name = document.createElement('span');
      name.className = 'osk-opt-name';
      name.textContent = label(value);
      item.appendChild(preview(value));
      item.appendChild(name);
      item.insertAdjacentHTML('beforeend', CHECK);
      item.addEventListener('click', function () { pick(value); });
      menu.appendChild(item);
      if (value === current) selected = item;
    });
    menu.addEventListener('mousedown', function (evt) { evt.stopPropagation(); });
    (trigger.closest('.modal') || document.body).appendChild(menu);
    trigger.setAttribute('aria-expanded', 'true');
    placeMenu();
    window.addEventListener('keydown', onMenuKey, true);
    document.addEventListener('pointerdown', onOutside, true);
    window.addEventListener('resize', onResize);
    window.addEventListener('scroll', placeMenu, true);
    (selected || menu).focus();
  }

  function settingRow() {
    if (document.getElementById('osk-setting')) return;
    var anchor = document.getElementById('chat-bubble-ui-label');
    if (!anchor || !anchor.closest('#tab-interface')) return;
    var row = anchor.parentElement;
    var wrapper = row && row.parentElement;
    if (!wrapper || !wrapper.parentElement) return;

    var block = document.createElement('div');
    block.id = 'osk-setting';
    var line = document.createElement('div');
    line.className = row.className || 'flex items-center justify-between gap-2.5';
    var name = document.createElement('div');
    name.id = 'osk-setting-label';
    name.className = anchor.className || 'min-w-0 text-xs text-gray-600 dark:text-gray-400';
    name.textContent = 'Message Loading Indicator';
    closeMenu(false);
    trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'osk-trigger text-xs text-gray-500 transition-colors hover:text-gray-900 dark:text-gray-500 dark:hover:text-white';
    trigger.setAttribute('aria-labelledby', 'osk-setting-label osk-setting-mode');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', 'osk-menu');
    trigger.addEventListener('click', function () {
      if (menu) closeMenu(false);
      else openMenu();
    });
    trigger.addEventListener('keydown', function (evt) {
      if (menu || (evt.key !== 'ArrowDown' && evt.key !== 'ArrowUp')) return;
      evt.preventDefault();
      openMenu();
    });
    renderTrigger();
    line.appendChild(name);
    line.appendChild(trigger);
    var note = document.createElement('p');
    note.className = 'mt-1.5 text-[0.6875rem] text-gray-400 dark:text-gray-600';
    note.textContent = 'How a reply shows that it is still loading. Saved in this browser.';
    block.appendChild(line);
    block.appendChild(note);
    wrapper.parentElement.insertBefore(block, wrapper.nextSibling);
  }

  function inReply(node) {
    if (node.closest('#response-content-container')) return true;
    for (var el = node.parentElement; el; el = el.parentElement) {
      var first = el.firstElementChild;
      if (first && /^responses-container-/.test(first.id || '')) return true;
    }
    return false;
  }

  function scan() {
    scheduled = false;
    var cursors = document.querySelectorAll('#response-content-container span.inline-block.align-text-bottom:is(.animate-pulse, .animate-cursor-pulse)');
    for (var i = 0; i < cursors.length; i++) decorate(cursors[i], 'cursor');
    var dots = document.querySelectorAll('span.animate-size');
    for (var j = 0; j < dots.length; j++) {
      var outer = dots[j].parentElement;
      if (outer && outer.tagName === 'SPAN' && outer.classList.contains('relative') && inReply(outer)) decorate(outer, 'dot');
    }
    if (menu && !(menu.isConnected && trigger && trigger.isConnected)) closeMenu(false);
    settingRow();
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(scan);
  }

  ensureStyle();
  apply();
  window.addEventListener('storage', function (evt) {
    if (evt.key !== KEY) return;
    apply();
    closeMenu(false);
    renderTrigger();
  });
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  schedule();
})();
"""


class Event:
    class Valves(BaseModel):
        default_style: Literal[
            "default",
            "cursor",
            "dot",
            "bars",
            "block",
            "typing",
            "wave",
            "spinner",
            "shimmer",
        ] = Field(
            default="default",
            description="What Default means for users who have not picked a style. 'default' keeps Open WebUI's own indicator, 'cursor' is the blinking cursor, 'dot' the pulsing dot, 'bars' the legacy loading bars, 'block' a terminal block cursor, 'typing' three bouncing dots, 'wave' an equalizer, 'spinner' a spinning ring, and 'shimmer' shimmering skeleton lines.",
        )

    LOADER_BLOCK_START = "// owui-message-loading-indicator:start"
    LOADER_BLOCK_END = "// owui-message-loading-indicator:end"
    ASSET_KEY = "message-loading-indicator"
    ASSET_ORDER = 0

    _fragment_cache = None
    instance = None

    def __init__(self):
        self.valves = self.Valves()
        Event.instance = self
        reload_bootstrap()

    def _loader_fragment(self) -> str:
        cache_key = self.valves.default_style
        cached = Event._fragment_cache
        if cached and cached[0] == cache_key:
            return cached[1]
        config = _json.dumps(
            {"fallback": self.valves.default_style, "names": STYLE_NAMES}
        )
        js = (
            LOADER_SCRIPT.strip()
            .replace("__CONFIG__", config)
            .replace("__CSS__", _json.dumps(OSK_CSS.strip()))
        )
        fragment = f"{self.LOADER_BLOCK_START}\n{js}\n{self.LOADER_BLOCK_END}"
        Event._fragment_cache = (cache_key, fragment)
        return fragment

    def _publish(self, app) -> None:
        asset_register(
            app,
            LOADER_PATH,
            self.ASSET_KEY,
            self.LOADER_BLOCK_START,
            self.LOADER_BLOCK_END,
            lambda: Event.instance._loader_fragment() if reload_active(app) else "",
            order=self.ASSET_ORDER,
        )

    async def event(
        self,
        event: Optional[dict] = None,
        __event_name__: str = "",
        __app__: Any = None,
        **kwargs,
    ) -> None:
        if __app__ is not None:
            await reload_on_event(__app__, event, __event_name__)


def register(app: Any) -> None:
    Event.instance._publish(app)
