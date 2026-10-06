"""The Supervisor: weigh the four agents against each other and against budget.

Conflicts are computed deterministically from the twin's own facts BEFORE the
model sees anything, and attached to the output whatever the model writes. A
model that describes a high-classification flood zone as "low risk" cannot
switch off the safety net by saying so.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from ..rag.retrieve import retrieve
from . import (budget_agent, economic_agent, education_agent, environment_agent,
               healthcare_agent, infrastructure_agent, transportation_agent)
from .llm import Backend, complete_json, resolve_backend

DOMAIN_AGENTS = {
    "economic": economic_agent,
    "infrastructure": infrastructure_agent,
    "transportation": transportation_agent,
    "environment": environment_agent,
    "healthcare": healthcare_agent,
    "education": education_agent,
    "budget": budget_agent,
}

BUDGET_QUERY = "budget allocation capital expenditure funding line item"

NEGATIVE_MARKERS = ("do not proceed", "not feasible", "reject", "halt", "unsafe", "do not")
CONDITIONAL_MARKERS = ("with conditions", "conditional", "pending", "subject to", "mitigat", "detour")
POSITIVE_MARKERS = ("proceed", "feasible", "recommend", "support", "viable")

OUTPUT_SCHEMA = {
    "final_recommendation": "one or two sentences naming the decision AND the trade-off it accepts",
    "rationale": "why this balances the agents' positions",
    "trade_offs": "array of {between, tension, resolution} — one per disagreement",
    "conditions": "array of conditions attached to proceeding",
    "cited_evidence": "array of {source_file, claim} drawn from the document context",
    "alternative_options": "array of options considered and why they were not chosen",
    "confidence": "one of: low, medium, high",
    "dissenting_agents": "array of agent names whose concerns are not fully resolved",
}

SYSTEM = """You are the Supervisor of an Ernakulam district urban planning system. \
Four domain agents have each analysed the same scenario from their own vantage \
point. Your job is not to summarise them and not to pick a favourite: it is to \
weigh them against each other and against budget reality, and to state any \
trade-off explicitly.

Hard rules:
- The DETECTED CONFLICTS section lists disagreements computed from the agents' \
own outputs. You must address every one of them in trade_offs. Never resolve a \
conflict by ignoring the agent that raised it.
- Prefer "proceed, with <specific condition>" over silently choosing one side.
- An environmental or safety objection is never overridden without naming the \
objection and the condition that answers it.
- Cite the budget context when you speak about affordability.
- Reply with a single JSON object and nothing else.

The JSON object must have exactly these keys:
{schema}"""

USER = """SCENARIO
{scenario}

TARGET ADMIN UNIT(S)
{admin_ids}

AGENT OUTPUTS
{agent_outputs}

