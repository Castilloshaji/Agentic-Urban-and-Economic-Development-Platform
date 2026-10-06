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

Two layers, and the boundary between them is the point of the whole design.

**The deterministic layer decides.** Parameters, domain weights, hazard
constraints, suitability scores, need assessment and the budget split are
arithmetic over measured data. No language model participates, so identical
inputs give identical outputs — proven in the tests by repeat runs and
byte-identical feature rebuilds.

**The model layer explains.** Seven domain agents and a Supervisor describe,
qualify and argue about a decision that has already been computed. They cannot
change a weight, clear a constraint, alter a need severity, or move money
between domains.

The seven are economic, infrastructure, transportation, environment, healthcare,
education and budget. Each owns a slice of the 34 parameters and gets a brief
built only from its own domain's measured readings.

**Budget is the one that constrains the rest.** Its spine is Kerala's published
devolution rule, not an invention: the State Finance Commission distributes the
basic grant to local governments on **80% population, 10% area, 10% inverse of
own income**. `parameters.py` computes every local body's share under that rule,
so `fiscal_entitlement` is a real entitlement. One term is substituted, because
own income is not published per local body, and the parameter says so everywhere
it appears. Budget carries non-zero relevance on every objective and is never
the leading domain on any, which is asserted in the tests: money shapes what is
affordable, it does not decide what is needed.

Phase 1 is the nine ingestion-to-report stages, matching
`../docs/ernakulam_phase1_architecture.md`. Phase 2 adds the decision engine,
the feature layers, the API and the website.

| Stage | Module | Does |
| --- | --- | --- |
| 1 Acquisition | `src/ingestion/` | Copy or fetch sources into `data/raw/`, sha256 every file |
| 2 Processing | `src/processing/` | CRS standardisation, admin-code reconciliation, value cleaning, hard/soft checks |
| 3 Storage | `src/storage/{postgres,neo4j,qdrant}/` | Load both stores; prove they agree |
| 4 Digital twin | `src/digital_twin/` | One query layer over PostGIS + Neo4j |
| 5 RAG | `src/rag/` | Chunk, embed, retrieve government documents |
| 6-7 Agents | `src/agents/` | Seven domain agents + Supervisor |
| 8-9 Scenario | `src/scenarios/` | Run end to end, write a cited Markdown report |
| **Features** | `src/features/` | Eight measured layers from the raw sources, plus allocated economic estimates |
| **Decision** | `src/decision/` | Parameters, weights, constraints, conflicts, needs, proposals, ideation, ledger |
| **API + UI** | `src/api/`, `frontend/` | 24 endpoints and the two-mode website |

### Directory layout

```
ernakulam-digital-twin/
├── README.md                   what this is, status, known gaps
├── PROJECT_GUIDE.md            this file
├── data_dictionary.md          provenance, feature layers, economic anchors
├── docs/
│   ├── PHASE2_STATUS.md        Review-2 requirement matrix
│   └── MISSING_DATA.md         every known gap, ranked
├── docker-compose.yml          postgres+postgis, neo4j, qdrant
├── requirements.txt            .env.example
├── src/
│   ├── ingestion/              ingest.py, describe.py, sources.yaml
│   ├── processing/             crs, reconcile_ids, clean_values,
│   │                           consistency_checks, run, paths
│   ├── features/               build_all, hazard, transit, roads, metro,
│   │                           water, row, taluks, econ, anchors
│   ├── storage/
│   │   ├── postgres/           schema.sql, load.py, load_features.py
│   │   ├── neo4j/              schema.cypher, load.py, verify.py
│   │   └── qdrant/             client.py
│   ├── digital_twin/           twin.py
│   ├── decision/               parameters, priority, constraints, conflicts,
│   │                           scenarios, runner, interpret, proposal,
│   │                           needs, ideate, implementations
│   ├── rag/                    ingest.py, retrieve.py
│   ├── agents/                 llm.py, base.py, {economic,infrastructure,
│   │                           transportation,environment,supervisor}_agent.py
│   ├── api/                    main.py — 24 endpoints, serves the frontend
│   └── scenarios/              run_scenario.py
├── frontend/                   index.html, app.js, app.css
├── scripts/
│   ├── run_phase1.sh           full pipeline; defaults to real data
│   ├── build_ernakulam_dataset.py   real data from OSM + Census + Ecostat
│   ├── build_benchmark_dataset.py   inject defects, emit ground truth
│   └── score_detection.py      score detection against that ground truth
├── tests/                      132 tests
└── data/
    ├── raw/                    Stage 1 output, wiped by --reset
    ├── processed/              Stage 2 output and reports
    ├── features/               the eight derived layers (committed)
    ├── derived/                scenario_reports, scenario_runs, proposals, ideation
    ├── sources/                upstream files (not redistributed)
    └── demo/  stress_test/  ernakulam/  bench/
```

