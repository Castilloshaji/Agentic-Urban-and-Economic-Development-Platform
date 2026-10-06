"""Right-of-way features from the OSM road network.

The original note on `right_of_way_constraint` said OSM lanes and width tags are
too sparse in India to support the parameter. That was checked rather than
assumed, and it holds: across the 83,763 driving-network segments inside the
Ernakulam bounding box, `lanes` is present on 5.7% and `width` on 0.8%. Building
a parameter on 0.8% coverage would be a guess wearing a measurement's clothes.

What *is* fully tagged is `highway` class, on 100% of segments. That supports a
different and answerable question: how much of a unit's network is narrow-class
road. A unit whose network is almost entirely `residential` and
`living_street` has a genuine right-of-way problem for anything needing
construction access or freight, regardless of what the width tags say. That is
what this module measures, and it is labelled a proxy because road class is a
proxy for carriageway width — the measured lane coverage travels with it so a
reader can see exactly how thin the direct evidence is.
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

# Classes that cannot be widened or used for heavy access without acquisition.
NARROW_CLASSES = {"residential", "living_street", "service", "unclassified",
                  "track", "path", "road"}
# Classes that carry a usable right of way today.
ARTERIAL_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary",
                    "motorway_link", "trunk_link", "primary_link",
                    "secondary_link", "tertiary_link"}


def build() -> pd.DataFrame:
    from pyrosm import OSM

    from .hazard import load_boundaries

    if not PBF.exists():
        raise FileNotFoundError(
            f"missing {PBF} — run: python3 src/ingestion/ingest.py --url gis --include-large")
    osm = OSM(str(PBF), bounding_box=ERNAKULAM_BBOX)
    roads = osm.get_network(network_type="driving", extra_attributes=["lanes", "width"])
    if roads is None or roads.empty:
        raise RuntimeError("pyrosm returned no road network for the Ernakulam bbox")
    print(f"  OSM driving segments: {len(roads):,}")
    for tag in ("lanes", "width"):
        if tag in roads.columns:
            share = roads[tag].notna().mean()
            print(f"  {tag} tag coverage: {share:.1%}  <- why this is a proxy")

    roads = roads.to_crs(METRIC_CRS)
    units = load_boundaries().to_crs(METRIC_CRS)

    roads["is_narrow"] = roads.highway.isin(NARROW_CLASSES)
    roads["is_arterial"] = roads.highway.isin(ARTERIAL_CLASSES)
    roads["has_lanes"] = roads["lanes"].notna() if "lanes" in roads.columns else False

    joined = gpd.sjoin(
        roads[["highway", "is_narrow", "is_arterial", "has_lanes", "geometry"]],
        units[["admin_id", "geometry"]], how="inner", predicate="intersects")
    joined["length_m"] = joined.geometry.length

    grouped = joined.groupby("admin_id")
    frame = pd.DataFrame({
        "row_total_length_m": grouped.length_m.sum().round(1),
        "row_narrow_length_m": grouped.apply(
            lambda g: g.loc[g.is_narrow, "length_m"].sum(), include_groups=False).round(1),
        "row_arterial_length_m": grouped.apply(
            lambda g: g.loc[g.is_arterial, "length_m"].sum(), include_groups=False).round(1),
        "row_lane_tag_coverage": grouped.has_lanes.mean().round(4),
    }).reset_index()

    frame["row_narrow_share"] = (
        frame.row_narrow_length_m / frame.row_total_length_m).round(4)
    frame["row_arterial_km_per_km2"] = None

    area = units[["admin_id"]].copy()
    area["area_km2"] = units.geometry.area / 1e6
    frame = area.merge(frame, on="admin_id", how="left")
    frame["row_arterial_km_per_km2"] = (
        frame.row_arterial_length_m / 1000 / frame.area_km2).round(3)

    frame["row_source"] = "geofabrik_osm_southern_zone highway class"
    frame["row_source_level"] = 3
    frame["row_data_year"] = 2026

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "row_features.csv"
    frame.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(frame)} rows)")
    print(f"  median narrow-class share: {frame.row_narrow_share.median():.1%}")
    print(f"  units with no arterial road at all: "
          f"{(frame.row_arterial_length_m.fillna(0) == 0).sum()}")
    return frame


if __name__ == "__main__":
    build()
