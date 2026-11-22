"""
Patch: GPUStack v0.7.1 Middleware – „null"-Content im SSE-Stream filtern.

Problem:
  llama-server (llama.cpp b7966) sendet im ersten SSE-Chunk eines Streaming-
  Responses ein role-only Delta: {"role":"assistant","content":null}
  Das ist korrekt gemäß OpenAI-Spec, aber llama-box hat das nicht gesendet.
  Clients (z.B. Dify) interpretieren das null als String "null" und zeigen
  es als Prefix vor der eigentlichen Antwort an.

Fix:
  Im process_chunk()-Code der Middleware werden Chunks mit "content":null
  übersprungen (continue), statt sie an den Client weiterzuleiten.
"""

import glob
import importlib.util
import os
import sys

# #2812: the interpreter differs per image (the Vulkan build ships python3.10,
# upstream gpustack/gpustack:v0.7.1-cpu ships python3.11), so the target file is
# located from the installed package, never from a hard-coded python version.
MIDDLEWARE_RELATIVE = os.path.join("api", "middlewares.py")
#: test/override hook: an explicit path wins over discovery
MIDDLEWARE_ENV = "GPUSTACK_MIDDLEWARE_PATH"
SITE_GLOB = "/usr/local/lib/python3*/dist-packages/gpustack"


def resolve_middleware_path():
    """Absolute path of gpustack/api/middlewares.py, or None when not found."""
    override = os.environ.get(MIDDLEWARE_ENV)
    if override:
        return override if os.path.isfile(override) else None
    try:
        spec = importlib.util.find_spec("gpustack")
    except (ImportError, ValueError):
        spec = None
    roots = list(spec.submodule_search_locations or []) if spec else []
    roots += sorted(glob.glob(SITE_GLOB))
    for root in roots:
        candidate = os.path.join(root, MIDDLEWARE_RELATIVE)
        if os.path.isfile(candidate):
            return candidate
    return None

# Der originale else-Block in process_chunk(), der Non-Usage-Chunks
# unverändert durchreicht:
OLD_CODE = '''\
        else:
            yield f"{line}\\n\\n".encode("utf-8")'''

# Neuer Code: Prüfe ob der Chunk ein null-Content-Delta ist und überspringe ihn.
NEW_CODE = '''\
        else:
            # Patch: Skip SSE chunks where delta.content is null.
            # llama-server sends {"role":"assistant","content":null} as first
            # streaming chunk. llama-box didn't do this. Clients like Dify
            # render null as the string "null" before the actual response.
            if '"content":null' in line or '"content": null' in line:
                try:
                    _chunk_json = json.loads(line.split('data: ', 1)[-1].strip())
                    _choices = _chunk_json.get('choices', [])
                    if _choices and all(
                        c.get('delta', {}).get('content') is None
                        for c in _choices
                    ):
                        continue  # skip this null-content chunk
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass  # parse error → pass through unchanged
            yield f"{line}\\n\\n".encode("utf-8")'''


def main():
    path = resolve_middleware_path()
    if path is None:
        sys.stderr.write(
            "FATAL(#2812): patch_null_content.py cannot find "
            "gpustack/" + MIDDLEWARE_RELATIVE.replace(os.sep, "/") + " "
            "(no importable gpustack package, nothing under " + SITE_GLOB + "). "
            "The null-content patch is NOT applied.\n")
        return 1
    with open(path, "r") as f:
        content = f.read()

    if OLD_CODE not in content:
        if "skip this null-content chunk" in content:
            print("Already patched – skipping.")
            return 0
        raise RuntimeError(
            f"Could not find expected code block in {path}.\n"
            "The GPUStack version may have changed."
        )

    patched = content.replace(OLD_CODE, NEW_CODE, 1)

    with open(path, "w") as f:
        f.write(patched)

    print("Successfully patched process_chunk() to filter null-content SSE chunks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
