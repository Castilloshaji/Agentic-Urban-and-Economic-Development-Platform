"""Hard constraints: whether an option is permitted at all.

Priority weights answer "how much influence does this domain have". Constraints
answer "is this allowed". They are different questions and the second one is not
negotiable: a scenario can be economically attractive and still be blocked.

Every threshold here is evaluated against measured KSDMA/GSI values before any
model runs, and the result is attached to the decision regardless of what the
model subsequently writes. A weight of 0.05 for environment does not soften a
BLOCK, and no LLM output can clear one.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from .parameters import parameters_for

BLOCK, CONDITION, ADVISORY = "block", "condition", "advisory"

# Thresholds are on the share of a unit's area that KSDMA models as flood-prone
# at the 50-year return period, and on the GSI susceptibility rank.
FLOOD_BLOCK = 0.40
FLOOD_CONDITION = 0.15
LANDSLIDE_BLOCK_RANK = 3        # "high"
CLIMATE_CONDITION_DELTA = 0.05  # RCP8.5 adds >5 points of flood-modelled area

# Scenarios that put permanent built structures or people into harm's way.
SITING_SCENARIOS = {"industrial_development", "affordable_housing",
                    "transit_oriented_development", "urban_service_expansion"}


@dataclass
class Constraint:
    code: str
    severity: str
    domain: str
    message: str
    measured_value: float | None
    threshold: float
    parameter: str
    source: str | None
    source_level: int | None
    data_year: int | None
    overridable_by_model: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate(admin_id: str, scenario_key: str, frame=None) -> dict:
    params = {p.name: p for p in parameters_for(admin_id, frame)}
    found: list[Constraint] = []

    flood = params.get("flood_risk")
    if flood and flood.value is not None:
        if flood.value >= FLOOD_BLOCK and scenario_key in SITING_SCENARIOS:
            found.append(Constraint(
                "ENV-FLOOD-BLOCK", BLOCK, "environment",
                f"{flood.value:.1%} of this local body is modelled flood-prone at the "
                f"50-year return period. New permanent siting cannot proceed without "
                f"flood mitigation and a revised layout.",
                flood.value, FLOOD_BLOCK, "flood_risk",
                flood.source, flood.source_level, flood.data_year))
        elif flood.value >= FLOOD_CONDITION:
            found.append(Constraint(
                "ENV-FLOOD-CONDITION", CONDITION, "environment",
                f"{flood.value:.1%} flood-modelled area — mitigation works and a "
                f"drainage assessment are required conditions.",
                flood.value, FLOOD_CONDITION, "flood_risk",
                flood.source, flood.source_level, flood.data_year))

    slide = params.get("hazard_exposure")
    if slide and slide.value is not None and slide.value >= LANDSLIDE_BLOCK_RANK:
        severity = BLOCK if scenario_key in SITING_SCENARIOS else CONDITION
        found.append(Constraint(
            "ENV-LANDSLIDE", severity, "environment",
            "GSI classifies land in this unit as high landslide susceptibility. "
            + ("Permanent siting is blocked pending a geotechnical assessment."
               if severity == BLOCK else
               "Works require a geotechnical assessment."),
            slide.value, float(LANDSLIDE_BLOCK_RANK), "hazard_exposure",
            slide.source, slide.source_level, slide.data_year))

    climate = params.get("climate_vulnerability")
    if climate and climate.value is not None and climate.value >= CLIMATE_CONDITION_DELTA:
        found.append(Constraint(
            "ENV-CLIMATE", CONDITION, "environment",
            f"Under RCP 8.5 the flood-modelled share of this unit rises by "
            f"{climate.value:.1%}. Design life must assume the projected extent, "
            f"not the historical one.",
            climate.value, CLIMATE_CONDITION_DELTA, "climate_vulnerability",
            climate.source, climate.source_level, climate.data_year))

    # Data-quality advisory: a stale feed must not silently pass as current.
    transit = params.get("accessibility_deficit")
    if transit and "stale" in (transit.note or ""):
        found.append(Constraint(
            "DATA-TRANSIT-STALE", ADVISORY, "transportation",
            f"Transit conclusions rest on a GTFS feed that is {transit.note}. "
            f"Service patterns are not verified as current.",
            None, float(12), "accessibility_deficit",
            transit.source, transit.source_level, transit.data_year))

    blocks = [c for c in found if c.severity == BLOCK]
    return {
        "admin_id": admin_id,
        "scenario": scenario_key,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "blocked" if blocks else ("conditional" if found else "clear"),
        "blocking_count": len(blocks),
        "constraints": [c.as_dict() for c in found],
        "note": ("A blocking constraint cannot be cleared by priority weights or by "
                 "model output. It is evaluated from measured hazard data before any "
                 "agent runs."),
    }


if __name__ == "__main__":
    import json, sys
    print(json.dumps(evaluate(sys.argv[1] if len(sys.argv) > 1 else "G07049",
                              sys.argv[2] if len(sys.argv) > 2 else "industrial_development"),
                     indent=2)[:1600])
