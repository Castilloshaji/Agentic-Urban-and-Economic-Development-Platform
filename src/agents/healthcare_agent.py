"""Healthcare Agent — access to care, and what a proposal does to it.

Inputs are the OSM facility layer (where hospitals, clinics, doctors and
pharmacies actually are) and the local population. It answers "can people here
reach care, and does this proposal help or strain that".

The honest limit, which the agent is told to state rather than paper over: OSM
maps government and private facilities together and its coverage is uneven, so
a low count can mean a thin service or a thinly mapped place. Distance from the
centroid to the nearest facility is far less sensitive to mapping effort than a
count is, so it carries most of the weight here.
"""

from __future__ import annotations

from ..digital_twin.twin import get_context, get_derived_features
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "access_assessment": "one or two sentences on whether people here can reach care",
    "service_gap": "the specific gap this area has, or null if there is no measured gap",
    "impact_of_proposal": "what the proposal does to healthcare access and load",
    "population_at_risk": "who is most affected, in plain terms",
    "recommendation": "one sentence: what should happen from a healthcare view",
    "confidence": "one of: low, medium, high",
    "risks": "array of risks, each naming the data vintage it rests on",
    "data_gaps": "array of things that could not be assessed from the data",
    "assumptions": "array of assumptions made where data was missing",
}

GUIDANCE = """Facility locations come from OpenStreetMap, which maps government
and private providers together and is mapped unevenly across the district. A low
facility count may mean a thin service or a thinly mapped place, and you must
not present it as a census of provision. Distance to the nearest facility is the
more reliable signal. Kerala has unusually high health coverage by Indian
standards, so do not infer crisis from a single sparse reading. There is no
bed-count, staffing or morbidity data at local-body level. Say so under
data_gaps rather than estimating any of it."""

RELEVANT_DERIVED = ()


def _derived_facts(admin_id: str) -> list[dict]:
    """Nothing in admin_derived_feature is healthcare-specific yet.

    Kept so every agent exposes the same shape, and so the healthcare layer can
    be added to the derived table later without touching this module.
    """
    return []


def gather(admin_id: str) -> dict:
    return {"context": get_context(admin_id), "derived": get_derived_features(admin_id)}


def facts(domain_data: dict) -> list[dict]:
    from ..decision.parameters import _derive, load_features, parameters_for

    context = domain_data["context"]
    collected: list[dict] = []

    if context.get("found") and context.get("population"):
        collected.append(flatten_fact("population", context["population"]["population"]))
        collected.append(flatten_fact("admin_area_m2", context["boundary"]["area_m2"]))

    # The healthcare parameters live in the decision engine's feature layers
    # rather than in PostGIS, so they are read from there directly.
    try:
        frame = _derive(load_features())
        for prm in parameters_for(domain_data["context"]["admin_id"], frame):
            if prm.domain != "healthcare" or prm.value is None:
                continue
            collected.append({
                "label": prm.name, "value": prm.value, "source": prm.source,
                "data_year": prm.data_year, "status": prm.status,
                "note": prm.note,
                **({"match_confidence": "unmatched-estimate"}
                   if prm.status == "proxy" else {}),
            })
    except Exception as error:
        collected.append({"label": "healthcare_parameters", "value": None,
                          "note": f"unavailable: {type(error).__name__}: {error}"})

    collected.append({
        "label": "coverage_caveat",
        "value": "OpenStreetMap facility coverage is uneven and mixes government "
                 "with private provision",
    })
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="healthcare",
    role="Healthcare Agent",
    domain_inputs="health facility locations, population, travel distance to care",
    core_question="can people here reach care, and what does this proposal do to that?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: f"health facility access primary health centre hospital: {scenario}",
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
