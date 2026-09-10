# Demo dataset — data dictionary

Synthetic data only. Field names and structure mirror the real schema in
ernakulam_phase1_architecture.md so ingestion/processing code written against
this demo drops in against real data later without changes to field names.

## gis/admin_boundaries_demo.geojson
CRS: EPSG:4326. Geometry: Polygon. One FeatureCollection covering all levels.
| field | meaning |
|---|---|
| admin_id | canonical ID (district/taluk/panchayat/ward) |
| level | district \| taluk \| panchayat \| ward |
| parent_id | admin_id of the containing unit (null for district) |
| boundary_vintage | which delimitation this geometry reflects |

## gis/roads_demo.geojson, gis/water_bodies_demo.geojson
CRS: EPSG:4326. Geometry: LineString (roads) / Polygon (water bodies).

## environment/flood_hazard_demo.geojson
CRS: EPSG:4326. Geometry: Polygon. `risk_level`: low/medium/high.
Deliberately overlaps Demo Panchayat B and C for testing OVERLAPS relationships.

## population/population_panchayat_demo.csv
Panchayat-level population, joins directly to admin_boundaries (panchayat rows).
`boundary_vintage: pre-2025-delimitation` — safe to join because panchayat
boundaries didn't change in the 2025 delimitation (only wards did).

## population/population_ward_legacy_demo.csv
**Deliberate test case.** A legacy 3-way ward split for Demo Panchayat A that
does NOT match the current 2-ward split (DEMO-W-01a / DEMO-W-01b) in
admin_boundaries_demo.geojson. Use this to test the hard/soft consistency-check
split and crosswalk logic discussed for the real dataset — this file should
fail a naive join and should be flagged as `match_confidence: unmatched`,
not silently reconciled.

## economy/economy_demo.csv
District-level indicators (gddp, per_capita_income) across 5 sample years.

## transportation/metro_stations_demo.csv, bus_stops_demo.csv
Point locations with lat/lon and nearest admin_id — for Neo4j CONNECTS_TO
relationships and Transportation Agent testing.

## documents/*.txt
Three short synthetic documents for RAG pipeline testing (chunk/embed/index).
Each one references specific twin entities (Demo Panchayat B, DEMO-FZ-01,
Demo Central Metro) so retrieval-to-entity linkage can be tested meaningfully,
not just chunking mechanics.
