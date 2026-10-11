# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""
Log Snapshot Manager for razzfazz.ai Stack

Collects container logs, script logs, system info, and checksum history
into compressed tar.gz snapshots for debugging and support purposes.

Storage: /stack/backups/logs/ (host: ./backups/logs/)
"""

import os
import subprocess
import tarfile
import tempfile
import shutil
from datetime import datetime, timezone

# #22: configurable so the CLI backend runs on the host (default /stack).
STACK_ROOT = os.environ.get("RAZZFAZZ_STACK_ROOT", "/stack")
LOGS_DIR = os.path.join(STACK_ROOT, "backups", "logs")
SETUP_LOG = "/var/log/setup.log"


def _ensure_logs_dir():
    """Create the logs directory if it doesn't exist."""
    os.makedirs(LOGS_DIR, exist_ok=True)


def _run_cmd(cmd, timeout=30):
    """Run a shell command and return stdout. Returns error string on failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str)
        )
        return result.stdout + (result.stderr if result.returncode != 0 else "")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT after {timeout}s]"
    except Exception as e:
        return f"[ERROR: {e}]"


def _get_running_containers():
    """Get list of running container names for the stack."""
    output = _run_cmd(
        ["docker", "compose", "-f", f"{STACK_ROOT}/compose.yml", "ps", "--format", "{{.Names}}"],
        timeout=15,
    )
    containers = [c.strip() for c in output.strip().split("\n") if c.strip()]
    return containers


def _get_container_logs(container_name, tail=2000):
    """Get the last N lines of logs for a container."""
    return _run_cmd(
        ["docker", "logs", "--tail", str(tail), "--timestamps", container_name],
        timeout=30,
    )


def _get_system_info():
    """Collect system information for the snapshot."""
    info_parts = []
    info_parts.append("=" * 60)
    info_parts.append("System Information Snapshot")
    info_parts.append("=" * 60)
    info_parts.append(f"\nTimestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")

    info_parts.append("--- OS / Kernel ---")
    info_parts.append(_run_cmd("uname -a"))

    info_parts.append("--- Docker Version ---")
    info_parts.append(_run_cmd(["docker", "version"], timeout=10))

    info_parts.append("--- Docker Compose Version ---")
    info_parts.append(_run_cmd(["docker", "compose", "version"], timeout=10))

    info_parts.append("--- Disk Usage ---")
    info_parts.append(_run_cmd(["df", "-h", STACK_ROOT]))

    info_parts.append("--- Memory ---")
    info_parts.append(_run_cmd(["free", "-h"], timeout=5))

    info_parts.append("--- Docker Disk Usage ---")
    info_parts.append(_run_cmd(["docker", "system", "df"], timeout=10))

    info_parts.append("--- Container Status ---")
    info_parts.append(
        _run_cmd(
            ["docker", "compose", "-f", f"{STACK_ROOT}/compose.yml", "ps", "--format",
             "table {{.Name}}\t{{.Status}}\t{{.Image}}"],
            timeout=15,
        )
    )

    return "\n".join(info_parts)


def _get_checksum_history():
    """Get checksum history as formatted text."""
    try:
        from checksum_manager import ChecksumManager
        csm = ChecksumManager()
        history = csm.get_history(limit=100)
        if not history:
            return "No checksum history found.\n"
        lines = []
        lines.append("=" * 80)
        lines.append("Checksum History")
        lines.append("=" * 80)
        for entry in history:
            lines.append(
                f"\n[Set #{entry['id']}] {entry['timestamp']} | {entry['source']}"
            )
            lines.append(f"  Comment:  {entry['comment']}")
            lines.append(f"  Overall:  {entry['overall_sha256']}")
            lines.append(f"  Files:    {entry['file_count']}")
        return "\n".join(lines) + "\n"
    except Exception as e:
        return f"[Could not retrieve checksum history: {e}]\n"


class LogManager:
    def __init__(self, logs_dir=LOGS_DIR):
        self.logs_dir = logs_dir

    def create_snapshot(self, reason="Manual snapshot"):
        """
        Collect all logs and create a compressed tar.gz snapshot.
        
        Args:
            reason: Human-readable reason for the snapshot
            
        Returns:
            dict with filename, filepath, size, container_count, reason
        """
        _ensure_logs_dir()

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"log-snapshot-{ts}.tar.gz"
        filepath = os.path.join(self.logs_dir, filename)

        # Create a temp directory to assemble all logs
        tmpdir = tempfile.mkdtemp(prefix="razzfazz-logs-")

        try:
            # 1. System info
            with open(os.path.join(tmpdir, "system-info.txt"), "w") as f:
                f.write(_get_system_info())

            # 2. Container logs
            containers = _get_running_containers()
            container_dir = os.path.join(tmpdir, "containers")
            os.makedirs(container_dir, exist_ok=True)
            for cname in containers:
                logdata = _get_container_logs(cname)
                safe_name = cname.replace("/", "_")
                with open(os.path.join(container_dir, f"{safe_name}.log"), "w") as f:
                    f.write(logdata)

            # 3. Setup log
            if os.path.isfile(SETUP_LOG):
                shutil.copy2(SETUP_LOG, os.path.join(tmpdir, "setup.log"))
            else:
                with open(os.path.join(tmpdir, "setup.log"), "w") as f:
                    f.write("[Setup log not found at /var/log/setup.log]\n")

            # 4. Checksum history
            with open(os.path.join(tmpdir, "checksum-history.txt"), "w") as f:
                f.write(_get_checksum_history())

            # 5. Metadata
            with open(os.path.join(tmpdir, "snapshot-info.txt"), "w") as f:
                f.write(f"Log Snapshot\n")
                f.write(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
                f.write(f"Reason: {reason}\n")
                f.write(f"Containers: {len(containers)}\n")
                f.write(f"Container list: {', '.join(containers)}\n")

            # Create tar.gz
            with tarfile.open(filepath, "w:gz") as tar:
                for entry in os.listdir(tmpdir):
                    tar.add(os.path.join(tmpdir, entry), arcname=entry)

            size = os.path.getsize(filepath)
            return {
                "filename": filename,
                "filepath": filepath,
                "size": size,
                "size_human": self._human_size(size),
                "container_count": len(containers),
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def list_snapshots(self):
        """
        List all available log snapshots.
        
        Returns:
            list of dicts with filename, size, size_human, created
        """
        _ensure_logs_dir()
        snapshots = []
        for fname in sorted(os.listdir(self.logs_dir), reverse=True):
            if fname.startswith("log-snapshot-") and fname.endswith(".tar.gz"):
                fpath = os.path.join(self.logs_dir, fname)
                stat = os.stat(fpath)
                snapshots.append({
                    "filename": fname,
                    "size": stat.st_size,
                    "size_human": self._human_size(stat.st_size),
                    "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    ),
                })
        return snapshots

    def get_snapshot_path(self, filename):
        """
        Get the full path for a snapshot file, with safety check.
        
        Returns:
            Absolute path string or None if file doesn't exist or is outside logs dir
        """
        # Prevent directory traversal
        safe_name = os.path.basename(filename)
        fpath = os.path.join(self.logs_dir, safe_name)
        if os.path.isfile(fpath) and fpath.startswith(self.logs_dir):
            return fpath
        return None

    def delete_snapshot(self, filename):
        """Delete a snapshot file."""
        fpath = self.get_snapshot_path(filename)
        if fpath:
            os.remove(fpath)
            return True
        return False

    @staticmethod
    def _human_size(nbytes):
        """Convert bytes to human-readable size."""
        for unit in ("B", "KB", "MB", "GB"):
            if nbytes < 1024:
                return f"{nbytes:.1f} {unit}"
            nbytes /= 1024
        return f"{nbytes:.1f} TB"
