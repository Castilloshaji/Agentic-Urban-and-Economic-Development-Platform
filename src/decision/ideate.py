"""Budget in, proposals out: the agents decide what to build, not just whether to.

The rest of the engine answers "is this idea any good". This answers the
question a planner with money and no shortlist actually has: given this budget
and this district, what should we do?

The order is the same discipline as everywhere else in the project, and it
matters more here, because a model asked to invent projects will happily invent
the need for them too:

  1. Needs are measured first (`needs.py`) — ranked shortfalls per domain,
     each with its percentile, its source and its vintage.
  2. The budget is split across domains by measured need pressure, before any
     model runs. An agent is told its envelope; it does not negotiate for one.
  3. Each domain agent proposes interventions against its own shortfalls,
     inside its envelope.
  4. Hazard blocks are computed from the data, not asked about. An area that
     GSI puts in the high-landslide class cannot host permanent siting, and the
     Supervisor is told so as a fact it may not overturn.
  5. The Supervisor assembles a portfolio that fits the budget, and says what
     it dropped and why.

No user prompt is involved anywhere in that chain. The only inputs are the
budget and the selected areas.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .constraints import SITING_SCENARIOS, evaluate
from .needs import assess
from .parameters import _derive, load_features

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT = PROJECT_ROOT / "data" / "derived" / "ideation"

# Single source of truth: scenarios.py. A local copy drifted the moment a
# domain was added, which is exactly what happened when healthcare,
# education and budget arrived.
from .scenarios import DOMAINS  # noqa: F401

# A domain with no measured shortfall still gets a floor share, because "nothing
# is badly wrong here" is not the same as "spend nothing here" — maintenance and
# resilience work is legitimate. It is small, so measured need still drives the
# split.
FLOOR_SHARE = 0.05

# Below this there is no point asking for a proposal: a crore and a half will not
# deliver anything an agent can cost honestly, and it invites invented numbers.
MIN_DOMAIN_BUDGET_CRORE = 2.0

MAX_UNITS = 97   # the whole district is a legitimate selection
MAX_IDEAS_PER_DOMAIN = 3


def _budget_split(pressure: dict[str, float], budget: float) -> dict[str, dict]:
    """Divide the budget across domains by measured need pressure.

    Deliberately arithmetic. The alternative — letting the agents bid — would
    make the allocation a function of how persuasively each one writes, which is
    exactly the failure this architecture exists to prevent.
    """
    raw = {d: max(pressure.get(d, 0.0), FLOOR_SHARE) for d in DOMAINS}
    total = sum(raw.values())
    split = {}
    for domain in DOMAINS:
        share = raw[domain] / total
        split[domain] = {
            "share": round(share, 4),
            "envelope_inr_crore": round(budget * share, 2),
            "need_pressure": round(pressure.get(domain, 0.0), 4),
            "basis": ("measured need pressure"
                      if pressure.get(domain, 0.0) > FLOOR_SHARE
                      else f"floor share ({FLOOR_SHARE:.0%}) — no measured shortfall"),
        }
    return split


# What a hazard block actually forbids, and what it does not.
#
# The first version of this said "any permanent siting or construction", which
# read literally forbids landslide mitigation in a landslide area — it would
# mean a hazardous place can never be made safer, and the agents duly proposed
# road widening in a blocked unit because the rule gave them no way to tell the
# difference. A block exists to stop new exposure being created, not to stop
# the exposure being reduced.
BLOCK_PROHIBITS = (
    "siting new permanent facilities, housing, industrial units or any "
    "development that places people or assets in the hazard zone"
)
BLOCK_PERMITS = (
    "hazard mitigation and remediation works (slope stabilisation, drainage, "
    "flood defence), maintenance of assets that already exist, and essential "
    "access — all of them only with the geotechnical or hydrological "
    "assessment named in the constraint"
)


def _hazard_blocks(admin_ids: list[str], frame) -> list[dict]:
    """Which areas carry a hazard block, computed from the hazard data.

    Scenario-independent on purpose. The blocking rule is driven by GSI
    landslide class and KSDMA flood share, not by the objective, so it can be
    established before anyone has proposed anything. The Supervisor receives
    this as binding.

    Each block states what it prohibits and what it still permits, because a
    block that forbids everything would forbid the remedy too.
    """
    siting_scenario = sorted(SITING_SCENARIOS)[0]
    blocks = []
    for admin_id in admin_ids:
        result = evaluate(admin_id, siting_scenario, frame)
        for constraint in result["constraints"]:
            if constraint["severity"] != "block":
                continue
            row = frame[frame.admin_id == admin_id].iloc[0]
            blocks.append({
                "admin_id": admin_id,
                "admin_name": row["name"],
                "code": constraint["code"],
                "message": constraint["message"],
                "parameter": constraint["parameter"],
                "measured_value": constraint["measured_value"],
                "threshold": constraint["threshold"],
                "source": constraint.get("source"),
                "prohibits": BLOCK_PROHIBITS,
                "permits_with_assessment": BLOCK_PERMITS,
                "overridable_by_model": False,
            })
    return blocks


def _normalise_title(title: str) -> str:
    """For comparing a proposal against the ledger.

    Deliberately crude — lowercase, alphanumeric only. It catches "Flood
    defence in Kalady" against "flood defences, Kalady" without pretending to
    understand either. Anything subtler would start merging genuinely different
    projects, and a false merge silently drops a proposal.
    """
    return "".join(c for c in (title or "").lower() if c.isalnum())


def _targets(value) -> list[str]:
    """Normalise an agent's `addresses` field to a list of parameter names.

    The schema asks for one parameter name and the agents mostly comply, but
    not always — a live run returned a list, which turned `dict.get(value)`
    into `TypeError: unhashable type: 'list'` and failed the whole request with
    a 500. Agent output is untrusted in shape as well as in content, so this
    accepts a string, a list, or nonsense and always returns a clean list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        # A None inside the list must drop out, not become the string "None".
        return [v for v in (str(x).strip() for x in value if x is not None) if v]
    return [str(value).strip()]


