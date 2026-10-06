"""Load the DataMeet taluk boundaries as a Level-3 source with their area deficit.

These are real polygons but 2001-census vintage: measured against the Census
taluk areas they total only ~78.5% of the district, with Kothamangalam at 34%
and Kunnathunad at 149%. They are better than a derived bounding box and worse
than authoritative, so the deficit is computed and stored alongside them rather
than discovered later by someone trusting the geometry.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.ops import transform

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CACHE = PROJECT_ROOT / "data" / "sources"
OUT = PROJECT_ROOT / "data" / "features"
ARCHIVE = CACHE / "gis" / "datameet_kerala_admin_boundaries.zip"
METRIC = Transformer.from_crs("EPSG:4326", "EPSG:32643", always_xy=True).transform

# Census 2011 taluk areas, km² — the reference the deficit is measured against.
CENSUS_AREA = {"Kanayannur": 303, "Kochi": 129, "Kunnathunad": 464, "Aluva": 532,
               "Paravur": 173, "Muvattupuzha": 522, "Kothamangalam": 928}


def build() -> pd.DataFrame:
    if not ARCHIVE.exists():
        raise FileNotFoundError(f"missing {ARCHIVE} — run ingest.py --url gis")
    target = CACHE / "extracted" / ARCHIVE.stem
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(ARCHIVE) as handle:
            handle.extractall(target)

    path = next(target.rglob("taluk.geojson"), None)
    if path is None:
        raise FileNotFoundError("taluk.geojson not found in the DataMeet archive")

    frame = gpd.read_file(path)
    ernakulam = frame[frame.DISTRICT.str.contains("Ernakulam", case=False, na=False)].copy()

    rows = []
    for _, r in ernakulam.iterrows():
        measured = transform(METRIC, r.geometry).area / 1e6
        match = next((k for k in CENSUS_AREA if k.lower()[:5] in str(r.TALUK).lower()), None)
        census = CENSUS_AREA.get(match)
        rows.append({
            "taluk_name": r.TALUK,
            "measured_area_km2": round(measured, 2),
            "census_area_km2": census,
            "area_ratio": round(measured / census, 4) if census else None,
            "area_deficit_km2": round(census - measured, 2) if census else None,
            "source": "datameet_geohacker_kerala",
            "source_level": 3,
            "data_year": 2018,
            "boundary_vintage": "2001-census-era",
            "quality_flag": ("area_mismatch_vs_census"
                             if census and abs(measured / census - 1) > 0.15 else "within_15pc"),
        })
    out = pd.DataFrame(rows)
    total_measured, total_census = out.measured_area_km2.sum(), sum(CENSUS_AREA.values())
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT / "taluk_boundary_quality.csv", index=False)
    print(f"  taluks: {len(out)}   measured {total_measured:,.0f} km² vs census "
          f"{total_census:,} km² = {100*total_measured/total_census:.1f}%")
    print(f"  flagged area_mismatch_vs_census: "
          f"{(out.quality_flag == 'area_mismatch_vs_census').sum()}/{len(out)}")
    print(f"  wrote {(OUT / 'taluk_boundary_quality.csv').relative_to(PROJECT_ROOT)}")
    return out


if __name__ == "__main__":
    build()
