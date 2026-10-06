"""Healthcare and education facilities per local body, from the OSM extract.

Kerala publishes district totals for health centres and schools, never a
per-panchayat breakdown. But the facilities themselves are mapped: OSM carries
hospitals, clinics, pharmacies, schools and colleges with coordinates, and the
Southern-Zone extract is already on disk for the road and water layers.

So this is a measurement, not an allocation. Where a facility is, is observed.
The published district totals in `anchors.py` are used only to sanity-check the
extract, never to generate it.

Two caveats travel with every number here, because both change what it means:

* **OSM coverage is uneven.** A panchayat with no mapped clinic may have no
  clinic, or may simply be less mapped than Kochi. The engine reports the count
  and the vintage and does not guess which. Distance to the nearest facility is
  the more robust signal and is what the parameters lean on.
* **Government and private are mixed.** OSM maps both, and Kerala's private
  health sector is large. The district anchor counts government facilities only,
  so the OSM count being several times larger is expected and is not an error.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE = PROJECT_ROOT / "data" / "sources"
OUT = PROJECT_ROOT / "data" / "features"
PBF = CACHE / "gis" / "geofabrik_southern_zone_osm.pbf"
ERNAKULAM_BBOX = [76.16, 9.62, 76.90, 10.35]
METRIC_CRS = "EPSG:32643"

# Weighted so a hospital is not counted the same as a pharmacy counter. The
# weights are a judgement about service capacity, stated here rather than buried:
# a hospital serves a catchment, a pharmacy serves a street.
HEALTH_WEIGHT = {"hospital": 6.0, "clinic": 2.0, "doctors": 1.5,
                 "health_post": 1.5, "pharmacy": 0.5}
EDUCATION_WEIGHT = {"university": 6.0, "college": 4.0, "school": 2.0,
                    "kindergarten": 0.7, "library": 0.7}

LAYERS = {
    "health": ("amenity", list(HEALTH_WEIGHT), HEALTH_WEIGHT),
    "education": ("amenity", list(EDUCATION_WEIGHT), EDUCATION_WEIGHT),
}


def extract(key: str) -> gpd.GeoDataFrame:
    """Pull one amenity family out of the clipped extract."""
    from pyrosm import OSM

    if not PBF.exists():
        raise FileNotFoundError(
            f"missing {PBF} — run: python3 src/ingestion/ingest.py --url gis --include-large")
    tag, values, _ = LAYERS[key]
    osm = OSM(str(PBF), bounding_box=ERNAKULAM_BBOX)
    got = osm.get_data_by_custom_criteria(
        custom_filter={tag: values}, filter_type="keep",
        keep_nodes=True, keep_ways=True, keep_relations=True)
    if got is None or got.empty:
        raise RuntimeError(f"pyrosm returned no {key} features for the Ernakulam bbox")
    return got[[tag, "geometry"]].rename(columns={tag: "kind"})


def _per_unit(points: gpd.GeoDataFrame, units: gpd.GeoDataFrame,
              weights: dict[str, float], prefix: str) -> pd.DataFrame:
    """Count, capacity-weight and measure distance, per local body."""
    # Buildings are mapped as polygons and clinics as nodes. Collapsing to a
    # representative point makes the two comparable without pretending a
    # hospital footprint is a different kind of thing from a clinic pin.
    points = points.copy()
    points["geometry"] = points.geometry.representative_point()
    points["weight"] = points["kind"].map(weights).fillna(1.0)

    joined = gpd.sjoin(points[["kind", "weight", "geometry"]],
                       units[["admin_id", "geometry"]],
                       how="inner", predicate="within")
    agg = (joined.groupby("admin_id")
                 .agg(**{f"{prefix}_count": ("kind", "count"),
                         f"{prefix}_capacity": ("weight", "sum")})
                 .reset_index())

    centroids = units.copy()
    centroids["geometry"] = centroids.geometry.centroid
    nearest = gpd.sjoin_nearest(
        centroids[["admin_id", "geometry"]], points[["kind", "geometry"]],
        how="left", distance_col=f"{prefix}_nearest_m").drop_duplicates("admin_id")

    frame = units[["admin_id"]].copy()
    frame["area_km2"] = units.geometry.area / 1e6
    frame = frame.merge(agg, on="admin_id", how="left")
    frame = frame.merge(nearest[["admin_id", f"{prefix}_nearest_m"]],
                        on="admin_id", how="left")
    for column in (f"{prefix}_count", f"{prefix}_capacity"):
        frame[column] = frame[column].fillna(0)
    frame[f"{prefix}_nearest_m"] = frame[f"{prefix}_nearest_m"].round(1)
    # Capacity per 1000 residents is the figure a planner actually reasons with,
    # but population is missing for 26 units, so it is left null there rather
    # than silently divided by a stand-in.
    frame[f"{prefix}_per_km2"] = (
        frame[f"{prefix}_capacity"] / frame.area_km2).round(3)
    return frame.drop(columns="area_km2")


def build() -> pd.DataFrame:
    from .hazard import load_boundaries

    units = load_boundaries().to_crs(METRIC_CRS)
    frame = units[["admin_id"]].copy()

    for key, prefix in (("health", "health"), ("education", "education")):
        points = extract(key).to_crs(METRIC_CRS)
        print(f"  OSM {key}: {len(points):,} features "
              f"({', '.join(f'{k} {v}' for k, v in points['kind'].value_counts().items())})")
        frame = frame.merge(_per_unit(points, units, LAYERS[key][2], prefix),
                            on="admin_id", how="left")

    census = (PROJECT_ROOT / "data" / "ernakulam" / "ernakulam_data" /
              "population" / "population_panchayat_ernakulam.csv")
    if census.exists():
        pop = pd.read_csv(census)[["admin_id", "population"]]
        frame = frame.merge(pop, on="admin_id", how="left")
        people = pd.to_numeric(frame.population, errors="coerce")
        for prefix in ("health", "education"):
            frame[f"{prefix}_per_1000"] = (
                frame[f"{prefix}_capacity"] / people * 1000).round(3)
        frame = frame.drop(columns="population")

    frame["social_source"] = "geofabrik_osm_southern_zone"
    frame["social_source_level"] = 3
    frame["social_data_year"] = 2026

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "social_features.csv"
    frame.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(frame)} rows)")
    from .anchors import HEALTH_FACILITIES, SCHOOLS
    for prefix, label, anchor in (("health", "health", HEALTH_FACILITIES),
                                  ("education", "education", SCHOOLS)):
        mapped = int(frame[f"{prefix}_count"].sum())
        print(f"  {label}: {mapped} mapped inside the 97 units, "
              f"{(frame[f'{prefix}_count'] == 0).sum()} unit(s) with none, "
              f"median distance {frame[f'{prefix}_nearest_m'].median():.0f} m")
        print(f"      vs {anchor.value:.0f} published ({anchor.unit}) "
              f"= {mapped / anchor.value:.1f}x. Expected to exceed it: OSM counts "
              f"private provision and sub-types the official figure excludes.")
    return frame


if __name__ == "__main__":
    build()
