# Ernakulam Digital Twin — Project Guide

A multi-agent urban development platform for Ernakulam district. It combines a
spatial data warehouse (PostGIS), an urban knowledge graph (Neo4j), a vector
store (Qdrant) and a local LLM driving LangGraph agents that reason over the
twin and run development scenarios.

Everything runs locally: the three datastores in Docker, the LLM natively on the
host.

**The design rule that governs the whole system: no value is ever returned
without the vintage it was published under.** A population of 38,500 is not a
fact; "38,500, census 2011, final, counted on the pre-2025 ward split" is.
Agents reasoning over the first form produce confident nonsense.

---

## 1. Architecture

Nine stages, matching `../docs/ernakulam_phase1_architecture.md`.

| Stage | Module | Does |
| --- | --- | --- |
| 1 Acquisition | `src/ingestion/` | Copy sources into `data/raw/`, sha256 every file |
| 2 Processing | `src/processing/` | CRS standardisation, admin-code reconciliation, value cleaning, hard/soft checks |
| 3 Storage | `src/storage/{postgres,neo4j,qdrant}/` | Load both stores; prove they agree |
| 4 Digital twin | `src/digital_twin/` | One query layer over PostGIS + Neo4j |
| 5 RAG | `src/rag/` | Chunk, embed, retrieve government documents |
| 6-7 Agents | `src/agents/` | Four domain agents + Supervisor |
| 8-9 Scenario | `src/scenarios/` | Run end to end, write a cited Markdown report |

### Directory layout

```
ernakulam-digital-twin/
├── PROJECT_GUIDE.md            this file
├── data_dictionary.md          provenance and vintage warnings per source
├── docker-compose.yml          postgres+postgis, neo4j, qdrant
├── requirements.txt
├── .env.example
├── src/
│   ├── ingestion/              ingest.py, describe.py, sources.yaml
│   ├── processing/             crs, reconcile_ids, clean_values,
│   │                           consistency_checks, run
│   ├── storage/
│   │   ├── postgres/           schema.sql, load.py
│   │   ├── neo4j/              schema.cypher, load.py, verify.py
│   │   └── qdrant/             client.py
│   ├── digital_twin/           twin.py
│   ├── rag/                    ingest.py, retrieve.py
│   ├── agents/                 llm.py, base.py, {economic,infrastructure,
│   │                           transportation,environment,supervisor}_agent.py
│   └── scenarios/              run_scenario.py
├── scripts/
│   ├── run_phase1.sh           full pipeline, both datasets
│   ├── build_ernakulam_dataset.py   real data from OSM + Census + Ecostat
│   ├── build_benchmark_dataset.py   inject defects, emit ground truth
│   └── score_detection.py      score detection against that ground truth
├── tests/
└── data/
    ├── raw/{population,economy,gis,transportation,environment,documents}/
    ├── processed/              Stage 2 output and reports
    ├── derived/                scenario reports
    ├── sources/                upstream source files (OSM LSG, census JSON)
    ├── demo/  stress_test/  ernakulam/  bench/
```

### The two hard invariants

**Hard checks halt individual records, never the run.** A bowtie polygon stops
that polygon loading; the rest of the file still loads. Findings go to
`data/processed/hard_check_failures.json`.

**Soft checks never halt anything.** They attach a `match_confidence` of
`exact` / `crosswalked` / `unmatched-estimate` that travels with the row into
both databases and into the final report, so uncertainty is visible at the point
of decision rather than in a log nobody opens.

---

## 2. Setup

### Step 1 — Environment

```bash
cp .env.example .env
```

Docker Compose reads `.env` directly, so `POSTGRES_*` and `NEO4J_AUTH` must be
set before Step 2. Keep `NEO4J_AUTH` (`user/password`) consistent with
`NEO4J_USER` / `NEO4J_PASSWORD`, which the Python driver uses.

### Step 2 — Datastores

```bash
docker compose up -d && docker compose ps
```

