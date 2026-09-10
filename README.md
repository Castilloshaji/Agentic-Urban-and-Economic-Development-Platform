# Ernakulam Digital Twin

A multi-agent urban development platform for Ernakulam district: PostGIS +
Neo4j + Qdrant behind a unified digital-twin query layer, with LangGraph agents
that reason over it and produce cited scenario reports.

Everything runs locally — datastores in Docker, the LLM natively on the host.

**Full documentation: [PROJECT_GUIDE.md](PROJECT_GUIDE.md)** — architecture,
setup, module reference, datasets, and the demo walkthrough.
Data provenance and vintage warnings: [data_dictionary.md](data_dictionary.md).

## Quick start

```bash
cp .env.example .env
docker compose up -d
pip install -r requirements.txt
ollama pull qwen2.5:7b-instruct-q4_K_M     # host-native, not Docker
bash scripts/run_phase1.sh demo
```
