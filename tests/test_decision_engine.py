"""Tests for the Review-2 decision engine.

The engine's central claim is that it is deterministic and that safety cannot be
weighted away. Both are claims a reviewer is entitled to see tested rather than
described, so these assert the properties directly: identical inputs give
identical weights, weights always sum to 1, a blocking constraint survives any
weight distribution, and unavailable data never becomes 0.
"""

from __future__ import annotations

import pytest

from src.decision.constraints import evaluate
from src.decision.parameters import (PROXY, REAL, _derive, load_features,
                                     parameters_for)
from src.decision.priority import compute
from src.decision.runner import compare, run
from src.decision.scenarios import CATALOGUE, DOMAINS

INLAND = "G07049"      # Thiruvaniyoor — low flood, inland
HIGH_SLIDE = "G07017"  # Malayattoor-Neeleswaram — GSI high landslide


@pytest.fixture(scope="module")
def frame():
    return _derive(load_features())


@pytest.fixture(scope="module")
def units(frame):
    return list(frame.admin_id)


# --- determinism -----------------------------------------------------------

def test_weights_are_bit_identical_across_repeated_runs(frame):
    """The whole audit trail is worthless if the same inputs drift."""
    first = compute(INLAND, "public_transport_expansion", frame)["weights"]
    for _ in range(25):
        assert compute(INLAND, "public_transport_expansion", frame)["weights"] == first


def test_no_model_participates_in_the_decision_path(frame):
    result = run(INLAND, "industrial_development", frame=frame, persist=False)
    assert result["determinism"]["llm_involved"] is False


# --- the weight contract ---------------------------------------------------

@pytest.mark.parametrize("scenario_key", sorted(CATALOGUE))
def test_weights_sum_to_exactly_one(frame, scenario_key):
    result = compute(INLAND, scenario_key, frame)
    assert result["weight_sum"] == pytest.approx(1.0, abs=1e-9)
    assert sum(result["weights"].values()) == pytest.approx(1.0, abs=1e-9)


def test_weights_sum_to_one_for_every_unit_and_scenario(frame, units):
    """Property test across the whole district, not just one convenient unit."""
    for admin_id in units[:30]:
        for scenario_key in ("public_transport_expansion", "industrial_development",
                             "flood_resilient_development"):
            total = compute(admin_id, scenario_key, frame)["weight_sum"]
            assert total == pytest.approx(1.0, abs=1e-9), (admin_id, scenario_key, total)


def test_every_domain_is_weighted_and_ranked(frame):
    result = compute(INLAND, "affordable_housing", frame)
    assert set(result["weights"]) == set(DOMAINS)
    # Derived from DOMAINS rather than written out, so adding a domain does not
    # fail a test that is still telling the truth.
    assert sorted(result["detail"][d]["rank"] for d in DOMAINS) == list(range(1, len(DOMAINS) + 1))


# --- scenario sensitivity --------------------------------------------------

def test_objective_changes_the_leading_domain_on_identical_data(frame):
    """The central Review-2 proof, as an assertion rather than a slide."""
    transport = compute(INLAND, "public_transport_expansion", frame)
    industrial = compute(INLAND, "industrial_development", frame)
    flood = compute(INLAND, "flood_resilient_development", frame)

    assert transport["ranking"][0] == "transportation"
    assert industrial["ranking"][0] == "economic"
    assert flood["ranking"][0] == "environment"


def test_scenario_shift_is_material_not_cosmetic(frame):
    result = compare(INLAND, "public_transport_expansion", "industrial_development")
    assert result["data_unchanged"] is True
    assert result["leading_domain_changed"] is True
    # transportation should lose a large share when the objective turns industrial
    assert result["weight_shift"]["transportation"] < -0.2


# --- confidence and missing data -------------------------------------------

def test_unavailable_parameters_are_null_never_zero(frame):
    """Wherever a parameter has no data path, it must say so rather than read 0.

    This no longer asserts that *some* parameter is unavailable: closing the six
    district-level gaps left the well-covered units with a full set. The
    invariant being protected was never "gaps exist" but "a gap is null" — so it
    is asserted across every unit, and the one unit with genuinely no road
    network is used to prove the branch is still reachable.
    """
    seen_unavailable = 0
    for admin_id in frame.admin_id:
        for prm in parameters_for(admin_id, frame):
            if prm.status != "unavailable":
                continue
            seen_unavailable += 1
            assert prm.value is None, f"{admin_id}/{prm.name}"
            assert prm.normalized is None, f"{admin_id}/{prm.name}"
            assert prm.confidence == 0.0, f"{admin_id}/{prm.name}"
            assert prm.note, f"{admin_id}/{prm.name} must say why"
    assert seen_unavailable, "the unavailable branch is unreachable — check the fixtures"


