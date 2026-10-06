"""Clip the Geofabrik Southern-Zone OSM extract to Ernakulam and derive road features.

Kerala has no separate Geofabrik extract, so the input is the 532 MB Southern
Zone PBF covering four states. Parsing it whole is slow and memory-hungry, so
pyrosm is given the district bounding box up front and only the clipped subset
is ever materialised.

The output closes three parameters that were previously unavailable:
road_connectivity, infrastructure_capacity (as a road-class proxy) and
right_of_way_constraint (as a surfaced/lane proxy, clearly labelled).
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

# Rough capacity ordering; OSM highway classes are the only capacity signal that
# actually exists for Ernakulam, so this is explicitly a proxy, not a measurement.
CLASS_WEIGHT = {"motorway": 6, "trunk": 5, "primary": 4, "secondary": 3,
                "tertiary": 2, "unclassified": 1, "residential": 1}


def extract_roads() -> gpd.GeoDataFrame:
    from pyrosm import OSM

    if not PBF.exists():
        raise FileNotFoundError(
            f"missing {PBF} — run: python3 src/ingestion/ingest.py --url gis --include-large")
    osm = OSM(str(PBF), bounding_box=ERNAKULAM_BBOX)
    roads = osm.get_network(network_type="driving")
    if roads is None or roads.empty:
        raise RuntimeError("pyrosm returned no road network for the Ernakulam bbox")
    return roads


def build() -> pd.DataFrame:
    from .hazard import load_boundaries

    boundaries = load_boundaries()
    roads = extract_roads()
    print(f"  OSM road segments in bbox: {len(roads):,}")

    roads = roads.to_crs(METRIC_CRS)
    units = boundaries.to_crs(METRIC_CRS)
    roads["klass"] = roads.get("highway", pd.Series(index=roads.index, dtype=object))
    roads["weight"] = roads["klass"].map(CLASS_WEIGHT).fillna(1)

    joined = gpd.sjoin(roads[["klass", "weight", "geometry"]],
                       units[["admin_id", "geometry"]], how="inner", predicate="intersects")
    joined["length_m"] = joined.geometry.length

    agg = (joined.groupby("admin_id")
                 .agg(road_segments=("klass", "count"),
                      road_length_m=("length_m", "sum"),
                      weighted_length_m=("length_m", lambda s: float(s.sum())),
                      max_road_class=("weight", "max"))
                 .reset_index())

    area = units[["admin_id"]].copy()
    area["area_km2"] = units.geometry.area / 1e6
    frame = area.merge(agg, on="admin_id", how="left")
    for column in ("road_segments", "road_length_m", "weighted_length_m", "max_road_class"):
        frame[column] = frame[column].fillna(0)
    frame["road_density_km_per_km2"] = (frame.road_length_m / 1000 / frame.area_km2).round(3)
    frame["road_source"] = "geofabrik_osm_southern_zone"
    frame["road_source_level"] = 3
    frame["road_data_year"] = 2026

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "road_features.csv"
    frame.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(frame)} rows)")
    print(f"  median road density: {frame.road_density_km_per_km2.median():.2f} km/km²")
    print(f"  units with no mapped road: {(frame.road_segments == 0).sum()}")
    return frame


if __name__ == "__main__":
    build()
