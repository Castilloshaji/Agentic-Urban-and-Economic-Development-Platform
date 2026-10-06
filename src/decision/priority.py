"""Priority engine: parameters + scenario relevance -> normalized agent weights.

    raw_i    = relevance_i^RELEVANCE_EXP  x  evidence_i  x  (FLOOR + (1-FLOOR) x signal_i)
    weight_i = raw_i / sum(raw)

The three terms answer three different questions and are kept separate on
purpose:

  relevance_i  does this domain bear on the stated objective?  (scenario matrix)
  evidence_i   how much do we trust this domain's data here?   (mean confidence)
  signal_i     how strongly does THIS unit exhibit the concern? (mean normalized)

An earlier version multiplied normalized x confidence inside one mean. That let a
domain with three high-confidence Census parameters outrank the domain the
scenario was actually about, because a score of 0.7 beat a score of 0.3 by more
than relevance 1.00 beat 0.70 — infrastructure won every scenario. Separating
the terms, raising relevance to a power to sharpen its contrast, and keeping
signal in [FLOOR, 1] so it modulates rather than dominates fixes that: relevance
leads, evidence gates, signal tilts.

The weights remain a pure function of the data and the scenario — no model is
consulted, so identical inputs always give identical numbers.

A domain whose parameters are all unavailable contributes raw 0 and is reported
as such rather than silently receiving an average score.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .parameters import Parameter, parameters_for
from .scenarios import DOMAINS, FORMULA_VERSION, get

# Sharpens the gap between a relevance of 1.00 and 0.70 so the scenario, not the
# unit's incidental scores, decides which domain leads.
RELEVANCE_EXP = 2.0
# Signal modulates within [SIGNAL_FLOOR, 1]; it cannot zero out a domain the
# scenario says is central, nor let a high score hijack an irrelevant one.
SIGNAL_FLOOR = 0.45


def compute(admin_id: str, scenario_key: str, frame=None) -> dict:
    scenario = get(scenario_key)
    params: list[Parameter] = parameters_for(admin_id, frame)
    by_name = {p.name: p for p in params}

    detail, raw = {}, {}
    for domain in DOMAINS:
        wanted = scenario.parameters.get(domain, [])
        used, skipped = [], []
        for name in wanted:
            prm = by_name.get(name)
            if prm is None or prm.normalized is None or prm.status == "unavailable":
                skipped.append({"parameter": name,
                                "reason": "unavailable" if prm else "not_defined"})
                continue
            used.append({"parameter": name, "normalized": prm.normalized,
                         "confidence": prm.confidence,
                         "status": prm.status, "source": prm.source,
                         "source_level": prm.source_level, "data_year": prm.data_year})
        signal = round(sum(u["normalized"] for u in used) / len(used), 4) if used else 0.0
        evidence = round(sum(u["confidence"] for u in used) / len(used), 4) if used else 0.0
        relevance = scenario.relevance.get(domain, 0.0)
        modulator = SIGNAL_FLOOR + (1.0 - SIGNAL_FLOOR) * signal
        raw[domain] = round((relevance ** RELEVANCE_EXP) * evidence * modulator, 6)
        detail[domain] = {"scenario_relevance": relevance,
                          "relevance_term": round(relevance ** RELEVANCE_EXP, 4),
                          "evidence_confidence": evidence,
                          "parameter_signal": signal,
                          "signal_modulator": round(modulator, 4),
                          "raw_weight": raw[domain],
                          "parameters_used": used, "parameters_skipped": skipped}

    total = sum(raw.values())
    if total > 0:
        weights = {d: round(raw[d] / total, 4) for d in DOMAINS}
        # absorb float drift into the largest so the sum is exactly 1
        drift = round(1.0 - sum(weights.values()), 4)
        if drift:
            top = max(weights, key=weights.get)
            weights[top] = round(weights[top] + drift, 4)
    else:
        weights = {d: 0.0 for d in DOMAINS}

    ranked = sorted(weights, key=weights.get, reverse=True)
    for position, domain in enumerate(ranked, 1):
        detail[domain]["final_weight"] = weights[domain]
        detail[domain]["rank"] = position

    return {
        "admin_id": admin_id,
        "scenario": scenario_key,
        "scenario_label": scenario.label,
        "formula_version": FORMULA_VERSION,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "weights": weights,
        "weight_sum": round(sum(weights.values()), 6),
        "ranking": ranked,
        "detail": detail,
        "parameters": [p.as_dict() for p in params],
    }


if __name__ == "__main__":
    import json, sys
    aid = sys.argv[1] if len(sys.argv) > 1 else "G07049"
    key = sys.argv[2] if len(sys.argv) > 2 else "public_transport_expansion"
    out = compute(aid, key)
    print(json.dumps({k: out[k] for k in ("admin_id", "scenario", "weights",
                                          "weight_sum", "ranking")}, indent=2))
