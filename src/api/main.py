"""FastAPI surface over the digital twin and the decision engine.

    uvicorn src.api.main:app --reload --port 8000

The deterministic endpoints (parameters, priorities, constraints, scenario runs)
need no LLM and no Neo4j, so the dashboard stays usable even when Ollama is down
or the graph is mid-reload. Endpoints that do need them say so in their error.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..decision import constraints as constraint_engine
from ..decision import runner
from ..decision.parameters import _derive, load_features, parameters_for
from ..decision.priority import compute
from ..decision.scenarios import CATALOGUE

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = PROJECT_ROOT / "frontend"
SOURCES = PROJECT_ROOT / "data" / "sources"

app = FastAPI(title="Ernakulam Agentic Urban Platform",
              version="2.0",
              description="Digital twin + parameter-driven decision engine for "
                          "Ernakulam district.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

_FRAME = None


def clean(value):
    """NaN and numpy scalars are not JSON — convert before they reach a response.

    pandas hands back float('nan') for a missing cell and numpy types for the
    rest; both make json.dumps raise or emit invalid JSON that a browser rejects.
    """
    import math
    import numpy as np

    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, str):
        return value
    # Nested containers matter as much as scalars: a single numpy int buried in
    # an agent envelope fails the whole response, so recurse rather than trust
    # that the leaves were converted upstream.
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [clean(v) for v in value.tolist()]
    try:
        if value != value:          # NaN of any flavour
            return None
    except Exception:
        pass
    return value


def frame():
    """Feature table, loaded once. 97 rows — cheap to hold, slow to rebuild."""
    global _FRAME
    if _FRAME is None:
        _FRAME = _derive(load_features())
    return _FRAME


@app.get("/api/health")
def health():
    f = frame()
    return {"status": "ok", "units": len(f), "scenarios": len(CATALOGUE)}


@app.get("/api/admin")
def list_admin(q: str | None = None, limit: int = 200):
    f = frame()
    rows = f[["admin_id", "name", "local_auth", "area_km2", "population",
              "flood_hist_50", "nearest_stop_m", "stop_count",
              "landslide_susceptibility"]].copy()
    if q:
        rows = rows[rows.name.str.contains(q, case=False, na=False)]
    return json.loads(rows.head(limit).to_json(orient="records"))


@app.get("/api/admin/{admin_id}/context")
def admin_context(admin_id: str):
    f = frame()
    row = f[f.admin_id == admin_id]
    if row.empty:
        raise HTTPException(404, f"unknown admin_id {admin_id}")
    r = row.iloc[0]
    num = clean
    return {
        "admin_id": admin_id, "name": clean(r["name"]), "local_body_type": r.get("local_auth"),
        "area_km2": num(r.area_km2),
        "population": {"value": num(r.get("population")), "data_year": 2011,
                       "source": "Census 2011", "source_level": 1,
                       "boundary_vintage": "pre-2025-delimitation"},
        "literacy_rate": {"value": num(r.get("literacy_rate")), "data_year": 2011,
                          "source": "Census 2011", "source_level": 1},
        "flood": {"historical_50yr_share": num(r.get("flood_hist_50")),
                  "rcp85_50yr_share": num(r.get("flood_rcp_50")),
                  "source": "KSDMA flood return probability", "source_level": 1,
                  "data_year": 2026},
        "landslide": {"susceptibility": clean(r.get("landslide_susceptibility")),
                      "source": "GSI via KSDMA", "source_level": 1, "data_year": 2025},
        "transit": {"stops": num(r.get("stop_count")),
                    "nearest_stop_m": num(r.get("nearest_stop_m")),
                    "stop_density_per_km2": num(r.get("stop_density_per_km2")),
                    "feed_months_stale": num(r.get("transit_feed_months_stale")),
                    "source": "Kochi GTFS (Jungle Bus)", "source_level": 3,
                    "data_year": 2022},
    }


@app.get("/api/scenarios")
def scenarios():
    return [s.as_dict() for s in CATALOGUE.values()]


@app.get("/api/admin/{admin_id}/parameters")
def admin_parameters(admin_id: str):
    try:
        return clean([p.as_dict() for p in parameters_for(admin_id, frame())])
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/scenario/{scenario_key}/priorities")
def priorities(scenario_key: str, admin_id: str = Query(...)):
    try:
        return clean(compute(admin_id, scenario_key, frame()))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/scenario/{scenario_key}/constraints")
def scenario_constraints(scenario_key: str, admin_id: str = Query(...)):
    try:
        return clean(constraint_engine.evaluate(admin_id, scenario_key, frame()))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/scenario/{scenario_key}/conflicts")
def scenario_conflicts(scenario_key: str, admin_id: str = Query(...)):
    from ..decision.conflicts import detect
    try:
        c = constraint_engine.evaluate(admin_id, scenario_key, frame())
        w = compute(admin_id, scenario_key, frame())["weights"]
        return clean(detect(c, w))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.post("/api/scenario/run")
def scenario_run(payload: dict):
    admin_id = payload.get("admin_id")
    scenario_key = payload.get("scenario")
    if not admin_id or not scenario_key:
        raise HTTPException(422, "admin_id and scenario are required")
    try:
        return clean(runner.run(admin_id, scenario_key,
                                budget_inr_crore=payload.get("budget_inr_crore"),
                                frame=frame(),
                                with_agents=bool(payload.get("with_agents"))))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.post("/api/proposal")
def submit_proposal(payload: dict):
    """The main entry point: an idea, the local bodies it affects, and a budget.

    Nothing is selected from a scenario menu. The objective is inferred only to
    look up which relevance row supplies the domain weights, and that inference
    is reported under weight_basis so it stays visible rather than hidden.
    """
    from ..decision.proposal import evaluate_proposal

    admin_ids = payload.get("admin_ids") or []
    if isinstance(admin_ids, str):
        admin_ids = [admin_ids]
    budget = payload.get("budget_inr_crore")
    try:
        budget = float(budget) if budget not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(422, "budget_inr_crore must be a number")

    try:
        return clean(evaluate_proposal(payload.get("text") or "", admin_ids,
                                       budget_inr_crore=budget, frame=frame(),
                                       with_agents=payload.get("with_agents", True)))
    except ValueError as error:
        raise HTTPException(422, str(error))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/scenario/compare")
def scenario_compare(admin_id: str, a: str, b: str):
    try:
        return clean(runner.compare(admin_id, a, b))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/scenario/{scenario_key}/ranking")
def ranking(scenario_key: str, limit: int = 15):
    if scenario_key not in CATALOGUE:
        raise HTTPException(404, f"unknown scenario {scenario_key}")
    return clean(runner.rank_units(scenario_key, limit))


@app.get("/api/map/boundaries")
def boundaries():
    """Ernakulam local-body polygons, thinned for browser rendering."""
    path = SOURCES / "gis" / "lsg_kerala_boundaries.geojson"
    if not path.exists():
        raise HTTPException(503, "boundary source not fetched; run ingest.py --url gis")
    import geopandas as gpd
    gdf = gpd.read_file(path)
    gdf = gdf[gdf["District"] == "Ernakulam"].copy()
    gdf = gdf.rename(columns={"LSGI_Code": "admin_id"})
    gdf["geometry"] = gdf.geometry.simplify(0.0008, preserve_topology=True)
    f = frame().set_index("admin_id")
    for col in ("flood_hist_50", "nearest_stop_m", "stop_count", "population",
                "landslide_susceptibility", "area_km2"):
        gdf[col] = gdf.admin_id.map(f[col]) if col in f.columns else None
    keep = ["admin_id", "name", "local_auth", "flood_hist_50", "nearest_stop_m",
            "stop_count", "population", "landslide_susceptibility", "area_km2", "geometry"]
    return JSONResponse(json.loads(gdf[keep].to_json()))


@app.get("/api/sources")
def sources():
    log = SOURCES / "acquisition_log.json"
    if not log.exists():
        return {"attempts": [], "note": "no acquisition log yet"}
    data = json.loads(log.read_text(encoding="utf-8"))
    return {"summary": data.get("summary"),
            "fetch_failures": data.get("fetch_failures"),
            "unavailable_by_design": data.get("unavailable_by_design")}


@app.post("/api/ideate")
def ideate(payload: dict):
    """Budget in, proposals out — no idea text, no scenario, no prompt.

    Needs are measured from the parameters, the budget is split across domains
    by that measured pressure, and only then do the four agents propose. The
    Supervisor assembles a portfolio that fits. Everything the model could
    influence is downstream of the arithmetic.
    """
    from ..decision.ideate import generate

    admin_ids = payload.get("admin_ids") or []
    if isinstance(admin_ids, str):
        admin_ids = [admin_ids]
    try:
        return clean(generate(admin_ids, payload.get("budget_inr_crore"),
                              frame=frame()))
    except ValueError as error:
        raise HTTPException(422, str(error))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.post("/api/implementations")
def create_implementation(payload: dict):
    """Commit a proposed idea to the implementation ledger.

    Takes an idea in the shape the ideation agents produce, so the website can
    hand a proposal straight through. Once logged, later ideation runs will not
    propose it again.
    """
    from ..decision.implementations import create

    idea = payload.get("idea") or payload
    admin_ids = payload.get("admin_ids") or idea.get("areas") or []
    if isinstance(admin_ids, str):
        admin_ids = [admin_ids]
    try:
        return clean(create(idea, admin_ids,
                            origin=payload.get("origin", "manual"),
                            origin_id=payload.get("origin_id"),
                            status=payload.get("status", "planned")))
    except ValueError as error:
        raise HTTPException(422, str(error))


@app.get("/api/implementations")
def list_implementations(admin_id: list[str] | None = Query(None),
                         status: str | None = None, limit: int = 200):
    """The ledger: what is planned, under way, done, held or cancelled."""
    from ..decision.implementations import listing

    try:
        return clean(listing(admin_ids=admin_id, status=status, limit=limit))
    except ValueError as error:
        raise HTTPException(422, str(error))


@app.get("/api/implementations/stats")
def implementation_stats():
    """Ledger-wide counts and committed spend."""
    from ..decision.implementations import stats

    return clean(stats())


@app.get("/api/implementations/{implementation_id}")
def implementation_history(implementation_id: int):
    """One implementation with every status change it has been through."""
    from ..decision.implementations import history

    try:
        return clean(history(implementation_id))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.patch("/api/implementations/{implementation_id}")
def update_implementation(implementation_id: int, payload: dict):
    """Move an implementation along. Illegal transitions are refused."""
    from ..decision.implementations import set_status

    status = payload.get("status")
    if not status:
        raise HTTPException(422, "status is required")
    try:
        return clean(set_status(implementation_id, status, payload.get("note")))
    except KeyError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(409, str(error))


@app.get("/api/needs")
def needs(admin_id: str = Query(..., description="repeatable")):
    """The measured shortfalls for one area, worst first. Deterministic."""
    from ..decision.needs import assess

    try:
        return clean(assess([admin_id], frame()))
    except KeyError as error:
        raise HTTPException(404, str(error))


@app.get("/api/admin/{admin_id}/derived")
def admin_derived(admin_id: str):
    """The derived feature layers for one unit: measurements and estimates, split."""
    from ..digital_twin.twin import get_derived_features

    out = clean(get_derived_features(admin_id))
    if not out.get("found") and "No boundary" in (out.get("note") or ""):
        raise HTTPException(404, out["note"])
    return out


@app.get("/api/anchors")
def anchors():
    """The published district figures the economic estimates are allocated from.

    Exposed so a reader can check the number an estimate rests on without
    reading the code that spends it. Every entry carries its publisher, its
    as-on date and its URL.
    """
    from ..features.anchors import table

    return {"anchors": clean(table()),
            "note": "district-level publications; the local-body figures derived "
                    "from them are tagged 'proxy', never 'real'"}


@app.get("/api/parameters/status")
def parameter_status():
    """How many parameters are measured, estimated, or still missing."""
    from ..decision.parameters import summary

    return clean(summary())


if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
