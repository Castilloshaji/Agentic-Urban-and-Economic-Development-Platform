# Phase 2 — Implementation Status

Everything below was built and verified. Nothing is marked done on the strength
of a description.

## Running it

```bash
python3 src/ingestion/ingest.py --url                  # real sources -> data/sources/
python3 -m src.features.hazard                         # KSDMA + GSI zonal statistics
python3 -m src.features.transit                        # GTFS accessibility
python3 -m src.features.taluks                         # DataMeet taluks + area deficit
python3 -m src.features.roads                          # OSM roads (needs --include-large)
python3 -m src.storage.postgres.load_features          # features -> PostGIS
python3 -m uvicorn src.api.main:app --port 8000        # API + dashboard
pytest                                                 # 132 tests, 0 skipped
```

## Status by step

| Step | What | Status |
| --- | --- | --- |
| A1 | `pyrosm` toolchain | **DONE** (0.15.0) |
| A2 | real `--url` fetcher, timeouts, retries, soft-404 detection | **DONE** — closes D20, score 20/20 |
| A3 | source register, Level 1–4, fetch modes | **DONE** — 20 entries |
| A4 | KSDMA flood rasters → features → PostGIS | **DONE** — `admin_hazard_feature`, 97 rows |
| A5 | GSI landslide shapefile → PostGIS | **DONE** |
| A6 | OSM road clip | **module built**, awaiting the 532 MB fetch |
| A7 | Kochi GTFS → features → PostGIS | **DONE** — `transit_feature`, 97 rows |
| A8 | DataMeet taluks as Level 3 with area deficit | **DONE** — 4/7 flagged `area_mismatch_vs_census` |
| A9 | MSME/UDISE registered unavailable | **CLOSED AS ESTIMATE** — district Udyam total (166,200, MSME Ministry 2024-12-15) allocated down; tagged `proxy` |
| B1–B4 | parameter engine, normalization, confidence | **DONE** — **34 parameters, 18 real / 16 proxy / 0 unavailable** across 7 domains |
| B5 | priority engine, formula v2 | **DONE** — `Σ weights = 1` |
| B6 | hard constraints | **DONE** — `overridable_by_model: false` |
| B7 | conflict engine extracted | **DONE** — `src/decision/conflicts.py` |
| C1 | scenario engine | **DONE** — 6 scenario types |
| C2 | twin Review-2 functions | **DONE** — 6 new functions reading PostGIS |
| C3 | Review-2 tables | **PARTIAL** — 8 tables created; 2 populated (97 rows each). The 6 audit tables are empty: `persist_run()` raises `can't adapt type 'numpy.int64'`, caught so the JSON still writes. See `MISSING_DATA.md` P2 |
| C4 | FastAPI | **DONE** — 24 endpoints; `POST /api/proposal` (idea in) and `POST /api/ideate` (budget in, no prompt) are the two entry points |
| C5 | dashboard | **DONE** — two modes. Idea + areas + budget → agent analyses, Supervisor decision, positives/negatives. Budget + areas alone → measured needs, need-weighted budget split, four domain proposals, costed portfolio |
| C6 | RAG corpus expansion | **NOT DONE** — corpus is 3 real Ernakulam documents under 2 KB; no authoritative PDF is openly downloadable. See `MISSING_DATA.md` P3 |
| D1 | same-data/different-scenario proof | **DONE** — asserted in tests |
| D2 | determinism test | **DONE** — 25 repeat runs, bit-identical |
| D3 | `Σ weights = 1`, constraint-bypass | **DONE** — property-tested over 30 units × 3 scenarios |
| D4 | 20 defect classes green | **DONE** — 20/20 |
| D5 | status matrix | this document; data gaps in `MISSING_DATA.md` |

## Seven domains, not four

The original plan had seven domain agents. Healthcare, Education and Budget
Allocation were added after the first four.

| Domain | Parameters | Data |
| --- | --- | --- |
| healthcare | `health_access`, `health_capacity`, `health_deficit` | 1,159 OSM facilities inside the 97 units |
| education | `education_access`, `education_capacity`, `education_deficit` | 2,044 OSM institutions |
| budget | `fiscal_entitlement`, `own_income_capacity`, `cost_exposure` | Kerala SFC devolution formula, applied |

**Budget is the important one.** Its spine is the State Finance Commission's
published basic-grant rule, **80% population / 10% area / 10% inverse own
income**, computed per local body in `parameters.py`. The 97 shares sum to 1.0.
One term is substituted because own income is not published below district
level, which is why it is tagged `proxy` and capped at `LEVEL_CONFIDENCE[1] ×
0.75`.

Budget carries non-zero relevance on **every** objective and is the leading
domain on **none**, both asserted in tests. Money shapes what is affordable. It
does not decide what is needed.

