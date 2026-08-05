"""Multimodal Evidence Agent (Week 2).

Ingests unstructured clinical documents (lab report PDFs, patient intake forms),
extracts structured facts under strict schemas, and emits those facts with a
document-span provenance so they flow through the SAME verification/grounding
gate the structured-FHIR path already uses (D-8: the LLM never authors a value).

The single design invariant carried over from Week 1: every clinical fact carries
a source id the verification layer can check. For FHIR that id is the resource id;
for documents it is a `DocumentSpan` (doc id + page + bbox + the exact text), so a
physician can click a claim and land on the pixels it came from.
"""