| Service | Image | Ports | Volume |
| --- | --- | --- | --- |
| postgres | `postgis/postgis:16-3.4` | 5432 | `postgres_data` |
| neo4j | `neo4j:5-community` | 7474, 7687 | `neo4j_data` |
| qdrant | `qdrant/qdrant:latest` | 6333 | `qdrant_data` |

Neo4j Browser: http://localhost:7474 · Qdrant dashboard:
http://localhost:6333/dashboard

Data lives in Docker named volumes, never in the repository. `docker compose
down` keeps them; `down -v` deletes them.

### Step 3 — Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Step 4 — Ollama (host-native, not Docker)

```bash
ollama serve
ollama pull qwen2.5:7b-instruct-q4_K_M
curl -s localhost:11434/api/ps | grep -o '"size_vram":[0-9]*'   # 0 means CPU
```

Ollama is installed here as a snap whose service (`snap.ollama.listener`) is
**disabled**, so it does not survive a reboot or a closed session. If
`size_vram` reads 0 it fell back to CPU and is roughly 3.4x slower — restart it
and it re-runs GPU discovery.

**Backend selection.** Agents resolve their LLM at call time: `ANTHROPIC_API_KEY`
set means the Anthropic API (`ANTHROPIC_MODEL`, default `claude-sonnet-5`);
otherwise Ollama. Switching is an `.env` change, not a code change. If the
configured `OLLAMA_MODEL` is not pulled, agents fall back to an available model
and record it in every report's `backend.note`.

---

## 3. Running the pipeline

```bash
python src/ingestion/ingest.py --reset --source-dir data/demo/demo_data
python -m src.processing.run
python -m src.storage.postgres.load --dry-run      # counts, no writes
python -m src.storage.postgres.load
python -m src.storage.neo4j.load --apply-schema --reset
python -m src.storage.neo4j.verify                 # exits non-zero on drift
python -m src.rag.ingest --recreate
python src/scenarios/run_scenario.py --admin-id DEMO-P-03 --scenario "..."
```

Or all of it, both datasets, exiting non-zero on the first failure:

```bash
bash scripts/run_phase1.sh              # demo pass, then stress pass
bash scripts/run_phase1.sh demo
```

If your environment is not on `PATH` (nohup, cron, CI), pass the interpreter:
`PYTHON=/path/to/python bash scripts/run_phase1.sh`.

### Tests

```bash
pytest
```

Suites assert demo-dataset facts and skip automatically when the stores hold
another edition, so a stress or bench run does not report false failures.

---

## 4. Module reference

### `src/ingestion/`
`ingest.py --source-dir DIR [--reset]` copies files into the matching
`data/raw/<category>/`, recording path, category, sha256, timestamp and source
in an append-only `ingestion_manifest.csv`. Files outside the six known
categories are skipped and listed, not guessed at. `--url CATEGORY` is stubbed
and raises `NotImplementedError`. `describe.py --write` regenerates the file
inventory inside `data_dictionary.md` from the manifest.

### `src/processing/`
`run.py` clears `data/processed/` and runs four steps in order.

- `crs.py` — reprojects any GeoJSON not already EPSG:4326; writes
  `crs_report.json` where `reprojections` is the report and `inspected` the
  audit trail.
- `reconcile_ids.py` — builds `admin_code_xref.csv` by normalised-name matching.
  Unmatched rows on both sides are kept, never dropped.
- `clean_values.py` — comma-formatted numbers, embedded units, mixed date
  formats. A blank cell stays NULL; it never becomes 0.
- `consistency_checks.py` — the hard/soft split, plus the name matcher described
  in §6.

### `src/storage/`
`postgres/load.py` loads 11 tables, honouring hard-check exclusions scoped to
`(source_file, record_id)` so a broken record in one edition does not suppress a
good copy in another. It reports natural-key collisions before insert and warns
about any processed file no builder read.

