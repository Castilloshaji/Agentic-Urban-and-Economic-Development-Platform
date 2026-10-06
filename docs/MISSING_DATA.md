# Missing data in the pipeline

Audited 2026-10-05, re-verified 2026-10-06 against the live stores and the
files on disk. Every figure here was read from the running system, not from
documentation.

Ordered by what it actually costs. The first item is far more serious than
anything below it.

---

## P1 — ~~The real district was never loaded into the Phase-1 stores~~ CLOSED 2026-10-05

**Resolved.** The real dataset is now loaded in all three stores as
`dataset_edition = ernakulam`, and the demo edition has been deleted.

| Store | Holds now |
| --- | --- |
| PostGIS | 105 boundaries, 163 code xrefs, 97 population rows, 7 taluk totals, 25 metro stations, 16 bus stops, 8 economic indicators, 11 roads, 4 flood zones, 3 water bodies |
| PostGIS | 97 hazard + 97 transit + **97 derived** feature rows |
| Neo4j | 164 nodes (83 panchayats, 13 municipalities, 1 corporation, 7 taluks, 1 district, 25 stations, 16 stops, 11 roads, 4 flood zones, 3 water bodies), 162 relationships, zero drift against PostGIS |
| Qdrant | 3 chunks from the real Ernakulam documents (see P3 — the corpus is still thin) |

All four Phase-1 twin functions now return `found=True` for real local bodies,
so the agents receive real twin facts for the first time.

Three further defects surfaced while closing this, all fixed:

1. **Points were invisible below district level.** The real KMRL and GTFS
   sources tag every station and stop with `admin_id = EKM-D`, so the twin's
   attribute join returned **zero stations for all 97 local bodies** while 25
   real stations sat in the table. `get_transit_access` now matches on
   geometry (`ST_Contains`), and the Neo4j loader attributes each point to the
   local body containing it, keeping the source's own value as
   `source_admin_id`. Kochi 13, Choornikkara 4, Kalamassery 4, Thrippunithura 3,
   Aluva 1 — 41/41 points attributed.
2. **The derived layers were not reachable from the twin.** New table
   `admin_derived_feature` (97 rows) plus `get_derived_features()` and
   `GET /api/admin/{id}/derived`. All four agents now gather the relevant
   values, with allocations forced to `match_confidence =
   unmatched-estimate`.
3. **The twin and agent tests had silently stopped running.** They asserted
   demo facts and skipped on any other edition, so loading real data left 34
   tests skipped and the whole twin layer untested. Rewritten against real
   fixtures: 105 pass, 0 skipped.

## P2 — The audit trail is never written (code defect, not missing input)

Six Review-2 tables are empty because `persist_run()` raises before writing:

```
persisted: {'json': True, 'database': False,
            'reason': "ProgrammingError: can't adapt type 'numpy.int64'"}
```

Empty: `scenario_run`, `scenario_parameter`, `agent_priority`,
`constraint_result`, `decision`, `audit_event`.

The failure is caught and the JSON still written, so nothing crashes and no run
is lost — which is why it went unnoticed. Eight runs and eight proposals sit in
`data/derived/` with no database row behind any of them.

Two separate problems:
1. `numpy.int64` values are passed to psycopg2 unconverted. Needs the same
   coercion the API's `clean()` applies.