Adding domains broke three things that are now pinned by tests: a `DOMAINS`
copy in two modules that drifted, a relevance row that would silently drop a
domain, and a rank assertion hard-coded to four.

## Beyond the Review-2 matrix

Three capabilities were added after the matrix above was written, because the
matrix assumed a user who already knows what they want to build.

| | |
| --- | --- |
| **Free-text idea mode** | `POST /api/proposal` — the four agents analyse the user's own wording, not a catalogue description. The classified objective only selects which relevance row supplies the weights, and is reported back under `weight_basis` so the inference stays visible. |
| **Budget-only ideation** | `POST /api/ideate` — no prompt at all. See the section below. |
| **Implementation ledger** | `implementation` + `implementation_event` — commit a proposal, track it, and stop it being re-proposed. See below. |

## Budget-only ideation

`POST /api/ideate` takes a budget and a set of local bodies and returns
proposals. No idea text, no scenario, no prompt. The chain:

1. **Needs measured** (`src/decision/needs.py`) — normalised parameters become
   ranked shortfalls. `DIRECTION` declares per parameter whether a high
   percentile is a deficit or an asset; the test suite asserts every parameter
   in the engine appears there, because a missing entry would read an asset as
   a deficit and have the system proposing flood defences for the driest
   panchayat in the district.
2. **Budget split** — arithmetic over domain need pressure, with a 5% floor so
   a domain with no measured shortfall can still fund maintenance. Computed
   before any model runs; agents are told their envelope and cannot bid for a
   bigger one.
3. **Hazard blocks** — computed from GSI landslide class and KSDMA flood share
   with no reference to any objective, so they are established before anything
   has been proposed.
4. **Four domain proposals** — each agent proposes at most 3 interventions
   inside its envelope, each naming the shortfall parameter it addresses.
5. **Portfolio** — the Supervisor selects within budget, reports headroom,
   defers the rest and must name the biggest need it did not fund.

`GET /api/needs?admin_id=…` returns step 1 alone, with no model, instantly.

## Implementation ledger

`implementation` + `implementation_event` record what someone committed to and
how it progressed. Lifecycle `planned → in_progress → completed` with `on_hold`
and `cancelled`; illegal transitions refused with 409; every change appended as
an event.

Two properties worth defending:

* **Ideation does not repeat itself.** A logged item names the parameter it
  addresses. Agent briefs mark covered shortfalls `ALREADY BEING ADDRESSED`,
  and the returned proposals are re-checked against the ledger by title and by
  target — flagged as `duplicate_of`, never silently dropped. Cancelled work
  releases its need again.
* **An intention never edits a measurement.** Completing drainage work does not
  move `flood_risk`; the figure is KSDMA's and changes only on
  re-measurement. Needs carry `addressed_by` alongside unchanged severity, and
  a test asserts coverage-on and coverage-off produce identical severities.

These are the only two tables excluded from `schema.sql`'s DROP list, because
they hold decisions rather than derived data.

## Closing the six unavailable parameters

| Parameter | Status | Route |
| --- | --- | --- |
| `metro_access` | real | KMRL station list joined down from `admin_id = EKM-D` to the 97 units |
| `water_body_proximity` | real | 14,212 OSM water features replacing 3 hand-drawn polygons |
| `right_of_way_constraint` | proxy | narrow-class network share; OSM `lanes` covers 5.7%, `width` 0.8%, `highway` 100% |
| `msme_presence` | proxy | 166,200 district Udyam registrations, allocated |
| `investment_potential` | proxy | ₹261,319 district per-capita income, modulated |
| `employment_potential` | proxy | enterprise density + population, percentile-ranked |

Two were measurements waiting to be joined; four are allocations of published
district figures. Allocated estimates reproduce their anchor exactly and are
capped at confidence 0.55, below a current Level-3 measurement. 26 units lack a
Census population and have it imputed from the district median, flagged per row
— see `data_dictionary.md` for why a regression was tried and rejected.

## The decision engine

```
raw_i    = relevance_i²  ×  evidence_i  ×  (0.45 + 0.55 × signal_i)
weight_i = raw_i / Σ raw
```

Three terms, three questions: **relevance** (does this domain bear on the
objective), **evidence** (how much do we trust the data here), **signal** (how
strongly does this unit exhibit the concern).

An earlier version multiplied normalized × confidence inside one mean. That let
a domain with three high-confidence Census parameters outrank the domain the
scenario was about — infrastructure won all six scenarios. Separating the terms
fixed it.

**Parameters: 13 real · 4 proxy · 8 unavailable.** Unavailable returns `null`
with a stated reason, never 0.

## The central demonstration

