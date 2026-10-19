"""
admin.py — operator CLI entry point.

Single front door for all non-bot tooling: pnl_store maintenance plus the
web dashboard and MCP server launchers. See `python admin.py --help`.
"""

import sys
from pathlib import Path

# Ensure the project root is importable regardless of CWD.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from admin.cli import main

if __name__ == "__main__":
    sys.exit(main())
