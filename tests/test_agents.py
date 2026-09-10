"""Smoke tests for the four domain agents against the loaded demo dataset.

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
ADMIN_ID = "DEMO-P-02"

# Same demo-edition guard as tests/test_digital_twin.py.
_loaded = get_context(ADMIN_ID)
pytestmark = pytest.mark.skipif(
    _loaded.get("dataset_edition") != "demo",
    reason=f"stores hold the {_loaded.get('dataset_edition')!r} edition; these assert demo facts",
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

    assert facts["DEMO-FZ-01.risk_level"]["value"] == "high"
    assert facts["DEMO-FZ-01.risk_level"]["data_year"] is not None

    zones = result["analysis"].get("affected_flood_zones") or []
    rendered = str(zones) + str(result["analysis"])
    assert "DEMO-FZ-01" in rendered, "environment agent did not name the flood zone"
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
    assert facts["figures_inherited_from"]["value"] == "DEMO-D-01"
    assert facts["figures_inherited_from"]["match_confidence"] == "unmatched-estimate"


def test_transportation_agent_sees_both_metro_stations(results):
    labels = {f["label"] for f in results["transportation"]["evidence"]["twin_facts"]}
    assert "DEMO-MS-01.name" in labels
    assert "DEMO-MS-02.name" in labels