def test_closed_gaps_report_a_value_and_a_source(frame):
    """The six parameters that used to be unavailable everywhere now carry data.

    Two became real measurements (the KMRL station list and the OSM water layer
    were on disk but unjoined); four are anchored estimates. An estimate is only
    acceptable here if it names what it was derived from, so the note is part of
    the assertion, not decoration.
    """
    closed = {"water_body_proximity": REAL, "metro_access": REAL,
              "right_of_way_constraint": PROXY, "msme_presence": PROXY,
              "investment_potential": PROXY, "employment_potential": PROXY}
    params = {p.name: p for p in parameters_for(INLAND, frame)}
    for name, expected_status in closed.items():
        prm = params[name]
        assert prm.status == expected_status, f"{name} is {prm.status}"
        assert prm.value is not None, name
        assert prm.normalized is not None, name
        assert prm.source and prm.source_level in (1, 2, 3, 4), name
        assert prm.note, name


def test_an_estimate_never_outranks_a_current_measurement(frame):
    """Allocated estimates sit below a current measurement of the same unit.

    Deliberately scoped to *current* measurements. A parameter carrying the
    stale-feed penalty falls to 0.42, below these estimates at 0.55, and that
    ordering is intended: a 2022 transit feed is a worse guide to 2026 service
    than a published district total shared out by a stated rule.
    """
    from src.decision.parameters import ALLOCATION_PENALTY, LEVEL_CONFIDENCE

    params = parameters_for(INLAND, frame)
    expected = LEVEL_CONFIDENCE[1] * ALLOCATION_PENALTY
    # Identified by the confidence signature the allocation penalty leaves,
    # not by wording in the source label. The label is display text and has
    # changed once already; the penalty is the thing under test.
    allocated = [p for p in params
                 if p.confidence == pytest.approx(expected) and p.status == PROXY]
    assert allocated, "expected the anchored economic estimates"

    for prm in allocated:
        assert prm.confidence == pytest.approx(expected), prm.name
    # The structural claim: an allocated estimate ranks under an unpenalised
    # Level-3 measurement and over a Level-4 secondary source.
    assert LEVEL_CONFIDENCE[4] < expected < LEVEL_CONFIDENCE[3]


def test_every_parameter_carries_provenance(frame):
    for prm in parameters_for(INLAND, frame):
        if prm.status == "unavailable":
            continue
        assert prm.source, prm.name
        assert prm.source_level in (1, 2, 3, 4), prm.name
        assert prm.data_year, prm.name


def test_stale_feed_reduces_transit_confidence(frame):
    """A 43-month-old GTFS feed must not carry the same weight as fresh data."""
    params = {p.name: p for p in parameters_for(INLAND, frame)}
    transit = params["transit_availability"]
    boundary = params["implementation_area"]      # same source level 3, not stale
    assert transit.source_level == boundary.source_level == 3
    assert transit.confidence < boundary.confidence


def test_unavailable_parameters_are_skipped_not_scored(frame):
    detail = compute(INLAND, "industrial_development", frame)["detail"]
    for domain, info in detail.items():
        for used in info["parameters_used"]:
            assert used["status"] != "unavailable"


# --- hard constraints ------------------------------------------------------

def test_blocking_constraint_is_not_overridable_by_a_model(frame):
    result = evaluate(HIGH_SLIDE, "industrial_development", frame)
    blocks = [c for c in result["constraints"] if c["severity"] == "block"]
    assert blocks, "expected a block on high landslide susceptibility"
    for constraint in blocks:
        assert constraint["overridable_by_model"] is False


def test_block_survives_an_unfavourable_weight_distribution(frame):
    """Environment can hold the smallest weight and still stop the scenario."""
    result = run(HIGH_SLIDE, "industrial_development", frame=frame, persist=False)
    assert result["decision"]["blocked"] is True
    assert result["decision"]["stance"] == "not_permitted"
    # the block holds regardless of where environment ranks
    assert result["constraints"]["blocking_count"] >= 1


def test_same_hazard_different_objective_changes_permission(frame):
    """Siting into a hazard is blocked; mitigating it is not."""
    siting = evaluate(HIGH_SLIDE, "industrial_development", frame)
    mitigation = evaluate(HIGH_SLIDE, "flood_resilient_development", frame)
    assert siting["verdict"] == "blocked"
    assert mitigation["verdict"] != "blocked"


def test_constraints_carry_measured_value_and_threshold(frame):
    for constraint in evaluate(HIGH_SLIDE, "industrial_development", frame)["constraints"]:
        assert constraint["threshold"] is not None
        assert constraint["parameter"]
        if constraint["severity"] != "advisory":
            assert constraint["measured_value"] is not None


# --- runner ----------------------------------------------------------------