def _flag_duplicates(proposals: dict, existing: list[dict]) -> int:
    """Mark proposals that duplicate work already in the ledger.

    The brief already tells each agent what not to re-propose, but an
    instruction is not a guarantee — on a small local model it is barely even a
    strong hint. So the overlap is also checked here, after the fact, where it
    cannot be talked around. Duplicates are flagged rather than deleted: a
    reader should be able to see that an agent proposed the same thing again.

    Nothing in here may raise. A malformed idea is a reason to skip that idea,
    never a reason to lose five minutes of agent work to a 500.
    """
    by_title = {_normalise_title(e.get("title", "")): e for e in existing}
    by_target: dict[str, list[dict]] = {}
    for entry in existing:
        for target in _targets(entry.get("addresses")):
            by_target.setdefault(target, []).append(entry)

    flagged = 0
    for payload in proposals.values():
        if not isinstance(payload, dict) or not payload.get("available"):
            continue
        for idea in payload.get("ideas") or []:
            if not isinstance(idea, dict):
                continue
            try:
                match = by_title.get(_normalise_title(idea.get("title", "")))
                reason = "same title as logged work" if match else None
                if not match:
                    for target in _targets(idea.get("addresses")):
                        hits = by_target.get(target)
                        if hits:
                            match = hits[0]
                            reason = "addresses a shortfall already covered"
                            break
                if match:
                    idea["duplicate_of"] = {
                        "id": match.get("id"), "title": match.get("title"),
                        "status": match.get("status"), "reason": reason,
                    }
                    flagged += 1
            except Exception as error:
                idea["duplicate_check_error"] = f"{type(error).__name__}: {error}"
    return flagged


# Below this share of its envelope, a domain has not made substantial use of it,
# whatever it says in envelope_use. Half is generous.
SUBSTANTIAL_USE = 0.5


