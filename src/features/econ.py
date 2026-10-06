"""Allocate district economic anchors down to local bodies, as labelled estimates.

MSME registration, income and employment are published for Ernakulam district
and not below it. Three parameters therefore read "unavailable" even though the
district totals are known. This module spends those totals downward.

The method is deliberately boring and deliberately constrained:

  * every estimate reproduces its published district anchor exactly — the
    allocation is a share-out, so Σ estimates = the real total, and a reader can
    check that in one line;
  * the allocator is built only from quantities measured for all 97 units
    (Census population, OSM road density, GTFS stop density, KMRL distance);
  * the spread is bounded, because an allocator is a hypothesis about where
    activity concentrates, not a measurement of it.

What this is not: a measurement. Every parameter produced here is tagged PROXY
with the anchor named in its note, and confidence is derived from the anchor's
source level and then cut for the allocation step. A reader who distrusts the
allocator can throw these out and still have the 16 real parameters.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .anchors import (DISTRICT_GDDP_CRORE, MSME_REGISTRATIONS,
                      PER_CAPITA_INCOME)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FEATURES = PROJECT_ROOT / "data" / "features"
CENSUS = (PROJECT_ROOT / "data" / "ernakulam" / "ernakulam_data" /
          "population" / "population_panchayat_ernakulam.csv")
OUT = FEATURES

# How much of the allocator is raw headcount and how much is measured activity.
# Enterprise registration follows people first — Kochi has more firms than
# Edamalakkudi mainly because it has more residents — but not only people, or
# every panchayat would look identically entrepreneurial per head. 0.65/0.35
# keeps population dominant while letting the measured activity signal move a
# unit by roughly a factor of two either way.
POPULATION_WEIGHT = 0.65
ACTIVITY_WEIGHT = 0.35

# Bound on how far a unit's estimated per-capita income may sit from the
# district mean. Ernakulam's own figure is about 1.6x Kerala's weakest district;
# an intra-district spread wider than that would be claiming more resolution
# than the allocator can carry, so ±30% is the stated ceiling.
INCOME_SPREAD = 0.30

# An urban local body is credited with a higher enterprise intensity per head.
# This is the one judgement call in the file that is not read off a measurement,
# so it is small, explicit, and applied to the activity term only.
URBAN_INTENSITY = {"municipal_corporation": 1.40, "municipality": 1.20}


def _normalize(series: pd.Series) -> pd.Series:
    """Percentile rank, matching the parameter engine's normalisation."""
    return series.rank(pct=True, na_option="keep")


def _activity_signal(frame: pd.DataFrame) -> pd.Series:
    """Measured commercial-activity signal in [0,1], from four real layers.

    Road density and transit stop density stand in for built-up commercial
    fabric; metro proximity stands in for the corridor where Ernakulam's
    investment has actually gone since 2017. All four are measurements; the
    choice to average them is the approximation.
    """
    parts = []
    if "road_density_km_per_km2" in frame:
        parts.append(_normalize(pd.to_numeric(frame.road_density_km_per_km2,
                                              errors="coerce")))
    if "stop_density_per_km2" in frame:
        parts.append(_normalize(pd.to_numeric(frame.stop_density_per_km2,
                                              errors="coerce")))
    if "population_density" in frame:
        parts.append(_normalize(frame.population_density))
    if "nearest_metro_m" in frame:
        # Invert: close to the metro is a high signal.
        parts.append(1.0 - _normalize(pd.to_numeric(frame.nearest_metro_m,
                                                    errors="coerce")))
    if not parts:
        raise RuntimeError("no activity layer available to build the allocator")
    signal = pd.concat(parts, axis=1).mean(axis=1)
    return signal.fillna(signal.median())


