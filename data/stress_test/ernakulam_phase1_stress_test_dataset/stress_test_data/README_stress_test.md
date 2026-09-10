# Ernakulam Phase 1 -- stress-test dataset

Same synthetic geography as the clean demo dataset (1 district, 1 taluk, 3
panchayats, 6 wards), but this version deliberately contains the data-quality
problems your pipeline will actually meet in the real Ernakulam sources --
invalid geometry, CRS mismatches, boundary-vintage mismatches, revision
conflicts, near-duplicate entities across sources, stale feeds, dead links,
and messy formatting.

See known_issues_manifest.md for the full list -- 20 injected issues, each
tied to a real problem already surfaced while validating the actual dataset
checklist, with the expected correct pipeline behavior for each.

This is a QA fixture, not a starting point for real ingestion -- use the
separate clean demo dataset for that. Use this one to confirm your Stage
1-4 code actually catches what it needs to catch before real data shows up.