def test_run_is_self_describing(frame):
    result = run(INLAND, "urban_service_expansion", frame=frame, persist=False)
    for key in ("run_id", "admin_id", "admin_name", "scenario", "decision",
                "priority", "constraints", "determinism"):
        assert key in result
    assert result["decision"]["stance"] in {
        "recommended", "recommended_with_conditions", "not_recommended", "not_permitted"}


def test_unknown_admin_id_raises_rather_than_guessing(frame):
    with pytest.raises(KeyError):
        run("NOT-A-REAL-ID", "industrial_development", frame=frame, persist=False)


def test_unknown_scenario_raises(frame):
    with pytest.raises(KeyError):
        compute(INLAND, "no_such_scenario", frame)


# --- free-text idea interpretation -----------------------------------------

def test_idea_interpretation_is_deterministic(frame):
    """Same words must always be read the same way, or the audit trail lies."""
    from src.decision.interpret import interpret
    names = dict(zip(frame.admin_id, frame.name))
    text = "Extend feeder bus service to Kuttampuzha, it has no public transport"
    first = interpret(text, names)
    for _ in range(5):
        again = interpret(text, names)
        assert again.scenario_key == first.scenario_key
        assert again.match_score == first.match_score
        assert again.target_admin_id == first.target_admin_id


@pytest.mark.parametrize("text,expected", [
    ("Extend feeder bus service, there is no public transport", "public_transport_expansion"),
    ("Build an industrial park with MSME units to create jobs", "industrial_development"),
    ("Protect the area from monsoon flooding with drainage works",
     "flood_resilient_development"),
    ("Build affordable homes for low income families", "affordable_housing"),
])
def test_ideas_classify_to_the_right_objective(frame, text, expected):
    from src.decision.interpret import interpret
    reading = interpret(text, dict(zip(frame.admin_id, frame.name)))
    assert reading.scenario_key == expected
    assert reading.confident


def test_an_unrelated_idea_is_refused_not_forced(frame):
    """Better to say 'no match' than to classify a moon base as housing."""
    from src.decision.interpret import interpret
    reading = interpret("Set up a space research centre on the moon",
                        dict(zip(frame.admin_id, frame.name)))
    assert reading.scenario_key is None
    assert reading.confident is False
    assert "does not resemble" in reading.note


def test_place_and_budget_are_extracted_from_the_sentence(frame):
    from src.decision.interpret import interpret
    names = dict(zip(frame.admin_id, frame.name))
    reading = interpret("Protect Chellanam from flooding, budget Rs 40 crore", names)
    assert reading.target_name.lower().startswith("chellanam")
    assert reading.budget_inr_crore == 40.0


def test_partial_compound_place_name_resolves(frame):
    """People say 'Malayattoor', the register says 'Malayattoor-Neeleswaram'."""
    from src.decision.interpret import interpret
    reading = interpret("industrial park near Malayattoor",
                        dict(zip(frame.admin_id, frame.name)))
    assert reading.target_admin_id is not None
    assert "partial_name_token" in reading.target_method


def test_interpretation_never_sets_the_weights(frame):
    """The reading picks the objective; the data still decides the weights."""
    from src.decision.interpret import interpret
    names = dict(zip(frame.admin_id, frame.name))
    reading = interpret("Extend bus service to Kuttampuzha", names)
    from_text = compute(reading.target_admin_id, reading.scenario_key, frame)["weights"]
    from_menu = compute(reading.target_admin_id, "public_transport_expansion", frame)["weights"]
    assert from_text == from_menu


# --- multi-area proposal evaluation ----------------------------------------
# These exercise the path the website actually uses: free text, a chosen set of
# local bodies and a budget, with no scenario selected by the user. They run
# with with_agents=False so they stay deterministic and need no model.

def test_proposal_evaluates_every_selected_area(frame):
    from src.decision.proposal import evaluate_proposal
    out = evaluate_proposal("MSME industrial park with an effluent plant, 900 jobs",
                            [INLAND, HIGH_SLIDE], budget_inr_crore=120,
                            frame=frame, persist=False, with_agents=False)
    assert [a["admin_id"] for a in out["areas"]] == [INLAND, HIGH_SLIDE]
    assert out["budget_inr_crore"] == 120
    assert out["agents"] is None and out["supervisor"] is None


def test_proposal_blended_weights_sum_to_one(frame):
    from src.decision.proposal import evaluate_proposal
    out = evaluate_proposal("Industrial park for MSME jobs", [INLAND, HIGH_SLIDE],
                            frame=frame, persist=False, with_agents=False)
    assert round(sum(out["weights"].values()), 4) == 1.0
    assert set(out["weights"]) == set(DOMAINS)


