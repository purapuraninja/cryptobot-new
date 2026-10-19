"""
admin/launchers.py — start the web dashboard and the MCP server.

Thin wrappers that shell out to the existing entry points (dashboard.py,
mcp_server.py) using the same Python interpreter, so `admin.py` is a single
front door for all non-bot tooling without duplicating their logic.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_dashboard() -> int:
    """Launch the Flask web dashboard (dashboard.py, port 5050)."""
    return subprocess.call([sys.executable, str(ROOT / "dashboard.py")])


def run_mcp(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8080) -> int:
    """Launch the MCP server (mcp_server.py).

    transport="stdio" for Cursor/Codex, "sse" for an HTTP endpoint.
    """
    cmd = [sys.executable, str(ROOT / "mcp_server.py"), "--transport", transport]
    if transport == "sse":
        cmd += ["--host", host, "--port", str(port)]
    return subprocess.call(cmd)
