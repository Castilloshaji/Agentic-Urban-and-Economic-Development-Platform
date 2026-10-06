"""Derive per-admin-unit transit features from the real Kochi GTFS feed.

Accessibility is computed as distance from the unit's centroid to the nearest
stop, plus stop density and scheduled service frequency inside the unit. The
feed's own service window is carried through, because a stop whose feed expired
in March 2023 must not be counted as current service without that caveat.
"""

from __future__ import annotations

import csv
import zipfile
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE = PROJECT_ROOT / "data" / "sources"
OUT = PROJECT_ROOT / "data" / "features"
GTFS_ZIP = CACHE / "transportation" / "kochi_transport_gtfs.zip"
METRIC_CRS = "EPSG:32643"


def read_gtfs() -> dict[str, list[dict]]:
    if not GTFS_ZIP.exists():
        raise FileNotFoundError(f"missing {GTFS_ZIP} — run: ingest.py --url transportation")
    tables = {}
    with zipfile.ZipFile(GTFS_ZIP) as archive:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt",
                     "frequencies.txt", "feed_info.txt"):
            if name in archive.namelist():
                with archive.open(name) as handle:
                    text = handle.read().decode("utf-8-sig").splitlines()
                    tables[name.replace(".txt", "")] = list(csv.DictReader(text))
    return tables


def stop_points(stops: list[dict]) -> gpd.GeoDataFrame:
    rows = []
    for s in stops:
        try:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({"stop_id": s["stop_id"], "stop_name": s.get("stop_name"),
                     "geometry": Point(lon, lat)})
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def build(boundaries: gpd.GeoDataFrame | None = None) -> pd.DataFrame:
    from .hazard import load_boundaries

    boundaries = load_boundaries() if boundaries is None else boundaries
    gtfs = read_gtfs()
    stops = stop_points(gtfs["stops"])
    print(f"  GTFS stops: {len(stops):,}   routes: {len(gtfs.get('routes', [])):,}")

    feed = (gtfs.get("feed_info") or [{}])[0]
    feed_end = feed.get("feed_end_date")
    months_stale = None
    if feed_end and len(feed_end) == 8:
        end = date(int(feed_end[:4]), int(feed_end[4:6]), int(feed_end[6:]))
        today = date.today()
        months_stale = (today.year - end.year) * 12 + (today.month - end.month)

    # Trips per stop, as a scheduled-service proxy. frequencies.txt gives
    # headways for the routes that have them; stop_times gives the rest.
    trips_per_stop: dict[str, int] = {}
    for row in gtfs.get("stop_times", []):
        sid = row.get("stop_id")
        if sid:
            trips_per_stop[sid] = trips_per_stop.get(sid, 0) + 1

    units = boundaries.to_crs(METRIC_CRS)
    pts = stops.to_crs(METRIC_CRS)
    pts["trips"] = pts["stop_id"].map(trips_per_stop).fillna(0).astype(int)

    joined = gpd.sjoin(pts, units[["admin_id", "geometry"]], how="left", predicate="within")
    inside = (joined.dropna(subset=["admin_id"])
                    .groupby("admin_id")
                    .agg(stop_count=("stop_id", "count"), trips_total=("trips", "sum"))
                    .reset_index())

    rows = []
    union = pts.union_all() if hasattr(pts, "union_all") else pts.unary_union
    for _, unit in units.iterrows():
        centroid = unit.geometry.centroid
        nearest_m = centroid.distance(union)
        area_km2 = unit.geometry.area / 1e6
        rows.append({"admin_id": unit.admin_id,
                     "area_km2": round(area_km2, 3),
                     "nearest_stop_m": round(float(nearest_m), 1)})
    frame = pd.DataFrame(rows).merge(inside, on="admin_id", how="left")
    frame[["stop_count", "trips_total"]] = frame[["stop_count", "trips_total"]].fillna(0)
    frame["stop_density_per_km2"] = (frame.stop_count / frame.area_km2).round(3)
    frame["trips_per_stop"] = np.where(frame.stop_count > 0,
                                       (frame.trips_total / frame.stop_count).round(2), 0.0)
    frame["transit_feed_end_date"] = feed_end
    frame["transit_feed_months_stale"] = months_stale
    frame["transit_source"] = "jungle_bus_kochi_gtfs"
    frame["transit_source_level"] = 3

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "transit_features.csv"
    frame.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(frame)} rows, "
          f"feed {months_stale} months stale)")
    return frame


if __name__ == "__main__":
    build()
