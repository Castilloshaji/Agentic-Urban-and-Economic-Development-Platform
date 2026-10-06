"""Infrastructure Agent — spatial and physical feasibility.

Section 3: inputs are admin boundaries, roads, buildings and land use; it returns
a feasibility verdict, the affected roads/parcels, an estimated cost and risks.
"""

from __future__ import annotations

from ..digital_twin.twin import _postgres, get_context, get_derived_features
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "feasibility_verdict": "one of: feasible, feasible with conditions, not feasible",
    "affected_roads_parcels": "array of road ids or parcels this scenario touches",
    "estimated_cost": "cost estimate with its unit, or null if the data cannot support one",
    "recommendation": "one sentence recommendation",
    "confidence": "one of: low, medium, high",
    "risks": "array of risks, each naming the data vintage it rests on",
    "assumptions": "array of assumptions made where data was missing",
}

GUIDANCE = """Roads flagged as near-duplicates across sources may be the same
physical road counted twice — do not treat the count as a capacity figure
without saying so."""


def _roads_intersecting(admin_id: str, edition: str) -> list[dict]:
    """Roads whose geometry intersects this admin unit, straight from PostGIS."""
    with _postgres() as cursor:
        cursor.execute(
            """
            SELECT r.road_id, r.name, r.road_type, r.source, r.data_year, r.revision_status,
                   r.match_confidence, r.dataset_edition,
                   ST_Length(ST_Intersection(r.geom, a.geom)::geography) AS length_in_admin_m
              FROM road r
              JOIN admin_boundary a
                ON a.admin_id = %s AND a.dataset_edition = %s
             WHERE ST_Intersects(r.geom, a.geom) AND r.dataset_edition = a.dataset_edition
             ORDER BY length_in_admin_m DESC
            """,
            (admin_id, edition),
        )
        return [dict(row) for row in cursor.fetchall()]


RELEVANT_DERIVED = ("road_density_km_per_km2", "max_road_class", "row_narrow_share", "row_arterial_km_per_km2",)


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
    context = get_context(admin_id)
    edition = context.get("dataset_edition")
    roads = _roads_intersecting(admin_id, edition) if context.get("found") else []
    return {"context": context, "roads": roads}


def facts(domain_data: dict) -> list[dict]:
    context, roads = domain_data["context"], domain_data["roads"]
    if not context.get("found"):
        return [{"label": "context", "value": None, "note": context.get("note")}]

    collected = [
        flatten_fact("area_m2", context["boundary"]["area_m2"]),
        flatten_fact("level", context["boundary"]["level"]),
        {"label": "road_count", "value": len(roads), "note": "roads intersecting this admin unit"},
        {"label": "child_units", "value": [c["id"] for c in context["connected_infrastructure"]["children"]]},
    ]
    if context.get("population"):
        collected.append(flatten_fact("population", context["population"]["population"]))
        collected.append(flatten_fact("households", context["population"]["households"]))
    for road in roads:
        collected.append({
            "label": f"{road['road_id']}.length_in_admin_m",
            "value": round(road["length_in_admin_m"], 1) if road["length_in_admin_m"] else None,
            "road_type": road["road_type"],
            "data_year": road["data_year"],
            "revision_status": road["revision_status"],
            "match_confidence": road["match_confidence"],
            "source": road["source"],
        })
    for zone in context["flood_risk"].get("flood_zones", []):
        collected.append(flatten_fact(f"{zone['flood_zone_id']}.overlap_ratio",
                                      zone["overlap_ratio_of_admin"],
                                      note="physical constraint on siting"))
    collected.extend(_derived_facts(domain_data["context"]["admin_id"]))
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="infrastructure",
    role="Infrastructure Agent",
    domain_inputs="admin boundaries, roads, buildings, land use",
    core_question="is this spatially and physically feasible given existing infrastructure?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: f"road construction infrastructure land use feasibility: {scenario}",
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
