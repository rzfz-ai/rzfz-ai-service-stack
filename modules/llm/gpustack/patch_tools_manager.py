"""
Patch for GPUStack v0.7.1 tools_manager.py
Fixes: _link_llama_box_default_dir does not handle existing non-symlink directories,
       causing FileExistsError when llama-box-default already exists as a regular directory.
"""
import sys

TARGET = "/usr/local/lib/python3.10/dist-packages/gpustack/worker/tools_manager.py"

with open(TARGET, "r") as f:
    content = f.read()

# Find the buggy section and replace it with a version that handles existing dirs
old_code = "            os.symlink(src_fold_name, dst_fold_name, dir_fd=target_dir_fd)"
new_code = """            if os.path.exists(dst_dir) and not os.path.islink(dst_dir):
                shutil.rmtree(dst_dir)
            os.symlink(src_fold_name, dst_fold_name, dir_fd=target_dir_fd)"""

if old_code not in content:
    print("WARNING: Could not find target code to patch!", file=sys.stderr)
    sys.exit(1)

# Only replace the SECOND occurrence (the one in _link_llama_box_default_dir, not _link_llama_box_rpc_server)
parts = content.split(old_code)
if len(parts) < 3:
    # If there's only one occurrence, just replace it
    content = content.replace(old_code, new_code, 1)
else:
    # Replace the second occurrence (index 1->2 boundary)
    content = old_code.join(parts[:2]) + new_code + old_code.join(parts[2:])

with open(TARGET, "w") as f:
    f.write(content)

print("Successfully patched _link_llama_box_default_dir")