def _check_envelope_claims(proposals: dict, split: dict) -> list[dict]:
    """Contradict an agent that claims to have used an envelope it did not.

    A live run had the economic agent propose INR 1.5 crore against an INR 64
    crore envelope and report that this "makes substantial use of the envelope".
    The arithmetic says otherwise. As with the duplicate check and the deferral
    check, the claim is contradicted rather than rewritten: a reader should see
    that the agent said it.
    """
    flagged = []
    for domain, payload in proposals.items():
        if not isinstance(payload, dict) or not payload.get("available"):
            continue
        envelope = (split.get(domain) or {}).get("envelope_inr_crore") or 0
        if not envelope:
            continue
        total = sum(float(i.get("est_cost_inr_crore") or 0)
                    for i in (payload.get("ideas") or []) if isinstance(i, dict))
        share = total / envelope
        payload["envelope_share"] = round(share, 4)
        if share >= SUBSTANTIAL_USE:
            continue
        claim = str(payload.get("envelope_use") or "").lower()
        # "does not make substantial use" is the honest answer and must not be
        # flagged, so look for the claim without its negation.
        claims_use = ("substantial use" in claim and "not" not in claim.split("substantial")[0][-24:])
        payload["envelope_shortfall"] = {
            "proposed_inr_crore": round(total, 2),
            "envelope_inr_crore": envelope,
            "share": round(share, 4),
            "claimed_substantial_use": claims_use,
            "note": (f"proposed {share:.0%} of its envelope"
                     + (". The agent nonetheless reported substantial use, which "
                        "the arithmetic contradicts." if claims_use else
                        ". The agent acknowledged this.")),
        }
        if claims_use:
            flagged.append({"domain": domain, **payload["envelope_shortfall"]})
    return flagged


def _domain_brief(domain: str, needs: list[dict], envelope: float,
                  areas: list[dict], blocks: list[dict],
                  existing: list[dict] | None = None) -> str:
    """What one agent is told before it proposes.

    Includes the work already logged in these areas. Without it the agents
    re-propose the same interventions every time a budget arrives, which is
    exactly what a planner does not want from a system meant to know what it
    has already recommended.
    """
    lines = [
        f"You are the {domain} agent for Ernakulam district. No one has given "
        f"you a proposal to review. Your task is to PROPOSE interventions.",
        "",
        f"YOUR BUDGET ENVELOPE: INR {envelope:.2f} crore. This was allocated "
        f"from measured need and is not negotiable.",
        "",
        f"Your proposals should total close to INR {envelope:.2f} crore. This is "
        f"money already committed to your domain: an unspent crore buys nothing, "
        f"and proposing INR 5 crore of work against an INR {envelope:.0f} crore "
        f"envelope wastes it.",
        "",
        f"Scale the WORK to the envelope, not just the count of items. With "
        f"INR {envelope:.0f} crore you are being asked what a programme at that "
        f"scale looks like, not for three small pilots. A bigger envelope should "
        f"buy a larger facility, more sites, a longer corridor or a deeper "
        f"retrofit, and your costs should reflect that. If your domain genuinely "
        f"cannot absorb the envelope, say so plainly in envelope_use and explain "
        f"why rather than proposing token amounts.",
        "",
        f"AREAS IN SCOPE ({len(areas)}): "
        + ", ".join(f"{a['admin_name']} (pop {a['population'] or 'unknown'})"
                    for a in areas),
        "",
        "MEASURED SHORTFALLS IN YOUR DOMAIN, worst first. These are percentile "
        "ranks across all 97 local bodies of the district, computed from "
        "government data before you were called:",
    ]
    if needs:
        for need in needs[:10]:
            mark = " [rests on a proxy parameter]" if need["status"] == "proxy" else ""
            if need.get("addressed_by"):
                work = need["addressed_by"][0]
                mark += (f"  >>> ALREADY BEING ADDRESSED by "
                         f"'{work['title']}' ({work['status']}). Do NOT propose "
                         f"this again. Propose something else, or an explicitly "
                         f"complementary step the existing work does not cover "
                         f"— and say how it differs.")
            lines.append(f"  - severity {need['severity']:.2f}: {need['reading']}"
                         f" (value {need['value']}, {need['source']}, "
                         f"{need['data_year']}){mark}")
    else:
        lines.append("  - none above the reporting floor. Your domain has no "
                     "measured shortfall in these areas, so propose maintenance "
                     "or resilience work only, and say that the mandate is weak.")
    if blocks:
        lines += ["", "BINDING HAZARD CONSTRAINTS in these areas:"]
        lines += [f"  - {b['admin_name']}: {b['message']}" for b in blocks]
        lines += [
            f"  PROHIBITED there: {BLOCK_PROHIBITS}.",
            f"  STILL PERMITTED there: {BLOCK_PERMITS}.",
            "  So mitigation work in a blocked area is legitimate and often the "
            "right answer; new development in one is not. If you propose "
            "anything in a blocked area, state the assessment it is conditional "
            "on.",
        ]
    mine = [e for e in (existing or []) if e.get("domain") == domain]
    if mine:
        lines += ["", "WORK ALREADY LOGGED IN YOUR DOMAIN IN THESE AREAS — do not "
                      "propose any of it again:"]
        for e in mine:
            cost = (f", INR {e['est_cost_inr_crore']} crore"
                    if e.get("est_cost_inr_crore") else "")
            targets = f", addresses {e['addresses']}" if e.get("addresses") else ""
            lines.append(f"  - '{e['title']}' ({e['status']}{cost}{targets})")
    other = [e for e in (existing or []) if e.get("domain") != domain]
    if other:
        lines += ["", "Logged in other domains, for context (another agent owns "
                  "these): " + "; ".join(f"{e['title']} ({e['status']})"
                                         for e in other[:6])]

    lines += [
        "",
        f"Propose at most {MAX_IDEAS_PER_DOMAIN} interventions. For each one give "
        "a cost in INR crore that you can defend, name the shortfall above that "
        "it addresses, and be honest about feasibility. Do not invent a figure "
        "that is not derivable from what you were given — say what you cannot "
        "cost instead. An intervention that does not address a listed shortfall "
        "should not be proposed at all.",
    ]
    return "\n".join(lines)