def test_proposal_blend_follows_population(frame):
    """A large body and a small one must not count equally."""
    from src.decision.proposal import _blend_weights
    # Full seven-domain dicts, which is the shape compute() actually returns.
    big = dict.fromkeys(DOMAINS, 0.0) | {
        "economic": 0.6, "infrastructure": 0.2, "transportation": 0.1, "environment": 0.1}
    small = dict.fromkeys(DOMAINS, 0.0) | {
        "economic": 0.1, "infrastructure": 0.1, "transportation": 0.2, "environment": 0.6}
    blended = _blend_weights([{"population": 500000, "weights": big},
                              {"population": 5000, "weights": small}])
    assert blended["economic"] > blended["environment"]
    assert round(sum(blended.values()), 4) == 1.0


def test_proposal_keeps_a_blocking_constraint_per_area(frame):
    """Selecting a safe area alongside a blocked one must not rescue the blocked one."""
    from src.decision.proposal import evaluate_proposal
    out = evaluate_proposal("Industrial park with an effluent treatment plant",
                            [INLAND, HIGH_SLIDE], frame=frame, persist=False,
                            with_agents=False)
    blocked = {a["admin_name"] for a in out["areas"] if a["blocked"]}
    assert blocked == set(out["blocked_areas"])
    assert any(a["blocked"] for a in out["areas"] if a["admin_id"] == HIGH_SLIDE)
    assert not any(a["blocked"] for a in out["areas"] if a["admin_id"] == INLAND)
    assert all(c["overridable_by_model"] is False for c in out["constraints"])


def test_proposal_reads_a_budget_out_of_the_text(frame):
    from src.decision.proposal import evaluate_proposal
    out = evaluate_proposal("Industrial park for MSME jobs, budget Rs 120 crore",
                            [INLAND], frame=frame, persist=False, with_agents=False)
    assert out["budget_inr_crore"] == 120


def test_proposal_weights_match_the_menu_driven_path(frame):
    """The user's words only choose the relevance row; the data sets the weights."""
    from src.decision.proposal import evaluate_proposal
    out = evaluate_proposal("Set up an MSME industrial park for manufacturing jobs",
                            [INLAND], frame=frame, persist=False, with_agents=False)
    assert out["weight_basis"]["scenario_key"] == "industrial_development"
    assert out["weights"] == compute(INLAND, "industrial_development", frame)["weights"]


def test_proposal_refuses_an_empty_selection(frame):
    from src.decision.proposal import evaluate_proposal
    with pytest.raises(ValueError):
        evaluate_proposal("An industrial park", [], frame=frame, persist=False,
                          with_agents=False)
    with pytest.raises(ValueError):
        evaluate_proposal("   ", [INLAND], frame=frame, persist=False, with_agents=False)
    with pytest.raises(KeyError):
        evaluate_proposal("An industrial park", ["NOPE"], frame=frame, persist=False,
                          with_agents=False)


def test_proposal_is_deterministic_without_agents(frame):
    from src.decision.proposal import evaluate_proposal
    kwargs = dict(frame=frame, persist=False, with_agents=False)
    text = "Flood protection works for the delta villages, Rs 40 crore"
    a = evaluate_proposal(text, [INLAND, HIGH_SLIDE], **kwargs)
    b = evaluate_proposal(text, [INLAND, HIGH_SLIDE], **kwargs)
    assert a["weights"] == b["weights"]
    assert a["areas"] == b["areas"]
    assert a["conflicts"] == b["conflicts"]


def test_proposal_text_reaches_the_agent_brief(frame):
    """The brief must carry the user's own words, not the catalogue description."""
    from src.decision.proposal import _brief, _evaluate_units, _blend_weights
    text = "A shared effluent treatment plant and a feeder bus to Angamaly station"
    units = _evaluate_units([HIGH_SLIDE], "industrial_development", frame)
    brief = _brief(text, units, _blend_weights(units), 120)
    assert text in brief
    assert CATALOGUE["industrial_development"].description not in brief
    assert "INR 120 crore" in brief
    assert "cannot clear these" in brief.lower()


def test_the_brief_carries_parameters_the_agents_cannot_otherwise_reach(frame):
    """Feature-layer parameters must reach the agents, not just the weights.

    The agents gather their twin facts from PostGIS, which holds the Phase-1
    tables. The metro distance, water setback, right-of-way situation and the
    allocated enterprise base live in the feature layers instead, so without
    them in the brief they would drive the weights while staying invisible to
    the four analysts weighing the proposal.
    """
    from src.decision.proposal import _blend_weights, _brief, _evaluate_units

    units = _evaluate_units([INLAND], "industrial_development", frame)
    brief = _brief("An MSME park", units, _blend_weights(units), 120)
    for name in ("metro_access", "water_body_proximity", "msme_presence"):
        assert name in brief, name
    # Rendered for a reader, not as a bare normalised score.
    assert "km to the nearest metro station" in brief
    assert "registered enterprises" in brief


