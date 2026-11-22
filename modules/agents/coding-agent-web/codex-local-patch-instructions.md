# Editing files with apply_patch

To change a file, call the `apply_patch` tool. Its `input` is the patch text and nothing else: no JSON around it, no Markdown code fence, no explanation.

The patch format, exactly:

- The first line is `*** Begin Patch` and the last line is `*** End Patch`.
- Between them, one or more file sections:
  - `*** Update File: <path>` followed by one or more hunks. A hunk starts with a line `@@` (optionally `@@ <a line that occurs above the change, such as a function header>`), followed by the lines of the change.
  - `*** Add File: <path>` followed by the new file's content, every line starting with `+`.
  - `*** Delete File: <path>` and nothing else.
- Inside a hunk every line starts with exactly one marker character: a space for an unchanged context line, `-` for a line to remove, `+` for a line to add.
- Context and `-` lines must be copied VERBATIM from the current file: same spelling, same indentation (spaces vs tabs), same trailing characters. Read the file first (for example with `sed -n` or `cat`) and copy the lines; never retype them from memory. If the patch fails, read the file again before retrying.
- Give about 3 unchanged lines of context above and below each change, enough to make the location unique.
- Paths are relative to the working directory.

Example: update a file

*** Begin Patch
*** Update File: src/greet.py
@@ def greet(name):
     """Return a greeting."""
-    return "hi " + name
+    return f"Hello, {name}!"
*** End Patch

Example: add a file

*** Begin Patch
*** Add File: tests/test_greet.py
+from src.greet import greet
+
+
+def test_greet():
+    assert greet("Ada") == "Hello, Ada!"
*** End Patch