IDEA_SCHEMA = {
    "ideas": ("array of objects, each with: title (short), what (one or two "
              "sentences on the intervention), addresses (the parameter name of "
              "the shortfall it targets), areas (array of local body names), "
              "est_cost_inr_crore (number, or null if it genuinely cannot be "
              "costed from the data given), feasibility (one of: high, medium, "
              "low), feasibility_reason (why), depends_on (array of "
              "prerequisites, may be empty), evidence (the measured reading it "
              "rests on)"),
    "total_est_cost_inr_crore": "number: the sum of your ideas' costs",
    "fits_envelope": "boolean: does that total fit the envelope you were given",
    "mandate": "one of: strong, weak, none — how well measured need supports acting here",
    "could_not_cost": "array of things you declined to put a number on, and why",
    "envelope_use": ("one or two sentences: does your total make substantial use "
                     "of the envelope, and if not, why can the domain not absorb "
                     "the rest"),
}


def _propose_domain(domain: str, brief: str, backend) -> dict:
    """Ask one agent for proposals in its own domain."""
    from ..agents.llm import complete_json

    system = "\n".join([
        f"You are the {domain} domain agent in an Ernakulam district planning "
        f"system. You propose interventions grounded in measured shortfalls.",
        "",
        "Hard rules:",
        "- Every idea must address one of the listed shortfalls, by parameter name.",
        "- Never propose permanent siting in an area listed as hazard-blocked.",
        "- Never invent a figure. If the data given cannot support a cost, put "
        "null and list it under could_not_cost.",
        "- Stay inside your own domain. Another agent covers the others.",
        "- Reply with a single JSON object and nothing else.",
        "",
        "Keys required:",
        json.dumps(IDEA_SCHEMA, indent=2),
    ])
    try:
        payload = complete_json(system, brief, backend)
    except Exception as error:
        return {"domain": domain, "available": False,
                "reason": f"{type(error).__name__}: {error}"}
    payload.setdefault("ideas", [])
    payload["domain"] = domain
    payload["available"] = True
    return payload


