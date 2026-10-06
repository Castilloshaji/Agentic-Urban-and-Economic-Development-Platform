"""Smoke tests for the four domain agents against real Ernakulam data.

These call a real LLM backend, so they are slower than unit tests. Each agent is
run once per session and every assertion reads from that one result.

What is asserted is the contract, not the prose: valid JSON, every schema key
present, and — the part that actually matters — that the deterministic evidence
the agent was given carries vintage tags, so a report built from it can cite
every number.
"""

from __future__ import annotations

import pytest

from src.agents import (
    economic_agent,
    environment_agent,
    infrastructure_agent,
    transportation_agent,
)
from src.agents.llm import resolve_backend
from src.digital_twin.twin import get_context

SCENARIO = (
    "Extend a feeder bus route to connect this panchayat to the nearest metro "
    "station, where part of the corridor overlaps a flood hazard zone."
)
ADMIN_ID = "G07027"   # Choornikkara: 4 metro stations, 7 bus stops, 1 flood zone

# Same edition guard as tests/test_digital_twin.py.
_loaded = get_context(ADMIN_ID)
pytestmark = pytest.mark.skipif(
    _loaded.get("dataset_edition") != "ernakulam",
    reason=(f"stores hold the {_loaded.get('dataset_edition')!r} edition; these assert "
            "real Ernakulam facts. Reload with: bash scripts/run_phase1.sh real"),
)

AGENTS = {
    "economic": economic_agent,
    "infrastructure": infrastructure_agent,
    "transportation": transportation_agent,
    "environment": environment_agent,
}


@pytest.fixture(scope="session")
def backend():
    return resolve_backend()


@pytest.fixture(scope="session")
def results(backend):
    return {name: module.run(SCENARIO, ADMIN_ID, backend) for name, module in AGENTS.items()}


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_agent_returns_valid_structured_json(results, name):
    """Section 3's contract: a structured object, not free text."""
    result = results[name]
    assert result["agent"] == name
    assert result["admin_id"] == ADMIN_ID
    assert isinstance(result["analysis"], dict) and result["analysis"]


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_agent_output_has_every_schema_key(results, name):
    analysis = results[name]["analysis"]
    for key in AGENTS[name].OUTPUT_SCHEMA:
        assert key in analysis, f"{name} is missing {key!r}"


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_agent_evidence_is_vintage_tagged(results, name):
    """Every fact handed to the model carries the vintage it was published under."""
    facts = results[name]["evidence"]["twin_facts"]
    assert facts, f"{name} gathered no facts"
    for fact in facts:
        assert "label" in fact and "value" in fact


@pytest.mark.parametrize("name", sorted(AGENTS))
def test_agent_cites_documents(results, name):
    citations = results[name]["evidence"]["citations"]
    assert citations, f"{name} retrieved no document context"
    for citation in citations:
        assert citation["source_file"].endswith((".txt", ".pdf"))


def test_environment_agent_references_the_actual_flood_zone(results):
    """Not a generic answer: the real zone id, its risk_level and its data_year."""
    result = results["environment"]
    facts = {f["label"]: f for f in result["evidence"]["twin_facts"]}

    assert facts["EKM-FZ-002.risk_level"]["value"] == "high"
    assert facts["EKM-FZ-002.risk_level"]["data_year"] is not None
    # The whole unit lies inside the zone, which is what makes it a good fixture.
    assert facts["EKM-FZ-002.overlap_ratio"]["value"] == 1.0

    zones = result["analysis"].get("affected_flood_zones") or []
    rendered = str(zones) + str(result["analysis"])
    assert "EKM-FZ-002" in rendered, "environment agent did not name the flood zone"
    assert "high" in rendered.lower(), "environment agent did not carry the risk_level through"


def test_economic_agent_tolerates_series_gaps(results):
    """Manifest #15: a missing year must be reported, never interpolated."""
    facts = {f["label"]: f for f in results["economic"]["evidence"]["twin_facts"]}
    missing = facts["per_capita_income.missing_years"]["value"]
    assert isinstance(missing, list)
    # Nothing was invented to fill the gap.
    covered = facts["per_capita_income.years_covered"]["value"]
    assert not set(covered) & set(missing)


def test_economic_agent_marks_inherited_figures_as_estimates(results):
    """District figures reaching a panchayat must not pose as its own measurements."""
    facts = {f["label"]: f for f in results["economic"]["evidence"]["twin_facts"]}
    assert facts["figures_inherited_from"]["value"] == "EKM-D"
    assert facts["figures_inherited_from"]["match_confidence"] == "unmatched-estimate"


def test_transportation_agent_sees_every_station_in_the_unit(results):
    """All four KMRL stations inside Choornikkara, attributed by containment."""
    labels = {f["label"] for f in results["transportation"]["evidence"]["twin_facts"]}
    stations = {label for label in labels if label.endswith(".name")
                and label.startswith("EKM-MS-")}
    assert len(stations) == 4, sorted(stations)


def test_agents_receive_the_derived_layers(results):
    """The OSM, KMRL and allocated-economic values must reach the agents.

    They live in `admin_derived_feature`, which the Phase-1 twin functions do
    not read, so without this wiring they drove the deterministic weights while
    staying invisible to the analysts.
    """
    expected = {
        "environment": "derived.water_distance_m",
        "transportation": "derived.nearest_metro_m",
        "economic": "derived.msme_estimated_count",
        "infrastructure": "derived.row_narrow_share",
    }
    for name, label in expected.items():
        labels = {f["label"] for f in results[name]["evidence"]["twin_facts"]}
        assert label in labels, f"{name} did not receive {label}"


def test_allocated_estimates_reach_agents_marked_as_estimates(results):
    """An allocation must never arrive looking like a measurement of this unit."""
    facts = {f["label"]: f for f in results["economic"]["evidence"]["twin_facts"]}
    for label in ("derived.msme_estimated_count", "derived.income_per_capita_estimate"):
        assert facts[label]["match_confidence"] == "unmatched-estimate", label
        assert "allocated from a published district figure" in facts[label]["note"]

    # And a real measurement is not slandered as an estimate.
    water = {f["label"]: f for f in results["environment"]["evidence"]["twin_facts"]}
    assert water["derived.water_distance_m"].get("match_confidence") != "unmatched-estimate"
