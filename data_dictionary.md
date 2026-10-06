# Data dictionary

Required by architecture doc section 1 and Stage 1: every file under `data/raw/`
is recorded here with its source URL, publisher, download date, dataset year and
geographic coverage. Raw files are never overwritten with cleaned data — Stage 2
writes to `data/processed/` instead, and the derived feature layers go to
`data/features/` (§3).

Two things this file is *not*: it is not the machine-readable provenance record
(that is `data/raw/ingestion_manifest.csv`, which carries a sha256 per file), and
it is not a schema reference for the databases (that is
`src/storage/postgres/schema.sql`). This is the human-facing account of where
each dataset came from and what it is safe to conclude from it.

Regenerate the inventory section with:

```bash
python -m src.ingestion.describe          # prints the table below from the manifest
```

---

## 1. Source catalogue

`src/ingestion/sources.yaml` is the machine-readable version, and
`GET /api/sources` serves the live acquisition log. **10 of the 20 catalogued
sources are acquired and on disk** (~600 MB); the rest cannot be fetched
unattended.

Source levels follow the Review-2 hierarchy: **1** government primary,
**2** open-government portal, **3** OSM/community, **4** secondary.

### Acquired (10)

| Category | Source | L | Year | Notes |
| --- | --- | --- | --- | --- |
| environment | KSDMA flood return probability, historical 10/25/50 yr | 1 | 2026 | GeoTIFF, EPSG:4326, ~93 m |
| environment | KSDMA flood return probability, historical 100/200/500 yr | 1 | 2026 | long return periods for extreme-event thresholds |
| environment | KSDMA flood return probability, RCP 8.5 10/25/50 yr | 1 | 2026 | climate-projection counterpart |
| environment | KSDMA flood return probability, RCP 8.5 100/200/500 yr | 1 | 2026 | |
| environment | KSDMA historical flood hazard map | 1 | 2026 | PDF, 2010 NCESS fieldwork — see the warnings below |
| environment | GSI landslide susceptibility, Ernakulam | 1 | 2025 | zones only; 78 of 97 units intersect none, which is a finding not a gap |
| gis | OpenDataKerala LSG boundaries | 3 | 2024 | the 97 local bodies + 7 taluks + district |
| gis | DataMeet Kerala admin boundaries | 3 | 2018 | taluk geometry; **4 of 7 taluks mismatch Census area** — see `taluk_boundary_quality.csv` |
| gis | Geofabrik OSM Southern Zone | 3 | 2026 | 532 MB, four states; clipped to the Ernakulam bbox. Feeds roads, water and right-of-way |
| transportation | Kochi Transport GTFS (Jungle Bus) | 3 | 2022 | **feed window ended March 2023** — see the warnings below |

### Not acquired (10)

| Category | Source | L | Blocker |
| --- | --- | --- | --- |
| population | `census_village_town_pca_kerala` | 2 | needs a data.gov.in API key. **This is the file that would close the 26 missing populations (§3).** |
| economy | `udyam_msme_district` | 2 | Kerala OGD catalog returns "No Result Found"; the national API needs a key. Worked around by the district anchor (§3) |
| population | `ernakulam_census_portal` | 1 | HTML only, no tabular export. Figures transcribed with provenance into `census2011_ernakulam.json` |
| economy | `kerala_ecostat_district_gdp` | 1 | portal responds after ~35 s; tables are HTML/PDF. GDDP and per-capita income transcribed into `economy_ernakulam.csv` |
| transportation | `kmrl_metro_open_data` | 1 | portal is HTML; no GTFS archive URL. The 25-station list with real coordinates is in the dataset and is now joined |
| environment | `imd_rainfall_series` | 1 | licence-gated. **No rainfall parameter exists anywhere in the engine.** |
| gis | `bhuvan_lulc` | 1 | thematic portal needs interactive selection. **No land-use parameter exists** — the single most useful missing layer |
| documents | `kerala_economic_review` | 1 | per-year PDF links change; no stable archive. Keeps the RAG corpus thin |
| documents | `kerala_budget_documents` | 1 | same |
| documents | `deliberately_unreachable_probe` | 2 | **not a real source.** A regression probe whose URL promises `.csv` and returns HTML, so the soft-404 detector stays tested. Do not remove — defect class D20 is scored from its logged failure |

