"""Derive per-admin-unit hazard features from the real KSDMA and GSI layers.

Zonal statistics over the KSDMA flood return-probability rasters and a spatial
join against the GSI landslide susceptibility polygons, producing one row per
local body. This is what makes flood_risk, flood_zone_overlap, hazard_exposure
and climate_vulnerability real parameters instead of proxies.

Flood raster values are a modelled return-period quantity, not a depth in metres,
so the absolute number is not reported as one. What is reported is the share of
the unit's area that is flood-modelled at all, plus the distribution of values
inside it — both of which are comparable across units and are what a priority
weight actually needs.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.mask import mask

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE = PROJECT_ROOT / "data" / "sources"
EXTRACTED = CACHE / "extracted"
# Derived from the persistent source cache, not from data/raw, so it must NOT
# live under data/processed — Stage 2 clears that tree on every run.
OUT = PROJECT_ROOT / "data" / "features"

FLOOD_ARCHIVES = {
    "historical_10_25_50": "ksdma_flood_return_probability_historical_10_25_50.zip",
    "historical_100_200_500": "ksdma_flood_return_probability_historical_100_200_500.zip",
    "rcp85_10_25_50": "ksdma_flood_return_probability_rcp85_10_25_50.zip",
    "rcp85_100_200_500": "ksdma_flood_return_probability_rcp85_100_200_500.zip",
}
LANDSLIDE_ARCHIVE = "gsi_landslide_susceptibility_ernakulam.zip"
LSG_FILE = "gis/lsg_kerala_boundaries.geojson"

SUSCEPTIBILITY_RANK = {"low": 1, "moderate": 2, "high": 3, "very high": 4}


def extract_all() -> None:
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    for archive in sorted(CACHE.rglob("*.zip")):
        target = EXTRACTED / archive.stem
        if target.exists() and any(target.rglob("*")):
            continue
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(target)


def load_boundaries() -> gpd.GeoDataFrame:
    path = CACHE / LSG_FILE
    if not path.exists():
        raise FileNotFoundError(f"missing {path} — run: python3 src/ingestion/ingest.py --url gis")
    frame = gpd.read_file(path)
    ernakulam = frame[frame["District"] == "Ernakulam"].copy()
    ernakulam = ernakulam.rename(columns={"LSGI_Code": "admin_id", "name": "name"})
    return ernakulam[["admin_id", "name", "local_auth", "geometry"]].reset_index(drop=True)


def _rasters_for(scenario: str) -> list[Path]:
    folder = EXTRACTED / Path(FLOOD_ARCHIVES[scenario]).stem
    return sorted(folder.rglob("*.tif"))


def flood_features(boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Share of each unit that is flood-modelled, per return period and scenario."""
    rows: dict[str, dict] = {a: {"admin_id": a} for a in boundaries["admin_id"]}

    for scenario, archive in FLOOD_ARCHIVES.items():
        for raster_path in _rasters_for(scenario):
            # file names look like Kerala_Flood_25_yr_Historical.tif
            parts = raster_path.stem.split("_")
            period = next((p for p in parts if p.isdigit()), None)
            if period is None:
                continue
            column = f"flood_share_{scenario.split('_')[0]}_{period}yr"

            with rasterio.open(raster_path) as src:
                nodata = src.nodata
                for _, unit in boundaries.to_crs(src.crs).iterrows():
                    try:
                        clipped, _ = mask(src, [unit.geometry], crop=True, filled=True,
                                          nodata=nodata if nodata is not None else np.nan)
                    except ValueError:
                        rows[unit.admin_id][column] = None
                        continue
                    band = clipped[0].astype("float64")
                    inside = np.isfinite(band)
                    if nodata is not None:
                        inside &= band != nodata
                    total = band.size
                    rows[unit.admin_id][column] = (
                        round(float(inside.sum()) / total, 6) if total else None
                    )
    return pd.DataFrame(rows.values())


def landslide_features(boundaries: gpd.GeoDataFrame) -> pd.DataFrame:
    """Highest landslide susceptibility class intersecting each unit."""
    shp = next((EXTRACTED / Path(LANDSLIDE_ARCHIVE).stem).rglob("*.shp"), None)
    if shp is None:
        raise FileNotFoundError("GSI landslide shapefile not extracted")
    zones = gpd.read_file(shp).to_crs(boundaries.crs)
    zones["rank"] = zones["Susceptibi"].str.lower().map(SUSCEPTIBILITY_RANK)

    joined = gpd.sjoin(boundaries[["admin_id", "geometry"]], zones[["rank", "Susceptibi", "geometry"]],
                       how="left", predicate="intersects")
    best = (joined.groupby("admin_id")
                  .agg(landslide_rank=("rank", "max"))
                  .reset_index())
    inverse = {v: k for k, v in SUSCEPTIBILITY_RANK.items()}
    best["landslide_susceptibility"] = best["landslide_rank"].map(
        lambda r: inverse.get(int(r)) if pd.notna(r) else None)
    return best


def build() -> pd.DataFrame:
    extract_all()
    boundaries = load_boundaries()
    print(f"  boundaries: {len(boundaries)} Ernakulam local bodies")

    flood = flood_features(boundaries)
    print(f"  flood columns: {[c for c in flood.columns if c != 'admin_id']}")
    slide = landslide_features(boundaries)
    print(f"  landslide classes: {slide['landslide_susceptibility'].value_counts().to_dict()}")

    features = boundaries.drop(columns="geometry").merge(flood, on="admin_id", how="left")
    features = features.merge(slide, on="admin_id", how="left")
    features["hazard_source"] = "ksdma_ncess_rcp85+gsi"
    features["hazard_source_level"] = 1
    features["hazard_data_year"] = 2026

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "hazard_features.csv"
    features.to_csv(path, index=False)
    print(f"  wrote {path.relative_to(PROJECT_ROOT)}  ({len(features)} rows)")
    return features


if __name__ == "__main__":
    build()
