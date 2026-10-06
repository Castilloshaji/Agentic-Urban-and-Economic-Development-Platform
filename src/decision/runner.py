"""Scenario runner: the deterministic decision path, end to end.

    parameters -> priorities -> constraints -> weighted position -> decision

The whole chain is arithmetic over measured data and runs without an LLM. Agent
narrative can be layered on afterwards, but the decision, the weights and the
constraints do not depend on it — which is what makes the result reproducible
and the audit trail meaningful.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .conflicts import detect
from .constraints import evaluate
from .parameters import _derive, load_features
from .priority import compute
from .scenarios import get

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "data" / "derived" / "scenario_runs"


def _suitability(priority: dict, constraints: dict) -> dict:
    """A weighted score, then the constraint verdict applied on top of it.

    The score is what the priorities say; the verdict is what safety says. They
    are reported separately so a reader can see an attractive option that is
    nonetheless not permitted.
    """
    score = 0.0
    for domain, weight in priority["weights"].items():
        signal = priority["detail"][domain]["parameter_signal"]
        # Environment signal is a hazard: high hazard lowers suitability.
        contribution = (1.0 - signal) if domain == "environment" else signal
        score += weight * contribution
    score = round(score, 4)

    blocked = constraints["blocking_count"] > 0
    if blocked:
        stance = "not_permitted"
    elif score >= 0.60:
        stance = "recommended"
    elif score >= 0.40:
        stance = "recommended_with_conditions"
    else:
        stance = "not_recommended"
    return {"suitability_score": score, "stance": stance, "blocked": blocked}


def _run_agents(admin_id: str, scenario_key: str, weights: dict[str, float],
                idea_text: str | None = None,
                constraints: dict | None = None,
                budget_inr_crore: float | None = None) -> dict | None:
    """Have the four domain agents analyse the actual proposal.

    The agents are given the user's own wording when there is one, not the
    catalogue description — a user who writes "an industrial park near
    Malayattoor for MSME jobs, 120 crore" deserves an analysis of that, not of
    the generic objective it was classified under. The classification only
    selects which relevance row the weights come from.

    They also receive the computed weights and any binding constraint as fixed
    context, so their reasoning is framed by them. Nothing they write can change
    a weight or clear a constraint; the deterministic result is already decided
    before they are called.
    """
    try:
        from ..agents.llm import resolve_backend
        backend = resolve_backend()
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}

    from ..agents import (budget_agent, economic_agent, education_agent,
                          environment_agent, healthcare_agent,
                          infrastructure_agent, transportation_agent)
    modules = {"economic": economic_agent, "infrastructure": infrastructure_agent,
               "transportation": transportation_agent, "environment": environment_agent,
               "healthcare": healthcare_agent, "education": education_agent,
               "budget": budget_agent}

    proposal = (idea_text or "").strip() or get(scenario_key).description
    parts = [f"PROPOSAL (analyse this specific proposal): {proposal}"]
    if idea_text:
        parts.append(f"It was classified as: {get(scenario_key).label}.")
    if budget_inr_crore:
        parts.append(f"Stated budget: INR {budget_inr_crore} crore.")
    parts.append("Domain decision weights, computed from measured data and FIXED: "
                 + ", ".join(f"{d} {w:.0%}" for d, w in
                             sorted(weights.items(), key=lambda kv: -kv[1]))
                 + ". Do not revise or restate these weights.")
    blocking = [c for c in (constraints or {}).get("constraints", [])
                if c["severity"] == "block"]
    if blocking:
        parts.append("BINDING CONSTRAINT already determined from measured hazard data: "
                     + " ".join(c["message"] for c in blocking)
                     + " You cannot clear this. Analyse within it.")
    scenario_text = " ".join(parts)

    out: dict[str, dict] = {"available": True,
                            "backend": {"kind": backend.kind, "model": backend.model}}
    for name, module in modules.items():
        try:
            result = module.run(scenario_text, admin_id, backend)
            out[name] = {"analysis": result.get("analysis"),
                         "priority_weight": weights.get(name),
                         "citations": [c["source_file"] for c in
                                       result.get("evidence", {}).get("citations", [])]}
        except Exception as error:
            out[name] = {"error": f"{type(error).__name__}: {error}",
                         "priority_weight": weights.get(name)}
    return out


def _supervise(proposal: str, priority: dict, constraints: dict, conflicts: dict,
               agents: dict, verdict: dict, budget_inr_crore: float | None) -> dict | None:
    """Synthesise the four analyses into one recommendation.

    The Supervisor is told the deterministic outcome up front and asked to
    explain and qualify it, not to re-derive it. That ordering matters: a
    supervisor asked to decide freely would be able to talk its way past a
    blocking constraint, which is exactly what the architecture forbids.
    """
    import json as _json

    from ..agents.llm import complete_json, resolve_backend

    try:
        backend = resolve_backend()
    except Exception as error:
        return {"available": False, "reason": f"{type(error).__name__}: {error}"}

    positions = {name: data.get("analysis") for name, data in agents.items()
                 if isinstance(data, dict) and data.get("analysis")}
    if not positions:
        return {"available": False, "reason": "no agent produced an analysis"}

    schema = {
        "final_recommendation": "one or two sentences naming the decision AND the trade-off it accepts",
        "rationale": "why this balances the weighted domain positions",
        "trade_offs": "array of {between, tension, resolution}",
        "conditions": "array of conditions attached to proceeding",
        "dissenting_domains": "array of domains whose concerns are not fully resolved",
        "confidence": "one of: low, medium, high",
        "data_gaps": "array of things that could not be assessed from available data",
    }
    rules = [
        "- The DETERMINED OUTCOME below was computed from measured data before you"
        " were called. You must not contradict it. If it says not_permitted, your"
        " recommendation must not advise proceeding as proposed.",
        "- Never invent a figure. Use only numbers present in the agent analyses.",
        "- Address every listed conflict in trade_offs.",
        "- Reply with a single JSON object and nothing else.",
    ]
    system = "\n".join([
        "You are the Supervisor of an Ernakulam district urban planning system."
        " Four domain agents have analysed one proposal. Weigh their positions"
        " against each other using the given weights.",
        "",
        "Hard rules:",
        *rules,
        "",
        "Keys required:",
        _json.dumps(schema, indent=2),
    ])

    def listed(severity: str) -> str:
        return _json.dumps([c["message"] for c in constraints["constraints"]
                            if c["severity"] == severity])

    user = "\n".join([
        "PROPOSAL", proposal, "",
        "DETERMINED OUTCOME (binding)",
        f"stance={verdict['stance']} suitability={verdict['suitability_score']} "
        f"blocked={verdict['blocked']}", "",
        "DOMAIN WEIGHTS", _json.dumps(priority["weights"]), "",
        "BINDING CONSTRAINTS", listed("block"), "",
        "CONDITIONS", listed("condition"), "",
        "DETECTED CONFLICTS",
        _json.dumps([c["tension"] for c in conflicts["conflicts"]]), "",
        "BUDGET", str(budget_inr_crore) if budget_inr_crore else "not stated", "",
        "AGENT POSITIONS", _json.dumps(positions, default=str)[:6000], "",
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
    payload["bound_by"] = {"stance": verdict["stance"], "blocked": verdict["blocked"],
                           "note": "the Supervisor cannot override this"}
    return payload


def run(admin_id: str, scenario_key: str, budget_inr_crore: float | None = None,
        frame=None, persist: bool = True, with_agents: bool = False,
        idea_text: str | None = None) -> dict:
    frame = _derive(load_features()) if frame is None else frame
    row = frame[frame.admin_id == admin_id]
    if row.empty:
        raise KeyError(f"unknown admin_id {admin_id!r}")
    unit = row.iloc[0]

    priority = compute(admin_id, scenario_key, frame)
    constraints = evaluate(admin_id, scenario_key, frame)
    verdict = _suitability(priority, constraints)
    scenario = get(scenario_key)

    agents = (_run_agents(admin_id, scenario_key, priority["weights"],
                          idea_text=idea_text, constraints=constraints,
                          budget_inr_crore=budget_inr_crore)
              if with_agents else None)
    agent_results = ({k: v for k, v in agents.items()
                      if isinstance(v, dict) and "analysis" in v}
                     if agents and agents.get("available") else None)
    conflicts = detect(constraints, priority["weights"], agent_results)

    conditions = [c["message"] for c in constraints["constraints"]
                  if c["severity"] in ("condition", "block")]
    advisories = [c["message"] for c in constraints["constraints"]
                  if c["severity"] == "advisory"]

    leading = priority["ranking"][0]
    headline = (
        f"Not permitted as sited: {constraints['constraints'][0]['message']}"
        if verdict["blocked"] else
        f"{scenario.label} in {unit['name']} scores {verdict['suitability_score']:.2f}; "
        f"{leading} carries the greatest decision weight "
        f"({priority['weights'][leading]:.0%}) for this objective."
    )

    supervisor = None
    if agents and agents.get("available"):
        supervisor = _supervise(idea_text or scenario.description, priority, constraints,
                                conflicts, agents, verdict, budget_inr_crore)
        if supervisor and supervisor.get("available") and supervisor.get("final_recommendation"):
            headline = supervisor["final_recommendation"]

    result = {
        "run_id": f"{admin_id}__{scenario_key}__{datetime.now(timezone.utc):%Y%m%dT%H%M%S}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "admin_id": admin_id,
        "admin_name": unit["name"],
        "local_body_type": unit.get("local_auth"),
        "scenario": scenario.as_dict(),
        "budget_inr_crore": budget_inr_crore,
        "decision": {**verdict, "headline": headline,
                     "leading_domain": leading,
                     "conditions": conditions, "advisories": advisories},
        "priority": priority,
        "constraints": constraints,
        "conflicts": conflicts,
        "agents": agents,
        "supervisor": supervisor,
        "proposal_text": idea_text,
        "determinism": {"formula_version": priority["formula_version"],
                        "llm_involved": bool(agents and agents.get("available")),
                        "llm_can_change_weights": False,
                        "llm_can_clear_constraints": False,
                        "note": "weights and constraints are computed from measured "
                                "data; no model participates in this path"},
    }

    if persist:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{result['run_id']}.json").write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8")
        try:
            from ..storage.postgres.load_features import persist_run
            persist_run(result)
            result["persisted"] = {"json": True, "database": True}
        except Exception as error:
            # A database that is down must not lose the run; the JSON is written.
            result["persisted"] = {"json": True, "database": False,
                                    "reason": f"{type(error).__name__}: {error}"}
    return result


def compare(admin_id: str, scenario_a: str, scenario_b: str) -> dict:
    """The central Review-2 proof: same data, two objectives, two decisions."""
    frame = _derive(load_features())
    a, b = run(admin_id, scenario_a, frame=frame, persist=False), \
           run(admin_id, scenario_b, frame=frame, persist=False)
    moved = {d: round(b["priority"]["weights"][d] - a["priority"]["weights"][d], 4)
             for d in a["priority"]["weights"]}
    return {
        "admin_id": admin_id, "admin_name": a["admin_name"],
        "data_unchanged": True,
        "scenario_a": {"key": scenario_a, "weights": a["priority"]["weights"],
                       "leading": a["priority"]["ranking"][0],
                       "stance": a["decision"]["stance"],
                       "score": a["decision"]["suitability_score"],
                       "verdict": a["constraints"]["verdict"]},
        "scenario_b": {"key": scenario_b, "weights": b["priority"]["weights"],
                       "leading": b["priority"]["ranking"][0],
                       "stance": b["decision"]["stance"],
                       "score": b["decision"]["suitability_score"],
                       "verdict": b["constraints"]["verdict"]},
        "weight_shift": moved,
        "leading_domain_changed": a["priority"]["ranking"][0] != b["priority"]["ranking"][0],
        "decision_changed": a["decision"]["stance"] != b["decision"]["stance"],
    }


def rank_units(scenario_key: str, limit: int = 10) -> list[dict]:
    """Rank every local body for one objective — the candidate-area search."""
    frame = _derive(load_features())
    rows = []
    for admin_id in frame.admin_id:
        try:
            r = run(admin_id, scenario_key, frame=frame, persist=False)
        except Exception:
            continue
        rows.append({"admin_id": admin_id, "name": r["admin_name"],
                     "type": r["local_body_type"],
                     "score": r["decision"]["suitability_score"],
                     "stance": r["decision"]["stance"],
                     "verdict": r["constraints"]["verdict"],
                     "leading_domain": r["decision"]["leading_domain"]})
    rows.sort(key=lambda x: (x["stance"] == "not_permitted", -x["score"]))
    return rows[:limit]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 3 and sys.argv[1] == "compare":
        print(json.dumps(compare(sys.argv[2], sys.argv[3], sys.argv[4]), indent=2))
    else:
        print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else "G07049",
                             sys.argv[2] if len(sys.argv) > 2 else "public_transport_expansion"),
                         indent=2, default=str)[:1800])