### The four hard invariants

**Hard checks halt individual records, never the run.** A bowtie polygon stops
that polygon loading; the rest of the file still loads. Findings go to
`data/processed/hard_check_failures.json`.

**Soft checks never halt anything.** They attach a `match_confidence` of
`exact` / `crosswalked` / `unmatched-estimate` that travels with the row into
both databases and into the final report, so uncertainty is visible at the point
of decision rather than in a log nobody opens.

**No model can change a weight or clear a constraint.** Every constraint
carries `overridable_by_model: false`, and a blocking one forces
`stance = not_permitted` whatever the weights or the prose say. Property-tested
across 30 local bodies × every objective.

**An intention never edits a measurement.** Completing an implementation does
not move `flood_risk` — that figure is KSDMA's, and it changes only when
someone re-measures. Committed work annotates a need with `addressed_by`
alongside its unchanged severity. The test suite asserts that coverage-on and
coverage-off produce identical severities.

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
python src/ingestion/ingest.py --reset --source-dir data/ernakulam/ernakulam_data
python -m src.processing.run
python -m src.storage.postgres.load --dry-run      # counts, no writes
python -m src.storage.postgres.load
python -m src.storage.postgres.load_features       # hazard, transit + derived layers
python -m src.storage.neo4j.load --apply-schema --reset
python -m src.storage.neo4j.verify                 # exits non-zero on drift
python -m src.rag.ingest --recreate
python src/scenarios/run_scenario.py --admin-id G07027 --scenario "..."
```

Order matters in two places. `load_features` must run **after**
`postgres.load`, because applying `schema.sql` drops and recreates every table
— running it first silently empties the feature tables. And `postgres.load`
must run before `neo4j.load`, which reads boundary geometry back out of PostGIS
to attribute stations and stops to the local body containing them.

Or all of it, exiting non-zero on the first failure:

```bash
bash scripts/run_phase1.sh               # real Ernakulam data (the default)
bash scripts/run_phase1.sh stress        # the 20-defect fixture, Stage 2 only
bash scripts/run_phase1.sh all           # stress pass, then real — ends on real
```

**The stores are meant to hold real Ernakulam data.** The demo and stress
editions exist to exercise the processing stage; leaving one loaded makes every
twin query return a fictional answer, which is a failure mode worth naming
because it is invisible — the queries succeed, they are just about a district
that does not exist.

If your environment is not on `PATH` (nohup, cron, CI), pass the interpreter:
`PYTHON=/path/to/python bash scripts/run_phase1.sh`.

### Tests

```bash
pytest
```

Suites assert demo-dataset facts and skip automatically when the stores hold
another edition, so a stress or bench run does not report false failures.

### The website

```bash
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8077
```

Then open http://127.0.0.1:8077/. There is no scenario menu. You write the idea
in your own words, click the local bodies it affects on the map (or tick them in
the list), give a budget, and press **Evaluate my idea**. The page returns the
four domain agent analyses, the Supervisor's recommendation, and the positives
and negatives of that decision.

There are **two modes**, and they answer different questions.

**1. You have an idea.** Write it, pick the affected areas, give a budget. The
four agents analyse your words; the Supervisor returns a decision with
positives and negatives.

**2. You only have a budget.** Enter it, pick the areas, press *Propose ideas
for this budget* — no prompt at all. The system measures what those areas are
short of, splits the budget across the four domains by that measured need, and
each agent proposes interventions inside its own envelope. The Supervisor
assembles a costed portfolio and names the biggest need it could not fund.

Mode 2's ordering is the point. Needs come from `src/decision/needs.py`, which
turns normalised parameters into ranked shortfalls — `DIRECTION` declares
whether a high percentile on each parameter is a deficit or an asset, because
normalisation alone cannot tell you which way is bad. The budget split is
arithmetic over that need pressure, computed **before** any model runs: letting
the agents bid for budget would make the allocation a function of how
persuasively each one writes. Hazard blocks are computed from GSI and KSDMA
data without reference to any objective, and the Supervisor is told it cannot
clear one.

`POST /api/proposal` is the same thing without the browser:

```bash
curl -s -X POST http://127.0.0.1:8077/api/proposal \
  -H 'Content-Type: application/json' -d '{
    "text": "MSME industrial park with a shared effluent plant and a feeder bus to Angamaly station, about 900 jobs",
    "admin_ids": ["G07017", "M07028", "G10095"],
    "budget_inr_crore": 120,
    "with_agents": true}'
