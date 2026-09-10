# Stress-test dataset -- known issues manifest

Every issue below was injected on purpose. Nothing here should be "fixed" by
editing this dataset -- the point is to run your Stage 1-4 pipeline against
it and confirm each issue is *detected and handled* the way it would need to
be for the real Ernakulam data. Treat this file as a checklist.

| # | File | Issue | Real-world analog | Expected pipeline behavior |
|---|---|---|---|---|
| 1 | admin_boundaries_messy.geojson (DEMO-W-02b) | Self-intersecting "bowtie" polygon | Digitizing errors are common in scanned/hand-traced government GIS layers | Hard check: geometry validity fails; flag for repair, don't load as-is |
| 2 | admin_boundaries_messy.geojson (DEMO-W-03a) | Null geometry | Some rows in official shapefiles ship with missing geometry | Hard check: null-geometry rows are rejected/flagged, not silently dropped |
| 3 | admin_boundaries_messy.geojson (DEMO-W-03b) | parent_id points to a non-existent panchayat (DEMO-P-99) | Typos / stale references in hand-maintained admin tables | Hard check: orphaned foreign key detected before load |
| 4 | admin_boundaries_messy.geojson (panchayat names) | Inconsistent casing/whitespace in name fields | Real Malayalam-to-English transliteration varies across sources | Names should be normalized (trim, case-fold) before any name-based matching |
| 5 | admin_boundaries_alt_scheme_demo.csv | Same entities under a different ID scheme, no crosswalk given | Real LGD codes vs LSG codes vs Census codes don't match natively | Requires an explicit name/geometry-based reconciliation step -- this is exactly Stage 4.1's admin_code_xref problem |
| 6 | roads_messy.geojson (DEMO-R-01-ALT) | Near-duplicate road from a second source, coordinates shifted ~30m, different name | Combining official + OSM/community road data produces overlapping features | Needs a dedup/reconciliation pass (proximity + name similarity), not a naive union |
| 7 | water_bodies_utm_messy.geojson | Coordinates in UTM zone 43N (meters), not WGS84 degrees | Older or agency-specific GIS exports are frequently in a projected CRS | CRS must be read from the file and reprojected to EPSG:4326 before anything else touches it |
| 8 | flood_hazard_messy.geojson | data_year 2010 while every other 2026 layer is current | Real KSDMA flood classification is based on 2010 NCESS fieldwork | Must be tagged and surfaced as a 2010-vintage static layer, never presented as real-time risk |
| 9 | population_panchayat_messy.csv (DEMO-P-01) | population value is the string "42,000" | Raw government CSV/Excel exports routinely use comma-formatted numbers | Numeric parsing must strip thousands separators, not just int()/float() the raw string |
| 10 | population_panchayat_messy.csv (DEMO-P-01) | source_date "04-09-2026" (DD-MM-YYYY) vs ISO elsewhere | Mixed date formats across, and even within, government sources | Date parsing must handle multiple formats explicitly, not assume one |
| 11 | population_panchayat_messy.csv (DEMO-P-02) | literacy_rate is empty | Real datasets have incomplete fields per admin unit | Missing-value handling (explicit null, not silently coerced to 0) |
| 12 | population_panchayat_messy.csv (DEMO-P-03, two rows) | Same admin_id, different population value, one "provisional" one "final" | A genuine data revision, not a duplicate-row bug | Keep both raw rows; resolve via revision_status + source_date, never silently overwrite |
| 13 | population_ward_legacy_messy.csv | Legacy 3-way ward split doesn't match current 2-ward split (DEMO-W-01a/b) | Kerala's 2025 delimitation added 1,712 wards statewide -- exactly this mismatch | Soft check: flag as match_confidence=unmatched; do not force a join |
| 14 | population_ward_legacy_messy.csv (LW3 row) | panchayat_id "DEMO-P-1" instead of "DEMO-P-01" | Manual data entry drops leading zeros constantly | ID normalization/validation before any join |
| 15 | economy_messy.csv | per_capita_income missing entirely for 2015 | Real published series have genuine gaps | Time-series code must handle missing years, not assume a complete sequence |
| 16 | economy_messy.csv (2015 gddp) | Value is the string "9180.5 Cr" with the unit embedded | Raw scraped/copy-pasted government tables often embed units inline | Value and unit must be split and validated as a pair, not parsed as one field |
| 17 | economy_messy.csv (2021 gddp, two rows) | Same admin_id/indicator/year, two different values, provisional vs final | A real published revision (provisional estimate later finalized) | Keep both; treat "final" as authoritative for downstream use, but never delete "provisional" |
| 18 | bus_stops_messy.csv (DEMO-BS-03 vs DEMO-BS-03-COMM) | Same physical stop, two sources, name whitespace/case differs, coordinates drifted ~60m | Official (KMRL) vs community (Jungle Bus) datasets describing the same real stop | Needs proximity + fuzzy-name dedup logic across sources, not a straight concat |
| 19 | bus_stops_messy.csv (DEMO-BS-03-COMM) | feed_start/end_date is 2022-09-26 to 2023-03-25 | The real KochiTransport GTFS feed is this exact vintage -- about 2 years stale as of now | Feed currency must be checked against today's date, not assumed valid because the file loads fine |
| 20 | source_manifest_messy.csv | One source URL marked unreachable_maintenance | The older kerala.data.gov.in portal vs the current datahub.kerala.gov.in -- easy to confuse | Ingestion must handle a failed/blocked fetch gracefully (log + fallback), not crash or record zero rows silently |

## How to use this
Run your Stage 1 (acquisition) through Stage 4 (digital twin + consistency
checks) code against this folder exactly as you would the real data. Every
row in the table above should surface as either a hard-check failure, a
soft-check flag, or a handled-gracefully acquisition error. If any of them
pass through silently, that's a gap in the pipeline worth closing before
real Ernakulam data arrives.