### Vintage warnings that must survive into any output

- **Flood hazard is 2010.** KSDMA's classification rests on 2010 NCESS fieldwork.
  It is a static historical hazard map, never a current or forecast product.
  `data_year` travels with every flood value through PostGIS, Neo4j and the
  scenario reports for exactly this reason.
- **Bus GTFS is ~2 years stale.** The community feed's service window ended in
  March 2023. Stops still load, flagged `feed_is_stale`, because absence of a
  stop is a worse lie than a stop with a known-expired feed.
- **Population and boundaries are on different delimitations.** Census counts are
  on the pre-2025 ward split; current boundaries are the 2025 delimitation, which
  added 1,712 wards statewide. Any sum across the two is an estimate and is
  labelled `unmatched-estimate`.

---

## 2. Synthetic datasets (fixtures, not the loaded edition)

**The stores hold real Ernakulam data (`dataset_edition = ernakulam`): 105
boundaries, 97 population rows, 25 KMRL stations, 16 bus stops, 7 taluk totals.**
Load it with `bash scripts/run_phase1.sh real`.

The two synthetic datasets below are fixtures for exercising the processing
stage. Both describe the same fictional district (`DEMO-D-01`). They are not
meant to be left loaded: a demo-loaded store answers every twin query about a
district that does not exist, and answers it successfully, so nothing looks
wrong.

| Dataset | Path | Purpose | Files |
| --- | --- | --- | --- |
| demo | `data/demo/demo_data/` | Clean, internally consistent. The happy path. | 12 in categories (+2 README/dictionary at root, skipped by the loader) |
| stress-test | `data/stress_test/…/stress_test_data/` | 20 deliberately planted defects. See `../docs/known_issues_manifest.md`. | 13 in categories (+2 at root) |

Only one edition is loaded at a time. Which one is live is recorded as
`dataset_edition` on every row in PostGIS and every node in Neo4j, and printed in
each scenario report's header.

**One file in the real Ernakulam dataset is also synthetic and must not be used
for measurement:** `data/ernakulam/.../gis/water_bodies_utm_ernakulam.geojson`
carries three polygons whose names are real (Vembanad, Periyar, Muvattupuzha) but
whose `source` is `drawn_geometry_real_names` — the geometry was drawn by hand.
It is fine as a map backdrop and useless for distance or area. The real water
layer is `data/features/water_features.csv`, built from 14,212 OSM features by
`src/features/water.py`.

### Field reference for the synthetic schema

| Field | Appears in | Meaning |
| --- | --- | --- |
| `admin_id` | boundaries, population, economy | Canonical admin unit id (`DEMO-D/T/P/W-nn`) |
| `parent_id` | boundaries | Parent admin unit; must resolve within one `boundary_vintage` |
| `boundary_vintage` | boundaries, population | Which delimitation the geometry/count belongs to |
| `revision_status` | population, economy, boundaries | `provisional` or `final`; both rows are kept on conflict |
| `data_year` | all | Year the observation describes — not the download date |
| `source_date` | most | Date the source published or was retrieved |
| `feed_start_date` / `feed_end_date` | bus stops | GTFS service window; drives the staleness check |
| `match_confidence` | every loaded row | `exact`, `crosswalked`, or `unmatched-estimate` (added by Stage 2) |
| `dataset_edition` | every loaded row | Which synthetic edition the row came from |

### Document chunks (Qdrant payload)

| Field | Meaning |
| --- | --- |
| `source_file` / `chunk_index` | Provenance — every report citation traces back through these |
| `text` | The chunk verbatim, as quoted in scenario reports |
| `admin_ids_mentioned` | Admin units the chunk implies — directly, or via an entity located in one (a metro station), or via a flood zone overlapping one |
| `token_count` | Real tokenizer count, not a word estimate |
| `encoder_windows` | How many encoder passes the chunk needed (>1 means it was pooled) |
| `embedding_model` | `all-MiniLM-L6-v2`, 384-dim, cosine |

Chunks target 500–800 tokens with ~50 tokens of overlap, per the build guide.
all-MiniLM-L6-v2 encodes only 256 tokens per pass, so a chunk over that limit is
embedded as several overlapping encoder windows whose vectors are mean-pooled and
re-normalised. Without this, ~61% of a target-sized chunk would be stored but
unsearchable. Demo documents are short enough to be one window each; real
Economic Review PDFs will not be.

