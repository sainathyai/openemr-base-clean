"""Typed contracts for the patient context.

These Pydantic models are the source of truth for tool outputs (per the
engineering requirement). Raw FHIR never reaches the agent unshaped: it is parsed
into these models, and every clinical fact carries a `source_id` (the FHIR
resource id) so attribution is correct by construction (D-8).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class Demographics(BaseModel):
    source_id: str
    name: str
    birth_date: Optional[date] = None
    sex: Optional[str] = None


class Problem(BaseModel):
    source_id: str
    code: Optional[str] = None
    text: str
    onset: Optional[date] = None
    clinical_status: Optional[str] = None


class Medication(BaseModel):
    source_id: str
    code: Optional[str] = None
    text: str
    status: Optional[str] = None
    authored_on: Optional[date] = None


class LabResult(BaseModel):
    source_id: str
    loinc: Optional[str] = None
    name: str
    value: Optional[str] = None   # kept as string: ~26% are non-numeric (DQ-4)
    unit: Optional[str] = None
    effective: Optional[datetime] = None  # anchored on order date (DQ-3)
    # abnormal_flag is intentionally NOT populated from source (DQ-2: absent).
    # It is computed later by the deterministic verification layer (D-9).


class VitalSign(BaseModel):
    source_id: str
    loinc: Optional[str] = None
    name: str
    value: Optional[str] = None
    unit: Optional[str] = None
    effective: Optional[datetime] = None


class Encounter(BaseModel):
    source_id: str
    date: Optional[datetime] = None
    reason: Optional[str] = None
    type: Optional[str] = None


class Allergy(BaseModel):
    source_id: str
    text: str
    criticality: Optional[str] = None


class PatientContext(BaseModel):
    """Everything the agent needs for one patient, filtered and attributed."""
    patient_id: str
    demographics: Optional[Demographics] = None
    problems: list[Problem] = Field(default_factory=list)
    medications: list[Medication] = Field(default_factory=list)
    labs: list[LabResult] = Field(default_factory=list)
    vitals: list[VitalSign] = Field(default_factory=list)
    encounters: list[Encounter] = Field(default_factory=list)
    allergies: list[Allergy] = Field(default_factory=list)
    # observability / provenance
    fetch_ms: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
