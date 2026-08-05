"""Patient-data source selection: live OpenEMR, or captured fixtures.

The whole app reads patient data through two calls, `patients()` (the roster) and
`get_context(uuid)` (one patient's typed context). `FhirClient` implements them
against a live OpenEMR; `FixtureClient` implements the same shape by reading JSON
snapshots captured with `scripts/capture_fixtures.py`.

This is what makes the Path-B public demo possible: set `COPILOT_FIXTURES` to the
snapshot directory and the Co-Pilot runs the three use cases with no EMR, no
database, and no network, so a single small container is the entire deployment.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import settings
from .schemas import PatientContext


class FixtureClient:
    """Serves the roster and per-patient context from captured JSON snapshots."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    async def patients(self, limit: int = 50) -> tuple[list[dict], int]:
        data = json.loads((self.root / "roster.json").read_text(encoding="utf-8"))
        return data[:limit], 0

    async def get_context(self, uuid: str) -> PatientContext:
        path = self.root / "contexts" / f"{uuid}.json"
        return PatientContext.model_validate_json(path.read_text(encoding="utf-8"))

    async def appointments(self, date_str: str, limit: int = 60) -> tuple[list[dict], int]:
        """Read a captured/synthesised schedule. `date_str` is ignored for fixtures
        (the snapshot is a single day); real FhirClient filters by date."""
        path = self.root / "schedule.json"
        if not path.exists():
            return [], 0
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[:limit], 0

    async def aclose(self) -> None:  # parity with FhirClient
        return None


def get_data_client():
    """FixtureClient when COPILOT_FIXTURES points at a snapshot dir, else the live
    FhirClient. Imported lazily so the fixtures path needs no httpx/OpenEMR."""
    if settings.fixtures_dir:
        return FixtureClient(settings.fixtures_dir)
    from .fhir_client import FhirClient
    return FhirClient()
