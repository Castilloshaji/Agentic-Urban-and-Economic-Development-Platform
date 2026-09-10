"""Environment Agent — flood and water risk for a scenario.

Section 3: inputs are flood hazard maps, water bodies and land use; it answers
"what's the environmental/flood risk?" and returns a risk score, the affected
flood zones and mitigation notes.
"""

from __future__ import annotations

from ..digital_twin.twin import get_context, get_flood_risk
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "risk_score": "number 0-1, where 1 is maximum flood risk",
    "risk_band": "one of: low, moderate, high, severe",
    "affected_flood_zones": "array of {flood_zone_id, risk_level, data_year, overlap_ratio}",
    "mitigation_notes": "array of concrete mitigation measures",
    "recommendation": "one sentence: proceed, proceed with conditions, or do not proceed",
    "confidence": "one of: low, medium, high",
    "risks": "array of risks, each naming the data vintage it rests on",
    "assumptions": "array of assumptions made where data was missing",
}

GUIDANCE = """The flood layer is a static historical hazard classification, not a
forecast. State its data_year explicitly in your risks and note that a zone
classified years ago may not reflect current conditions."""


def gather(admin_id: str) -> dict:
    return {"flood_risk": get_flood_risk(admin_id), "context": get_context(admin_id)}


def facts(domain_data: dict) -> list[dict]:
    flood, context = domain_data["flood_risk"], domain_data["context"]
    collected = []
    if not flood.get("found"):
        return [{"label": "flood_risk", "value": None, "note": flood.get("note")}]

    collected.append(flatten_fact("max_overlap_ratio", flood.get("max_overlap_ratio")))
    for zone in flood.get("flood_zones", []):
        zone_id = zone["flood_zone_id"]
        collected.append(flatten_fact(f"{zone_id}.risk_level", zone["risk_level"], flood_zone_id=zone_id))
        collected.append(flatten_fact(f"{zone_id}.overlap_area_m2", zone["overlap_area_m2"], flood_zone_id=zone_id))
        collected.append(flatten_fact(f"{zone_id}.overlap_ratio", zone["overlap_ratio_of_admin"], flood_zone_id=zone_id))
    if context.get("found"):
        collected.append(flatten_fact("admin_area_m2", context["boundary"]["area_m2"]))
        if context.get("population"):
            collected.append(flatten_fact("population", context["population"]["population"]))
    collected.append({"label": "flood_layer_caveat", "value": flood.get("caveat")})
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="environment",
    role="Environment Agent",
    domain_inputs="flood hazard zones, water bodies, land use",
    core_question="what is the environmental and flood risk of this policy?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: f"flood hazard risk mitigation environmental impact: {scenario}",
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
