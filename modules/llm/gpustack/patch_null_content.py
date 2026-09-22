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

import re

MIDDLEWARE_PATH = (
    "/usr/local/lib/python3.10/dist-packages/gpustack/api/middlewares.py"
)

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
    with open(MIDDLEWARE_PATH, "r") as f:
        content = f.read()

    if OLD_CODE not in content:
        if "skip this null-content chunk" in content:
            print("Already patched – skipping.")
            return
        raise RuntimeError(
            f"Could not find expected code block in {MIDDLEWARE_PATH}.\n"
            "The GPUStack version may have changed."
        )

    patched = content.replace(OLD_CODE, NEW_CODE, 1)

    with open(MIDDLEWARE_PATH, "w") as f:
        f.write(patched)

    print("Successfully patched process_chunk() to filter null-content SSE chunks")


if __name__ == "__main__":
    main()
