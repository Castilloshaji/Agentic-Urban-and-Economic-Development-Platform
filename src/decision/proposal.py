"""Free-form proposal evaluation: an idea, the areas it affects, and a budget.

This is the path a user actually walks. They write what they want to build, tick
the local bodies it touches and state what they can spend. Nothing is chosen
from a menu.

The scenario catalogue still exists underneath, but only as a lookup table: the
classified objective selects which row of the relevance matrix supplies the
domain weights. It is never presented as the thing being evaluated, and the
agents are never shown its wording — they are shown the user's own.

Order matters and is not negotiable. Every measured quantity, every weight and
every blocking constraint is computed for every selected area *before* a model
is called. The agents analyse a proposal whose hard limits are already fixed,
and the Supervisor is handed the arithmetic as binding. That is the only way the
positives and negatives it reports can be trusted: it is explaining a decision,
not making one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .conflicts import detect
from .constraints import evaluate
from .parameters import UNAVAILABLE, _derive, load_features, parameters_for
from .priority import compute
from .runner import _suitability
from .scenarios import get

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "data" / "derived" / "proposals"

# Single source of truth: scenarios.py. A local copy drifted the moment a
# domain was added, which is exactly what happened when healthcare,
# education and budget arrived.
from .scenarios import DOMAINS  # noqa: F401

# A proposal spanning many local bodies still gets one analysis per domain, not
# one per area — seven agents times ninety-seven panchayats is both unaffordable
# on a local 7B model and the wrong unit of reasoning. The agents reason over the
# whole affected area, anchored on the unit that carries the most population.
#
# The whole district is a legitimate selection, so the ceiling is the district.
MAX_UNITS = 97

# How many units get their measured position written out in full. Beyond this
# the brief rolls the rest into a summary line. Listing all 97 produced a 95,000
# character brief, roughly 24,000 tokens, which is slower and less reliable than
# a shorter one that leads with the units that actually carry the decision.
BRIEF_DETAIL_UNITS = 12


# Parameters the agents cannot reach any other way. The agents gather their twin
# facts from PostGIS, which holds the Phase-1 tables; these come from the feature
# layers, so without putting them in the brief the engine would weigh evidence
# the analysts never see — the metro distance, the water setback, the enterprise
# base and the right-of-way situation would drive the weights invisibly.
BRIEF_PARAMETERS = ("metro_access", "water_body_proximity",
                    "right_of_way_constraint", "msme_presence",
                    "investment_potential", "road_connectivity")

# How to render each one as something an analyst can act on. A bare normalised
# score tells them nothing; "19.6 km to Aluva" does.
BRIEF_FORMAT = {
    "metro_access": lambda v: f"{v / 1000:.1f} km to the nearest metro station",
    "water_body_proximity": lambda v: f"{v:.0f} m from the nearest water body",
    "right_of_way_constraint": lambda v: f"{v:.0%} of the road network is narrow-class",
    "msme_presence": lambda v: f"~{v:,.0f} registered enterprises",
    "investment_potential": lambda v: f"~INR {v:,.0f} per-capita income",
    "road_connectivity": lambda v: f"{v:.1f} km of road per km2",
}


def _parameter_lines(parameters: list) -> list[str]:
    """Render the brief parameters, each marked measured or estimated.

    The status marker is not decoration. An agent told "~1,198 registered
    enterprises" will reason about it as a count; told "(estimated, allocated
    from a district total)" it can discount it, and several of them do.
    """
    by_name = {prm.name: prm for prm in parameters}
    lines = []
    for name in BRIEF_PARAMETERS:
        prm = by_name.get(name)
        if prm is None or prm.status == UNAVAILABLE or prm.value is None:
            continue
        rendered = BRIEF_FORMAT[name](prm.value)
        marker = ("measured" if prm.status == "real"
                  else "ESTIMATED, not measured")
        lines.append(f"    {name}: {rendered} [{marker}; {prm.source}]")
    return lines


def _unit_brief(result: dict) -> str:
    """One line per area: what was measured, and what it forbids."""
    blocks = [c["message"] for c in result["constraints"]["constraints"]
              if c["severity"] == "block"]
    conditions = [c["message"] for c in result["constraints"]["constraints"]
                  if c["severity"] == "condition"]
    parts = [f"- {result['admin_name']} ({result['local_body_type'] or 'LSG'}): "
             f"population {result['population'] or 'unknown'}, "
             f"suitability {result['suitability_score']:.2f}, "
             f"stance {result['stance']}"]
    parts.extend(_parameter_lines(result.get("parameters") or []))
    if blocks:
        parts.append("  BLOCKED: " + " ".join(blocks))
    if conditions:
        parts.append("  CONDITIONS: " + " ".join(conditions))
    return "\n".join(parts)


def _evaluate_units(admin_ids: list[str], scenario_key: str, frame) -> list[dict]:
    """The deterministic core, per selected area. No model has run yet."""
    evaluated = []
    for admin_id in admin_ids:
        row = frame[frame.admin_id == admin_id]
        if row.empty:
            raise KeyError(f"unknown admin_id {admin_id!r}")
        unit = row.iloc[0]
        priority = compute(admin_id, scenario_key, frame)
        constraints = evaluate(admin_id, scenario_key, frame)
        verdict = _suitability(priority, constraints)
        population = unit.get("population")
        evaluated.append({
            "admin_id": admin_id,
            "admin_name": unit["name"],
            "local_body_type": unit.get("local_auth"),
            "population": None if population is None or population != population
                          else int(population),
            "suitability_score": verdict["suitability_score"],
            "stance": verdict["stance"],
            "blocked": verdict["blocked"],
            "weights": priority["weights"],
            "leading_domain": priority["ranking"][0],
            "priority": priority,
            "constraints": constraints,
            "conflicts": detect(constraints, priority["weights"], None),
            "parameters": parameters_for(admin_id, frame),
        })
    return evaluated


def _blend_weights(units: list[dict]) -> dict[str, float]:
    """Population-weighted mean of the per-area weights.

    A proposal touching Kochi Corporation and a 9,000-person hill panchayat is
    not half about each. Weighting by population keeps the blended figure
    honest; areas with no published population fall back to an equal share
    rather than vanishing from the average.
    """
    sizes = [u["population"] or 0 for u in units]
    if not any(sizes):
        sizes = [1] * len(units)
    else:
        floor = min(s for s in sizes if s) / 2
        sizes = [s or floor for s in sizes]
    total = sum(sizes)
    # .get rather than [] so a weights dict that predates a new domain degrades
    # to a zero contribution instead of raising. The renormalisation below keeps
    # the result summing to 1 either way.
    blended = {d: round(sum(u["weights"].get(d, 0.0) * s
                            for u, s in zip(units, sizes)) / total, 4)
               for d in DOMAINS}
    # Renormalise away the rounding drift so the displayed shares sum to 100%.
    drift = round(1.0 - sum(blended.values()), 4)
    if drift:
        top = max(blended, key=blended.get)
        blended[top] = round(blended[top] + drift, 4)
    return blended


def _brief(text: str, units: list[dict], weights: dict[str, float],
           budget_inr_crore: float | None) -> str:
    """The proposal as the agents see it: the user's words, then the arithmetic."""
    areas = ", ".join(u["admin_name"] for u in units)
    blocked = [u for u in units if u["blocked"]]
    # Blocked units first, because they carry the binding constraints, then the
    # largest by population. A brief that leads with the units that decide the
    # answer is more useful than one that lists them alphabetically.
    ordered = sorted(units, key=lambda u: (not u["blocked"], -(u["population"] or 0)))
    detailed, rest = ordered[:BRIEF_DETAIL_UNITS], ordered[BRIEF_DETAIL_UNITS:]

    lines = [
        f"PROPOSAL (analyse this specific proposal, in the user's own words):\n{text}",
        "",
        f"AFFECTED AREAS ({len(units)}): {areas}",
        "",
        "MEASURED POSITION PER AREA (computed from Census, KSDMA flood, GSI "
        "landslide, GTFS transit and OSM road data — these figures are fixed):",
        *[_unit_brief(u) for u in detailed],
        "",
        *([f"- and {len(rest)} further area(s), summarised: combined population "
           f"{sum(u['population'] or 0 for u in rest):,}, "
           f"{sum(1 for u in rest if u['blocked'])} blocked, "
           f"suitability {min(u['suitability_score'] for u in rest):.2f} to "
           f"{max(u['suitability_score'] for u in rest):.2f}. Their measured "
           f"positions are in the record and were used to compute the weights "
           f"below, but they are not written out here because the brief would "
           f"be too long to reason over."] if rest else []),
        "",
        f"BUDGET STATED BY THE USER: "
        f"{f'INR {budget_inr_crore} crore' if budget_inr_crore else 'not stated'}",
        "",
        "DOMAIN DECISION WEIGHTS across the affected area, computed from measured "
        "data and FIXED: " + ", ".join(f"{d} {w:.0%}" for d, w in
                                       sorted(weights.items(), key=lambda kv: -kv[1]))
        + ". Do not revise or restate these weights.",
    ]
    if blocked:
        lines += [
            "",
            "BINDING CONSTRAINTS already determined from measured hazard data for "
            + ", ".join(u["admin_name"] for u in blocked)
            + ". You cannot clear these. Analyse the proposal within them.",
        ]
    lines += [
        "",
        "Assess this proposal from your domain only. Judge whether the budget is "
        "adequate for what is proposed across these areas, and name what you "
        "cannot assess from the available data rather than estimating it.",
    ]
    return "\n".join(lines)