def test_the_brief_marks_an_estimate_as_an_estimate(frame):
    """An allocated figure must never reach an agent looking like a measurement."""
    from src.decision.proposal import _evaluate_units, _parameter_lines

    units = _evaluate_units([INLAND], "industrial_development", frame)
    lines = {line.split(":")[0].strip(): line
             for line in _parameter_lines(units[0]["parameters"])}
    assert "ESTIMATED, not measured" in lines["msme_presence"]
    assert "ESTIMATED, not measured" in lines["investment_potential"]
    # And a real measurement is not slandered as an estimate.
    assert "[measured;" in lines["metro_access"]
    assert "[measured;" in lines["water_body_proximity"]


# --- budget-only ideation ---------------------------------------------------
# The deterministic half: needs measured from parameters, and a budget split
# computed from them. Both must be settled before any agent is called, so both
# are testable without a model.

def test_every_parameter_declares_a_need_direction(frame):
    """A missing direction would silently read an asset as a deficit.

    This is the failure that would have the system proposing flood defences for
    the driest panchayat in the district, so the mapping is asserted complete
    rather than assumed.
    """
    from src.decision.needs import DIRECTION

    for admin_id in (INLAND, HIGH_SLIDE):
        for prm in parameters_for(admin_id, frame):
            assert prm.name in DIRECTION, f"{prm.name} has no declared direction"
    assert set(DIRECTION.values()) <= {"deficit", "asset", "scale"}


def test_need_severity_points_the_right_way(frame):
    """A hazard and an asset at the same percentile must score oppositely."""
    from src.decision.needs import needs_for

    slide = {n["parameter"]: n for n in
             [x.as_dict() for x in needs_for(HIGH_SLIDE, "Malayattoor", frame)]}
    # GSI puts this unit in the high-landslide class, so hazard_exposure is a
    # severe need; it is an outright blocking constraint elsewhere in the engine.
    assert slide["hazard_exposure"]["severity"] > 0.8
    # road_connectivity is an asset: this unit is poorly served, so the need is
    # high even though its normalised percentile is low.
    assert slide["road_connectivity"]["severity"] > 0.8
    assert parameters_for(HIGH_SLIDE, frame)
    norms = {p.name: p.normalized for p in parameters_for(HIGH_SLIDE, frame)}
    assert norms["road_connectivity"] < 0.2, "expected a low percentile for the asset"


def test_needs_never_rest_on_an_unavailable_parameter(frame):
    from src.decision.needs import assess

    out = assess([INLAND, HIGH_SLIDE], frame)
    for need in out["ranked"]:
        assert need["status"] in ("real", "proxy")
        assert need["value"] is not None
        assert need["severity"] >= out["report_floor"]


def test_budget_split_follows_measured_need_and_sums_to_the_budget(frame):
    from src.decision.ideate import _budget_split
    from src.decision.needs import assess

    pressure = assess([INLAND, HIGH_SLIDE], frame)["domain_pressure"]
    split = _budget_split(pressure, 120.0)
    assert set(split) == set(DOMAINS)
    assert sum(v["share"] for v in split.values()) == pytest.approx(1.0, abs=0.001)
    assert sum(v["envelope_inr_crore"] for v in split.values()) == pytest.approx(120.0, abs=0.05)
    # The domain under most measured pressure gets the largest envelope.
    worst = max(pressure, key=pressure.get)
    assert split[worst]["share"] == max(v["share"] for v in split.values())


def test_a_domain_with_no_measured_need_still_gets_a_floor_share(frame):
    from src.decision.ideate import FLOOR_SHARE, _budget_split

    split = _budget_split({"environment": 0.95}, 100.0)
    for domain in ("economic", "infrastructure", "transportation"):
        assert split[domain]["share"] > 0, domain
        assert "floor share" in split[domain]["basis"], domain
    assert FLOOR_SHARE < split["environment"]["share"]


def test_hazard_blocks_are_computed_without_a_user_scenario(frame):
    """The block comes from GSI and KSDMA data, not from what was proposed."""
    from src.decision.ideate import _hazard_blocks

    blocks = _hazard_blocks([INLAND, HIGH_SLIDE], frame)
    blocked = {b["admin_id"] for b in blocks}
    assert HIGH_SLIDE in blocked
    assert INLAND not in blocked
    for block in blocks:
        assert block["overridable_by_model"] is False
        # A block states both what it forbids and what it still allows; see
        # test_a_hazard_block_permits_the_remedy_it_implies for why.
        assert block["prohibits"] and block["permits_with_assessment"]


def test_ideation_refuses_a_bad_budget_or_empty_selection(frame):
    from src.decision.ideate import generate

    for bad in (0, -5, None, "lots"):
        with pytest.raises(ValueError):
            generate([INLAND], bad, frame=frame, persist=False)
    with pytest.raises(ValueError):
        generate([], 120, frame=frame, persist=False)
    with pytest.raises(KeyError):
        generate(["NOPE"], 120, frame=frame, persist=False)


