# Agentic Urban and Economic Development Platform

A multi-agent decision-support platform for urban and economic development,
built on a digital twin of **Ernakulam district, Kerala** — 97 local bodies,
real government data, everything running locally.

Its central claim is a separation of powers: the decision is **arithmetic over
measured data**, and the language models only explain it. Domain weights,
hazard constraints and suitability scores are computed without any model
involvement, so identical inputs give identical results. An agent can describe
a decision, qualify it, and argue about its trade-offs; it cannot change a
weight or clear a safety constraint.

The second rule is provenance: **no value is returned without the vintage it
was published under.** Every figure carries `data_year`, `revision_status`,
`boundary_vintage`, `source`, `source_level` and a confidence, so a 2010 hazard
classification can never be presented as current fact, and an estimate
allocated from a district total can never pass as a measurement of one
panchayat.

## Documentation

| | |
| --- | --- |
| **[PROJECT_GUIDE.md](PROJECT_GUIDE.md)** | setup, the full pipeline, module reference, both decision modes, the implementation ledger |
| **[data_dictionary.md](data_dictionary.md)** | every data source and feature layer, how the six unavailable parameters were closed, the economic anchors |
| **[docs/PHASE2_STATUS.md](docs/PHASE2_STATUS.md)** | Review-2 requirement matrix, item by item, with what is partial and why |
| **[docs/MISSING_DATA.md](docs/MISSING_DATA.md)** | every known gap, ranked, with whether it is closable |

## Quick start

```bash
cp .env.example .env                        # then edit the placeholders
docker compose up -d                        # postgres, neo4j, qdrant
pip install -r requirements.txt
ollama serve &                              # host-native, not Docker
ollama pull qwen2.5:7b-instruct-q4_K_M
bash scripts/run_phase1.sh                  # real Ernakulam data (the default)
python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8077
```

Then open <http://127.0.0.1:8077/>.

Two caveats that look like bugs and are not. The **first request takes ~14
seconds** while the sentence-embedding model loads; warm requests are 0.2–0.4 s,
so send one throwaway request before a demo. And Ollama is installed here as a
snap whose service is **disabled**, so `ollama serve` has to be running — if it
is not, the agents report themselves unavailable and the deterministic decision
still completes.

## What it does

**Mode 1 — you have an idea.** Write it in your own words, click the affected
local bodies on the map, give a budget. The four domain agents analyse *your
text*, and a Supervisor returns a decision with its positives and negatives.

**Mode 2 — you only have a budget.** Enter it, pick the areas, and press
*Propose ideas for this budget*. No prompt at all: the system measures what
those areas are short of, splits the budget across the four domains by that
measured need, and each agent proposes interventions inside its own envelope.
The Supervisor assembles a costed portfolio and names the biggest need it could
not fund.

**Implementation ledger.** Any proposal can be committed and then tracked
(`planned → in_progress → completed`, plus `on_hold` and `cancelled`), with
every status change kept as an event. Committed work is excluded from later
proposals, so a new budget does not produce the same ideas again.

**Explore first, instantly.** Three views answer from measured data alone in
under a second: what the selected areas are short of, how all 97 local bodies
rank for one objective, and the same area under two different objectives side
by side.

## Status

Phase 1 (nine stages) and Phase 2 complete, against real data.

| | |
| --- | --- |
| Automated tests | **132 passing, 0 skipped** |
| Parameters | 25 — **16 measured, 9 proxy, 0 unavailable** |
| Cross-store drift (PostGIS ↔ Neo4j) | **0** |
| Scored defect detection | **20 / 20** (see the note below) |
| Local bodies | 97, `dataset_edition = ernakulam` |
| API | 24 endpoints |

The defect score needs both halves of the pipeline to have run: 19 of the 20
classes are caught by Stage 2 from a `--source-dir` pass, and D20 is an
acquisition failure that only appears once `ingest.py --url` has attempted the
deliberately unreachable probe and logged it. Scoring a `--source-dir` pass
alone correctly reports 19/20.

### Known gaps

Documented in full in [docs/MISSING_DATA.md](docs/MISSING_DATA.md). The ones
worth knowing up front:

- **The audit-trail tables are empty.** `persist_run()` fails on a numpy type
  before writing, caught so the JSON report is still produced. Runs are on disk;
  the six database audit tables are not populated.
- **The RAG corpus is three short documents.** Citations are real but thin —
  the Kerala Economic Review and Budget PDFs have no stable download URLs.
- **Budget-only ideation allocates 20–28 % of the budget** on the local 7B
  model. The under-allocation is recomputed server-side and reported rather
  than hidden; a frontier model via `ANTHROPIC_API_KEY` is an `.env` change.
- **26 of 97 local bodies have no Census 2011 population**, because their
  boundaries post-date the census. The economic allocator substitutes the
  district median and flags every affected row.

## Data

The repository ships the synthetic `demo` and `stress_test` fixtures and the
derived feature layers. The real source data (KSDMA flood rasters, GSI
landslide zones, Census 2011, Kochi GTFS, OSM, KMRL) is **not redistributed** —
rebuild it with:

```bash
python scripts/build_ernakulam_dataset.py
python src/ingestion/ingest.py --url gis --include-large   # 532 MB OSM extract
python -m src.features.build_all
```

The `demo` and `stress_test` editions exist to exercise the processing stage.
Leaving one loaded makes every twin query return a fictional answer — and answer
it *successfully*, so nothing looks wrong. `run_phase1.sh` therefore defaults to
real data and its `all` mode ends on real data.

## License

MIT — see [LICENSE](LICENSE).