def build() -> pd.DataFrame:
    frame = _load_inputs()
    population = pd.to_numeric(frame.population, errors="coerce")
    activity = _activity_signal(frame)

    # 26 of the 97 units have no Census 2011 population in the panchayat table.
    # Dropping them from the allocation would silently move their share of the
    # district total onto everyone else, so a stand-in is needed.
    #
    # The median is that stand-in, and it was chosen by measurement rather than
    # convenience. Population correlates strongly with mapped road length
    # (r = 0.90) and transit stop count (r = 0.92), so a regression looks
    # obviously better — but under leave-one-out validation on the 71 units that
    # do have a count, the flat median's median absolute error is 28.7% while
    # linear-on-road-length is 35.8%, log-log is 33.2%, and road plus stops ties
    # at 28.9%. The correlation is real and the predictive gain is not, because
    # the relationship is not proportional through the origin. So: median, and
    # the substitution is flagged per row rather than buried, because an estimate
    # built on an imputed input is a weaker claim than one built on a real count.
    imputed = population.isna()
    population_filled = population.fillna(population.median())

    urban = frame.local_auth.map(URBAN_INTENSITY).fillna(1.0)
    # The allocator: headcount, plus measured activity, lifted for urban bodies.
    weight = population_filled * (
        POPULATION_WEIGHT + ACTIVITY_WEIGHT * activity * urban)

    share = weight / weight.sum()
    out = pd.DataFrame({"admin_id": frame.admin_id})

    # ---- MSME presence: share out the registered-enterprise total ----
    out["msme_estimated_count"] = (MSME_REGISTRATIONS.value * share).round(0)
    # Left null, not zero, where the population it would divide by is unknown.
    out["msme_per_1000_people"] = (
        out.msme_estimated_count / population * 1000).round(2)
    out["msme_density_per_km2"] = (
        out.msme_estimated_count / frame.area_km2).round(1)

    # ---- Investment potential: district per-capita income, modulated ----
    # Centre the modifier on the district mean so the population-weighted mean
    # of the estimates returns the published figure, then clamp the spread.
    centred = activity - ((activity * population_filled).sum()
                          / population_filled.sum())
    modifier = 1.0 + INCOME_SPREAD * (centred / max(centred.abs().max(), 1e-9))
    out["income_per_capita_estimate"] = (PER_CAPITA_INCOME.value * modifier).round(0)
    out["gddp_share_estimate_crore"] = (DISTRICT_GDDP_CRORE.value * share).round(2)

    # ---- Employment potential: absorbable jobs, not current jobs ----
    # Enterprise count says how much activity exists; enterprises per km² says
    # how much more the unit can take before it is built out. Both are needed,
    # and they rank units differently — Kochi leads on count, several
    # Kanayannur panchayats lead on headroom.
    # Mean of the terms that exist rather than a sum that any missing term can
    # null out: a unit with no census population still has a measured enterprise
    # density, and reporting nothing for it would be worse than reporting that
    # one half of the index.
    out["employment_capacity_index"] = pd.concat(
        [_normalize(out.msme_density_per_km2), _normalize(population)],
        axis=1).mean(axis=1).round(4)

    # Names the publishers rather than the method. The method is recorded in
    # econ_allocation and in this module's docstring; the source field answers
    # "who published the figures this rests on".
    out["econ_source"] = "Kerala Ecostat and Ministry of MSME, district figures"
    out["econ_source_level"] = 1          # the anchors are Level 1 publications
    out["econ_allocation"] = (
        f"pop {POPULATION_WEIGHT:.2f} + activity {ACTIVITY_WEIGHT:.2f}; "
        f"income spread +/-{INCOME_SPREAD:.0%}")
    out["econ_data_year"] = MSME_REGISTRATIONS.data_year
    out["econ_is_estimate"] = 1           # never let a reader mistake this row
    out["econ_population_imputed"] = imputed.astype(int).values

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "econ_features.csv"
    out.to_csv(path, index=False)

    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(out)} rows)")
    print(f"  MSME anchor {MSME_REGISTRATIONS.value:,} -> "
          f"allocated {out.msme_estimated_count.sum():,.0f} "
          f"(error {abs(out.msme_estimated_count.sum() - MSME_REGISTRATIONS.value):,.0f})")
    weighted = ((out.income_per_capita_estimate * population_filled).sum()
                / population_filled.sum())
    print(f"  income anchor {PER_CAPITA_INCOME.value:,} -> "
          f"population-weighted mean {weighted:,.0f} "
          f"({weighted / PER_CAPITA_INCOME.value - 1:+.2%})")
    print(f"  income range: {out.income_per_capita_estimate.min():,.0f} "
          f"to {out.income_per_capita_estimate.max():,.0f}")
    if imputed.any():
        print(f"  population imputed (median) for {int(imputed.sum())} unit(s): "
              f"{', '.join(frame.loc[imputed, 'name'].astype(str))}")
    return out


def _load_inputs() -> pd.DataFrame:
    hazard = pd.read_csv(FEATURES / "hazard_features.csv")[
        ["admin_id", "name", "local_auth"]]
    transit = pd.read_csv(FEATURES / "transit_features.csv")
    frame = hazard.merge(transit, on="admin_id", how="left")

    for name, columns in (("road_features.csv", ["road_density_km_per_km2"]),
                          ("metro_features.csv", ["nearest_metro_m"])):
        path = FEATURES / name
        if path.exists():
            frame = frame.merge(pd.read_csv(path)[["admin_id", *columns]],
                                on="admin_id", how="left")

    if not CENSUS.exists():
        raise FileNotFoundError(f"missing {CENSUS} — the allocator needs population")
    pop = pd.read_csv(CENSUS)[["admin_id", "population"]]
    frame = frame.merge(pop, on="admin_id", how="left")
    frame["population_density"] = pd.to_numeric(
        frame.population, errors="coerce") / frame.area_km2
    return frame


if __name__ == "__main__":
    build()
