"""Async FHIR client for OpenEMR.

Implements the D-7 read strategy: authenticate as the user (password grant for
local dev), then read the patient's resources in parallel, filtering the fat
Observation endpoint to a recent window. All reads carry the user's token, so
OpenEMR enforces scope per request (CTRL-3) and we never reimplement authz.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from .config import settings
from .schemas import (
    Allergy, Demographics, Encounter, LabResult, Medication, PatientContext,
    Problem, VitalSign,
)


# ------- small FHIR extraction helpers -------

def _cc_text(cc: Optional[dict]) -> Optional[str]:
    if not cc:
        return None
    if cc.get("text"):
        return cc["text"]
    for c in cc.get("coding", []):
        if c.get("display"):
            return c["display"]
    return None


def _cc_code(cc: Optional[dict], system_contains: str = "") -> Optional[str]:
    if not cc:
        return None
    for c in cc.get("coding", []):
        if not system_contains or system_contains in (c.get("system") or ""):
            return c.get("code")
    return None


def _human_name(res: dict) -> str:
    names = res.get("name") or []
    if not names:
        return "(unknown)"
    n = names[0]
    given = " ".join(n.get("given", []))
    return f"{given} {n.get('family', '')}".strip()


def _clean(v: Optional[str]) -> Optional[str]:
    # DQ-6: some imported results carry the literal template placeholder
    # "{entry.value}" (and similar). Never surface these as if they were data.
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"none", "null"} or ("{" in s and "}" in s):
        return None
    return s


def _fmt_qty(v: Any) -> str:
    """Render a FHIR quantity value without a spurious trailing .0 (BP is integral)."""
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else str(f)
    except (TypeError, ValueError):
        return str(v)


def _norm_bp_unit(u: Optional[str]) -> str:
    return "mmHg" if (u or "").replace(" ", "") in {"mm[Hg]", "mmHg"} else (u or "mmHg")


def _component_qty(res: dict, loinc_code: str) -> Optional[dict]:
    for c in res.get("component") or []:
        if _cc_code(c.get("code"), "loinc") == loinc_code:
            q = c.get("valueQuantity")
            if q and q.get("value") is not None:
                return q
    return None


def _obs_value(res: dict) -> tuple[Optional[str], Optional[str]]:
    if "valueQuantity" in res:
        q = res["valueQuantity"]
        return (_clean(str(q.get("value"))), q.get("unit"))
    if "valueString" in res:
        return (_clean(res["valueString"]), None)
    if "valueCodeableConcept" in res:
        return (_clean(_cc_text(res["valueCodeableConcept"])), None)
    # Blood pressure (and similar panels) carry no top-level value: the reading is
    # in component[]. Systolic (LOINC 8480-6) over diastolic (8462-4) is rendered as
    # a single "120/80" vital, so both numbers surface and ground together.
    if res.get("component"):
        sys_q = _component_qty(res, "8480-6")
        dia_q = _component_qty(res, "8462-4")
        if sys_q and dia_q:
            return (f"{_fmt_qty(sys_q['value'])}/{_fmt_qty(dia_q['value'])}",
                    _norm_bp_unit(sys_q.get("unit")))
        # single meaningful component: fall back to its own value/unit
        for c in res["component"]:
            q = c.get("valueQuantity")
            if q and q.get("value") is not None:
                return (_clean(_fmt_qty(q["value"])), q.get("unit"))
    return (None, None)


def _dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


class FhirClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        self._client = httpx.AsyncClient(verify=settings.verify_tls, timeout=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        data = {
            "grant_type": "password",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "scope": ("openid api:fhir user/Patient.read user/Condition.read "
                      "user/MedicationRequest.read user/Observation.read "
                      "user/Encounter.read user/AllergyIntolerance.read"),
            "user_role": "users",
            "username": settings.dev_user,
            "password": settings.dev_pass,
        }
        r = await self._client.post(settings.token_url, data=data)
        r.raise_for_status()
        body = r.json()
        if "access_token" not in body:
            raise RuntimeError(f"token error: {body}")
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    async def _fetch(self, path: str, params: Optional[dict] = None) -> tuple[Any, int]:
        token = await self._get_token()
        t0 = time.perf_counter()
        r = await self._client.get(
            f"{settings.fhir}/{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        ms = int((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        return r.json(), ms

    @staticmethod
    def _entries(bundle: Any) -> list[dict]:
        if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
            return []
        return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]

    # ------- per-resource reads -------

    async def patient(self, uuid: str) -> tuple[Optional[Demographics], int]:
        res, ms = await self._fetch(f"Patient/{uuid}")
        if res.get("resourceType") != "Patient":
            return None, ms
        return Demographics(
            source_id=f"Patient/{res.get('id')}",
            name=_human_name(res),
            birth_date=res.get("birthDate"),
            sex=res.get("gender"),
        ), ms

    async def conditions(self, uuid: str) -> tuple[list[Problem], int]:
        b, ms = await self._fetch("Condition", {"patient": uuid})
        out = [
            Problem(
                source_id=f"Condition/{r.get('id')}",
                code=_cc_code(r.get("code"), "snomed") or _cc_code(r.get("code")),
                text=_cc_text(r.get("code")) or "(unnamed problem)",
                onset=(_dt(r.get("onsetDateTime")).date() if _dt(r.get("onsetDateTime")) else None),
                clinical_status=_cc_text(r.get("clinicalStatus")),
            )
            for r in self._entries(b)
        ]
        return out, ms

    async def medications(self, uuid: str) -> tuple[list[Medication], int]:
        b, ms = await self._fetch("MedicationRequest", {"patient": uuid})
        out = [
            Medication(
                source_id=f"MedicationRequest/{r.get('id')}",
                code=_cc_code(r.get("medicationCodeableConcept"), "rxnorm"),
                text=_cc_text(r.get("medicationCodeableConcept")) or "(unnamed medication)",
                status=r.get("status"),
                authored_on=(_dt(r.get("authoredOn")).date() if _dt(r.get("authoredOn")) else None),
            )
            for r in self._entries(b)
        ]
        return out, ms

    async def labs(self, uuid: str) -> tuple[list[LabResult], int]:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=30 * settings.lab_lookback_months)).date().isoformat()
        # Filter to a recent window (D-7 / DQ-3) and cap the count (PERF-4).
        b, ms = await self._fetch("Observation", {
            "patient": uuid, "category": "laboratory",
            "date": f"ge{cutoff}", "_count": str(settings.lab_max),
        })
        out = []
        for r in self._entries(b):
            val, unit = _obs_value(r)
            out.append(LabResult(
                source_id=f"Observation/{r.get('id')}",
                loinc=_cc_code(r.get("code"), "loinc"),
                name=_cc_text(r.get("code")) or "(unnamed lab)",
                value=val, unit=unit,
                effective=_dt(r.get("effectiveDateTime")),
            ))
        return out, ms

    async def vitals(self, uuid: str) -> tuple[list[VitalSign], int]:
        b, ms = await self._fetch("Observation", {
            "patient": uuid, "category": "vital-signs", "_count": "200",
        })
        out = []
        for r in self._entries(b):
            val, unit = _obs_value(r)
            out.append(VitalSign(
                source_id=f"Observation/{r.get('id')}",
                loinc=_cc_code(r.get("code"), "loinc"),
                name=_cc_text(r.get("code")) or "(unnamed vital)",
                value=val, unit=unit,
                effective=_dt(r.get("effectiveDateTime")),
            ))
        return out, ms

    async def encounters(self, uuid: str) -> tuple[list[Encounter], int]:
        b, ms = await self._fetch("Encounter", {"patient": uuid})
        out = [
            Encounter(
                source_id=f"Encounter/{r.get('id')}",
                date=_dt((r.get("period") or {}).get("start")),
                reason=(_cc_text(r["reasonCode"][0]) if r.get("reasonCode") else None),
                type=(_cc_text(r["type"][0]) if r.get("type") else None),
            )
            for r in self._entries(b)
        ]
        return out, ms

    async def allergies(self, uuid: str) -> tuple[list[Allergy], int]:
        b, ms = await self._fetch("AllergyIntolerance", {"patient": uuid})
        out = [
            Allergy(
                source_id=f"AllergyIntolerance/{r.get('id')}",
                text=_cc_text(r.get("code")) or "(unnamed allergy)",
                criticality=r.get("criticality"),
            )
            for r in self._entries(b)
        ]
        return out, ms

    # ------- the D-7 parallel context pull -------

    async def get_context(self, uuid: str) -> PatientContext:
        results = await asyncio.gather(
            self.patient(uuid), self.conditions(uuid), self.medications(uuid),
            self.labs(uuid), self.vitals(uuid), self.encounters(uuid),
            self.allergies(uuid),
            return_exceptions=True,
        )
        # (fetch label, PatientContext attribute)
        slots = [
            ("patient", "demographics"), ("conditions", "problems"),
            ("medications", "medications"), ("labs", "labs"),
            ("vitals", "vitals"), ("encounters", "encounters"),
            ("allergies", "allergies"),
        ]
        ctx = PatientContext(patient_id=uuid)
        for (label, attr), res in zip(slots, results):
            if isinstance(res, Exception):
                ctx.warnings.append(f"{label} failed: {res!r}")
                continue
            data, ms = res
            ctx.fetch_ms[label] = ms
            setattr(ctx, attr, data)
        return ctx
