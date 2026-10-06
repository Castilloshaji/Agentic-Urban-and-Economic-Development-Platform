"""Education Agent — reach of schooling, and what a proposal does to it.

Inputs are the OSM education layer (schools, colleges, universities, libraries)
and the local population and literacy. It answers "can people here reach
schooling, and does this proposal help or strain it".

The limit worth naming: facilities are weighted by *type*, never by enrolment,
because UDISE publishes enrolment at district level and nowhere below it. A
school counted here is a school of unknown size. Kerala's literacy is near
universal, so the interesting question is rarely whether schooling exists but
whether it is reachable and whether a proposal adds load to it.
"""

from __future__ import annotations

from ..digital_twin.twin import get_context, get_derived_features
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "access_assessment": "one or two sentences on whether people here can reach schooling",
    "service_gap": "the specific gap this area has, or null if there is no measured gap",
    "impact_of_proposal": "what the proposal does to school access and load",
    "workforce_implication": "what it means for local skills and the working-age population",
    "recommendation": "one sentence: what should happen from an education view",
    "confidence": "one of: low, medium, high",
    "risks": "array of risks, each naming the data vintage it rests on",
    "data_gaps": "array of things that could not be assessed from the data",
    "assumptions": "array of assumptions made where data was missing",
}

GUIDANCE = """Institution locations come from OpenStreetMap and are weighted by
type, never by enrolment: UDISE publishes enrolment only at district level, so a
school here is a school of unknown size. Mapping coverage is uneven, so a low
count may mean thin provision or a thinly mapped place. Kerala's literacy is
near universal and the Census literacy rate is carried in your facts, so do not
infer an literacy crisis from a sparse facility reading. There is no
teacher-count, pupil-teacher ratio or pass-rate data below district level. Say
so under data_gaps rather than estimating it."""

RELEVANT_DERIVED = ()


def _derived_facts(admin_id: str) -> list[dict]:
    """Nothing in admin_derived_feature is education-specific yet.

    Kept so every agent exposes the same shape, and so the education layer can
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
        collected.append(flatten_fact("literacy_rate", context["population"]["literacy_rate"]))
        collected.append(flatten_fact("admin_area_m2", context["boundary"]["area_m2"]))

    # The healthcare parameters live in the decision engine's feature layers
    # rather than in PostGIS, so they are read from there directly.
    try:
        frame = _derive(load_features())
        for prm in parameters_for(domain_data["context"]["admin_id"], frame):
            if prm.domain != "education" or prm.value is None:
                continue
            collected.append({
                "label": prm.name, "value": prm.value, "source": prm.source,
                "data_year": prm.data_year, "status": prm.status,
                "note": prm.note,
                **({"match_confidence": "unmatched-estimate"}
                   if prm.status == "proxy" else {}),
            })
    except Exception as error:
        collected.append({"label": "education_parameters", "value": None,
                          "note": f"unavailable: {type(error).__name__}: {error}"})

    collected.append({
        "label": "coverage_caveat",
        "value": "OpenStreetMap coverage is uneven, and institutions are weighted "
                 "by type because enrolment is not published below district level",
    })
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="education",
    role="Education Agent",
    domain_inputs="school and college locations, population, literacy, travel distance",
    core_question="can people here reach schooling, and what does this proposal do to that?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: f"school education literacy skills training: {scenario}",
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