```

With `with_agents: false` it answers in about a second and returns the measured
position, weights and constraints only — useful for checking a selection before
spending minutes on the agents.

Mode 2, and the deterministic need assessment on its own:

```bash
curl -s -X POST http://127.0.0.1:8077/api/ideate \
  -H 'Content-Type: application/json' \
  -d '{"admin_ids": ["G07017", "M07028", "G10095"], "budget_inr_crore": 120}'

curl -s 'http://127.0.0.1:8077/api/needs?admin_id=G07017'   # no model, instant
```

### Implementation ledger

A proposal only matters once someone commits to it, so the website can log any
idea and track it:

```bash
# commit an idea (the shape the ideation agents produce)
curl -s -X POST http://127.0.0.1:8077/api/implementations \
  -H 'Content-Type: application/json' -d '{
    "idea": {"title": "Flood defence in Kalady", "domain": "environment",
             "addresses": "climate_vulnerability", "est_cost_inr_crore": 12},
    "admin_ids": ["G10095"], "origin": "ideation"}'

curl -s http://127.0.0.1:8077/api/implementations            # the ledger
curl -s http://127.0.0.1:8077/api/implementations/2          # one item + its events
curl -s -X PATCH http://127.0.0.1:8077/api/implementations/2 \
  -H 'Content-Type: application/json' -d '{"status": "in_progress"}'
