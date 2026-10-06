"""Extract the OSM water layer for Ernakulam and derive water-proximity features.

The dataset shipped three water polygons with real names (Vembanad, Periyar,
Muvattupuzha) but `source: drawn_geometry_real_names` — the geometry was hand
drawn, so it is fine for a demo map and useless for measuring how close a unit
is to water. The OSM Southern-Zone extract already on disk for the road layer
carries the real thing, so this reads from there instead.

Two quantities come out, because they answer different questions. Area share
says how much of a unit *is* water, which bears on buildable land. Distance from
the centroid says how close development would sit to a water body, which bears
on effluent risk and CRZ-type setbacks. A unit can score low on one and high on
the other.
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

# Lakes, rivers, reservoirs and the backwaters — the features a siting decision
# has to respect. Drains and ditches are excluded: they are mapped
# inconsistently in Kerala and would make a unit look waterfront because a
# roadside channel runs through it.
WATER_TAGS = {
    "natural": ["water"],
    "waterway": ["river", "riverbank", "canal"],
    "landuse": ["reservoir", "basin"],
}


def extract_water() -> gpd.GeoDataFrame:
    from pyrosm import OSM

    if not PBF.exists():
        raise FileNotFoundError(
            f"missing {PBF} — run: python3 src/ingestion/ingest.py --url gis --include-large")
    osm = OSM(str(PBF), bounding_box=ERNAKULAM_BBOX)
    layers = []
    for key, values in WATER_TAGS.items():
        got = osm.get_data_by_custom_criteria(
            custom_filter={key: values}, filter_type="keep",
            keep_nodes=False, keep_ways=True, keep_relations=True)
        if got is not None and not got.empty:
            layers.append(got[["geometry"]])
            print(f"  OSM {key}={values}: {len(got):,} features")
    if not layers:
        raise RuntimeError("pyrosm returned no water features for the Ernakulam bbox")
    return gpd.GeoDataFrame(pd.concat(layers, ignore_index=True), crs="EPSG:4326")


def build() -> pd.DataFrame:
    from .hazard import load_boundaries

    water = extract_water().to_crs(METRIC_CRS)
    units = load_boundaries().to_crs(METRIC_CRS)

    # Polygons carry area; rivers mapped as lines do not. Keep both — lines
    # still answer the distance question — but only polygons count toward share.
    water["geometry"] = water.geometry.buffer(0)
    polygons = water[water.geom_type.isin(("Polygon", "MultiPolygon"))]
    print(f"  water polygons: {len(polygons):,} of {len(water):,} features")

    rows = []
    merged = polygons.union_all() if not polygons.empty else None
    all_water = water.union_all()
    for _, unit in units.iterrows():
        area_m2 = unit.geometry.area
        covered = unit.geometry.intersection(merged).area if merged is not None else 0.0
        rows.append({
            "admin_id": unit.admin_id,
            "water_area_share": round(covered / area_m2, 5) if area_m2 else None,
            "water_distance_m": round(unit.geometry.centroid.distance(all_water), 1),
        })

    frame = pd.DataFrame(rows)
    frame["water_source"] = "geofabrik_osm_southern_zone"
    frame["water_source_level"] = 3
    frame["water_data_year"] = 2026

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "water_features.csv"
    frame.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(frame)} rows)")
    print(f"  median water share: {frame.water_area_share.median():.4f}")
    print(f"  units with a water body inside: {(frame.water_area_share > 0).sum()}")
    print(f"  median centroid distance to water: {frame.water_distance_m.median():.0f} m")
    return frame


if __name__ == "__main__":
    build()