`neo4j/load.py` mirrors the same record set as nodes, plus `LOCATED_IN`
(hierarchy) and `OVERLAPS` (flood zone to admin unit, computed with Shapely
against PostGIS geometry). `neo4j/verify.py` compares per-`(label, edition)`
counts across both stores and **exits non-zero on any drift**.

`qdrant/client.py` owns the Qdrant connection and collection lifecycle.

### `src/digital_twin/twin.py`
Four entry points, all returning vintage-tagged values:

```python
get_context(admin_id)            # boundary + population + economy + infrastructure
get_flood_risk(admin_id)         # zones, overlap area and ratio
get_transit_access(admin_id)     # metro, bus, feed staleness
get_economic_profile(admin_id)   # indicator series, gaps, revisions
```

Economic indicators are published at district level, so a panchayat query walks
up the hierarchy, returns the ancestor's figure marked `inherited_from`, and
forces `match_confidence` to `unmatched-estimate`.

### `src/rag/`
Chunks at 500-800 tokens with ~50 overlap. all-MiniLM-L6-v2 encodes only 256
tokens per pass, so longer chunks are embedded as overlapping windows whose
vectors are mean-pooled and re-normalised — without this, ~61% of a
target-sized chunk would be stored but unsearchable. `retrieve(query,
admin_id)` boosts chunks that mention the target admin unit, reporting the raw
score and boost separately.

### `src/agents/`
Each agent runs the same LangGraph loop: gather domain data, gather document
context, reason, finalise. **Facts and reasoning are kept apart** — twin numbers
are collected deterministically into `evidence.twin_facts` with their vintages;
the model only writes the analysis block.

`supervisor_agent.py` runs all four concurrently, then resolves conflicts. Its
conflict detection is deterministic and runs **before** the model sees anything,
derived from the twin's own facts rather than the agents' prose — a model that
describes a high-classification flood zone as "low risk" cannot switch off the
safety net by saying so.

### `src/scenarios/run_scenario.py`
Runs the whole pipeline and writes a Markdown report plus raw JSON to
`data/derived/scenario_reports/<timestamp>.md`. Includes a traceability audit
that extracts every number from each agent's prose and checks it against that
agent's own inputs, so "never invent a figure" is measured rather than trusted.

---

## 5. Datasets

| dataset | what it is | use for |
| --- | --- | --- |
| `demo` | 12 files, clean, invented | fast happy path; test suites assert against it |
| `stress_test` | 13 files, 20 planted defects | the original defect checklist |
| `ernakulam` | real OSM boundaries + Census 2011 + Ecostat | the actual demo; realism check |
| `bench` | `ernakulam` with defects injected + ground-truth manifest | scored regression testing |

`bench` ships `data/bench/defect_manifest.json` recording what was injected,
where, and which check should catch it, so detection is scored rather than
eyeballed:

```bash
python scripts/build_ernakulam_dataset.py     # real base
python scripts/build_benchmark_dataset.py     # inject defects
python src/ingestion/ingest.py --reset --source-dir data/bench/bench_data
python -m src.processing.run
python scripts/score_detection.py             # currently 19/20 = 95%
```

The one miss is D20 (unreachable source URL) — the `--url` fetch path is still
stubbed, so nothing can catch a fetch failure yet.

See `data_dictionary.md` for per-source provenance and the vintage warnings that
must survive into any output.

---

## 6. Notes that are easy to get wrong

**Name matching is not string similarity.** An official layer writes "Mahatma
Gandhi Road" where OSM writes "M G Road" (0.593 raw similarity), and no raw
threshold separates that from "North Paravur" vs "South Paravur" (0.846, and
different places). `consistency_checks.name_match` expands abbreviations, strips
generic type words, matches initialisms structurally, and treats a
directional-token mismatch as an outright veto. Calibrated on real Kerala pairs:
every genuine pair scores >= 0.727, the nearest false pair scores 0.455.

**Two boundary vintages coexist.** Kerala's 2025 delimitation added 1,712 wards
statewide. Boundaries are current; census counts are on the old split. Any sum
across the two is an estimate and is labelled one.