def _portfolio(proposals: dict, split: dict, needs: dict, blocks: list[dict],
               budget: float, areas: list[dict], backend,
               existing: list[dict] | None = None) -> dict:
    """The Supervisor assembles one costed portfolio from the four domains.

    It is given the budget, the measured need ranking and the hazard blocks, and
    asked to choose — but not to re-rank the needs or clear a block. Its job is
    the trade-off between domains, which is the one judgement no arithmetic in
    this project makes for it.
    """
    from ..agents.llm import complete_json

    offered = {d: p.get("ideas", []) for d, p in proposals.items()
               if isinstance(p, dict) and p.get("available")}
    if not any(offered.values()):
        return {"available": False, "reason": "no agent produced a proposal"}

    schema = {
        "portfolio": ("array of selected ideas, highest priority first, each with: "
                      "title, domain, areas, est_cost_inr_crore, why_selected, "
                      "feasibility, conditions (array)"),
        "total_cost_inr_crore": "number: the portfolio total",
        "budget_headroom_inr_crore": "number: budget minus total, may be negative",
        "deferred": ("array of ideas not selected, each with title, domain and "
                     "why_deferred"),
        "rationale": "two or three sentences on how this balances the domains",
        "biggest_unmet_need": "the highest-severity shortfall this portfolio does not address, and why",
        "sequencing": "array of {phase, items, reason} if order matters, else empty",
        "confidence": "one of: low, medium, high",
        "data_gaps": "array of things that could not be assessed from the data",
    }
    system = "\n".join([
        "You are the Supervisor of the Ernakulam district planning system. Four "
        "domain agents have each proposed interventions against measured "
        "shortfalls. Assemble one portfolio that fits the budget.",
        "",
        "Hard rules:",
        "- The total must fit the budget. If you exceed it, say so explicitly in "
        "budget_headroom_inr_crore as a negative number and justify it.",
        "- The hazard blocks below are computed from GSI and KSDMA data and are "
        "binding. In a blocked area you may NOT select anything that "
        f"{BLOCK_PROHIBITS}. You MAY select {BLOCK_PERMITS}. Mitigation work in "
        "a blocked area is frequently the correct answer; new development there "
        "is not. Anything you select in a blocked area must carry its "
        "assessment as a condition.",
        "- Use only the costs the agents gave. Do not invent or adjust a figure.",
        "- FIRST do this arithmetic: compare the total cost of everything "
        "proposed against the budget. If the budget covers all of it, select ALL "
        "of it. There is no reason to leave funded work unselected.",
        "- You may NOT defer anything for budget or cost reasons while budget "
        "remains unspent. That is a contradiction and it is checked "
        "automatically. If you defer something while money is left, the reason "
        "must be a real one: sequencing, delivery capacity, a dependency, a "
        "hazard block, or the work not being needed. If you have no such reason, "
        "select it.",
        "- If the proposed work costs less than the budget, say so plainly in "
        "rationale and state how much has no proposed use. Do not pretend a "
        "surplus is a constraint.",
        "- Work already logged in these areas is listed below. Do not select "
        "anything that repeats it. An idea carrying `duplicate_of` was flagged "
        "automatically as a repeat and must be deferred with that as the reason.",
        "- Name the biggest need you are NOT addressing. A portfolio that claims "
        "to cover everything is a failed answer.",
        "- Reply with a single JSON object and nothing else.",
        "",
        "Keys required:",
        json.dumps(schema, indent=2),
    ])
    offered_now = sum(float(i.get("est_cost_inr_crore") or 0)
                      for ideas in offered.values() for i in ideas
                      if isinstance(i, dict))
    affordable = ("The budget covers EVERYTHING proposed. Select all of it "
                  "unless a non-budget reason applies."
                  if offered_now <= budget else
                  "The proposals cost more than the budget, so you must choose.")
    user = "\n".join([
        f"TOTAL BUDGET: INR {budget} crore",
        f"TOTAL COST OF ALL PROPOSED WORK: INR {offered_now:.2f} crore",
        f"=> {affordable}", "",
        "AREAS IN SCOPE",
        json.dumps([{k: a[k] for k in ("admin_name", "local_body_type", "population")}
                    for a in areas], default=str), "",
        "BUDGET SPLIT BY MEASURED NEED (indicative, computed before any agent ran)",
        json.dumps(split), "",
        "MEASURED NEED RANKING (top 12, percentile of need across the district)",
        json.dumps([{k: n[k] for k in ("parameter", "domain", "admin_name",
                                       "severity", "status")}
                    for n in needs["ranked"][:12]]), "",
        "BINDING HAZARD BLOCKS",
        json.dumps([{k: b[k] for k in ("admin_name", "code", "message")}
                    for b in blocks]) or "[]", "",
        "WORK ALREADY LOGGED IN THESE AREAS (do not repeat any of it)",
        json.dumps([{k: e.get(k) for k in ("title", "domain", "status", "addresses",
                                           "est_cost_inr_crore")}
                    for e in (existing or [])]) or "[]", "",
        "IDEAS PROPOSED BY DOMAIN",
        json.dumps(offered, default=str)[:7000], "",
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

    # Recompute utilisation from the selected items rather than trusting the
    # model's own arithmetic, and state it plainly. A portfolio that uses a
    # fifth of the budget may be right, but it should never pass unremarked.
    selected = payload.get("portfolio") or []
    counted = sum(float(i.get("est_cost_inr_crore") or 0)
                  for i in selected if isinstance(i, dict))
    offered_total = sum(float(i.get("est_cost_inr_crore") or 0)
                        for ideas in offered.values() for i in ideas
                        if isinstance(i, dict))
    headroom = budget - counted
    # A deferral blamed on budget while budget remains is a factual
    # contradiction, and a live run produced eight of them at once while sitting
    # on 94% of the money. The model is not corrected here, because silently
    # rewriting its reasoning would hide the failure. It is contradicted, in the
    # payload, with the arithmetic that disproves it.
    BUDGET_WORDS = ("budget", "cost", "fund", "afford", "money", "financial")
    contradictions = []
    for item in (payload.get("deferred") or []):
        if not isinstance(item, dict):
            continue
        reason = str(item.get("why_deferred") or "").lower()
        if any(word in reason for word in BUDGET_WORDS) and headroom > 0:
            contradictions.append({
                "title": item.get("title"), "domain": item.get("domain"),
                "stated_reason": item.get("why_deferred"),
                "contradiction": (f"deferred for budget reasons while INR "
                                  f"{headroom:.2f} crore was unallocated"),
            })

    # Work that was proposed and then simply disappeared: not selected, not
    # deferred, not accounted for. A live run lost three items worth INR 41
    # crore this way.
    def _norm(title):
        return "".join(c for c in str(title or "").lower() if c.isalnum())

    accounted = {_norm(i.get("title")) for i in selected if isinstance(i, dict)}
    accounted |= {_norm(i.get("title")) for i in (payload.get("deferred") or [])
                  if isinstance(i, dict)}
    unaccounted = [
        {"title": i.get("title"), "domain": domain,
         "est_cost_inr_crore": i.get("est_cost_inr_crore")}
        for domain, ideas in offered.items() for i in ideas
        if isinstance(i, dict) and _norm(i.get("title")) not in accounted
    ]

    payload["budget_check"] = {
        "budget_inr_crore": budget,
        "selected_cost_inr_crore": round(counted, 2),
        "utilisation": round(counted / budget, 4) if budget else None,
        "total_offered_by_agents_inr_crore": round(offered_total, 2),
        "headroom_inr_crore": round(headroom, 2),
        # Money the agents never found a use for, as distinct from money the
        # Supervisor chose not to spend. The two have different causes and
        # different fixes.
        "unproposed_inr_crore": round(max(budget - offered_total, 0.0), 2),
        "agents_absorbed_share": round(offered_total / budget, 4) if budget else None,
        "over_budget": counted > budget,
        "contradictory_deferrals": contradictions,
        "unaccounted_proposals": unaccounted,
        "note": ("computed from the selected items, not from the model's own "
                 "total. A large unspent share is reported, not corrected. "
                 "`unproposed` is budget the agents never proposed work for; "
                 "`headroom` is budget the Supervisor did not select."),
    }
    payload["bound_by"] = {
        "budget_inr_crore": budget,
        "hazard_blocked_areas": sorted({b["admin_name"] for b in blocks}),
        "note": "computed from measured data; the Supervisor cannot override it",
    }
    return payload


def generate(admin_ids: list[str], budget_inr_crore: float,
             frame=None, persist: bool = True) -> dict:
    """A budget and some areas in; a costed, measured-need-driven portfolio out."""
    admin_ids = list(dict.fromkeys(admin_ids or []))
    if not admin_ids:
        raise ValueError("select at least one local body")
    if len(admin_ids) > MAX_UNITS:
        raise ValueError(f"select at most {MAX_UNITS} local bodies "
                         f"({len(admin_ids)} given)")
    try:
        budget = float(budget_inr_crore)
    except (TypeError, ValueError):
        raise ValueError("budget_inr_crore must be a number")
    if budget <= 0:
        raise ValueError("budget must be greater than zero")

    frame = _derive(load_features()) if frame is None else frame
    unknown = [a for a in admin_ids if frame[frame.admin_id == a].empty]
    if unknown:
        raise KeyError(f"unknown admin_id {unknown[0]!r}")

    areas = []
    for admin_id in admin_ids:
        row = frame[frame.admin_id == admin_id].iloc[0]
        population = row.get("population")
        areas.append({
            "admin_id": admin_id,
            "admin_name": row["name"],
            "local_body_type": row.get("local_auth"),
            "population": None if population is None or population != population
                          else int(population),
        })

    needs = assess(admin_ids, frame)
    split = _budget_split(needs["domain_pressure"], budget)
    blocks = _hazard_blocks(admin_ids, frame)

    try:
        from .implementations import covered_titles, listing
        existing = covered_titles(admin_ids)
        ledger = listing(admin_ids=admin_ids)
        ledger_error = None
    except Exception as error:
        # The ledger is a convenience for not repeating ourselves, not a
        # precondition for assessing need. If it is unreachable, say so and
        # carry on rather than refusing to propose anything.
        existing, ledger, ledger_error = [], None, f"{type(error).__name__}: {error}"

    result = {
        "ideation_id": f"ideation__{datetime.now(timezone.utc):%Y%m%dT%H%M%S}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "budget_inr_crore": budget,
        "areas": areas,
        "needs": needs,
        "budget_split": split,
        "hazard_blocks": blocks,
        "existing_work": {
            "count": len(existing),
            "items": existing,
            "committed_inr_crore": (ledger or {}).get("committed_inr_crore"),
            "by_status": (ledger or {}).get("by_status"),
            "error": ledger_error,
            "note": ("work already logged in these areas. The agents were told "
                     "not to re-propose it, and any proposal that duplicates it "
                     "anyway is flagged with duplicate_of."),
        },
        "proposals": None,
        "portfolio": None,
        "determinism": {
            "needs_from": "measured parameters, percentile-ranked across 97 local bodies",
            "budget_split_from": "measured need pressure, computed before any model ran",
            "llm_can_change_needs": False,
            "llm_can_change_the_split": False,
            "llm_can_clear_a_hazard_block": False,
        },
    }

    try:
        from ..agents.llm import resolve_backend
        backend = resolve_backend()
    except Exception as error:
        result["proposals"] = {"available": False,
                              "reason": f"{type(error).__name__}: {error}"}
        return _persist(result) if persist else result

    proposals = {}
    for domain in DOMAINS:
        envelope = split[domain]["envelope_inr_crore"]
        if envelope < MIN_DOMAIN_BUDGET_CRORE:
            proposals[domain] = {
                "domain": domain, "available": False,
                "reason": (f"envelope of INR {envelope:.2f} crore is below the "
                           f"INR {MIN_DOMAIN_BUDGET_CRORE} crore floor — too "
                           f"small to cost an intervention honestly"),
            }
            continue
        brief = _domain_brief(domain, needs["by_domain"].get(domain, []),
                              envelope, areas, blocks, existing)
        proposals[domain] = _propose_domain(domain, brief, backend)
        proposals[domain]["brief"] = brief

    duplicates = _flag_duplicates(proposals, existing)
    envelope_claims = _check_envelope_claims(proposals, split)
    result["proposals"] = {"available": True,
                           "backend": {"kind": backend.kind, "model": backend.model},
                           "duplicates_flagged": duplicates,
                           "overstated_envelope_use": envelope_claims,
                           **proposals}
    result["portfolio"] = _portfolio(proposals, split, needs, blocks, budget,
                                     areas, backend, existing)
    return _persist(result) if persist else result


def _persist(result: dict) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{result['ideation_id']}.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8")
    result["persisted"] = {"json": True}
    return result