def _run_agents(brief: str, anchor_admin_id: str) -> dict:
    """The four domain agents, each analysing the proposal from its own side."""
    from ..agents import (budget_agent, economic_agent, education_agent,
                          environment_agent, healthcare_agent,
                          infrastructure_agent, transportation_agent)
    from ..agents.llm import resolve_backend

    modules = {"economic": economic_agent, "infrastructure": infrastructure_agent,
               "transportation": transportation_agent, "environment": environment_agent,
               "healthcare": healthcare_agent, "education": education_agent,
               "budget": budget_agent}
    try:
        backend = resolve_backend()
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}

    out: dict = {"available": True,
                 "backend": {"kind": backend.kind, "model": backend.model},
                 "anchor_admin_id": anchor_admin_id}
    for name, module in modules.items():
        try:
            out[name] = module.run(brief, anchor_admin_id, backend)
        except Exception as error:
            out[name] = {"agent": name, "error": f"{type(error).__name__}: {error}"}
    return out


def _supervise(text: str, units: list[dict], weights: dict[str, float],
               agents: dict, budget_inr_crore: float | None,
               conflicts: list[str]) -> dict:
    """Synthesise one recommendation, with its positives and its negatives.

    The Supervisor is given the deterministic outcome first and told it is
    binding. A supervisor asked to decide freely could talk its way past a
    blocking flood constraint, which is exactly what the architecture forbids —
    so it explains and qualifies the decision instead of re-deriving it.
    """
    from ..agents.llm import complete_json, resolve_backend

    positions = {name: agents[name]["analysis"] for name in DOMAINS
                 if isinstance(agents.get(name), dict) and agents[name].get("analysis")}
    if not positions:
        return {"available": False, "reason": "no agent produced an analysis"}
    try:
        backend = resolve_backend()
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}

    blocked = [u["admin_name"] for u in units if u["blocked"]]
    permitted = [u["admin_name"] for u in units if not u["blocked"]]
    schema = {
        "final_recommendation": "two or three sentences: what to do about this "
                                "proposal, naming the areas it applies to",
        "verdict": "one of: proceed, proceed_with_conditions, revise, do_not_proceed",
        "positives": "array of {point, domain, evidence} — concrete benefits, each "
                     "traceable to an agent analysis or a measured figure",
        "negatives": "array of {point, domain, evidence, severity} — concrete "
                     "drawbacks, risks and costs; severity is low, medium or high",
        "budget_assessment": "whether the stated budget is adequate for this "
                             "proposal across these areas, or null if not stated",
        "trade_offs": "array of {between, tension, resolution}",
        "conditions": "array of conditions that must be met to proceed",
        "dissenting_domains": "array of domains whose concerns are not resolved",
        "confidence": "one of: low, medium, high",
        "data_gaps": "array of things that could not be assessed from the data",
    }
    system = "\n".join([
        "You are the Supervisor of the Ernakulam district urban planning system.",
        "Four domain agents have analysed one citizen proposal. Weigh their "
        "positions against each other using the fixed weights given below, and "
        "report both sides of the decision honestly.",
        "",
        "Hard rules:",
        "- The MEASURED OUTCOME below was computed from government hazard, census "
        "and transit data before you were called. It is binding. Where an area is "
        "blocked you must not recommend proceeding there as proposed.",
        "- Never invent a figure. Every item in positives and negatives must cite "
        "evidence drawn from the agent analyses or the measured outcome.",
        "- Give negatives even when the proposal is sound, and positives even when "
        "it is blocked. A one-sided answer is a failed answer.",
        "- Reply with a single JSON object and nothing else.",
        "",
        "Keys required:",
        json.dumps(schema, indent=2),
    ])
    user = "\n".join([
        "PROPOSAL", text, "",
        f"BUDGET: {budget_inr_crore if budget_inr_crore else 'not stated'}", "",
        "MEASURED OUTCOME (binding)",
        f"areas permitted: {json.dumps(permitted)}",
        f"areas blocked: {json.dumps(blocked)}",
        json.dumps([{k: u[k] for k in
                     ("admin_name", "stance", "suitability_score", "leading_domain")}
                    for u in units], default=str), "",
        "DOMAIN WEIGHTS (fixed)", json.dumps(weights), "",
        "BINDING CONSTRAINTS AND CONDITIONS",
        json.dumps([c["message"] for u in units for c in u["constraints"]["constraints"]
                    if c["severity"] in ("block", "condition")]), "",
        "DETECTED CONFLICTS", json.dumps(conflicts), "",
        "AGENT POSITIONS", json.dumps(positions, default=str)[:7000], "",
        "Reply with the JSON object only.",
    ])
    try:
        payload = complete_json(system, user, backend)
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}

    for key in schema:
        payload.setdefault(key, None)
    payload["available"] = True
    payload["backend"] = {"kind": backend.kind, "model": backend.model}
    payload["bound_by"] = {
        "areas_blocked": blocked,
        "note": "computed from measured data; the Supervisor cannot override it",
    }
    return payload


