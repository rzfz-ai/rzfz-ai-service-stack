# SPDX-License-Identifier: Apache-2.0
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Runtime patches in razzfazz-dify-api: three to `unstructured` (#1981, #2073)
and one to Dify's structured output (#3229).

Loaded by `razzfazz_unstructured_patches.pth` in the venv's site-packages. The
.pth only installs an import hook; each patch is applied when its module is
FIRST imported, so a Python process that never imports unstructured (gunicorn
before the first upload, celery beat, flask CLI, healthchecks) pays nothing.

1. `str(element)` returns "" when `element.text` is None (`.text` stays None).
   unstructured 0.27.5's hi_res path leaves `Image.text = None` when OCR finds
   no text in an image (a signature logo). `Text.__str__` returns `self.text`,
   `Image` inherits it, and graphon's `.msg` extractor (Dify's workflow
   Document Extractor: `"\\n".join(str(e) for e in elements)`) raises
   `TypeError: __str__ returned non-string`, failing the whole document. Both
   `Text.__str__` and `Element.__str__` are patched: Text overrides Element's.

2. A `.msg` attachment that fails with an error other than the ones upstream
   already skips (UnsupportedFileFormatError, ImportError, FileNotFoundError)
   is logged and skipped; the mail body and the other attachments are kept.
   Without the extras those attachments raised ImportError and were skipped;
   with them, an unexpected error in ONE attachment (a missing model cache, a
   corrupt PDF, a tesseract failure) aborted the whole message. MemoryError is
   NOT swallowed.

3. (#2073) Image files never infer tables. `auto.partition` turns table
   inference on for every type outside `skip_infer_table_types` (default
   pdf, jpg, png, heic), so a TIFF or BMP attachment loads
   microsoft/table-transformer-structure-recognition through the hub. That
   model is not baked, and the image's HF_HUB_OFFLINE=1 refuses it on every
   box: the attachment would be skipped (patch 2) and its text lost. With
   this patch an image goes hi_res with the baked YOLOX layout model plus
   tesseract, exactly as JPG/PNG already do upstream. A caller that passes
   `infer_table_structure=True` explicitly is not overridden (Dify does not).

4. (#3229) Booleans in a structured-output schema stay booleans. Dify 1.18's
   `_prepare_schema_for_model` (core/llm_generator/output_parser/
   structured_output.py) rewrites every `"type": "boolean"` to `"string"`
   before the schema goes to a model with native structured output, and
   graphon's LLM node then validates the parsed reply against the ORIGINAL
   schema (Draft7Validator, graphon #237): `'false' is not of type 'boolean'`
   on every workflow node with a boolean field. Upstream removed the
   conversion (langgenius/dify#44962, commit 0bd201254, in 1.19.0); this
   patch makes `convert_boolean_to_string` a no-op, which is the same
   behaviour. Remove it with the bump to Dify >= 1.19.

Remove patch 1 once upstream `str(Image(text=None))` returns "" without it
(check in a throwaway container after `rm` of the .pth); remove patch 2
once upstream skips any per-attachment Exception; remove patch 3 if a table
model is ever baked. On a Dify bump, the build guard verify-api-extras.py
fails if any patch no longer takes effect.
Registry: docs/upstream-monkey-patches.md.
"""
from __future__ import annotations

import functools
import importlib.machinery
import logging
import sys

ELEMENTS = "unstructured.documents.elements"
MSG = "unstructured.partition.msg"
AUTO = "unstructured.partition.auto"
STRUCTURED_OUTPUT = "core.llm_generator.output_parser.structured_output"

#: Every file type unstructured routes to partition_image (file_utils/model.py,
#: 0.27.5). No table model is baked for them.
IMAGE_TYPES = frozenset({"bmp", "heic", "jpg", "png", "tiff"})


def _str(self) -> str:
    t = self.text
    if isinstance(t, str):
        return t
    return "" if t is None else str(t)


def patch_elements(mod) -> None:
    # Text overrides Element.__str__ and Image(Text) inherits Text's: both are patched.
    for cls in (mod.Element, mod.Text):
        if "__str__" in vars(cls):
            cls.__str__ = _str
    mod._razzfazz_1981_str = True


#: A parser's exception text can quote a fragment of the attachment (a
#: ValueError from a number conversion); the log keeps the type and a prefix.
MAX_LOGGED_ERROR = 200


def _capped(e: BaseException) -> str:
    text = str(e)
    return text if len(text) <= MAX_LOGGED_ERROR else text[:MAX_LOGGED_ERROR] + "…"


def patch_msg(mod) -> None:
    cls = mod._AttachmentPartitioner
    orig = cls._iter_elements
    log = getattr(mod, "logger", None) or logging.getLogger("unstructured")

    def _iter_elements(self):
        try:
            elements = list(orig(self))
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 - the decision is: one broken attachment never loses the mail
            log.warning(
                "Skipping attachment %s: %s (razzfazz #1981: kept the message body and the other attachments)",
                self._attachment_file_name,
                f"{type(e).__name__}: {_capped(e)}",
            )
            return
        yield from elements

    _iter_elements._razzfazz_1981 = True
    cls._iter_elements = _iter_elements
    mod._razzfazz_1981_attachments = True


def patch_auto(mod) -> None:
    orig = mod.decide_table_extraction

    @functools.wraps(orig)
    def decide_table_extraction(filetype, *args, **kwargs):
        if filetype is not None and filetype.name.lower() in IMAGE_TYPES:
            return False
        return orig(filetype, *args, **kwargs)

    decide_table_extraction._razzfazz_2073 = True
    mod.decide_table_extraction = decide_table_extraction
    mod._razzfazz_2073_no_image_tables = True


def _keep_booleans(schema) -> None:
    """#3229: upstream 1.19's behaviour. The schema is left as it is."""
    return None


_keep_booleans._razzfazz_3229 = True


def patch_structured_output(mod) -> None:
    # `_prepare_schema_for_model` looks the name up in the module globals at
    # call time, so replacing the module attribute is enough. A Dify without
    # the function (>= 1.19) needs nothing: the guard reports it as obsolete.
    if hasattr(mod, "convert_boolean_to_string"):
        mod.convert_boolean_to_string = _keep_booleans
        mod._razzfazz_3229_keep_booleans = True


PATCHES = {ELEMENTS: patch_elements, MSG: patch_msg, AUTO: patch_auto, STRUCTURED_OUTPUT: patch_structured_output}


class _Finder:
    """Wraps the normal loader of the target modules: run it, then patch."""

    def find_spec(self, name, path, target=None):
        patch = PATCHES.get(name)
        if patch is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is not None and spec.loader is not None and hasattr(spec.loader, "exec_module"):
            orig = spec.loader.exec_module

            def exec_module(module, _orig=orig, _patch=patch):
                _orig(module)
                _patch(module)

            spec.loader.exec_module = exec_module
        return spec


def install() -> None:
    for name, patch in PATCHES.items():
        if name in sys.modules:
            patch(sys.modules[name])
    if not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.insert(0, _Finder())


install()
