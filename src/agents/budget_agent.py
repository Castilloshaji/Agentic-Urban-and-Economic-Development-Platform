"""Budget Agent — can this be paid for, out of whose money, and over how long.

The other six agents argue about what a place needs. This one argues about what
it can afford, which is a different question and usually the binding one.

Its spine is not invented. Kerala's State Finance Commission distributes the
basic grant to local governments on a published rule: **80% population, 10%
area, 10% inverse of own income**. The engine computes each local body's share
under that rule in `parameters.py`, so `fiscal_entitlement` is a real
entitlement under a real formula rather than a number this agent made up. One
term is substituted, because own income is not published per local body, and
the parameter says so wherever it appears.

The agent's job is to take that entitlement, the cost of building on this
particular ground, and the proposal in front of it, and say whether the
arithmetic closes. It is explicitly told it may conclude that it does not.
"""

from __future__ import annotations

from ..digital_twin.twin import get_context, get_derived_features
from .base import AgentSpec, flatten_fact, run_agent

OUTPUT_SCHEMA = {
    "affordability": "one of: comfortable, tight, unaffordable, cannot assess",
    "assessment": "two or three sentences on whether the money works",
    "annual_entitlement_share": "this unit's share of a devolved grant, as given in the facts",
    "funding_route": "where the money would realistically come from: own funds, "
                     "devolved plan grant, state scheme, centrally sponsored scheme, "
                     "borrowing, or a stated mix",
    "phasing": "array of {phase, what, share_of_cost} if the spend should be split "
               "across years, otherwise an empty array",
    "cost_pressures": "array of things about this ground that make delivery dearer, "
                      "each tied to a measured reading",
    "opportunity_cost": "what this local body plausibly gives up by funding this",
    "recommendation": "one sentence: fund it, phase it, seek external funding, or do not",
    "confidence": "one of: low, medium, high",
    "risks": "array of fiscal risks, each naming the data vintage it rests on",
    "data_gaps": "array of things that could not be assessed from the data",
}

GUIDANCE = """Your fiscal_entitlement fact is this local body's share under the
Kerala State Finance Commission's published basic-grant rule: 80% population,
10% area, 10% inverse of own income. It is a share, not a rupee figure, because
the size of the pool is set each year in the state budget and is not in your
inputs. Do not convert it into rupees unless a total pool is given to you.

What you do not have, and must list under data_gaps rather than estimate: this
local body's actual budget, its own revenue, its existing committed spend, any
scheme it already draws on, and unit costs for construction in Kerala. You may
reason about relative affordability and about phasing. You may not invent a
rupee figure for anything.

The cost_exposure reading is a PERCENTILE RANK from 0 to 1 across the 97 local
bodies. It is not a percentage and not a price. A reading of 0.90 means this
ground is dearer to build on than 90% of the district. It does NOT mean costs
are 90% above average, and writing that would be a factual error. The same
applies to fiscal_entitlement, which is a share of a whole, and to any other
0-to-1 reading you are given: state it as a rank or a share, never convert it
into a percentage premium.

Concluding that something is unaffordable is a legitimate and useful answer."""

RELEVANT_DERIVED = ("income_per_capita_estimate", "msme_estimated_count")


def _derived_facts(admin_id: str) -> list[dict]:
    """Fiscal capacity signals from the derived layer, marked as estimates."""
    derived = get_derived_features(admin_id)
    if not derived.get("found"):
        return [{"label": "derived_features", "value": None,
                 "note": derived.get("note")}]
    collected = []
    for label in RELEVANT_DERIVED:
        tag = derived["estimated"].get(label) or derived["measured"].get(label)
        if not tag:
            continue
        fact = {"label": f"derived.{label}", "value": tag["value"],
                "source": tag.get("source"), "data_year": tag.get("data_year")}
        if label in derived["estimated"]:
            fact["match_confidence"] = "unmatched-estimate"
            fact["note"] = ("allocated from a published district figure, not "
                            "measured here")
        collected.append(fact)
    return collected


def gather(admin_id: str) -> dict:
    return {"context": get_context(admin_id)}


def facts(domain_data: dict) -> list[dict]:
    from ..decision.parameters import _derive, load_features, parameters_for

    context = domain_data["context"]
    admin_id = context["admin_id"]
    collected: list[dict] = []

    if context.get("found") and context.get("population"):
        collected.append(flatten_fact("population", context["population"]["population"]))
        collected.append(flatten_fact("admin_area_m2", context["boundary"]["area_m2"]))

    try:
        frame = _derive(load_features())
        for prm in parameters_for(admin_id, frame):
            if prm.domain != "budget" or prm.value is None:
                continue
            collected.append({
                "label": prm.name, "value": prm.value, "source": prm.source,
                "data_year": prm.data_year, "status": prm.status, "note": prm.note,
                **({"match_confidence": "unmatched-estimate"}
                   if prm.status == "proxy" else {}),
            })
    except Exception as error:
        collected.append({"label": "budget_parameters", "value": None,
                          "note": f"unavailable: {type(error).__name__}: {error}"})

    collected.extend(_derived_facts(admin_id))

    # The rule itself travels with the numbers, so the agent can cite it and a
    # reader can check it.
    from ..features.anchors import DEVOLUTION_FORMULA, DEVOLVED_PLAN_SHARE
    collected.append({
        "label": "devolution_rule", "value": DEVOLUTION_FORMULA.note,
        "source": DEVOLUTION_FORMULA.publisher, "data_year": DEVOLUTION_FORMULA.data_year,
    })
    collected.append({
        "label": "state_devolved_plan_share",
        "value": f"{DEVOLVED_PLAN_SHARE.value}% of the State Plan outlay goes to "
                 f"local governments",
        "source": DEVOLVED_PLAN_SHARE.publisher,
        "data_year": DEVOLVED_PLAN_SHARE.data_year,
    })
    return [f for f in collected if f]


SPEC = AgentSpec(
    name="budget",
    role="Budget Allocation Agent",
    domain_inputs="devolution entitlement, local fiscal capacity, cost of building here",
    core_question="can this be paid for, out of whose money, and over how long?",
    output_schema=OUTPUT_SCHEMA,
    gather=gather,
    facts=facts,
    rag_query=lambda scenario: (
        f"local government budget plan fund allocation devolution finance: {scenario}"),
    guidance=GUIDANCE,
)


def run(scenario: str, admin_id: str, backend=None) -> dict:
    return run_agent(SPEC, scenario, admin_id, backend)