---

## 3. Derived feature layers

Built by `python3 -m src.features.build_all` into `data/features/`. The parameter
engine reads these, never the raw files.

| Layer | File | Covers | Source level |
| --- | --- | --- | --- |
| hazard | `hazard_features.csv` | KSDMA flood return-probability shares (historical + RCP 8.5), GSI landslide rank | 1 |
| transit | `transit_features.csv` | Kochi GTFS nearest-stop distance, stop density, trips per stop, feed staleness | 3 |
| roads | `road_features.csv` | OSM road length, density and highest highway class | 3 |
| metro | `metro_features.csv` | distance to nearest KMRL station, stations inside the unit | 2 |
| water | `water_features.csv` | OSM water-area share, centroid distance to nearest water body | 3 |
| row | `row_features.csv` | narrow-class share of the road network, arterial density, lane-tag coverage | 3 |
| social | `social_features.csv` | health and education facility counts, capacity weights, distance to nearest | 3 |
| econ | `econ_features.csv` | estimated enterprise count/density, per-capita income, employment headroom | 1 anchor, allocated |
| taluks | `taluk_boundary_quality.csv` | DataMeet taluk area vs Census area, with deficit flags | 3 |

### Parameters that were closed after the first pass

Six of the 25 parameters reported `unavailable` in the first pass. All six now
carry data, but by two different routes, and the distinction matters:

| Parameter | Now | How |
| --- | --- | --- |
| `metro_access` | **real** | The KMRL station list was already in the dataset with real coordinates but tagged `admin_id = EKM-D` — the district. Joining it down to the 97 units is a measurement, not an estimate. |
| `water_body_proximity` | **real** | The dataset's three water polygons were `drawn_geometry_real_names` — hand-drawn. Replaced by 14,212 real features from the OSM extract. |
| `right_of_way_constraint` | **proxy** | OSM `lanes` is tagged on 5.7% of segments and `width` on 0.8%, so width cannot be measured. `highway` class is tagged on 100%, so narrow-class share of the network stands in. The measured tag coverage travels with the parameter. |
| `msme_presence` | **proxy** | District total of 166,200 Udyam registrations shared out by population and measured activity. |
| `investment_potential` | **proxy** | District per-capita income of ₹261,319 modulated by measured activity, constrained so the population-weighted mean returns the district figure. |
| `employment_potential` | **proxy** | Half estimated enterprise density, half population, both percentile-ranked — headroom, not a job count. |

### Healthcare and education, and why the counts exceed the official ones

`social_features.csv` holds **1,159 health** and **2,044 education** facilities
mapped inside the 97 local bodies, from the same OSM extract the road and water
layers use. Where a facility is, is observed, not allocated.

Those counts are **13x and 6x the published district figures** (87 government
health facilities, 323 schools). That is expected, and the reason matters:

- The official health figure counts **government facilities only** (10 CHC, 12
  block PHC, 65 mini PHC). Kerala's private health sector is large and OSM maps
  it. 439 of the OSM features are pharmacies, which the official count would
  never include.
- The education layer counts **libraries, kindergartens and colleges** alongside
  schools, and OSM includes private and unaided institutions the district school
  list does not.

So the anchors are an **order-of-magnitude sanity check, not a target to
reproduce**, and they are recorded at source level 4 for that reason. Two
caveats travel with every value derived from this layer:

1. **Coverage is uneven.** A local body with no mapped clinic may have no
   clinic, or may simply be less mapped than Kochi. Six units have no mapped
   health facility and one has no mapped education facility.
2. **Counts are weighted by type, never by capacity.** A hospital counts 6, a
   clinic 2, a pharmacy 0.5. There is no bed count, staffing, enrolment or
   pupil-teacher ratio below district level anywhere, so capacity is a proxy
   and is labelled one.

Distance from the centroid to the nearest facility is the robust signal and is
what `health_access` and `education_access` use. It moves far less with mapping
effort than a count does, which is why those two are the only parameters here
marked `real`.

### The budget domain and the devolution formula

`fiscal_entitlement` is each local body's share of a devolved grant under
Kerala's **published** State Finance Commission rule: **80% population (Census
2011), 10% area, 10% inverse of own income**. The 97 shares sum to 1.0, which
the test suite asserts.

