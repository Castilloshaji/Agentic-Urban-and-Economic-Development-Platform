# Data dictionary

Required by architecture doc section 1 and Stage 1: every file under `data/raw/`
is recorded here with its source URL, publisher, download date, dataset year and
geographic coverage. Raw files are never overwritten with cleaned data — Stage 2
writes to `data/processed/` instead.

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

## 1. Phase 1 target sources

These are the real Ernakulam sources the pipeline is being built for. None are
ingested yet — the `--url` fetch path is still stubbed, so acquisition is manual.
`src/ingestion/sources.yaml` holds the machine-readable version.

| Category | Dataset | Publisher | URL | Year | Coverage | Status |
| --- | --- | --- | --- | --- | --- | --- |
| population | District Census Handbook | Census India | _to fill_ | 2011 | Ernakulam district | not acquired |
| economy | GDDP / NDDP series | Kerala DES (ecostat.kerala.gov.in) | _to fill_ | _varies_ | District | not acquired |
| economy | Kerala Data Portal (fallback) | datahub.kerala.gov.in | _to fill_ | _varies_ | State | not acquired |
| gis | Taluk/district boundaries | Bharatlas / LGD 2024 | _to fill_ | 2024 | Kerala | not acquired |
| gis | Panchayat/ward boundaries | OpenDataKerala (lsg-kerala-data) | _to fill_ | _varies_ | Kerala LSGs | not acquired |
| gis | Roads, buildings, land use, water | Geofabrik OSM (Kerala extract) | _to fill_ | rolling | Kerala — clip to Ernakulam | not acquired |
| gis | LULC cross-check | Bhuvan (ISRO/NRSC) | _to fill_ | _varies_ | India | not acquired |
| transportation | Kochi Metro GTFS | KMRL Open Data | _to fill_ | _varies_ | Kochi Metro network | not acquired |
| transportation | Bus GTFS | KochiTransport (Jungle Bus) | _to fill_ | 2022–2023 | Kochi bus network | not acquired — **feed is ~2 years stale** |
| environment | Flood hazard maps | KSDMA | _to fill_ | 2010 | Kerala | not acquired — **2010 NCESS fieldwork, static** |
| documents | Kerala Economic Review | Kerala State Planning Board | _to fill_ | _varies_ | State | not acquired |
| documents | State Budget | Kerala Finance Department | _to fill_ | _varies_ | State | not acquired |

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

## 2. Synthetic datasets currently loaded

Two synthetic datasets stand in for the real sources while the pipeline is built.
Both describe the same fictional district (`DEMO-D-01`).

| Dataset | Path | Purpose | Files |
| --- | --- | --- | --- |
| demo | `data/demo/demo_data/` | Clean, internally consistent. The happy path. | 12 in categories (+2 README/dictionary at root, skipped by the loader) |
| stress-test | `data/stress_test/…/stress_test_data/` | 20 deliberately planted defects. See `../docs/known_issues_manifest.md`. | 13 in categories (+2 at root) |

Only one edition is loaded at a time. Which one is live is recorded as
`dataset_edition` on every row in PostGIS and every node in Neo4j, and printed in
each scenario report's header.

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

## 3. Ingested file inventory

Generated from `data/raw/ingestion_manifest.csv`, which is append-only and holds
a sha256 for every file copied. Re-run `python -m src.ingestion.describe` after
any ingest to refresh this.

<!-- INVENTORY:BEGIN -->
**13 file(s)** currently in `data/raw/`, from: `stress_test_data`.

| Category | File | Ingested (UTC) | sha256 (first 12) |
| --- | --- | --- | --- |
| documents | `budget_excerpt_demo.txt` | 2026-09-05T15:25:04 | `e36d8c4b94bd` |
| documents | `economic_review_excerpt_demo.txt` | 2026-09-05T15:25:04 | `2272ebb0a3ed` |
| documents | `source_manifest_messy.csv` | 2026-09-05T15:25:04 | `8b2e234f7450` |
| economy | `economy_messy.csv` | 2026-09-05T15:25:04 | `34d5d97bc852` |
| environment | `flood_hazard_messy.geojson` | 2026-09-05T15:25:04 | `4be8f1cba2a0` |
| gis | `admin_boundaries_alt_scheme_demo.csv` | 2026-09-05T15:25:04 | `a2377e6f77c7` |
| gis | `admin_boundaries_messy.geojson` | 2026-09-05T15:25:04 | `92a77c85805c` |
| gis | `roads_messy.geojson` | 2026-09-05T15:25:04 | `21d2cea27774` |
| gis | `water_bodies_utm_messy.geojson` | 2026-09-05T15:25:04 | `99ade98029fd` |
| population | `population_panchayat_messy.csv` | 2026-09-05T15:25:04 | `ca69057586a1` |
| population | `population_ward_legacy_messy.csv` | 2026-09-05T15:25:04 | `dfbd8c044679` |
| transportation | `bus_stops_messy.csv` | 2026-09-05T15:25:04 | `c5c5f329e9f4` |
| transportation | `metro_stations_demo.csv` | 2026-09-05T15:25:04 | `4ba89878739a` |

Full hashes and source directories are in `data/raw/ingestion_manifest.csv`. The manifest is append-only, so it also records what was ingested previously and when.
<!-- INVENTORY:END -->