def test_the_domain_brief_carries_the_envelope_needs_and_blocks(frame):
    """An agent must not have to guess its budget or invent a mandate."""
    from src.decision.ideate import _domain_brief, _hazard_blocks
    from src.decision.needs import assess

    needs = assess([HIGH_SLIDE], frame)
    blocks = _hazard_blocks([HIGH_SLIDE], frame)
    areas = [{"admin_name": "Malayattoor-Neeleswaram Gramapanchayath",
              "population": None}]
    brief = _domain_brief("environment", needs["by_domain"]["environment"],
                          32.96, areas, blocks)
    assert "INR 32.96 crore" in brief
    assert "PROPOSE interventions" in brief
    assert "landslide" in brief.lower()
    assert "PROHIBITED there:" in brief and "STILL PERMITTED there:" in brief
    assert "Do not invent a figure" in brief


def test_a_hazard_block_permits_the_remedy_it_implies(frame):
    """A block must stop new exposure, not stop the exposure being reduced.

    The first version of this said "any permanent siting or construction",
    which read literally forbids landslide mitigation in a landslide area — so
    a hazardous place could never be made safer, and the agents duly proposed
    road widening in a blocked unit because nothing told them the difference.
    """
    from src.decision.ideate import (BLOCK_PERMITS, BLOCK_PROHIBITS,
                                     _domain_brief, _hazard_blocks)
    from src.decision.needs import assess

    blocks = _hazard_blocks([HIGH_SLIDE], frame)
    assert blocks
    for block in blocks:
        assert "new permanent facilities" in block["prohibits"]
        assert "mitigation" in block["permits_with_assessment"]
        assert block["overridable_by_model"] is False

    needs = assess([HIGH_SLIDE], frame)
    brief = _domain_brief("environment", needs["by_domain"]["environment"],
                          32.0, [{"admin_name": "Malayattoor", "population": None}],
                          blocks)
    # Both halves must reach the agent, or it cannot tell them apart.
    assert BLOCK_PROHIBITS in brief
    assert BLOCK_PERMITS in brief
    assert "mitigation work in a blocked area is legitimate" in brief.lower()


def test_the_agent_brief_asks_for_the_envelope_to_be_used(frame):
    """Nothing in the first version said to spend the money.

    A live run proposed INR 3.5 crore of transport work against a INR 26.87
    crore envelope and the Supervisor left INR 93.5 crore of INR 120 unspent,
    because fitting inside an envelope was the only stated requirement.
    """
    from src.decision.ideate import _domain_brief
    from src.decision.needs import assess

    needs = assess([HIGH_SLIDE], frame)
    brief = _domain_brief("transportation", needs["by_domain"].get("transportation", []),
                          26.87, [{"admin_name": "Malayattoor", "population": None}], [])
    assert "should total close to INR 26.87 crore" in brief
    assert "an unspent crore buys nothing" in brief
    assert "cannot absorb the envelope" in brief


# --- the seven-domain engine -----------------------------------------------
# Healthcare, Education and Budget were added after the original four. These
# pin the things that silently break when a domain is added: a relevance row
# that forgets one, a DOMAINS copy that drifts, a need direction left undeclared.

def test_every_scenario_scores_every_domain(frame):
    """A missing relevance entry drops a domain out of the weighting silently."""
    for key, scenario in CATALOGUE.items():
        missing = set(DOMAINS) - set(scenario.relevance)
        assert not missing, f"{key} has no relevance for {missing}"
        for domain, value in scenario.relevance.items():
            assert 0.0 <= value <= 1.0, f"{key}/{domain} = {value}"


def test_the_domain_list_has_one_source_of_truth():
    """Three modules used to keep their own copy. Adding a domain broke two."""
    from src.decision.ideate import DOMAINS as ideate_domains
    from src.decision.proposal import DOMAINS as proposal_domains

    assert ideate_domains is DOMAINS
    assert proposal_domains is DOMAINS
    assert len(DOMAINS) == 7


def test_there_is_an_agent_for_every_domain():
    from src.agents.supervisor_agent import DOMAIN_AGENTS

    assert set(DOMAIN_AGENTS) == set(DOMAINS)
    for domain, module in DOMAIN_AGENTS.items():
        assert module.SPEC.name == domain
        assert module.OUTPUT_SCHEMA, domain
        assert callable(module.run), domain


def test_weights_still_sum_to_one_across_seven_domains(frame):
    for admin_id in list(frame.admin_id)[:12]:
        for key in CATALOGUE:
            weights = compute(admin_id, key, frame)["weights"]
            assert set(weights) == set(DOMAINS)
            assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)


def test_budget_relevance_is_present_but_never_dominant():
    """Money shapes what is affordable. It does not decide what is needed."""
    for key, scenario in CATALOGUE.items():
        budget = scenario.relevance["budget"]
        assert budget > 0, f"{key} ignores cost entirely"
        assert budget < max(scenario.relevance.values()), f"{key} is led by budget"


