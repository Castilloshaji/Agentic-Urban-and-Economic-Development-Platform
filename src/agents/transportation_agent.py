"""Transportation Agent — mobility, congestion and accessibility.

Section 3: inputs are metro GTFS, bus GTFS, the road network and population near
transit; it returns an accessibility impact, a congestion estimate and a
recommendation.
"""

from __future__ import annotations

from ..digital_twin.twin import (get_context, get_derived_features,
                                 get_transit_access)
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "accessibility_impact": "how the scenario changes access to transit for residents",
    "congestion_estimate": "expected effect on congestion, with reasoning",
    "recommendation": "one sentence recommendation",
    "affected_stops": "array of stop or station ids this touches",
    "confidence": "one of: low, medium, high",
    "risks": "array of risks, each naming the data vintage it rests on",
    "assumptions": "array of assumptions made where data was missing",
}

GUIDANCE = """If any bus stop has feed_is_stale true, its service pattern is not
verified as current — say so rather than assuming buses still run there."""


RELEVANT_DERIVED = ("nearest_metro_m", "nearest_metro_station", "metro_stations_in_unit", "road_density_km_per_km2",)
def _derived_facts(admin_id: str) -> list[dict]:
    """The derived-layer values this domain should see, as twin facts.

    Pulled from `admin_derived_feature`, which the Phase-1 tables do not carry.
    Estimates keep their `is_estimate` flag and their `match_confidence` is
    forced to `unmatched-estimate`, because an allocation of a district total is
    not a measurement of this unit and must never read like one.
    """
    derived = get_derived_features(admin_id)
    if not derived.get("found"):
        return [{"label": "derived_features", "value": None,
                 "note": derived.get("note")}]

    collected = []
    for label in RELEVANT_DERIVED:
        for block, estimated in (("measured", False), ("estimated", True)):
            if label not in derived[block]:
                continue
            tag = derived[block][label]
            fact = {"label": f"derived.{label}", "value": tag["value"],
                    "source": tag.get("source"), "data_year": tag.get("data_year")}
            if estimated:
                fact["match_confidence"] = "unmatched-estimate"
                fact["note"] = ("allocated from a published district figure, not "
                                "measured here; " + derived["caveat"])
                if derived.get("population_imputed"):
                    fact["note"] += (" This unit's population was imputed, so its "
                                     "share is weaker than the others.")
            collected.append(fact)
    return collected


def gather(admin_id: str) -> dict:
    return {"transit": get_transit_access(admin_id), "context": get_context(admin_id)}


def facts(domain_data: dict) -> list[dict]:
    transit, context = domain_data["transit"], domain_data["context"]
    if not transit.get("found"):
        return [{"label": "transit_access", "value": None, "note": transit.get("note")}]

    collected = [
        flatten_fact("metro_station_count", transit.get("metro_station_count")),
        flatten_fact("bus_stop_count", transit.get("bus_stop_count")),
        flatten_fact("stale_feed_count", transit.get("stale_feed_count")),
    ]
    for station in transit.get("metro_stations", []):
        collected.append(flatten_fact(f"{station['station_id']}.name", station["name"],
                                      station_id=station["station_id"]))
    for stop in transit.get("bus_stops", []):
        collected.append(flatten_fact(f"{stop['stop_id']}.feed_end_date", stop["feed_end_date"],
                                      stop_id=stop["stop_id"]))
        collected.append(flatten_fact(f"{stop['stop_id']}.feed_is_stale", stop["feed_is_stale"],
                                      stop_id=stop["stop_id"]))
    if context.get("found") and context.get("population"):
        collected.append(flatten_fact("population", context["population"]["population"]))
        collected.append(flatten_fact("area_m2", context["boundary"]["area_m2"]))
    collected.extend(_derived_facts(domain_data["context"]["admin_id"]))
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="transportation",
    role="Transportation Agent",
    domain_inputs="metro GTFS, bus GTFS, road network, population near transit",
    core_question="how does this affect mobility, congestion and accessibility?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: f"transit bus route metro accessibility connectivity: {scenario}",
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
