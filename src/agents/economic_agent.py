"""Economic Development Agent — economic upside and cost.

Section 3: inputs are GDDP/NDDP, per-capita income and sector shares; it returns
a recommendation, an estimated cost, an income/employment impact, a confidence
and risks.
"""

from __future__ import annotations

from ..digital_twin.twin import get_context, get_economic_profile
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "recommendation": "one sentence recommendation",
    "estimated_cost": "cost estimate with its unit, or null if the data cannot support one",
    "income_employment_impact": "expected effect on income and employment",
    "confidence": "one of: low, medium, high",
    "risks": "array of risks, each naming the data vintage it rests on",
    "data_gaps": "array of years or indicators that were missing from the series",
    "assumptions": "array of assumptions made where data was missing",
}

GUIDANCE = """Some years are absent from the published series — they are listed
under missing_years. Do not interpolate across them or treat a gap as a zero;
report the affected years in data_gaps instead. Figures marked
match_confidence "unmatched-estimate" were inherited from a parent admin unit
and are not measurements of this one."""


def gather(admin_id: str) -> dict:
    return {"economy": get_economic_profile(admin_id), "context": get_context(admin_id)}


def facts(domain_data: dict) -> list[dict]:
    economy, context = domain_data["economy"], domain_data["context"]
    if not economy.get("found"):
        return [{"label": "economic_profile", "value": None, "note": economy.get("note")}]

    collected: list[dict] = []
    if economy.get("inherited_from"):
        collected.append({
            "label": "figures_inherited_from",
            "value": economy["inherited_from"]["admin_id"],
            "match_confidence": "unmatched-estimate",
            "note": economy.get("note"),
        })

    for indicator, series in economy.get("indicators", {}).items():
        collected.append({
            "label": f"{indicator}.years_covered",
            "value": series.get("years_covered"),
            "note": "years with a published observation",
        })
        collected.append({
            "label": f"{indicator}.missing_years",
            "value": series.get("missing_years"),
            "note": "genuine gaps in the published series — do not interpolate",
        })
        if series.get("latest_final"):
            collected.append(flatten_fact(f"{indicator}.latest_final", series["latest_final"],
                                          year=series["latest_final"].get("year"),
                                          unit=series.get("unit")))
        for observation in series.get("observations", []):
            collected.append(flatten_fact(
                f"{indicator}.{observation.get('year')}", observation,
                year=observation.get("year"), unit=series.get("unit")))
        if series.get("has_revision_conflict"):
            collected.append({
                "label": f"{indicator}.revision_conflict",
                "value": True,
                "note": "published twice with different revision_status; 'final' supersedes "
                        "'provisional' but both are retained",
            })

    if context.get("found") and context.get("population"):
        collected.append(flatten_fact("population", context["population"]["population"]))
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="economic",
    role="Economic Development Agent",
    domain_inputs="GDDP/NDDP, per-capita income, sector shares",
    core_question="what is the economic upside and cost of this policy?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: f"economic growth budget allocation income employment: {scenario}",
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