def test_the_fiscal_entitlement_implements_the_published_formula(frame):
    """80% population, 10% area, 10% inverse own income, and it sums to 1."""
    from src.features.anchors import DEVOLUTION_FORMULA

    assert DEVOLUTION_FORMULA.value == 0.80
    shares = frame.fiscal_share.dropna()
    assert len(shares) > 90
    assert shares.sum() == pytest.approx(1.0, abs=1e-6), "shares must partition the grant"
    assert (shares > 0).all()

    # Population dominates, so the largest local body takes the largest share.
    biggest = frame.loc[frame.population.idxmax()]
    assert biggest.fiscal_share == frame.fiscal_share.max()


def test_budget_parameters_are_available_and_labelled_proxy(frame):
    params = {p.name: p for p in parameters_for(INLAND, frame)}
    for name in ("fiscal_entitlement", "own_income_capacity", "cost_exposure"):
        prm = params[name]
        assert prm.domain == "budget"
        assert prm.value is not None, name
        # The formula is Level 1 but one of its three terms is substituted, so
        # none of these may claim to be a measurement.
        assert prm.status == PROXY, name
    assert "80% population" in params["fiscal_entitlement"].note


def test_new_domains_declare_their_need_direction():
    from src.decision.needs import DIRECTION, PHRASING

    for name in ("health_access", "health_capacity", "health_deficit",
                 "education_access", "education_capacity", "education_deficit",
                 "fiscal_entitlement", "own_income_capacity", "cost_exposure"):
        assert name in DIRECTION, name
        assert name in PHRASING, name
    # Cost is the one that points the other way: dear ground is a deficit.
    assert DIRECTION["cost_exposure"] == "deficit"
    assert DIRECTION["fiscal_entitlement"] == "asset"


def test_budget_splits_across_seven_domains(frame):
    from src.decision.ideate import _budget_split
    from src.decision.needs import assess

    pressure = assess([INLAND, HIGH_SLIDE], frame)["domain_pressure"]
    split = _budget_split(pressure, 140.0)
    assert set(split) == set(DOMAINS)
    assert sum(v["share"] for v in split.values()) == pytest.approx(1.0, abs=0.001)
    assert sum(v["envelope_inr_crore"] for v in split.values()) == pytest.approx(140.0, abs=0.07)


def test_rank_parameters_say_they_are_ranks(frame):
    """A live run had the Budget agent read a 0.906 percentile as "90.6% dearer".

    The reading was right and the label was not. Any 0-to-1 parameter an agent
    can see has to say what the number is, because a model handed a bare decimal
    will reach for the most familiar interpretation and that is a percentage.
    """
    params = {p.name: p for p in parameters_for(INLAND, frame)}
    note = params["cost_exposure"].note
    assert "PERCENTILE RANK" in note
    assert "not a percentage" in note.lower()
    assert 0.0 <= params["cost_exposure"].value <= 1.0

    from src.agents.budget_agent import GUIDANCE
    assert "PERCENTILE RANK" in GUIDANCE
    assert "never convert it" in GUIDANCE


# --- budget contradictions --------------------------------------------------
# A live run on a INR 500 crore budget selected INR 31.78 crore and deferred
# eight items "due to budget constraints" while holding 94% of the money. The
# reasoning was self-contradictory and the page reported it as if it were a
# finding. These pin the detection.

def test_a_deferral_blamed_on_budget_with_money_left_is_flagged():
    from src.decision.ideate import _portfolio

    # Drive the checker directly with a payload shaped like the failing run.
    class FakeBackend:
        kind, model = "test", "test"

    payload = {
        "portfolio": [{"title": "A", "est_cost_inr_crore": 31.78}],
        "deferred": [
            {"title": "Flood risk reduction", "domain": "environment",
             "why_deferred": "Not selected due to budget constraints."},
            {"title": "Bus frequency", "domain": "transportation",
             "why_deferred": "Not selected due to budget constraints."},
            {"title": "Sequenced later", "domain": "infrastructure",
             "why_deferred": "Depends on the drainage works completing first."},
        ],
    }
    def fake_complete_json(system, user, backend):
        return dict(payload)

    import src.agents.llm as llm
    saved = llm.complete_json
    llm.complete_json = fake_complete_json
    proposals = {"environment": {"available": True, "ideas": [
        {"title": "Flood risk reduction", "est_cost_inr_crore": 20.0}]}}
    try:
        out = _portfolio(proposals, {}, {"ranked": []}, [], 500.0, [],
                         FakeBackend(), [])
    finally:
        llm.complete_json = saved

    check = out["budget_check"]
    assert check["headroom_inr_crore"] == pytest.approx(468.22)
    flagged = {c["title"] for c in check["contradictory_deferrals"]}
    assert flagged == {"Flood risk reduction", "Bus frequency"}
    # A real, non-budget reason must not be flagged.
    assert "Sequenced later" not in flagged