2. `evaluate_proposal()` (the website's path) never calls `persist_run()` at
   all — it only writes JSON. So even once (1) is fixed, proposals stay unlogged.

---

## P3 — RAG corpus is three short paragraphs

`ernakulam_docs` holds **3 points**: one chunk each from
`economic_review_ernakulam.txt`, `budget_ernakulam.txt` and
`development_report_ernakulam.txt`. These are now the real Ernakulam documents
rather than the demo excerpts — but they total under 2 KB, so citations are
still thin on the ground. `data/sources/documents/` is **empty**.

Review-2 item C6, still open.

**Blocked on acquisition, not code.** Both intended sources are `fetch: manual`:

| Source | Why it cannot be fetched |
| --- | --- |
| `kerala_economic_review` | Per-year PDF links change; no stable archive URL |
| `kerala_budget_documents` | Same |

---

## P4 — Sources that cannot be acquired unattended

Of 20 catalogued sources, **10 fetch automatically** and are present. The
remaining 10 do not.

### Needs an API key (2)

| Source | Level | Blocker |
| --- | --- | --- |
| `census_village_town_pca_kerala` | 2 | Village/Town Primary Census Abstract. Needs a data.gov.in API key. **This is the file that would close P5.** |
| `udyam_msme_district` | 2 | Kerala OGD catalog returns "No Result Found"; the national API needs a key. Worked around by the district anchor in `src/features/anchors.py`. |

### Needs manual download (7)

| Source | Level | Blocker | Impact |
| --- | --- | --- | --- |
| `imd_rainfall_series` | 1 | Gated behind request/licence | No rainfall parameter exists at all |
| `kmrl_metro_open_data` | 1 | Portal is HTML; no GTFS archive URL | Worked around: the station list in the dataset is real and now joined |
| `bhuvan_lulc` | 1 | Thematic portal needs interactive selection | No land-use/land-cover parameter |
| `ernakulam_census_portal` | 1 | HTML only, no tabular export | Partly transcribed; see P5 |
| `kerala_ecostat_district_gdp` | 1 | Portal responds after ~35 s; HTML/PDF tables | Worked around: GDDP + per-capita income are in the dataset |
| `kerala_economic_review` | 1 | Per-year links change | P3 |
| `kerala_budget_documents` | 1 | Per-year links change | P3 |

### Acquired (1 large)

`geofabrik_southern_zone_osm` — 532 MB, present. Feeds roads, water and
right-of-way.

---

## P5 — Per-unit gaps in the feature layers

| Field | Missing | Note |
| --- | --- | --- |
| `population` | **26 / 97** | Boundaries post-date Census 2011. Econ allocator imputes the district median and flags each row with `econ_population_imputed = 1`. |
| `literacy_rate` | **26 / 97** | Same 26 units. |
| `male_population` / `female_population` | 26 / 97 | Same 26 units. |
| `households` | **97 / 97** | Column exists and is entirely empty. No household count anywhere in the pipeline. |
| `msme_per_1000_people` | 26 / 97 | Derived; null wherever population is null, by design. |
| `row_narrow_share` | 1 / 97 | Edamalakkudi — no OSM road network mapped. Correct: it is a remote Ghats settlement with no mapped roads. |

`landslide_rank` is null for 78 units. **This is not a gap**: GSI maps only
susceptible zones, so "no zone intersects" is a finding. 19 units carry a zone
(17 high, 1 moderate, 1 low).

The 26 missing populations are the single highest-value gap after P1, because
population feeds `population_affected`, `population_served`,
`settlement_density`, `congestion_pressure`, `economic_catchment` and the whole
economic allocator. `census_village_town_pca_kerala` (P4) is the file that would
fix it — Census PCA is published at village/town level and aggregates to local
bodies.

A regression was tried as a substitute and **rejected on measurement**:
population correlates strongly with mapped road length (r = 0.90) and transit
stop count (r = 0.92), but under leave-one-out validation on the 71 units that
do have a count, the flat median's median absolute error is 28.7% against 35.8%
for linear-on-road-length, 33.2% for log-log and 28.9% for the best
two-variable fit. The correlation is real; the predictive gain is not.

Taluk totals were **not** used as a better anchor because the project has
already measured those DataMeet boundaries as unreliable — Kothamangalam's
mapped area is 0.34× its Census area (`data/features/taluk_boundary_quality.csv`).

---

## P6 — Parameters with no data path at all

All 25 parameters now return a value for a well-covered unit (16 real, 9 proxy).
Three have **no measured input anywhere in the pipeline** and rest entirely on
allocation or substitution:

| Parameter | Rests on | Would need |
| --- | --- | --- |
| `msme_presence` | District Udyam total, allocated | `udyam_msme_district` (P4) |
| `investment_potential` | District per-capita income, modulated | Sub-district income — not published |
| `employment_potential` | Enterprise density + population | Labour-force survey below district level — not published |

Two more are absent from the catalogue entirely, so no parameter exists for them:

- **Rainfall / precipitation** — `imd_rainfall_series` is licence-gated.
- **Land use / land cover** — `bhuvan_lulc` needs interactive selection. This is
  the most useful missing layer for siting: it would turn
  `implementation_area` from "how big is the unit" into "how much buildable
  non-agricultural land does it have".

---

## P7 — One file in the real dataset is synthetic

`data/ernakulam/.../gis/water_bodies_utm_ernakulam.geojson` carries three
polygons with real names (Vembanad, Periyar, Muvattupuzha) but
`source: drawn_geometry_real_names` — hand-drawn geometry. Fine as a map
backdrop, useless for distance or area.

Already superseded: `src/features/water.py` builds the real layer from 14,212
OSM features. Flagged here so the file is not reused by mistake.

---

## P8 — Budget-only ideation under-allocates on the local model

Not missing data — a model-capability limit, recorded here because it shapes
what the output looks like.

Given ₹120 crore across three local bodies, the four agents offer ~₹78 crore of
work in total and the Supervisor selects ₹24–34 crore of it: **20–28%
utilisation**. Environment and infrastructure fill their envelopes; economic and
transportation under-propose.

Two rounds of prompt pressure moved it from 22% to 28% and then stopped
helping, which is the signal that `qwen2.5:7b-instruct-q4_K_M` is the
constraint rather than the instructions.

**It is reported, not corrected.** `_portfolio()` recomputes spend from the
selected items rather than trusting the model's own total, and the page states
*"Only 28% of the budget is allocated. The agents offered ₹77.96 cr of work in
total. Figures recomputed from the selected items, not taken from the model."*

Running the same code path against a frontier model via `ANTHROPIC_API_KEY` is
an `.env` change, not a code change.

---

## Summary

| # | Gap | Type | Closable now? |
| --- | --- | --- | --- |
| P1 | ~~Real district absent from PostGIS + Neo4j~~ | pipeline not run | **CLOSED** — loaded as `ernakulam`; demo deleted |
| P2 | Audit trail never written | code defect | **Yes** — numpy coercion + wire up the proposal path |
| P3 | RAG corpus is 3 short real chunks (<2 KB) | acquisition | No — no stable URLs |
| P4 | 10 of 20 sources not auto-fetchable | acquisition | 2 need an API key; 7 need manual download |
| P5 | 26 populations, 97 household counts | upstream data | Partly — via Census PCA (needs key) |
| P6 | 3 parameters have no measured input; rainfall and LULC have none | not published / gated | No |
| P7 | One synthetic file in the real dataset | hygiene | Already superseded |
| P8 | Ideation allocates 20–28% of a budget | model capability | Partly — a frontier backend is an `.env` change |

P1 is closed. P2 needs no new data either — one type coercion plus wiring
`evaluate_proposal` to `persist_run`. Everything else is blocked on
acquisition.
