"""Join the KMRL metro station list to local bodies and derive access features.

The station list was already in the dataset, carrying real KMRL coordinates for
the 25 Line-1 stations from Aluva to Thripunithura, but tagged `admin_id =
EKM-D` — the district, not a local body. That is why `metro_access` read
"unavailable" while the data sat on disk: nothing had ever joined it down to the
97 units.

This is a measurement, not an approximation. Distance from a unit's centroid to
the nearest station is computed in a metric CRS from published coordinates, so
the resulting parameter is REAL at source level 2 (KMRL), not a proxy.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIONS = (PROJECT_ROOT / "data" / "ernakulam" / "ernakulam_data" /
            "transportation" / "metro_stations_ernakulam.csv")
OUT = PROJECT_ROOT / "data" / "features"
METRIC_CRS = "EPSG:32643"

# Beyond this a station is not plausibly walkable or feeder-reachable, so the
# unit counts as having no metro access rather than a very poor one. 2 km is the
# catchment KMRL's own feeder-service planning uses.
CATCHMENT_M = 2000


def load_stations() -> gpd.GeoDataFrame:
    if not STATIONS.exists():
        raise FileNotFoundError(f"missing {STATIONS}")
    frame = pd.read_csv(STATIONS)
    missing = {"name", "lat", "lon"} - set(frame.columns)
    if missing:
        raise ValueError(f"station file lacks {sorted(missing)}")
    frame = frame.dropna(subset=["lat", "lon"])
    return gpd.GeoDataFrame(
        frame, geometry=gpd.points_from_xy(frame.lon, frame.lat), crs="EPSG:4326")


def build() -> pd.DataFrame:
    from .hazard import load_boundaries

    stations = load_stations().to_crs(METRIC_CRS)
    units = load_boundaries().to_crs(METRIC_CRS)
    print(f"  KMRL stations: {len(stations)}")

    centroids = units.copy()
    centroids["geometry"] = centroids.geometry.centroid

    # sjoin_nearest gives both the station and the distance in one pass, which
    # keeps the station name attached — useful provenance for a reader checking
    # whether the nearest station is the one they would expect.
    nearest = gpd.sjoin_nearest(
        centroids[["admin_id", "geometry"]],
        stations[["name", "geometry"]].rename(columns={"name": "nearest_metro_station"}),
        how="left", distance_col="nearest_metro_m")
    nearest = nearest.drop_duplicates(subset="admin_id")

    frame = nearest[["admin_id", "nearest_metro_station", "nearest_metro_m"]].copy()
    frame["nearest_metro_m"] = frame.nearest_metro_m.round(1)
    frame["metro_in_catchment"] = (frame.nearest_metro_m <= CATCHMENT_M).astype(int)

    # Stations actually inside the unit, which is a different question from
    # distance: a unit can host a station and still have a far centroid.
    inside = gpd.sjoin(stations[["name", "geometry"]], units[["admin_id", "geometry"]],
                       how="inner", predicate="within")
    counts = inside.groupby("admin_id").size().rename("metro_stations_in_unit")
    frame = frame.merge(counts, on="admin_id", how="left")
    frame["metro_stations_in_unit"] = frame.metro_stations_in_unit.fillna(0).astype(int)

    frame["metro_source"] = "kmrl_station_list"
    frame["metro_source_level"] = 2
    frame["metro_data_year"] = 2024

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "metro_features.csv"
    frame.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(frame)} rows)")
    print(f"  units hosting a station: {(frame.metro_stations_in_unit > 0).sum()}")
    print(f"  units within {CATCHMENT_M} m: {int(frame.metro_in_catchment.sum())}")
    print(f"  median distance to nearest station: {frame.nearest_metro_m.median() / 1000:.1f} km")
    return frame


if __name__ == "__main__":
    build()