Same unit (Thiruvaniyoor), same data, six objectives:

| objective | econ | infra | trans | envt | leads |
| --- | --- | --- | --- | --- | --- |
| public transport expansion | 0.102 | 0.368 | **0.446** | 0.084 | transportation |
| industrial development | **0.367** | 0.326 | 0.037 | 0.269 | economic |
| flood-resilient development | 0.047 | 0.349 | 0.083 | **0.521** | environment |
| transit-oriented development | 0.235 | **0.325** | 0.309 | 0.130 | infrastructure |
| affordable housing | 0.135 | **0.530** | 0.130 | 0.205 | infrastructure |
| urban service expansion | 0.187 | **0.608** | 0.066 | 0.139 | infrastructure |

Transportation moves 0.037 → 0.446 on identical data.

**Priority vs constraint:** Malayattoor-Neeleswaram is `blocked` for industrial
development (GSI high landslide, measured 3.0 ≥ threshold 3.0) but only
`conditional` for flood-resilient development. Same unit, same data, different
permission — and the block holds while environment carries any weight.

## Real data validated by geography

- Flood exposure concentrates in **Paravur/Aluva** — the Periyar delta
  (Chittattukara 50.0%, Alangad 47.4%)
- Landslide-high units are **all eastern Western Ghats foothills**
- **RCP 8.5 ≥ historical in 97/97 units**, mean +5.0 points
- Best transit is the urban Kochi corridor; worst is **Edamalakkudi at 52 km**
- 43/97 units have zero stops; median nearest stop 907 m
- Return-period monotonicity holds for every unit (tested)

## Audit trail — designed, not yet populated

The schema is there and the chain is right: `scenario_run` → `agent_priority`
(every formula term per domain) → `scenario_parameter` (all 25 with provenance)
→ `constraint_result` (measured vs threshold, `overridable_by_model = false`) →
`decision` → `audit_event`.

**But all six tables are empty**, so a decision is *not* currently
reconstructable from SQL. `persist_run()` raises
`can't adapt type 'numpy.int64'` before its first insert, and the exception is
caught so the JSON report is still written and nothing appears to fail. The
runs exist — `data/derived/scenario_runs/` and `data/derived/proposals/` — but
only as files.

Two things are needed, neither of which requires new data: coerce numpy
scalars before they reach psycopg2 (the same fix `src/api/main.py`'s `clean()`
already applies), and call `persist_run()` from `evaluate_proposal()`, which
currently writes JSON only. Tracked as `MISSING_DATA.md` P2.

## Known gaps

| Gap | Why |
| --- | --- |
| **Audit-trail tables empty** | `persist_run()` numpy coercion bug, caught silently. **Closable now, no new data** — see above |
| RAG corpus still 3 short documents | Economic Review / Budget PDFs are not openly downloadable at stable URLs |
| Budget-only ideation allocates 20–28% | Local 7B is conservative. `budget_check` recomputes and reports it server-side rather than correcting it; a frontier model is an `.env` change |
| 26 of 97 populations missing | Boundaries post-date Census 2011. `census_village_town_pca_kerala` would close it but needs an API key |
| MSME, sub-district economy | Kerala OGD catalog empty; not published below district level |
| Ward-level analysis | 2025 delimitation map is a JS viewer with no export |
| Rainfall, LULC | IMD gated; Bhuvan needs interactive selection |
| Healthcare / Education agents | No machine-readable source |
| Quantitative impact prediction | No cost or elasticity model exists |
| Agent narrative | Works when Ollama is up; the deterministic path never depends on it |

## Regression

Phase 1 intact: demo pipeline 47 rows, 20 nodes, **zero drift**, bench detection
**20/20** defect detection (the `--url` acquisition step is required for D20 —
see `PROJECT_GUIDE.md` §5), pyflakes clean, **132 tests pass with 0 skipped**:

| suite | tests | asserts |
| --- | --- | --- |
| `test_decision_engine.py` | 55 | weights, constraints, conflicts, needs, budget split, hazard-block semantics, duplicate flagging |
| `test_agents.py` | 22 | live LLM calls — schema compliance, vintage-tagged evidence, derived layers reaching the agents |
| `test_features.py` | 20 | feature layers, anchor reconstruction, the ±30% income bound |
| `test_digital_twin.py` | 18 | the twin against real Ernakulam facts, via both live stores |
| `test_implementations.py` | 17 | ledger lifecycle, transition guards, coverage join, agent-output coercion |

The agent and twin suites hit the live databases and a live model, so they
**skip if the stores hold the demo or stress edition** — a `demo` pass leaves 34
tests skipped and the twin layer untested while the suite still reports green.
Always finish on `bash scripts/run_phase1.sh real`.
