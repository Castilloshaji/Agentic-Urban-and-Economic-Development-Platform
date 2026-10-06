"""Where the measured data says a place is short of something.

This is the deterministic half of budget-only ideation. Before any model is
asked to propose anything, the parameters are read for the selected areas and
turned into a ranked list of needs: what is measurably lacking, how badly, and
on what evidence.

The direction of each parameter has to be declared, because normalisation alone
cannot tell you which way is bad. A high percentile on `flood_risk` is a
problem; a high percentile on `road_connectivity` is an asset. Getting that
backwards would have the system proposing flood defences for the driest
panchayat in the district, so the mapping below is explicit and every parameter
in the engine appears in it.

What this module does NOT do is decide what to build. It says "Kuttampuzha is in
the 97th percentile for distance to transit, on a 2022 GTFS feed" and stops.
The agents propose; this establishes the facts they must propose against.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .parameters import UNAVAILABLE, parameters_for

# How a high normalised value should be read.
#   "deficit"  — high percentile means the place is badly off (need is high)
#   "asset"    — high percentile means the place is well served (need is low)
#   "scale"    — neither; it sizes the problem rather than grading it
DIRECTION = {
    # environment: hazard is a deficit, there is no "asset" direction here
    "flood_risk": "deficit",
    "flood_zone_overlap": "deficit",
    "climate_vulnerability": "deficit",
    "hazard_exposure": "deficit",
    "environmental_sensitivity": "deficit",
    "water_body_proximity": "deficit",
    # transportation
    "accessibility_deficit": "deficit",
    "transit_availability": "asset",
    "distance_to_transit": "asset",
    "service_frequency": "asset",
    "congestion_pressure": "deficit",
    "metro_access": "asset",
    "population_affected": "scale",
    # infrastructure
    "road_connectivity": "asset",
    "infrastructure_capacity": "asset",
    "right_of_way_constraint": "deficit",
    "population_served": "scale",
    "settlement_density": "scale",
    "implementation_area": "scale",
    # economic
    "literacy_capacity": "asset",
    "economic_catchment": "scale",
    "urbanisation": "scale",
    "msme_presence": "asset",
    "investment_potential": "asset",
    "employment_potential": "asset",
    # healthcare and education: more access and more capacity are assets
    "health_access": "asset",
    "health_capacity": "asset",
    "health_deficit": "asset",
    "education_access": "asset",
    "education_capacity": "asset",
    "education_deficit": "asset",
    # budget: entitlement and own income are assets, cost exposure is a deficit
    "fiscal_entitlement": "asset",
    "own_income_capacity": "asset",
    "cost_exposure": "deficit",
}

# A need this weak is not worth a line in a brief; it is the ordinary condition
# of a median local body, and listing it would bury the real gaps.
REPORT_FLOOR = 0.55

# Plain-language framing per parameter, so a generated brief reads like a finding
# rather than a variable dump. Only the parameters that can carry a need appear.
PHRASING = {
    "flood_risk": "flood-modelled land share",
    "flood_zone_overlap": "overlap with mapped flood zones",
    "climate_vulnerability": "projected flood increase under RCP 8.5",
    "hazard_exposure": "GSI landslide susceptibility",
    "environmental_sensitivity": "combined flood and landslide exposure",
    "water_body_proximity": "proximity to a water body",
    "accessibility_deficit": "distance to the nearest transit stop",
    "transit_availability": "transit stop density",
    "distance_to_transit": "transit reach from the centre",
    "service_frequency": "bus trips per stop",
    "congestion_pressure": "settlement density as a congestion signal",
    "metro_access": "distance to the nearest metro station",
    "road_connectivity": "road length per km²",
    "infrastructure_capacity": "highest road class available",
    "right_of_way_constraint": "share of the network that is narrow-class",
    "literacy_capacity": "literacy rate",
    "msme_presence": "registered enterprise base",
    "investment_potential": "estimated per-capita income",
    "employment_potential": "headroom for new employment",
    "health_access": "distance to the nearest health facility",
    "health_capacity": "health facility density",
    "health_deficit": "health capacity per 1,000 residents",
    "education_access": "distance to the nearest school or college",
    "education_capacity": "education facility density",
    "education_deficit": "education capacity per 1,000 residents",
    "fiscal_entitlement": "share of a devolved grant under the state formula",
    "own_income_capacity": "ability to raise and match funds locally",
    "cost_exposure": "cost of building on this ground",
}


@dataclass
class Need:
    parameter: str
    domain: str
    admin_id: str
    admin_name: str
    severity: float          # 0-1, where 1 is the worst position in the district
    reading: str             # the finding, in words
    value: float | None
    status: str              # real | proxy — a need built on a proxy says so
    source: str | None
    source_level: int | None
    data_year: int | None
    # Implementations already logged against this exact gap in this exact area.
    # Populated by assess(); empty means nobody is working on it.
    addressed_by: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _severity(parameter, direction: str) -> float | None:
    """Translate a percentile into "how badly off is this place"."""
    if parameter.normalized is None or direction == "scale":
        return None
    return parameter.normalized if direction == "deficit" else 1.0 - parameter.normalized


def needs_for(admin_id: str, admin_name: str, frame) -> list[Need]:
    """Every measurable shortfall for one local body, worst first."""
    collected = []
    for parameter in parameters_for(admin_id, frame):
        if parameter.status == UNAVAILABLE:
            continue
        direction = DIRECTION.get(parameter.name, "scale")
        severity = _severity(parameter, direction)
        if severity is None or severity < REPORT_FLOOR:
            continue
        label = PHRASING.get(parameter.name, parameter.name.replace("_", " "))
        collected.append(Need(
            parameter=parameter.name,
            domain=parameter.domain,
            admin_id=admin_id,
            admin_name=admin_name,
            severity=round(severity, 4),
            reading=(f"{admin_name}: {label} puts it in the "
                     f"{severity:.0%} percentile of need across the district"),
            value=parameter.value,
            status=parameter.status,
            source=parameter.source,
            source_level=parameter.source_level,
            data_year=parameter.data_year,
        ))
    return sorted(collected, key=lambda n: -n.severity)


def assess(admin_ids: list[str], frame, with_coverage: bool = True) -> dict:
    """Needs across the selected areas, grouped by domain.

    Returned per-domain because that is how the ideation is dispatched: each
    agent is handed its own domain's shortfalls and asked what would address
    them. A domain with no shortfall above the floor is reported as such rather
    than omitted, so an agent proposing into it knows it is working without a
    measured mandate.

    When `with_coverage` is set, each need is annotated with any implementation
    already logged against that exact parameter in that exact area. The
    severity is **not** reduced by it: a gap with work under way is still a
    gap until someone re-measures, and discounting it here would quietly
    rewrite a government measurement to reflect an intention. The annotation
    exists so the ideation path can stop re-proposing work already committed,
    which is a different problem from the gap having closed.
    """
    names = dict(zip(frame.admin_id, frame.name))
    everything: list[Need] = []
    for admin_id in admin_ids:
        everything.extend(needs_for(admin_id, names.get(admin_id, admin_id), frame))

    covered: dict[str, list[dict]] = {}
    if with_coverage:
        try:
            from .implementations import coverage
            covered = coverage(admin_ids)["by_parameter_and_area"]
        except Exception as error:
            # A ledger that is unreachable must not take the need assessment
            # with it; the measurements are the part that matters.
            covered = {}
            coverage_error = f"{type(error).__name__}: {error}"
        else:
            coverage_error = None
    else:
        coverage_error = None

    by_domain: dict[str, list[Need]] = {}
    for need in everything:
        need.addressed_by = covered.get(f"{need.parameter}|{need.admin_id}", [])
        by_domain.setdefault(need.domain, []).append(need)
    for needs in by_domain.values():
        needs.sort(key=lambda n: -n.severity)

    # The domain-level figure is the worst shortfall in it, not the mean: a
    # single blocking hazard is a bigger call on a budget than four mediocre
    # readings, and averaging would hide it.
    pressure = {domain: round(max(n.severity for n in needs), 4)
                for domain, needs in by_domain.items()}

    uncovered = [n for n in everything if not n.addressed_by]
    return {
        "admin_ids": admin_ids,
        "by_domain": {d: [n.as_dict() for n in ns] for d, ns in by_domain.items()},
        "domain_pressure": pressure,
        "ranked": [n.as_dict() for n in sorted(everything, key=lambda n: -n.severity)],
        "report_floor": REPORT_FLOOR,
        "covered_count": len(everything) - len(uncovered),
        "uncovered_count": len(uncovered),
        "coverage_error": coverage_error,
        "note": ("Severity is percentile-of-need across all 97 local bodies, "
                 "computed from measured parameters. Needs resting on a proxy "
                 "parameter carry status 'proxy'. A need with `addressed_by` "
                 "already has work logged against it; its severity is "
                 "deliberately unchanged, because a measurement only moves when "
                 "someone re-measures."),
    }