def test_budget_check_separates_unproposed_from_unselected():
    """Money nobody proposed work for is a different failure from money the
    Supervisor declined to spend, and they have different fixes."""
    import src.agents.llm as llm
    from src.decision.ideate import _portfolio

    class FakeBackend:
        kind, model = "test", "test"

    offered = {"economic": [{"title": "X", "est_cost_inr_crore": 100.0}]}
    saved = llm.complete_json
    llm.complete_json = lambda s, u, b: {
        "portfolio": [{"title": "X", "est_cost_inr_crore": 40.0}], "deferred": []}
    try:
        out = _portfolio({"economic": {"available": True, "ideas": offered["economic"]}},
                         {}, {"ranked": []}, [], 500.0, [], FakeBackend(), [])
    finally:
        llm.complete_json = saved

    check = out["budget_check"]
    assert check["selected_cost_inr_crore"] == 40.0
    assert check["total_offered_by_agents_inr_crore"] == 100.0
    assert check["unproposed_inr_crore"] == 400.0   # never proposed for
    assert check["headroom_inr_crore"] == 460.0     # not selected
    assert check["agents_absorbed_share"] == pytest.approx(0.2)


def test_the_agent_brief_asks_for_work_scaled_to_the_envelope():
    """Agents handed INR 71 crore proposed INR 5 crore items. Fitting inside an
    envelope was all the brief ever asked for."""
    from src.decision.ideate import _domain_brief

    brief = _domain_brief("healthcare", [], 71.4,
                          [{"admin_name": "Thuravoor", "population": 20000}], [])
    assert "should total close to INR 71.40 crore" in brief
    assert "Scale the WORK to the envelope" in brief
    assert "not for three small pilots" in brief


def test_an_agent_cannot_claim_an_envelope_it_did_not_use():
    """A live run had the economic agent propose INR 1.5 crore against INR 64
    crore and report that this "makes substantial use of the envelope"."""
    from src.decision.ideate import SUBSTANTIAL_USE, _check_envelope_claims

    proposals = {
        "economic": {"available": True, "ideas": [{"title": "A", "est_cost_inr_crore": 1.5}],
                     "envelope_use": "The proposed intervention makes substantial use of the envelope."},
        "infrastructure": {"available": True, "ideas": [{"title": "B", "est_cost_inr_crore": 8.0}],
                           "envelope_use": "The proposed cost does not make substantial use of the envelope."},
        "transportation": {"available": True, "ideas": [{"title": "C", "est_cost_inr_crore": 78.41}],
                           "envelope_use": "Fills it."},
    }
    split = {"economic": {"envelope_inr_crore": 64.05},
             "infrastructure": {"envelope_inr_crore": 72.69},
             "transportation": {"envelope_inr_crore": 78.41}}

    flagged = _check_envelope_claims(proposals, split)
    assert [f["domain"] for f in flagged] == ["economic"]

    # An agent that admits the shortfall is recorded but not accused.
    assert proposals["infrastructure"]["envelope_shortfall"]["claimed_substantial_use"] is False
    assert "acknowledged" in proposals["infrastructure"]["envelope_shortfall"]["note"]
    # An agent that filled its envelope gets no shortfall block at all.
    assert "envelope_shortfall" not in proposals["transportation"]
    assert proposals["transportation"]["envelope_share"] >= SUBSTANTIAL_USE


def test_proposed_work_cannot_silently_disappear():
    """Three items worth INR 41 crore were offered, never selected and never
    deferred. Work does not get to vanish without a reason."""
    import src.agents.llm as llm
    from src.decision.ideate import _portfolio

    class FakeBackend:
        kind, model = "test", "test"

    proposals = {"environment": {"available": True, "ideas": [
        {"title": "Selected one", "est_cost_inr_crore": 10.0},
        {"title": "Deferred one", "est_cost_inr_crore": 5.0},
        {"title": "Vanished one", "est_cost_inr_crore": 14.5}]}}
    saved = llm.complete_json
    llm.complete_json = lambda s, u, b: {
        "portfolio": [{"title": "Selected one", "est_cost_inr_crore": 10.0}],
        "deferred": [{"title": "Deferred one", "domain": "environment",
                      "why_deferred": "Depends on the drainage works."}]}
    try:
        out = _portfolio(proposals, {}, {"ranked": []}, [], 500.0, [], FakeBackend(), [])
    finally:
        llm.complete_json = saved

    unaccounted = out["budget_check"]["unaccounted_proposals"]
    assert [u["title"] for u in unaccounted] == ["Vanished one"]
    assert unaccounted[0]["est_cost_inr_crore"] == 14.5
