# SPDX-License-Identifier: Apache-2.0
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Build-time effect guard for razzfazz-dify-api (#1981).

Runs inside the image's venv as the last step of the extras install
(modules/dify/Dockerfile.api). It refuses, by failing the BUILD:

* a CUDA torch: `unstructured[pdf]` needs torch through unstructured-inference,
  and PyPI's default torch wheel is the CUDA build. With it the extras added
  8.61 GB on a test box; on the CPU wheels the same extras add about 0.9 GB.
* any nvidia-* distribution (the CUDA runtime wheels that come with it);
* GUI opencv (`opencv-python`): it dlopens libGL at `import cv2`. Without
  libGL that is an ImportError, and partition_msg counts ImportError as an
  expected attachment error, so it SKIPS the PDF and image attachments with a
  warning. The attachments vanish without a failure.
* any attachment parser that does not import;
* a missing runtime tool: tesseract (with eng + deu) and poppler's pdftoppm /
  pdfinfo. A missing tesseract raises TesseractNotFoundError, an OSError, not
  an expected attachment error;
* an inactive #1981 patch (razzfazz_unstructured_patches.py, loaded by a .pth):
  `str(Image(text=None))` must be "", and an attachment whose partitioner
  raises a non-ImportError must be skipped, not abort the message.
* a model that would be fetched at run time (#2073): unstructured downloads
  en_core_web_sm from GitHub the first time it splits sentences, and the
  hi_res layout model (YOLOX) from Hugging Face. Both are baked; this loads
  them through unstructured's own entry points in a child process with every
  INET socket refused, once with the offline belt's env and HOME unset (where
  even an ATTEMPT to dial fails the build) and once without the belt and with
  HOME unwritable (the hub must fall back to the baked cache, and that run
  must have met a refused connection to huggingface.co). As root the child
  runs as `dify` (no `dify` user is a failure), so the baked files must be
  readable by the runtime user. The spaCy wheel sha the build checked
  (/opt/razzfazz/models.env) must equal unstructured's own pin, and the layout
  model's Apache-2.0 LICENSE.txt must sit next to the weights. The cut is
  Python-level: sockets opened by native code (hf_xet, a subprocess) bypass
  it, so `docker run --network none` on a box is the authoritative proof.
* image files that would infer tables (#2073 patch 3): a TIFF/BMP attachment
  would load the table-transformer model, which is not baked.

It stays in the image, so `python /opt/razzfazz/verify-api-extras.py` in a
running container answers the same question on a box.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import types

#: The parsers partition_msg dispatches attachments to, and what each needs.
PARSERS = (
    "unstructured.partition.pdf",
    "unstructured.partition.image",
    "unstructured.partition.xlsx",
    "unstructured.partition.pptx",
    "unstructured.partition.docx",
    "unstructured.partition.msg",
)


#: Binaries the parsers shell out to, and the tesseract languages the stack needs.
TOOLS = ("tesseract", "pdftoppm", "pdfinfo")
TESSERACT_LANGS = ("eng", "deu")


#: What compose.offline.yml gives the four Dify Python services
#: (scripts/gen-offline-overlay.py, TELEMETRY_OFF); test_2073 keeps them equal.
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
               "DO_NOT_TRACK": "1", "SCARF_NO_ANALYTICS": "true"}
#: Not creatable even by root (/dev/null is not a directory).
UNWRITABLE_HOME = "/dev/null/razzfazz-no-home"
RUNTIME_USER = "dify"
#: The hub the run without the belt must have been refused by.
HUB_HOST = "huggingface.co"
#: Docker's proxies.default (apply-corporate-proxy.sh) injects these into every
#: build RUN and container. The probe simulates NO network, so they are dropped:
#: with them the hub dials the proxy, never HUB_HOST (measured, hub 1.6.0).
PROXY_VARS = tuple(v for n in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") for v in (n, n.lower()))
#: Written by Dockerfile.api: the spaCy pins the build checked the wheel against.
MODELS_ENV = os.environ.get("RAZZFAZZ_MODELS_ENV", "/opt/razzfazz/models.env")

#: Runs in the child. The socket patch is the network cut: getaddrinfo,
#: create_connection and every INET/INET6 connect raise and are recorded.
MODEL_PROBE = r"""
import json, os, socket
attempts = []
def _refuse(*a, **k):
    attempts.append(repr(a[:2])[:120])
    raise OSError(101, "network is off (razzfazz build guard, #2073)")
socket.getaddrinfo = _refuse
socket.create_connection = _refuse
_connect, _connect_ex = socket.socket.connect, socket.socket.connect_ex
def _guarded(orig):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _refuse(address)
        return orig(self, address)
    return connect
socket.socket.connect = _guarded(_connect)
socket.socket.connect_ex = _guarded(_connect_ex)
out = {"attempts": attempts}
try:
    import importlib.metadata as md
    from unstructured.nlp import tokenize
    out["sentences"] = len(tokenize.sent_tokenize("The model is baked. Nothing is fetched."))
    out["spacy_pin"] = tokenize._SPACY_MODEL_VERSION
    out["spacy_baked"] = md.version("en_core_web_sm")
    out["spacy_pin_sha"] = tokenize._SPACY_MODEL_SHA256
    from unstructured.partition.pdf import default_hi_res_model
    from unstructured_inference.models.base import get_model
    out["layout_model"] = default_hi_res_model()
    # not realpath: the snapshot entry is a symlink into blobs/
    out["layout_path"] = os.path.abspath(get_model(out["layout_model"]).model_path)
    out["layout_license"] = os.path.isfile(os.path.join(os.path.dirname(out["layout_path"]), "LICENSE.txt"))
except BaseException as e:
    out["error"] = f"{type(e).__name__}: {e}"[:400]
print("RAZZFAZZ-MODELS " + json.dumps(out))
"""


def _probe_env(*, belt: bool, home: str | None) -> dict[str, str]:
    env = dict(os.environ)
    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HOME", *PROXY_VARS):
        env.pop(k, None)
    if belt:
        env.update(OFFLINE_ENV)
    if home is not None:
        env["HOME"] = home
    # the root pass runs on a throwaway numba cache (main()); the dify probe
    # gets the image's dir back, so it warms the dir that ships, as dify
    if _IMAGE_NUMBA_CACHE_DIR is not None:
        env["NUMBA_CACHE_DIR"] = _IMAGE_NUMBA_CACHE_DIR
    # the child sees the same modules this interpreter sees (a venv, or a test's stub site)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


def _runtime_user() -> dict:
    """{} when not root; raises LookupError as root without the runtime user
    (the probe would run as root and prove nothing about readability)."""
    if os.geteuid() != 0:
        return {}
    try:
        pwd.getpwnam(RUNTIME_USER)
    except KeyError:
        raise LookupError(f"running as root but there is no '{RUNTIME_USER}' user to run the model probe as")
    # extra_groups=[]: drop root's supplementary groups, or group-readable root files would pass
    return {"user": RUNTIME_USER, "group": RUNTIME_USER, "extra_groups": []}


def _baked_spacy_sha() -> str | None:
    try:
        with open(MODELS_ENV, encoding="utf-8") as f:
            pins = dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError:
        return None
    return pins.get("SPACY_MODEL_SHA256")


def model_problems() -> list[str]:
    found: list[str] = []
    try:
        as_user = _runtime_user()
    except LookupError as e:
        return [f"#2073: {e}"]
    runs = (("offline belt, HOME unset", True, None),
            ("hub unreachable without the belt, HOME unwritable", False, UNWRITABLE_HOME))
    for label, belt, home in runs:
        cmd = [sys.executable, *(["-S"] if sys.flags.no_site else []), "-c", MODEL_PROBE]
        r = subprocess.run(cmd, env=_probe_env(belt=belt, home=home), cwd="/", capture_output=True, text=True,
                           timeout=600, **as_user)
        line = next((l for l in r.stdout.splitlines() if l.startswith("RAZZFAZZ-MODELS ")), None)
        if r.returncode != 0 or line is None:
            found.append(f"#2073 model probe ({label}) crashed, rc={r.returncode}: {r.stderr.strip()[-400:]}")
            continue
        out = json.loads(line[len("RAZZFAZZ-MODELS "):])
        if "error" in out:
            found.append(f"#2073: a baked model does not load with the network off ({label}): {out['error']}")
            continue
        if out["sentences"] != 2:
            found.append(f"#2073: spaCy split a two-sentence text into {out['sentences']} ({label})")
        if out["spacy_baked"] != out["spacy_pin"]:
            found.append(f"#2073: en_core_web_sm {out['spacy_baked']} is baked but unstructured pins"
                         f" {out['spacy_pin']} - move SPACY_MODEL_VERSION/SHA256 in Dockerfile.api")
        baked_sha = _baked_spacy_sha()
        if baked_sha != out["spacy_pin_sha"]:
            found.append(f"#2073: the spaCy wheel baked has sha256 {baked_sha} ({MODELS_ENV}) but unstructured pins"
                         f" {out['spacy_pin_sha']} - move SPACY_MODEL_SHA256 in Dockerfile.api")
        if not out["layout_license"]:
            found.append(f"#2073: the layout model's LICENSE.txt (Apache-2.0) is not next to {out['layout_path']}")
        if "/snapshots/" not in out["layout_path"]:
            found.append(f"#2073: layout model {out['layout_model']} resolved outside the hub cache: {out['layout_path']}")
        if belt and out["attempts"]:
            found.append(f"#2073: loading the models tried the network with the offline belt set ({label}):"
                         f" {out['attempts'][:3]}")
        if not belt and not any(HUB_HOST in a for a in out["attempts"]):
            # without the belt the hub is always asked first; no refused
            # connection to it means the probe never met the cut it claims to prove
            found.append(f"#2073: no connection to {HUB_HOST} was refused ({label}) - the network cut was not"
                         f" exercised (refused: {out['attempts'][:3]})")
    return found


def _dist_names() -> set[str]:
    return {(d.metadata["Name"] or "").lower() for d in md.distributions()}


def problems() -> list[str]:
    found: list[str] = []
    try:
        import torch
    except ImportError as e:
        return [f"torch does not import: {e}"]
    if torch.version.cuda is not None:
        found.append(f"CUDA torch {torch.__version__} (cuda {torch.version.cuda}) - the CPU index was bypassed")
    names = _dist_names()
    cuda = sorted(n for n in names if n.startswith("nvidia-"))
    if cuda:
        found.append(f"nvidia-* dists in the venv: {cuda}")
    if "triton" in names:
        found.append("triton is installed - it comes only with a CUDA torch")
    if "opencv-python" in names:
        found.append("GUI opencv-python is installed - it needs libGL; use opencv-python-headless")
    if "opencv-python-headless" not in names:
        found.append("opencv-python-headless is not installed")
    for mod in ("cv2", "msoffcrypto", *PARSERS):
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001 - any failure here is a broken image
            found.append(f"{mod} does not import: {type(e).__name__}: {e}")
    found += tool_problems()
    found += patch_problems()
    found += structured_output_problems()
    found += model_problems()
    return found


def tool_problems() -> list[str]:
    found = [f"{t} is not on PATH" for t in TOOLS if shutil.which(t) is None]
    if shutil.which("tesseract"):
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=60)
        langs = set((r.stdout + r.stderr).split())
        missing = [lang for lang in TESSERACT_LANGS if lang not in langs]
        if r.returncode != 0 or missing:
            found.append(f"tesseract --list-langs (rc={r.returncode}) lacks {missing or 'a usable list'}")
    return found


def patch_problems() -> list[str]:
    """Run both #1981 patches for real, through the public classes they patch."""
    found: list[str] = []
    try:
        from unstructured.documents.elements import Image, Text

        img = Image(text=None)
        if str(img) != "" or img.text is not None:
            found.append("str(Image(text=None)) is not '' (or .text changed) - the #1981 str patch is not active")
        if str(Text(text="a")) != "a":
            found.append("str(Text('a')) is not 'a' - the #1981 str patch broke normal text")
    except Exception as e:  # noqa: BLE001
        found.append(f"str patch check failed: {type(e).__name__}: {e}")
    try:
        import unstructured.partition.auto as auto
        import unstructured.partition.msg as msg

        def _broken(*_a, **_k):
            raise RuntimeError("razzfazz build guard: a broken attachment")

        att = types.SimpleNamespace(file_name="broken.bin", file_bytes=b"x", last_modified=None)
        opts = types.SimpleNamespace(metadata_file_path="guard.msg", metadata_last_modified=None, partitioning_kwargs={})
        saved = auto.partition
        auto.partition = _broken
        try:
            out = list(msg._AttachmentPartitioner.iter_elements(att, opts))
        finally:
            auto.partition = saved
        if out:
            found.append(f"a broken attachment yielded {len(out)} elements")
    except Exception as e:  # noqa: BLE001
        found.append(f"a broken .msg attachment is not skipped - the #1981 attachment patch is not active ({type(e).__name__}: {e})")
    try:
        import unstructured.partition.auto as auto

        for kind in ("tiff", "bmp"):
            if auto.decide_table_extraction(types.SimpleNamespace(name=kind.upper()), ["pdf", "jpg", "png", "heic"], False):
                found.append(f"#2073: a {kind} attachment would infer tables and load the un-baked table model"
                             " - the #2073 patch 3 is not active")
    except Exception as e:  # noqa: BLE001
        found.append(f"#2073 table check failed for tiff/bmp: {type(e).__name__}: {e}")
    return found


#: Dify's api package; the guard runs from /opt/razzfazz, so it is not on sys.path.
DIFY_API = "/app/api"


def structured_output_problems() -> list[str]:
    """#3229 (patch 4): a boolean in a structured-output schema reaches the model
    as a boolean. Runs Dify's own `_prepare_schema_for_model` on a schema with a
    nested boolean and an array of booleans."""
    found: list[str] = []
    if DIFY_API not in sys.path:
        sys.path.insert(0, DIFY_API)
    try:
        import core.llm_generator.output_parser.structured_output as so
    except Exception as e:  # noqa: BLE001
        return [f"#3229: cannot import Dify's structured_output module to check patch 4 ({type(e).__name__}: {e})"]
    if not hasattr(so, "convert_boolean_to_string"):
        print("note(#3229): Dify no longer converts booleans (>= 1.19) - patch 4 is obsolete, remove it", file=sys.stderr)
        return found
    schema = {"type": "object", "properties": {"is_scan": {"type": "boolean"},
                                               "pages": {"type": "array", "items": {"type": "boolean"}}}}
    try:
        model = types.SimpleNamespace(model="qwen3.6")
        out = so._prepare_schema_for_model("langgenius/openai_api_compatible/openai_api_compatible", model, schema)
        sent = out.get("schema", out)
        if sent["properties"]["is_scan"]["type"] != "boolean" or sent["properties"]["pages"]["items"]["type"] != "boolean":
            found.append("#3229: Dify still sends booleans as strings - patch 4 (razzfazz_unstructured_patches.py) is not active,"
                         " and every structured-output node with a boolean field fails validation")
    except Exception as e:  # noqa: BLE001
        found.append(f"#3229 structured-output check failed: {type(e).__name__}: {e}")
    return found


# NUMBA_CACHE_DIR as the image sets it; main() swaps the process's own value
_IMAGE_NUMBA_CACHE_DIR: str | None = None


def main() -> int:
    # The build runs this as root after the chown, with the image's HOME
    # (/app/api/.home). Its own imports write caches under HOME (matplotlib's
    # font list, onnxruntime's device id); they must not ship root-owned in
    # dify's HOME, so this process gets a throwaway one (#2073).
    # The same for numba: importing the parsers compiles xycut's
    # @njit(cache=True) functions, and numba makes a per-source subdir in
    # NUMBA_CACHE_DIR. Made by root in the image's dir, the dify probe and
    # dify at run time cannot write it ("no locator available"). The root
    # pass gets a throwaway dir; the dify probe gets the image's (#2073).
    global _IMAGE_NUMBA_CACHE_DIR
    _IMAGE_NUMBA_CACHE_DIR = os.environ.get("NUMBA_CACHE_DIR")
    with tempfile.TemporaryDirectory(prefix="razzfazz-guard-home-") as home, \
            tempfile.TemporaryDirectory(prefix="razzfazz-guard-numba-") as numba:
        os.environ["HOME"] = home
        os.environ["NUMBA_CACHE_DIR"] = numba
        return _report(problems())


def _report(found: list[str]) -> int:
    if found:
        for p in found:
            print(f"FATAL(#1981): {p}", file=sys.stderr)
        return 1
    import torch

    print(f"dify-api extras OK: torch {torch.__version__} cpu-only, headless opencv, {len(PARSERS)} parsers import,"
          f" {'/'.join(TOOLS)} present, #1981 patches active; #3229 booleans kept;"
          " #2073 en_core_web_sm + yolox layout model load with the network off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
