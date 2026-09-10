#!/usr/bin/env bash
# Full Phase 1 verification: both datasets, every stage, in the guide's order.
#
#   bash scripts/run_phase1.sh            # demo pass, then stress pass
#   bash scripts/run_phase1.sh demo       # one pass only
#   bash scripts/run_phase1.sh stress
#
# Exits non-zero on the first failure. Stage markers are grep-friendly (">>>").
set -euo pipefail
cd "$(dirname "$0")/.."

# Resolve the interpreter explicitly: this script is often launched from a
# non-login shell (nohup, cron, CI) where the project's env is not on PATH.
PY="${PYTHON:-$(command -v python || command -v python3)}"
if [ -z "$PY" ]; then echo "no python found; set PYTHON=/path/to/python" >&2; exit 127; fi
# A bare python3 on PATH is often the system interpreter, which lacks this
# project's dependencies. Fail here with a usable message rather than three
# stages later with an ImportError.
if ! "$PY" -c "import psycopg2, neo4j, qdrant_client" 2>/dev/null; then
  echo "ERROR: $PY lacks this project's dependencies." >&2
  echo "Activate your environment, or: PYTHON=/path/to/python bash $0 ${1:-all}" >&2
  exit 127
fi
echo "interpreter: $PY ($("$PY" -V 2>&1))"

DEMO_DIR=data/demo/demo_data
STRESS_DIR=data/stress_test/ernakulam_phase1_stress_test_dataset/stress_test_data
SCENARIO="Extend a feeder bus route to connect this underserved panchayat to the nearest Kochi Metro station, where part of the route corridor overlaps a KSDMA-flagged flood-hazard zone."

pass () {
  local label="$1" src="$2"
  echo ">>> [$label] Step 1 — ingest"
  "$PY" src/ingestion/ingest.py --reset --source-dir "$src" | tail -1

  echo ">>> [$label] Step 2 — processing"
  "$PY" -m src.processing.run | tail -6

  echo ">>> [$label] Step 3 — PostGIS"
  "$PY" -m src.storage.postgres.load --dry-run | sed -n '/^table/,/^TOTAL/p'
  docker exec -i edt-postgres psql -U postgres -d ernakulam -q < src/storage/postgres/schema.sql 2>/dev/null
  "$PY" -m src.storage.postgres.load | tail -1

  echo ">>> [$label] Step 4 — Neo4j"
  "$PY" -m src.storage.neo4j.load --apply-schema --reset | tail -1
  "$PY" -m src.storage.neo4j.verify | tail -1

  echo ">>> [$label] Step 6 — Qdrant"
  "$PY" -m src.rag.ingest --recreate 2>/dev/null | grep Upserted

  echo ">>> [$label] Steps 5+7 — test suite"
  "$PY" -m pytest tests/ -q 2>&1 | tail -2

  echo ">>> [$label] Steps 8+9 — scenario report"
  "$PY" src/scenarios/run_scenario.py --admin-id DEMO-P-03 --scenario "$SCENARIO" 2>/dev/null | tail -2
  echo ">>> [$label] DONE"
}

case "${1:-all}" in
  demo)   pass demo "$DEMO_DIR" ;;
  stress) pass stress "$STRESS_DIR" ;;
  all)    pass demo "$DEMO_DIR"; pass stress "$STRESS_DIR" ;;
  *) echo "usage: $0 [demo|stress|all]" >&2; exit 2 ;;
esac
echo ">>> ALL PASSES COMPLETE"
