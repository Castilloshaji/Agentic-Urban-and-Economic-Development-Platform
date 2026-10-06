"""Scenario catalogue and the scenario-relevance matrix.

`relevance` is the only place where a human judgement about domain importance is
encoded, and it is deliberately explicit, versioned and inspectable rather than
hidden inside a prompt. Everything downstream is arithmetic over measured data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

FORMULA_VERSION = "priority-v2-separated-terms"
DOMAINS = ("economic", "infrastructure", "transportation", "environment",
           "healthcare", "education", "budget")


@dataclass
class ScenarioType:
    key: str
    label: str
    description: str
    relevance: dict[str, float]
    parameters: dict[str, list[str]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# Relevance is "how much does this domain bear on this objective", 0-1.
# It is NOT a safety judgement — environment stays safety-critical through hard
# constraints even where its relevance number is low.
#
# `budget` is relevant to everything, because every objective spends money, but
# it is never dominant: fiscal capacity shapes what is affordable, it does not
# decide what is needed. Healthcare and education are near zero for siting
# objectives and dominant for service ones, which is the honest reading. A
# domain with 0.0 relevance drops out of the weighting entirely.
CATALOGUE: dict[str, ScenarioType] = {
    "public_transport_expansion": ScenarioType(
        "public_transport_expansion", "Public transport expansion",
        "Extend bus or feeder service to improve connectivity.",
        {"transportation": 1.00, "infrastructure": 0.70, "economic": 0.45, "environment": 0.35,
         "healthcare": 0.40, "education": 0.45, "budget": 0.55},
        {"transportation": ["accessibility_deficit", "transit_availability",
                            "service_frequency", "population_affected"],
         "infrastructure": ["population_served", "settlement_density", "implementation_area"],
         "economic": ["economic_catchment", "urbanisation"],
         "environment": ["flood_risk", "hazard_exposure"],
         "healthcare": ["health_access", "health_capacity"],
         "education": ["education_access", "education_capacity"],
         "budget": ["fiscal_entitlement", "own_income_capacity"]}),
    "industrial_development": ScenarioType(
        "industrial_development", "Industrial development",
        "Identify an area suitable for industrial or MSME development.",
        {"economic": 1.00, "infrastructure": 0.85, "environment": 0.80, "transportation": 0.45,
         "healthcare": 0.25, "education": 0.40, "budget": 0.65},
        {"economic": ["economic_catchment", "urbanisation", "literacy_capacity"],
         "infrastructure": ["implementation_area", "settlement_density", "population_served"],
         "environment": ["flood_risk", "climate_vulnerability", "hazard_exposure",
                         "environmental_sensitivity"],
         "transportation": ["transit_availability", "accessibility_deficit"],
         "healthcare": ["health_access"],
         "education": ["education_capacity", "literacy_capacity"],
         "budget": ["fiscal_entitlement", "own_income_capacity", "cost_exposure"]}),
    "flood_resilient_development": ScenarioType(
        "flood_resilient_development", "Flood-resilient development",
        "Prioritise mitigation and resilient siting against flood hazard.",
        {"environment": 1.00, "infrastructure": 0.75, "transportation": 0.45, "economic": 0.30,
         "healthcare": 0.45, "education": 0.20, "budget": 0.60},
        {"environment": ["flood_risk", "flood_zone_overlap", "climate_vulnerability",
                         "hazard_exposure"],
         "infrastructure": ["population_served", "settlement_density"],
         "transportation": ["accessibility_deficit", "population_affected"],
         "economic": ["economic_catchment"],
         "healthcare": ["health_access", "health_capacity"],
         "education": ["education_access"],
         "budget": ["fiscal_entitlement", "cost_exposure"]}),
    "affordable_housing": ScenarioType(
        "affordable_housing", "Affordable housing",
        "Site affordable housing with access and safety considered.",
        {"infrastructure": 1.00, "transportation": 0.70, "environment": 0.65, "economic": 0.50,
         "healthcare": 0.55, "education": 0.60, "budget": 0.70},
        {"infrastructure": ["implementation_area", "settlement_density", "population_served"],
         "transportation": ["distance_to_transit", "transit_availability"],
         "environment": ["flood_risk", "hazard_exposure"],
         "economic": ["economic_catchment", "literacy_capacity"],
         "healthcare": ["health_access", "health_capacity"],
         "education": ["education_access", "education_capacity"],
         "budget": ["fiscal_entitlement", "own_income_capacity"]}),
    "transit_oriented_development": ScenarioType(
        "transit_oriented_development", "Transit-oriented development",
        "Concentrate development around existing high-service transit.",
        {"transportation": 1.00, "economic": 0.75, "infrastructure": 0.70, "environment": 0.50,
         "healthcare": 0.35, "education": 0.40, "budget": 0.55},
        {"transportation": ["transit_availability", "service_frequency", "distance_to_transit"],
         "economic": ["urbanisation", "economic_catchment"],
         "infrastructure": ["settlement_density", "population_served"],
         "environment": ["flood_risk"],
         "healthcare": ["health_access"],
         "education": ["education_access"],
         "budget": ["fiscal_entitlement", "own_income_capacity"]}),
    "urban_service_expansion": ScenarioType(
        "urban_service_expansion", "Urban service expansion",
        "Extend municipal services to underserved areas.",
        {"infrastructure": 1.00, "economic": 0.55, "transportation": 0.55, "environment": 0.50,
         "healthcare": 0.80, "education": 0.75, "budget": 0.70},
        {"infrastructure": ["population_served", "settlement_density", "implementation_area"],
         "economic": ["economic_catchment", "literacy_capacity"],
         "transportation": ["accessibility_deficit"],
         "environment": ["flood_risk", "hazard_exposure"],
         "healthcare": ["health_access", "health_capacity", "health_deficit"],
         "education": ["education_access", "education_capacity", "education_deficit"],
         "budget": ["fiscal_entitlement", "own_income_capacity", "cost_exposure"]}),
}


def get(key: str) -> ScenarioType:
    if key not in CATALOGUE:
        raise KeyError(f"unknown scenario {key!r}; known: {sorted(CATALOGUE)}")
    return CATALOGUE[key]
