"""Turn a free-text development idea into a runnable scenario — deterministically.

The user types an idea in their own words. Something has to decide which domains
that idea bears on, and it must not be an LLM: the whole point of the priority
engine is that the weights come from measured data and an inspectable relevance
matrix, not from a model's opinion. A model asked "how important is environment
here" would be choosing the weights by the back door.

So interpretation is embedding similarity against the scenario catalogue, using
the same all-MiniLM encoder the RAG index already uses. Same text in, same
cosine scores out, every time — and the match is returned with its score so the
user can see (and override) what the system decided their idea was.

Place names are matched by the same normalisation the admin crosswalk uses, so
"a bus route for Kuttampuzha" resolves to the real local body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from functools import lru_cache

from ..processing.consistency_checks import name_match
from .scenarios import CATALOGUE

# Below this the idea is too far from anything in the catalogue to classify.
MATCH_FLOOR = 0.25
# Keyword hits are a small, transparent nudge on top of the embedding score —
# they exist because short inputs ("industrial park") carry little context for
# an encoder but are unambiguous to a reader.
KEYWORD_BOOST = 0.12

KEYWORDS: dict[str, tuple[str, ...]] = {
    "public_transport_expansion": ("bus", "feeder", "route", "connectivity", "public transport",
                                    "last mile", "service", "commute"),
    "industrial_development": ("industrial", "industry", "factory", "msme", "manufacturing",
                                "park", "warehouse", "logistics", "employment", "jobs"),
    "flood_resilient_development": ("flood", "resilience", "resilient", "drainage", "mitigation",
                                     "hazard", "disaster", "monsoon", "inundation", "climate"),
    "affordable_housing": ("housing", "house", "homes", "residential", "affordable", "settlement",
                            "slum", "shelter"),
    "transit_oriented_development": ("metro", "transit oriented", "tod", "station area",
                                      "densification", "around the station"),
    "urban_service_expansion": ("water supply", "sanitation", "waste", "sewer", "electricity",
                                 "municipal service", "services", "utility", "underserved"),
}


@dataclass
class Interpretation:
    text: str
    scenario_key: str | None
    scenario_label: str | None
    match_score: float
    method: str
    candidates: list[dict]
    target_admin_id: str | None
    target_name: str | None
    target_method: str | None
    budget_inr_crore: float | None
    confident: bool
    note: str

    def as_dict(self) -> dict:
        return asdict(self)


@lru_cache(maxsize=1)
def _encoder():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer("all-MiniLM-L6-v2")


@lru_cache(maxsize=1)
def _scenario_vectors():
    import numpy as np

    keys = sorted(CATALOGUE)
    corpus = [f"{CATALOGUE[k].label}. {CATALOGUE[k].description}" for k in keys]
    return keys, np.asarray(_encoder().encode(corpus, normalize_embeddings=True))


def classify(text: str) -> tuple[str | None, float, str, list[dict]]:
    """Rank the catalogue against the idea. Deterministic: no model is asked to judge."""
    import numpy as np

    keys, matrix = _scenario_vectors()
    vector = np.asarray(_encoder().encode(text, normalize_embeddings=True))
    cosine = matrix @ vector

    lowered = text.lower()
    scored = []
    for key, base in zip(keys, cosine):
        hits = [w for w in KEYWORDS.get(key, ()) if w in lowered]
        total = float(base) + (KEYWORD_BOOST if hits else 0.0)
        scored.append({"scenario": key, "label": CATALOGUE[key].label,
                       "embedding_score": round(float(base), 4),
                       "keyword_hits": hits,
                       "score": round(total, 4)})
    scored.sort(key=lambda s: -s["score"])
    best = scored[0]
    method = "embedding+keyword" if best["keyword_hits"] else "embedding"
    if best["score"] < MATCH_FLOOR:
        return None, best["score"], method, scored
    return best["scenario"], best["score"], method, scored


def find_budget(text: str) -> float | None:
    """Pull a budget out of the sentence if one is stated."""
    patterns = [
        r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)\s*(?:cr|crore)",
        r"([\d,]+(?:\.\d+)?)\s*(?:cr\b|crore)",
        r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)\s*(?:lakh|lakhs)",
    ]
    for index, pattern in enumerate(patterns):
        found = re.search(pattern, text, re.I)
        if found:
            value = float(found.group(1).replace(",", ""))
            return round(value / 100, 4) if index == 2 else value
    return None


# Local-body names carry their type: "Kuttampuzha Gramapanchayath". A user types
# the place, not the type. These are stripped here rather than in the shared
# matcher's GENERIC_TOKENS, because stripping "panchayat" globally would collapse
# the demo fixtures ("Demo Panchayat A" / "…B") into near-identical cores.
LSG_TYPE_WORDS = re.compile(
    r"\b(grama\s*panchayath?|gramapanchayath?|grama\s*panchayat|panchayath?|"
    r"municipality|municipal\s*corporation|corporation)\b", re.I)


def place_core(name: str) -> str:
    return LSG_TYPE_WORDS.sub(" ", name or "").strip()


def find_place(text: str, names: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """Resolve a place mentioned in the idea to a real local body.

    Uses the pipeline's own name matcher, so "Kuttampuzha" resolves the same way
    the admin crosswalk resolves it — including the directional veto that stops
    "North Paravur" matching "South Paravur".
    """
    cleaned = re.sub(r"[^\w\s-]", " ", text)
    words = cleaned.split()
    phrases = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}

    best = (0.0, None, None, None)
    for admin_id, name in names.items():
        bare = place_core(name)
        if not bare:
            continue
        for phrase in phrases:
            if len(phrase) < 5:
                continue
            score, method = name_match(phrase, bare)
            if score > best[0]:
                best = (score, admin_id, name, method)
    if best[0] >= 0.86:
        return best[1], best[2], f"{best[3]} ({best[0]:.2f})"

    # Fallback: a hyphenated or compound local body named only in part.
    # "Malayattoor" is how people refer to "Malayattoor-Neeleswaram". Accept it
    # only when the typed phrase is a whole leading or trailing token of the
    # name and matches exactly one unit, so a partial word cannot pull in a
    # neighbour by accident.
    def tokens_of(value: str) -> list[str]:
        return [t for t in re.split(r"[\s\-]+", value.lower()) if t]

    hits = []
    for admin_id, name in names.items():
        parts = tokens_of(place_core(name))
        if not parts:
            continue
        for phrase in phrases:
            typed = tokens_of(phrase)
            if not typed or len(" ".join(typed)) < 6:
                continue
            if typed == parts[:len(typed)] or typed == parts[-len(typed):]:
                hits.append((admin_id, name, " ".join(typed)))
    unique = {h[0] for h in hits}
    if len(unique) == 1:
        admin_id, name, typed = hits[0]
        return admin_id, name, f"partial_name_token ({typed})"
    return None, None, None


def interpret(text: str, names: dict[str, str] | None = None,
              admin_id: str | None = None) -> Interpretation:
    text = (text or "").strip()
    if not text:
        raise ValueError("an idea is required")

    scenario_key, score, method, candidates = classify(text)
    budget = find_budget(text)

    target_id, target_name, target_method = admin_id, names.get(admin_id) if names and admin_id else None, "supplied"
    if admin_id is None and names:
        target_id, target_name, target_method = find_place(text, names)

    if scenario_key is None:
        note = (f"This idea does not resemble any objective in the catalogue "
                f"(best match {score:.2f} < {MATCH_FLOOR}). Pick an objective "
                f"manually, or add a scenario type for it.")
    elif score < 0.45:
        note = (f"Weak match ({score:.2f}) — the idea was read as "
                f"'{CATALOGUE[scenario_key].label}'. Check that is what you meant.")
    else:
        note = f"Read as '{CATALOGUE[scenario_key].label}' (match {score:.2f})."

    return Interpretation(
        text=text, scenario_key=scenario_key,
        scenario_label=CATALOGUE[scenario_key].label if scenario_key else None,
        match_score=round(score, 4), method=method,
        candidates=candidates[:4],
        target_admin_id=target_id, target_name=target_name, target_method=target_method,
        budget_inr_crore=budget,
        confident=bool(scenario_key) and score >= 0.45,
        note=note)
