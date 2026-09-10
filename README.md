# Agentic Urban and Economic Development Platform

AI-powered multi-agent platform for urban and economic development. Uses AI
agents, data analytics, spatial insights and scenario simulation to analyse
infrastructure, employment, investment and development opportunities. The
prototype focuses on Ernakulam district, Kerala, supporting data-driven
planning and decision-making.

Everything runs locally — PostGIS, Neo4j and Qdrant in Docker, the LLM natively
on the host.

## Documentation

- **[PROJECT_GUIDE.md](PROJECT_GUIDE.md)** — architecture, setup, module
  reference, datasets and the demo walkthrough
- **[data_dictionary.md](data_dictionary.md)** — data provenance and the vintage
  warnings that must survive into any output

## Quick start

```bash
cp .env.example .env                        # then edit the placeholders
docker compose up -d                        # postgres, neo4j, qdrant
pip install -r requirements.txt
ollama pull qwen2.5:7b-instruct-q4_K_M      # host-native, not Docker
bash scripts/run_phase1.sh demo             # full pipeline on the demo dataset
```

## Phase 1 status

Nine stages complete and verified end to end: acquisition, validation, dual
storage with cross-store consistency checks, a unified digital-twin query layer,
RAG retrieval, four domain agents, a supervisor that resolves their conflicts,
and a scenario runner that produces a cited report.

| | |
| --- | --- |
| Automated tests | 34 passing |
| Cross-store drift | 0 |
| Scored defect detection | 19 / 20 |

The design rule throughout: **no value is returned without the vintage it was
published under.** Every figure carries `data_year`, `revision_status`,
`boundary_vintage` and a confidence level, so an agent can never present a 2010
hazard classification or a superseded ward count as current fact.

## Data

The repository ships the synthetic `demo` and `stress_test` fixtures. The real
Ernakulam dataset (OpenStreetMap LSG boundaries, Census 2011, Ecostat) is not
redistributed — rebuild it with:

```bash
python scripts/build_ernakulam_dataset.py
```

## License

MIT — see [LICENSE](LICENSE).