It is tagged `proxy`, not `real`, for one specific reason: own income is not
published per local body, so estimated per-capita income substitutes for that
one term of three. The parameter note says so wherever it appears. Confidence is
`LEVEL_CONFIDENCE[1] × 0.75`, below a clean Level-1 measurement.

`cost_exposure` is a relative index across the district, not a price: density,
flood exposure and narrow-road share combined. High means dear to build on. No
construction unit costs exist in the pipeline and the Budget agent is explicitly
forbidden from inventing one.

### Economic anchors

The economic estimates are allocations of published district figures, held in
`src/features/anchors.py` with publisher, as-on date and URL, and served at
`GET /api/anchors`.

| Anchor | Value | As on | Publisher |
| --- | --- | --- | --- |
| Udyam MSME registrations | 166,200 | 2024-12-15 | Ministry of MSME, Lok Sabha reply |
| Per-capita income | ₹261,319 | 2024-03-31 | Kerala Ecostat |
| District GDDP | ₹167,661.9 crore | 2024-03-31 | Kerala Ecostat |
| District population | 3,282,388 | 2011-03-01 | Census of India 2011 |

Every allocated estimate reproduces its anchor: Σ estimated enterprises = 166,200
and the population-weighted mean of the income estimates = ₹261,319, both
asserted in the test suite. Confidence is capped at `LEVEL_CONFIDENCE[1] ×
ALLOCATION_PENALTY` = 0.55, which sits below a current Level-3 measurement (0.70)
and above a Level-4 source (0.50).

**Known limitation.** 26 of the 97 units have no Census 2011 population, because
their boundaries post-date the census. The allocator substitutes the district
median for those units and flags each affected row with
`econ_population_imputed = 1`; the flag reaches the parameter note, so a reader
sees it on the unit rather than in a footnote. A regression on mapped road length
(r = 0.90) and stop count (r = 0.92) was tried as a better substitute and
rejected: under leave-one-out validation on the 71 units that do have a count,
the flat median's median absolute error is 28.7% against 35.8% for
linear-on-road-length and 28.9% for the best two-variable fit.

---

## 4. Ingested file inventory

Generated from `data/raw/ingestion_manifest.csv`, which is append-only and holds
a sha256 for every file copied. Re-run `python -m src.ingestion.describe` after
any ingest to refresh this.

<!-- INVENTORY:BEGIN -->
**13 file(s)** currently in `data/raw/`, from: `ernakulam_data`.

| Category | File | Ingested (UTC) | sha256 (first 12) |
| --- | --- | --- | --- |
| documents | `budget_ernakulam.txt` | 2026-10-06T06:16:51 | `430c3a54c4c6` |
| documents | `development_report_ernakulam.txt` | 2026-10-06T06:16:51 | `5c41a10f567c` |
| documents | `economic_review_ernakulam.txt` | 2026-10-06T06:16:51 | `bd94ec8054b1` |
| economy | `economy_ernakulam.csv` | 2026-10-06T06:16:51 | `e37b363f20ae` |
| environment | `flood_hazard_ernakulam.geojson` | 2026-10-06T06:16:51 | `c975de9b97d1` |
| gis | `admin_boundaries_alt_scheme_ernakulam.csv` | 2026-10-06T06:16:51 | `a01b8b15dafe` |
| gis | `admin_boundaries_ernakulam.geojson` | 2026-10-06T06:16:51 | `98ea4691742b` |
| gis | `roads_ernakulam.geojson` | 2026-10-06T06:16:51 | `1d29844c75ca` |
| gis | `water_bodies_utm_ernakulam.geojson` | 2026-10-06T06:16:51 | `e786b281cab6` |
| population | `population_panchayat_ernakulam.csv` | 2026-10-06T06:16:51 | `2df31b83401a` |
| population | `population_taluk_ernakulam.csv` | 2026-10-06T06:16:51 | `c06d7ad26efc` |
| transportation | `bus_stops_ernakulam.csv` | 2026-10-06T06:16:51 | `eaa0962b53d8` |
| transportation | `metro_stations_ernakulam.csv` | 2026-10-06T06:16:51 | `1e1a2723fd97` |

Full hashes and source directories are in `data/raw/ingestion_manifest.csv`. The manifest is append-only, so it also records what was ingested previously and when.
<!-- INVENTORY:END -->
