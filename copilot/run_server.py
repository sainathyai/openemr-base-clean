"""Launch the Clinical Co-Pilot serving layer.

    python run_server.py            # http://localhost:8000
    COPILOT_PORT=8080 python run_server.py

Without ANTHROPIC_API_KEY the deterministic stubs answer (no Claude spend), which
is the intended mode for building and demoing the UI. Requires OpenEMR reachable at
OPENEMR_BASE for live patient data.
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "server.main:app",
        host=os.getenv("COPILOT_HOST", "127.0.0.1"),
        port=int(os.getenv("COPILOT_PORT", "8000")),
        reload=bool(os.getenv("COPILOT_RELOAD")),
    )
