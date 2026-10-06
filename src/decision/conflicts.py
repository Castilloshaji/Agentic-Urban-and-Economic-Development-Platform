"""Deterministic conflict detection, outside the LLM.

Lifted out of supervisor_agent.py so the same rules serve both the agent path
and the parameter-driven path. A conflict is computed from the twin's own facts
and from the agents' structured output — never from their prose — so a model
that writes a reassuring sentence cannot make a disagreement disappear.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

CRITICAL, MAJOR, MINOR = "critical", "major", "minor"

NEGATIVE = ("do not proceed", "not feasible", "reject", "halt", "unsafe", "do not")
CONDITIONAL = ("with conditions", "conditional", "pending", "subject to", "mitigat", "detour")
POSITIVE = ("proceed", "feasible", "recommend", "support", "viable")


@dataclass
class Conflict:
    kind: str
    severity: str
    between: list[str]
    tension: str
    evidence: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def stance_of(analysis: dict) -> str:
    text = " ".join(str(analysis.get(k, "")) for k in
                    ("recommendation", "feasibility_verdict", "final_recommendation",
                     "risk_band")).lower()
    if any(m in text for m in NEGATIVE):
        return "oppose"
    if any(m in text for m in CONDITIONAL):
        return "conditional"
    if any(m in text for m in POSITIVE):
        return "proceed"
    return "unclear"


def from_constraints(constraints: dict, weights: dict[str, float]) -> list[Conflict]:
    """A blocking constraint against any domain carrying real decision weight.

    This is the conflict that matters most and it needs no agents at all: the
    measured hazard is already in tension with the objective.
    """
    found: list[Conflict] = []
    blocks = [c for c in constraints.get("constraints", []) if c["severity"] == "block"]
    for block in blocks:
        for domain, weight in sorted(weights.items(), key=lambda kv: -kv[1]):
            if domain == block["domain"] or weight < 0.10:
                continue
            found.append(Conflict(
                "hard_constraint_vs_objective", CRITICAL,
                [block["domain"], domain],
                f"{block['code']} blocks this siting on measured "
                f"{block['parameter']} = {block['measured_value']} "
                f"(threshold {block['threshold']}), while {domain} carries "
                f"{weight:.0%} of the decision weight.",
                {"code": block["code"], "measured_value": block["measured_value"],
                 "threshold": block["threshold"], "source": block["source"],
                 "source_level": block["source_level"],
                 "overridable_by_model": False}))
    return found


def from_agents(results: dict[str, dict], twin_facts: dict | None = None) -> list[Conflict]:
    """Disagreements computed from the agents' structured output."""
    found: list[Conflict] = []
    stances = {name: stance_of(r.get("analysis") or {}) for name, r in results.items()}

    opposed = [n for n, s in stances.items() if s == "oppose"]
    supportive = [n for n, s in stances.items() if s == "proceed"]
    for objector in opposed:
        for supporter in supportive:
            found.append(Conflict(
                "opposed_recommendations", MAJOR, [objector, supporter],
                f"{objector} opposes while {supporter} supports."))

    low = [n for n, r in results.items()
           if str((r.get("analysis") or {}).get("confidence", "")).lower() == "low"]
    if low:
        found.append(Conflict("low_confidence", MINOR, low,
                              f"low confidence reported by {', '.join(low)}"))
    return found


def detect(constraints: dict, weights: dict[str, float],
           agent_results: dict[str, dict] | None = None) -> dict:
    found = from_constraints(constraints, weights)
    if agent_results:
        found += from_agents(agent_results)
    by_severity: dict[str, int] = {}
    for conflict in found:
        by_severity[conflict.severity] = by_severity.get(conflict.severity, 0) + 1
    return {
        "count": len(found),
        "by_severity": by_severity,
        "has_critical": any(c.severity == CRITICAL for c in found),
        "conflicts": [c.as_dict() for c in found],
        "note": "computed from measured facts and structured agent output before "
                "any narrative is generated",
    }