**The flood layer is 2010 KSDMA/NCESS fieldwork.** Its `data_year` travels with
every value so it can never be read as current conditions.

**Concurrency is a no-op on a local backend.** Ollama runs
`OLLAMA_NUM_PARALLEL=1` and serialises the four agents even though the code
dispatches them concurrently. A scenario is a few minutes on GPU.

---

## 7. Demonstrating it

A 15-minute walkthrough. Pre-load the demo dataset beforehand
(`bash scripts/run_phase1.sh demo`) so you start from a working system, and have
a generated report on hand as a fallback.

**Act 1 — the pipeline refuses bad data (4 min).** Run Stage 2 against
`stress_test` live: 3 hard failures, 7 soft flags, 1 reprojection. The line that
lands: *these three records will not load; the other eight in the same file
will.* Rejecting the file would have been easy and wrong.

**Act 2 — two stores forced to agree (2 min).** Run `verify.py`, then break it
on purpose:

```bash
docker exec edt-neo4j cypher-shell -u neo4j -p password \
  "MATCH (n:BusStop {stop_id:'DEMO-BS-01'}) DETACH DELETE n;"
python -m src.storage.neo4j.verify; echo "exit: $?"   # 1, names the drift
python -m src.storage.neo4j.load                       # restore
python -m src.storage.neo4j.verify; echo "exit: $?"   # 0
```

A check nobody has seen fail is not a check.

**Act 3 — the twin (3 min).** `get_context('G07049')` on the real dataset shows
a real panchayat with real Census figures — and the boundary on the 2025
delimitation beside a population counted on the pre-2025 one. Then
`get_economic_profile` on a panchayat, to show a district figure arriving marked
`unmatched-estimate`.

**Act 4 — agents and Supervisor (5 min).** Walk a report: Analysis separated
from Evidence; then §4.2, the conflicts computed from the twin's facts *before*
the Supervisor saw them. The true story to tell: an earlier run had the
Environment Agent call a `high`-classification zone `"low"` risk, and the
detector fired anyway. Finish on the Traceability Summary, which counts figures
the model invented.

**Act 5 — the scorecard (1 min).** `python scripts/score_detection.py` → 95%,
one command, exits non-zero on regression.

### Questions to expect

**"Why not just fix the bad data?"** Because this rehearses real Ernakulam data,
where you cannot edit the source. Success is defects *detected and labelled*,
not absent.

**"Which numbers did the AI make up?"** §6 of every report answers that
mechanically, per agent.

**"Is there a trained model?"** No — and Phase 1 has no training stage. This is
retrieval-augmented inference over a validated knowledge base with a frozen
model. The engineering contribution is the validation layer.

**"Is this just RAG?"** RAG is ~7% of the code. Storage and cross-store
verification is 27%, processing and validation 20%, agents 14%.

### If something breaks mid-demo

| Symptom | Fix |
| --- | --- |
| `No LLM backend available` | Ollama died — `ollama serve` |
| Suddenly ~3x slower | Fell back to CPU — check `size_vram`, restart Ollama |
| `Qdrant collection does not exist` | `python -m src.rag.ingest --recreate` |
| `verify.py` drift | `python -m src.storage.neo4j.load --apply-schema --reset` |
| Tests all skip | Stores hold another edition; reload demo |
| Everything wedged | `bash scripts/run_phase1.sh demo` |

---

## 8. Known gaps

- `--url` acquisition is stubbed; sources are downloaded by hand.
- Roads, water bodies and flood zones in the `ernakulam` dataset have real names
  and real vintages but drawn geometry.
- 26 of 97 LSGs have no Census match — the Census publishes villages and towns,
  not panchayats. Explicit nulls, not zeros.
- Economy is 3 GDDP years; no open year-by-year series was found and nothing is
  interpolated.
- `PROJECT_ROOT` is defined in five modules and `storage/postgres/load.py` is
  imported by unrelated modules for paths and DB config. Known coupling, left
  in place deliberately.
