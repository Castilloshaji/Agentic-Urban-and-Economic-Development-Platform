# Ernakulam Phase 1 — demo dataset

Small, entirely synthetic dataset for building and testing the pipeline
before real Ernakulam data is ingested. One fictional district ("Demo
District") with 1 taluk, 3 panchayats, and 6 wards, sized so the whole
pipeline (ingest -> process -> PostGIS/Neo4j -> digital twin -> RAG ->
agents -> Supervisor) can be run end to end in seconds.

## How to use
Drop this folder's contents into `data/raw/` in the same shape as the real
folder structure (population/, economy/, gis/, transportation/,
environment/, documents/) and point your Stage 1-2 scripts at it. Every
field name matches the real schema in ernakulam_phase1_architecture.md, so
swapping in real downloads later is a file-content swap, not a code change.

## What's included
- 4-level admin hierarchy (district/taluk/panchayat/ward) with valid nested
  geometries, so topology checks (ward inside panchayat, etc.) pass cleanly.
- Panchayat-level population (joins cleanly) + a deliberate legacy
  ward-level population file that does NOT match current ward boundaries —
  use it to test the hard/soft consistency-check and crosswalk logic before
  the real 2011-vs-2025-delimitation mismatch shows up for real.
- District-level economic indicators across 5 years.
- Metro stations, bus stops, roads, a water body, and a flood hazard zone
  that deliberately overlaps two panchayats.
- 3 short text documents for RAG testing, written to reference the demo
  twin's own entities so retrieval-to-entity linkage is testable, not just
  chunking mechanics.

## What's NOT included
Real Ernakulam data. Every value here is fictional/synthetic and must not
be used in the actual review deliverable — swap it out once real datasets
are downloaded per the checklist.
