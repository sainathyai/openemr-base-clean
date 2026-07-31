"""Runtime configuration, loaded from environment (.env for local dev)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    base: str = os.getenv("OPENEMR_BASE", "https://localhost:9300")
    verify_tls: bool = _bool("OPENEMR_VERIFY_TLS", False)
    client_id: str = os.getenv("COPILOT_CLIENT_ID", "")
    client_secret: str = os.getenv("COPILOT_CLIENT_SECRET", "")
    dev_user: str = os.getenv("COPILOT_DEV_USER", "drhouse")
    dev_pass: str = os.getenv("COPILOT_DEV_PASS", "DocPass123!")
    lab_lookback_months: int = int(os.getenv("COPILOT_LAB_LOOKBACK_MONTHS", "18"))
    lab_max: int = int(os.getenv("COPILOT_LAB_MAX", "200"))

    @property
    def fhir(self) -> str:
        return f"{self.base}/apis/default/fhir"

    @property
    def token_url(self) -> str:
        return f"{self.base}/oauth2/default/token"


settings = Settings()