```

Lifecycle: `planned → in_progress → completed`, plus `on_hold` and `cancelled`.
Illegal moves are refused with 409 — `completed` is terminal, because a tracker
that lets it silently become `planned` cannot answer "what is in progress",
which is the question it exists to answer. Every change is appended to
`implementation_event`, so "when did this stall" stays answerable.

**This stops the same ideas coming back.** A logged item records the parameter
it addresses, and ideation consults the ledger in two places: the agent briefs
mark a covered shortfall `ALREADY BEING ADDRESSED` and list the work by name,
and `_flag_duplicates` checks the returned proposals against the ledger
afterwards — because an instruction is not a guarantee on a 7B model. A repeat
is flagged with `duplicate_of`, not deleted, so a reader can see the agent
proposed it again. `cancelled` work releases its need, which then reappears.

**What completing an implementation deliberately does NOT do** is change a
measured parameter. Finish drainage work and `flood_risk` still reads exactly
what KSDMA measured. Editing a measurement to reflect an intervention would
fabricate data and destroy the provenance the engine rests on; a measurement
moves only when someone re-measures. So severity is unchanged and the need
simply carries `addressed_by` alongside it — the tests assert that
`assess(..., with_coverage=True)` and `with_coverage=False` give identical
severities.

`implementation` and `implementation_event` are the only tables **excluded from
`schema.sql`'s DROP list**. Everything else is derived and can be rebuilt; these
hold decisions that exist nowhere else.

`/api/ideate` makes five model calls (four domains plus the Supervisor), so it
takes several minutes on the local 7B. `/api/needs` is pure arithmetic and
returns immediately — it is the honest way to show what the agents were given.

The scenario catalogue still exists, but only as the lookup that supplies the
relevance row for the weights. The classified objective is reported back under
`weight_basis` so the inference stays visible; the agents are never shown its
wording, only yours.

---

## 4. Module reference

### `src/ingestion/`
`ingest.py --source-dir DIR [--reset]` copies files into the matching
`data/raw/<category>/`, recording path, category, sha256, timestamp and source
in an append-only `ingestion_manifest.csv`. Files outside the six known
categories are skipped and listed, not guessed at.

`--url CATEGORY [--include-large]` is a real fetcher: streaming download with a
35 s connect / 300 s read timeout, 3 retries, `.part` staging, and content-type
validation that catches a soft 404 (`kerala.data.gov.in` answers HTTP 200 with
1 MB of HTML for a missing CSV). Unreachable sources are logged to
`data/sources/acquisition_log.json` under `fetch_failures` and the run
continues. That log is what makes defect class D20 detectable — see §5.

`describe.py --write` regenerates the file inventory inside
`data_dictionary.md` from the manifest.

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

### `src/features/`
Eight layers built from the acquired data into `data/features/`, read by the
parameter engine:

```bash
python3 -m src.features.build_all            # all layers, in dependency order
python3 -m src.features.build_all --only econ
```

`hazard` (KSDMA flood + GSI landslide), `transit` (Kochi GTFS), `roads`,
`water`, `row` and `social` (OSM clip), `metro` (KMRL station list), `taluks`
(DataMeet boundary quality) and `econ`.

`social` extracts healthcare and education facilities from the same OSM extract
the road layer uses, and is a measurement: where a clinic or a school is, is
observed. Two caveats travel with every number it produces, because both change
what it means. OSM mixes government and private provision, and Kerala's private
health sector is large, so the OSM count being several times the published
government total is expected rather than an error. And mapping coverage is
uneven, so a low count may mean thin provision or a thinly mapped place.
Distance from the centroid to the nearest facility is far less sensitive to
mapping effort than a count, which is why the access parameters lean on it. Layers needing the 532 MB OSM extract are skipped
with a message, not a failure — the engine is designed to report their
parameters unavailable instead.

`econ` runs last because it reads road, transit and metro output to build its
activity signal. It allocates published district totals down to local bodies and
every figure it produces is tagged `proxy`. `anchors.py` holds those district
figures with publisher, as-on date and URL, and serves them at `GET
/api/anchors`:

| Anchor | Value | Publisher |
| --- | --- | --- |
| Udyam MSME registrations | 166,200 (2024-12-15) | Ministry of MSME, Lok Sabha reply |
| Per-capita income | ₹261,319 (2024) | Kerala Ecostat |
| District GDDP | ₹167,661.9 crore (2024) | Kerala Ecostat |
| District population | 3,282,388 (2011) | Census of India 2011 |

Each allocation reproduces its anchor exactly, which the test suite asserts.
Parameter coverage is **16 real / 9 proxy / 0 unavailable** of 25; see
`data_dictionary.md` §3 for how the six previously-unavailable parameters were
closed and what the 26 imputed populations cost.

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

### `src/decision/`
The deterministic engine. No module here calls a language model except
`proposal.py` and `ideate.py`, and both compute their answer before doing so.

- `parameters.py` — 25 parameters per local body, each `real` / `proxy` /
  `unavailable`, percentile-normalised across all 97 units, with source level,
  vintage and confidence. `ALLOCATION_PENALTY` caps an allocated estimate at
  0.55 confidence.
- `priority.py` — `raw = relevance² × evidence × (0.45 + 0.55 × signal)`,
  normalised so `Σ weights = 1`. Three separate terms, because a single
  multiplied score let infrastructure win every objective.
- `constraints.py` — hazard thresholds from KSDMA and GSI. Every constraint
  carries `overridable_by_model: false`; a blocking one forces
  `stance = not_permitted`.
- `conflicts.py` — computed from twin facts *before* any model output exists.
- `scenarios.py` — six objectives with an explicit relevance matrix.
- `runner.py` / `proposal.py` — single-area and multi-area evaluation.
  `proposal.py` is what the website's idea mode calls.
- `interpret.py` — deterministic free-text classification by sentence
  embedding, used only to pick which relevance row supplies the weights.
- `needs.py` — turns normalised parameters into ranked shortfalls. `DIRECTION`
  declares per parameter whether a high percentile is a deficit or an asset;
  normalisation alone cannot tell you which way is bad, and a missing entry
  would have the system proposing flood defences for the driest panchayat in
  the district. A test asserts the map is complete.
- `ideate.py` — budget-only proposal generation. Needs measured, budget split by
  need pressure, hazard blocks established, *then* the agents propose.
- `implementations.py` — the ledger. Lifecycle, transition guards, event
  history, and the coverage join that stops work being re-proposed.

### `src/api/main.py`
24 endpoints and the static frontend. `clean()` recursively converts numpy
scalars and NaN before they reach a response — a single buried `numpy.int64`
fails an entire payload, which it did on five endpoints before this was made
recursive.

### `frontend/`
Three files, no build step, no framework. `index.html` + `app.js` + `app.css`,
served by FastAPI at `/`. Leaflet for the choropleth, loaded from a CDN.

### `src/scenarios/run_scenario.py`
Runs the whole pipeline and writes a Markdown report plus raw JSON to
`data/derived/scenario_reports/<timestamp>.md`. Includes a traceability audit
that extracts every number from each agent's prose and checks it against that
agent's own inputs, so "never invent a figure" is measured rather than trusted.

---

## 5. Datasets

| dataset | what it is | use for |
| --- | --- | --- |
| **`ernakulam`** | **real** OSM/OpenDataKerala boundaries, Census 2011, Ecostat, KMRL, Kochi GTFS, KSDMA, GSI | **the loaded edition — everything asserts against it** |
| `stress_test` | 13 files, 20 planted defects | exercising Stage 2's checks |
| `demo` | 12 files, clean, invented | a fast happy path for the processing stage |
| `bench` | real base with defects injected + ground-truth manifest | scored regression testing |

The test suite asserts **real Ernakulam facts** and skips if the stores hold
another edition — so a `demo` or `stress` pass leaves 34 tests skipped and the
twin layer untested while the suite still reports green. Always finish on
`bash scripts/run_phase1.sh real`.

`bench` ships `data/bench/defect_manifest.json` recording what was injected,
where, and which check should catch it, so detection is scored rather than
eyeballed:

```bash
python scripts/build_ernakulam_dataset.py     # real base
python scripts/build_benchmark_dataset.py     # inject defects
python src/ingestion/ingest.py --url documents   # populates fetch_failures -> D20
python src/ingestion/ingest.py --reset --source-dir data/bench/bench_data
python -m src.processing.run
python scripts/score_detection.py             # 20/20
```

**The `--url` step is not optional for a full score.** 19 of the 20 classes are
caught by Stage 2 from the `--source-dir` pass. D20 is an *acquisition* defect:
it is scored from `fetch_failures` in `data/sources/acquisition_log.json`, which
only gets written when `--url` has actually attempted the
`deliberately_unreachable_probe` entry in `sources.yaml`. Score a
`--source-dir` pass on its own and it correctly reports **19/20, missing D20** —
that is the checker working, not a regression.

Scoring reads `data/processed`, so it scores **whatever edition was processed
last**. Run the bench pass, score it, then reload real data
(`bash scripts/run_phase1.sh real`) — scoring a real-data pass against the
bench manifest reports a meaninglessly low number.

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

A 20-minute walkthrough. Start the services, then **send one throwaway request**
— the first call spends ~14 s loading the sentence-embedding model, and that
pause looks like a hang in front of an audience. Have a generated report and a
saved `data/derived/ideation/*.json` on hand as fallbacks.

```bash
ollama serve &
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8077 &
curl -s "http://127.0.0.1:8077/api/needs?admin_id=G07027" > /dev/null   # warm it
```

**Act 1 — the pipeline refuses bad data (4 min).** Run Stage 2 against
`stress_test` live: 3 hard failures, soft flags, reprojections. The line that
lands: *these three records will not load; the others in the same file will.*
Rejecting the file would have been easy and wrong. Reload real data afterwards
(`bash scripts/run_phase1.sh real`) — leaving the stress edition loaded makes
every later query answer about a fictional district.

**Act 2 — two stores forced to agree (2 min).** Run `verify.py`, then break it
on purpose:

```bash
docker exec edt-neo4j cypher-shell -u neo4j -p password \
  "MATCH (n:BusStop {stop_id:'EKM-BS-001'}) DETACH DELETE n;"
python -m src.storage.neo4j.verify; echo "exit: $?"   # 1, names the drift
python -m src.storage.neo4j.load --reset               # restore
python -m src.storage.neo4j.verify; echo "exit: $?"   # 0
```

A check nobody has seen fail is not a check.

**Act 3 — explore, instantly (3 min).** On the website, select two or three
local bodies and use the three no-model views. *What are these areas short of?*
returns the measured shortfall table in under a second. *Rank all 97 local
bodies* scores the whole district for one objective. *Compare two objectives*
is the strongest single slide in the project: same area, same data, and
economic weight moves 32% → 4% while environment moves 32% → 53%, under a
header stating the underlying data is byte-identical in both columns.

**Act 4 — a decision that refuses (3 min).** Type an idea — *"MSME industrial
park with a shared effluent plant near Malayattoor, 900 jobs"* — select
Malayattoor, Angamaly and Kalady, leave the agents unticked. It answers in
under a second: Malayattoor `not_permitted` on the GSI high-landslide class
while Angamaly is `recommended`. Open **Hard constraints** and show
`model override: not permitted`. Then the **Data coverage** card: 25
parameters, 16 measured / 9 proxy / 0 unavailable, with the anchors table
linking the Ministry of MSME and Ecostat sources an estimate was allocated
from.

**Act 5 — the agents (5 min).** Re-run with the agents ticked, or walk a
pre-generated report. Analysis is separated from Evidence; §4 is the
Supervisor; the conflicts in it were computed from the twin's facts *before*
the Supervisor saw them. The true story to tell: an earlier run had the
Environment Agent call a `high`-classification zone `"low"` risk, and the
deterministic detector fired anyway. Finish on §6, the Traceability Summary,
which counts figures the model introduced.

**Act 6 — budget in, ideas out (3 min, pre-run).** Give a budget with no
prompt at all. Show the measured need table, then the budget split — ₹120
crore divided across the domains by need pressure, computed before any model
ran — then the four domain proposals and the Supervisor's costed portfolio.
Be straight about the under-allocation: the local 7B selects 20–28% of the
budget, the server recomputes that figure rather than trusting the model, and
the page says so.

**Act 7 — the ledger (2 min).** Press **Implement** on one proposal, move it to
*in progress* with a note, expand its **history** to show the event trail. Then
re-run the ideation: that intervention does not come back. Say plainly that
completing it will *not* change `flood_risk` — the figure is KSDMA's and moves
only on re-measurement.

**Act 8 — the scorecard (1 min).**

```bash
python scripts/score_detection.py      # 20/20, exits non-zero on regression
```

### Questions to expect

**"Why not just fix the bad data?"** Because this rehearses real Ernakulam
data, where you cannot edit the source. Success is defects *detected and
labelled*, not absent.

**"Which numbers did the AI make up?"** §6 of every report answers that
mechanically, per agent. On the website, the Data coverage card marks every
parameter `real` / `proxy`, and every allocated estimate reaches the agents
with `match_confidence: unmatched-estimate`.

**"So the AI makes the decision?"** No. Ask them to watch the first click: the
stance, the weights and the constraints appear in under a second, before any
model is called. Untick the agents and the decision is identical.

**"Is there a trained model?"** No. This is retrieval-augmented inference over
a validated knowledge base with a frozen model. The engineering contribution is
the validation layer.

**"Is this just RAG?"** RAG is a small fraction of the code. The decision
engine, the feature layers, storage and cross-store verification dominate.

### If something breaks mid-demo

| Symptom | Fix |
| --- | --- |
| First request hangs ~14 s | Expected — the embedding model is loading. Warm it before you start. |
| `No LLM backend available` | Ollama died — `ollama serve` |
| Suddenly ~3x slower | Fell back to CPU — check `size_vram`, restart Ollama |
| `Qdrant collection does not exist` | `python -m src.rag.ingest --recreate` |
| `verify.py` drift | `python -m src.storage.neo4j.load --apply-schema --reset` |
| Tests skip, or the twin answers about "Demo Panchayat" | Stores hold the demo or stress edition — `bash scripts/run_phase1.sh real` |
| Feature tables empty after a schema reapply | `schema.sql` drops them; re-run `postgres.load` then `load_features` |
| Everything wedged | `bash scripts/run_phase1.sh real` |

---

## 8. Known gaps

Full ranking, with what is closable, in
[docs/MISSING_DATA.md](docs/MISSING_DATA.md). In short:

- **The audit-trail tables are empty.** `persist_run()` raises
  `can't adapt type 'numpy.int64'` before writing and the error is caught, so
  the JSON report is still produced and nothing looks broken. Six tables —
  `scenario_run`, `scenario_parameter`, `agent_priority`, `constraint_result`,
  `decision`, `audit_event` — have no rows. Needs a type coercion plus wiring
  `evaluate_proposal` to `persist_run`; no new data required.
- **The RAG corpus is three documents under 2 KB.** Citations are real but
  thin. The Kerala Economic Review and Budget PDFs change URL per year, so
  there is nothing stable to fetch.
- **Budget-only ideation allocates 20–28% of the budget** on the local 7B. The
  figure is recomputed from the selected items server-side and surfaced, not
  corrected. A frontier model via `ANTHROPIC_API_KEY` is an `.env` change.
- **26 of 97 local bodies have no Census 2011 population**, because their
  boundaries post-date the census. Explicit nulls, not zeros. The economic
  allocator substitutes the district median and flags each affected row; a
  regression on road length and stop count was tried and rejected because
  leave-one-out validation put it *worse* than the median.
- **Three parameters have no measured input at any level** —
  `msme_presence`, `investment_potential`, `employment_potential` rest entirely
  on allocated district anchors. Rainfall and land-use/land-cover have no
  parameter at all: IMD is licence-gated and Bhuvan needs interactive selection.
- **10 of 20 catalogued sources cannot be fetched unattended** — 2 need a
  data.gov.in API key, 7 need a manual download.
- **One shipped file is synthetic.**
  `data/ernakulam/.../gis/water_bodies_utm_ernakulam.geojson` has real names and
  hand-drawn geometry. Superseded by `data/features/water_features.csv`, built
  from 14,212 real OSM features — do not reuse the geojson for measurement.
- **`PROJECT_ROOT` is defined in several modules** and
  `storage/postgres/load.py` is imported by unrelated modules for paths and DB
  config. Known coupling, left in place deliberately.