DETECTED CONFLICTS (computed from the agents' own JSON — address every one)
{conflicts}

BUDGET CONTEXT (retrieved from government documents)
{budget_context}

Reply with the JSON object only."""


def _stance(agent_output: dict) -> str:
    """Reduce an agent's prose to proceed / conditional / oppose, for conflict detection."""
    analysis = agent_output.get("analysis") or {}
    text = " ".join(
        str(analysis.get(key, ""))
        for key in ("recommendation", "feasibility_verdict", "final_recommendation", "risk_band")
    ).lower()
    if any(marker in text for marker in NEGATIVE_MARKERS):
        return "oppose"
    if any(marker in text for marker in CONDITIONAL_MARKERS):
        return "conditional"
    if any(marker in text for marker in POSITIVE_MARKERS):
        return "proceed"
    return "unclear"


def detect_conflicts(results: dict[str, dict]) -> list[dict]:
    """Disagreements computed from the agents' JSON, before any model sees it."""
    conflicts: list[dict] = []
    stances = {name: _stance(result) for name, result in results.items()}

    opposed = [n for n, s in stances.items() if s == "oppose"]
    supportive = [n for n, s in stances.items() if s == "proceed"]
    for objector in opposed:
        for supporter in supportive:
            conflicts.append({
                "between": [objector, supporter],
                "tension": (
                    f"{objector} opposes while {supporter} supports: "
                    f"{results[objector]['analysis'].get('recommendation')!r} vs "
                    f"{results[supporter]['analysis'].get('recommendation')!r}"
                ),
                "kind": "opposed_recommendations",
            })

    # An environmental conflict is derived from the TWIN'S FACTS, not from the
    # Environment Agent's own summary judgement. A model that describes a
    # high-classification hazard zone as "low risk" because the overlap is small
    # must not be able to switch off the safety net by saying so — the whole
    # point of computing this deterministically is that it survives the model.
    environment = results.get("environment")
    if environment:
        hazardous = []
        for fact in environment.get("evidence", {}).get("twin_facts", []):
            if str(fact.get("label", "")).endswith(".risk_level") and \
                    str(fact.get("value", "")).lower() in {"high", "severe"}:
                hazardous.append({
                    "flood_zone_id": fact.get("flood_zone_id"),
                    "risk_level": fact.get("value"),
                    "data_year": fact.get("data_year"),
                })
        overlap = next((f.get("value") for f in environment.get("evidence", {}).get("twin_facts", [])
                        if f.get("label") == "max_overlap_ratio"), None)

        if hazardous:
            reported_band = str((environment.get("analysis") or {}).get("risk_band") or "").lower()
            for name, stance in stances.items():
                if name != "environment" and stance in {"proceed", "conditional"}:
                    tension = (
                        f"the digital twin records flood zone(s) "
                        f"{[z['flood_zone_id'] for z in hazardous]} classified "
                        f"{[z['risk_level'] for z in hazardous]} (data_year "
                        f"{[z['data_year'] for z in hazardous]}) overlapping "
                        f"{overlap if overlap is not None else 'part of'} of this unit, "
                        f"while {name} advances the scenario"
                    )
                    if reported_band in {"low", "moderate"}:
                        tension += (
                            f" — note the Environment Agent summarised this as "
                            f"{reported_band!r} risk; the underlying zone classification is "
                            f"{hazardous[0]['risk_level']!r}"
                        )
                    conflicts.append({
                        "between": ["environment", name],
                        "tension": tension,
                        "kind": "environmental_risk_vs_development",
                        "evidence": hazardous,
                    })

    low_confidence = [n for n, r in results.items()
                      if str((r.get("analysis") or {}).get("confidence", "")).lower() == "low"]
    if low_confidence:
        conflicts.append({
            "between": low_confidence,
            "tension": f"low confidence reported by {', '.join(low_confidence)} — evidence is thin",
            "kind": "low_confidence",
        })
    return conflicts


def _summarise(result: dict) -> dict:
    analysis = result.get("analysis") or {}
    return {
        "agent": result["agent"],
        "analysis": analysis,
        "cited_sources": sorted({c["source_file"] for c in result["evidence"]["citations"]}),
        "fact_count": len(result["evidence"]["twin_facts"]),
    }


def run(scenario: str, admin_ids: str | list[str], backend: Backend | None = None,
        max_workers: int = 4) -> dict:
    """Run all four domain agents concurrently, then supervise their outputs."""
    backend = backend or resolve_backend()
    if isinstance(admin_ids, str):
        admin_ids = [admin_ids]
    primary = admin_ids[0]

    def run_one(item):
        name, module = item
        try:
            return name, module.run(scenario, primary, backend)
        except Exception as error:
            return name, {"agent": name, "analysis": {}, "error": str(error),
                          "evidence": {"twin_facts": [], "citations": []}}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = dict(pool.map(run_one, DOMAIN_AGENTS.items()))

    failed = [n for n, r in results.items() if r.get("error")]
    conflicts = detect_conflicts({n: r for n, r in results.items() if not r.get("error")})
    budget_context = retrieve(f"{BUDGET_QUERY}: {scenario}", admin_id=primary, top_k=3)

    payload = complete_json(
        SYSTEM.format(schema=json.dumps(OUTPUT_SCHEMA, indent=2)),
        USER.format(
            scenario=scenario,
            admin_ids=", ".join(admin_ids),
            agent_outputs=json.dumps([_summarise(r) for r in results.values()], indent=2, default=str),
            conflicts=json.dumps(conflicts, indent=2) if conflicts else "(none detected)",
            budget_context="\n\n".join(
                f"[{c['source_file']}] {c['text']}" for c in budget_context) or "(none retrieved)",
        ),
        backend,
    )
    for key in OUTPUT_SCHEMA:
        payload.setdefault(key, None)

    # The detected conflicts are attached whatever the model chose to write, so a
    # trade-off can never be lost by a model that preferred a tidy answer.
    payload["detected_conflicts"] = conflicts

    return {
        "scenario": scenario,
        "admin_ids": admin_ids,
        "generated_at": datetime.now().astimezone().isoformat(),
        "backend": {"kind": backend.kind, "model": backend.model, "note": backend.note or None},
        "agent_outputs": results,
        "budget_context": budget_context,
        "supervisor": payload,
        "warnings": [f"{n} failed: {results[n]['error']}" for n in failed],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="supervisor", description="Run the Supervisor agent.")
    parser.add_argument("--admin-id", required=True, action="append", dest="admin_ids")
    parser.add_argument("--scenario", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.scenario, args.admin_ids), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