def evaluate_proposal(text: str, admin_ids: list[str],
                      budget_inr_crore: float | None = None,
                      frame=None, persist: bool = True,
                      with_agents: bool = True) -> dict:
    """An idea plus its affected areas plus a budget, in; a decision, out."""
    text = (text or "").strip()
    if not text:
        raise ValueError("an idea is required")
    admin_ids = list(dict.fromkeys(admin_ids or []))
    if not admin_ids:
        raise ValueError("select at least one affected local body")
    if len(admin_ids) > MAX_UNITS:
        raise ValueError(f"select at most {MAX_UNITS} local bodies "
                         f"({len(admin_ids)} given)")

    from .interpret import classify, find_budget

    frame = _derive(load_features()) if frame is None else frame
    scenario_key, match_score, method, candidates = classify(text)
    if not scenario_key:
        # No catalogue row matched well enough to pick a relevance profile. The
        # highest-scoring row is still the best available basis for weights, and
        # match_score is reported so the user can see the read was weak.
        scenario_key = candidates[0]["scenario"]
    if budget_inr_crore is None:
        budget_inr_crore = find_budget(text)

    units = _evaluate_units(admin_ids, scenario_key, frame)
    weights = _blend_weights(units)
    anchor = max(units, key=lambda u: u["population"] or 0)

    conflicts = sorted({c["tension"] for u in units for c in u["conflicts"]["conflicts"]})
    brief = _brief(text, units, weights, budget_inr_crore)
    agents = _run_agents(brief, anchor["admin_id"]) if with_agents else None
    supervisor = (_supervise(text, units, weights, agents, budget_inr_crore, conflicts)
                  if agents and agents.get("available") else None)

    blocked = [u["admin_name"] for u in units if u["blocked"]]
    headline = (
        f"Not permitted as proposed in {', '.join(blocked)}."
        if blocked else
        f"Permitted across all {len(units)} selected area(s); "
        f"{max(weights, key=weights.get)} carries the greatest decision weight."
    )
    if supervisor and supervisor.get("final_recommendation"):
        headline = supervisor["final_recommendation"]

    result = {
        "proposal_id": f"proposal__{datetime.now(timezone.utc):%Y%m%dT%H%M%S}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "idea": text,
        "budget_inr_crore": budget_inr_crore,
        "budget_source": "stated" if budget_inr_crore else None,
        "areas": [{k: u[k] for k in
                   ("admin_id", "admin_name", "local_body_type", "population",
                    "suitability_score", "stance", "blocked", "leading_domain",
                    "weights")} for u in units],
        "anchor_admin_id": anchor["admin_id"],
        "headline": headline,
        "blocked_areas": blocked,
        "weights": weights,
        "weight_basis": {
            "scenario_key": scenario_key,
            "scenario_label": get(scenario_key).label,
            "match_score": match_score,
            "method": method,
            "note": "the classified objective selects which relevance row supplies "
                    "the weights; it is not shown to the agents and is not the "
                    "thing being evaluated",
        },
        "constraints": [
            {"admin_name": u["admin_name"], **c}
            for u in units for c in u["constraints"]["constraints"]
        ],
        "conflicts": conflicts,
        "priority_detail": {u["admin_id"]: u["priority"] for u in units},
        "parameters": {u["admin_id"]: [prm.as_dict() for prm in u["parameters"]]
                       for u in units},
        "agents": agents,
        "supervisor": supervisor,
        "determinism": {
            "formula_version": units[0]["priority"]["formula_version"],
            "llm_involved": bool(agents and agents.get("available")),
            "llm_can_change_weights": False,
            "llm_can_clear_constraints": False,
            "note": "areas, weights and constraints were computed from measured "
                    "data before any model was called",
        },
    }

    if persist:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{result['proposal_id']}.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8")
        result["persisted"] = {"json": True}
    return result
