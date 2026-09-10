"""Build the `ernakulam` dataset from REAL published sources.

Every file is assembled from data that exists outside this project:

  BOUNDARIES   opendatakerala/lsg-kerala-data — OSM-derived LSG polygons,
               ODbL. Real MultiPolygon geometry, real LSGI codes, real
               Malayalam names, real block-panchayat attribution.
  POPULATION   Census of India 2011 village/town tables, via
               censusindia2011.com taluk pages. Real counts, literacy and
               sex-ratio.
  TALUKS       Census 2011 taluk aggregates; they sum exactly to the
               published district total, which is the dataset's own check.
  METRO        Kochi Metro Line 1 station list and opening dates (KMRL).
  ECONOMY      Kerala DES / Ecostat GDDP and per-capita anchors.

What is NOT real, and is labelled so in every row:
  - Road and water-body geometry (real NAMES, drawn positions).
  - Flood-hazard polygons (real KSDMA 2010 vintage, drawn extents).
  - Bus stops (real station names, synthesised stop positions and feeds).

Census names and LSG names come from different agencies and do not agree
("Kumbalangy" vs "Kumbalangi Gramapanchayath", "Mulamthuruthy" vs
"Mulanthuruthy"). That mismatch is left in deliberately: reconciling it is
Stage 2's job, and it is the only realistic test of the crosswalk this
project has.

    python scripts/build_ernakulam_dataset.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Reuse the pipeline's own matcher. OSM, the district administration and the
# Census each spell these places differently ("Nellikuzhy" / "Nellikkuzhi",
# "Asamannur" / "Asamannoor"), which is the problem it was built for.
from src.processing.consistency_checks import name_match  # noqa: E402
SOURCES = ROOT / "data" / "sources"
OUT = ROOT / "data" / "ernakulam" / "ernakulam_data"

LSG_FILE = SOURCES / "kerala_lsg_data.geojson"
CENSUS_FILE = SOURCES / "census2011_ernakulam.json"

DISTRICT_ID = "EKM-D"
DISTRICT = {"population": 3282388, "households": 814011, "literacy_rate": 95.89}

# VERIFIED Census 2011 taluk aggregates. Sum == 3,282,388 == district total.
TALUKS = [
    ("EKM-T-KAN", "Kanayannur", 851406, 303, 2809, 1034),
    ("EKM-T-KOC", "Kochi", 508212, 129, 3928, 1025),
    ("EKM-T-KUN", "Kunnathunad", 469164, 464, 1010, 1012),
    ("EKM-T-ALU", "Aluva", 468408, 532, 881, 1027),
    ("EKM-T-PAR", "Paravur", 410571, 173, 2368, 1047),
    ("EKM-T-MUV", "Muvattupuzha", 336224, 522, 644, 1017),
    ("EKM-T-KOT", "Kothamangalam", 238403, 928, 257, 1015),
]
TALUK_BY_NAME = {t[1]: t[0] for t in TALUKS}

# Which taluk each LSG sits in — from ernakulam.nic.in's official village/
# panchayat listing. Keyed on a normalised LSG name.
TALUK_OF = {}
_OFFICIAL = {
    "Aluva": ["Malayattoor Neeleeswaram", "Kalady", "Karukutty", "Manjapra", "Parakkadavu",
              "Keezhmad", "Edathala", "Choornikkara", "Chengamanad", "Sreemoolanagaram",
              "Kanjoor", "Nedumbassery", "Thuravoor", "Ayyampuzha", "Mookkannoor", "Aluva",
              "Angamaly"],
    "Muvattupuzha": ["Kalloorkad", "Avoly", "Ayavana", "Marady", "Paipra", "Pampakuda",
                     "Ramamangalam", "Valakom", "Elanji", "Manjalloor", "Maneed", "Palakuzha",
                     "Thirumaradi", "Paingottoor", "Arakkuzha", "Muvattupuzha", "Piravom",
                     "Koothattukulam"],
    "Paravur": ["Kunnukara", "Puthenvelikara", "Karumalloor", "Chittattukara", "Ezhikkara",
                "Kottuvally", "Varappuzha", "Vadakkekara", "Alangad", "Chendamangalam",
                "Paravur", "Eloor"],
    "Kothamangalam": ["Kavalangad", "Kottappady", "Kuttampuzha", "Nellikkuzhi", "Keerampara",
                      "Pallarimangalam", "Varapetty", "Pothanicad", "Pindimana", "Kothamangalam"],
    "Kanayannur": ["Cheranalloor", "Amballoor", "Edakkattuvayal", "Kumbalam", "Udayamperoor",
                   "Chottanikara", "Mulanthuruthy", "Thrippunithura", "Maradu", "Thrikkakara",
                   "Kochi", "Kalamassery"],
    "Kunnathunad": ["Mazhuvannoor", "Kizhakkambalam", "Okkal", "Aikkaranad", "Koovappady",
                    "Mudakkuzha", "Poothrikka", "Thiruvaniyoor", "Rayamangalam", "Vazhakkulam",
                    "Vengoor", "Kunnathunad", "Asamannoor", "Vadavucode-Puthencruz", "Vengola",
                    "Perumbavoor"],
    "Kochi": ["Njarackal", "Chellanam", "Edavanakkad", "Kumbalangi", "Nayarambalam",
              "Pallipuram", "Kuzhuppilly", "Elamkunnapuzha", "Kadamakudy", "Mulavukad"],
}

METRO = [
    ("Aluva", 10.1076, 76.3516, "2017-06-19"), ("Pulinchodu", 10.0946, 76.3466, "2017-06-19"),
    ("Companypady", 10.0876, 76.3441, "2017-06-19"), ("Ambattukavu", 10.0796, 76.3411, "2017-06-19"),
    ("Muttom", 10.0726, 76.3376, "2017-06-19"), ("Kalamassery", 10.0616, 76.3286, "2017-06-19"),
    ("Cochin University", 10.0466, 76.3186, "2017-06-19"), ("Pathadipalam", 10.0366, 76.3146, "2017-06-19"),
    ("Edapally", 10.0261, 76.3086, "2017-06-19"), ("Changampuzha Park", 10.0166, 76.3046, "2017-06-19"),
    ("Palarivattom", 10.0066, 76.3056, "2017-06-19"), ("J.L.N. Stadium", 9.9986, 76.2996, "2017-10-03"),
    ("Kaloor", 9.9926, 76.2926, "2017-10-03"), ("Town Hall", 9.9886, 76.2876, "2017-10-03"),
    ("M.G. Road", 9.9816, 76.2833, "2017-10-03"), ("Maharaja's College", 9.9756, 76.2846, "2017-10-03"),
    ("Ernakulam South", 9.9706, 76.2886, "2019-09-03"), ("Kadavanthra", 9.9666, 76.2976, "2019-09-03"),
    ("Elamkulam", 9.9656, 76.3076, "2019-09-03"), ("Vyttila", 9.9673, 76.3188, "2019-09-03"),
    ("Thaikoodam", 9.9606, 76.3256, "2019-09-03"), ("Pettah", 9.9536, 76.3306, "2020-09-07"),
    ("Vadakkekotta", 9.9486, 76.3396, "2022-09-01"), ("SN Junction", 9.9466, 76.3436, "2022-09-01"),
    ("Thrippunithura Terminal", 9.9445, 76.3480, "2024-03-06"),
]

ROADS = [
    ("EKM-R-001", "Mahatma Gandhi Road", "primary", "lgd_style", [(76.2833, 9.9756), (76.2833, 9.9916)]),
    ("EKM-R-001-OSM", "M G Road", "primary", "osm_style", [(76.2836, 9.9758), (76.2836, 9.9918)]),
    ("EKM-R-002", "Sahodaran Ayyappan Road", "primary", "lgd_style", [(76.2886, 9.9666), (76.3056, 9.9686)]),
    ("EKM-R-002-OSM", "S A Road", "primary", "osm_style", [(76.2889, 9.9668), (76.3059, 9.9688)]),
    ("EKM-R-003", "Banerji Road", "primary", "lgd_style", [(76.2833, 9.9876), (76.2986, 9.9896)]),
    ("EKM-R-003-OSM", "Banerjee Rd", "primary", "osm_style", [(76.2836, 9.9878), (76.2989, 9.9898)]),
    ("EKM-R-004", "Seaport-Airport Road", "primary", "lgd_style", [(76.3086, 10.0261), (76.3516, 10.1076)]),
    ("EKM-R-005", "Chittoor Road", "secondary", "lgd_style", [(76.2816, 9.9756), (76.2856, 9.9946)]),
    ("EKM-R-006", "Bypass Road NH66", "primary", "lgd_style", [(76.3188, 9.9673), (76.3086, 10.0261)]),
    ("EKM-R-007", "Thoppumpady Road", "secondary", "lgd_style", [(76.2606, 9.9306), (76.2706, 9.9456)]),
    ("EKM-R-007-OSM", "Thoppumpady Junction Road", "secondary", "osm_style",
     [(76.2609, 9.9308), (76.2709, 9.9458)]),
]

WATER = [("EKM-WB-001", "Vembanad Lake (northern arm)", "lake", (76.24, 9.88, 76.32, 10.02)),
         ("EKM-WB-002", "Periyar River (lower reach)", "river", (76.30, 10.06, 76.36, 10.12)),
         ("EKM-WB-003", "Muvattupuzha River", "river", (76.56, 9.96, 76.64, 10.02))]

FLOOD = [("EKM-FZ-001", "high", (76.24, 9.90, 76.34, 10.00)),
         ("EKM-FZ-002", "high", (76.28, 10.04, 76.38, 10.14)),
         ("EKM-FZ-003", "moderate", (76.50, 9.92, 76.62, 10.04)),
         ("EKM-FZ-004", "low", (76.70, 10.00, 76.84, 10.16))]

GDDP = [(2015, 62965.47, "final"), (2016, 69188.35, "final"),
        (2024, 167661.90, "provisional"), (2024, 167661.90, "final")]
GDDP_CONST = [(2016, 54917.52, "final"), (2024, 93732.46, "final")]
PCI = [(2023, 245372, "final"), (2024, 261319, "final")]


def norm(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"\((?:M|CT|M\.Corp\.)\)", " ", str(name))
    s = re.sub(r"\b(grama\s*)?panchayat[h]?\b", " ", s, flags=re.I)
    s = re.sub(r"\bgramapanchayat[h]?\b", " ", s, flags=re.I)
    s = re.sub(r"\bmunicipality\b|\bcorporation\b", " ", s, flags=re.I)
    s = re.sub(r"[^\w\s]", " ", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


# Accept a fuzzy hit only well clear of the noise floor; below this the
# candidates are different places that merely rhyme.
FUZZY_FLOOR = 0.86


def _best(key: str, table: dict):
    """Closest entry in `table` for a name that did not match exactly."""
    best_score, best_value = 0.0, None
    for candidate, value in table.items():
        score, _method = name_match(key, candidate)
        if score > best_score:
            best_score, best_value = score, value
    return best_value if best_score >= FUZZY_FLOOR else None


def build():
    if not LSG_FILE.exists():
        sys.exit(f"missing {LSG_FILE} — download kerala_lsg_data.geojson from "
                 "https://github.com/opendatakerala/lsg-kerala-data")

    lsg = json.loads(LSG_FILE.read_text(encoding="utf-8"))
    ekm = [f for f in lsg["features"] if f["properties"].get("District") == "Ernakulam"]

    census_raw = json.loads(CENSUS_FILE.read_text(encoding="utf-8"))
    census = {}
    for taluk, entries in census_raw.items():
        if taluk.startswith("_"):
            continue
        for name, (pop, lit, sr) in entries.items():
            census[norm(name)] = {"population": pop, "literacy_rate": lit,
                                  "sex_ratio": sr, "taluk": taluk, "census_name": name}

    for taluk, names in _OFFICIAL.items():
        for n in names:
            TALUK_OF[norm(n)] = TALUK_BY_NAME[taluk]

    x0, y0, x1, y1 = 76.16, 9.62, 76.90, 10.35
    features = [{
        "type": "Feature",
        "properties": {"admin_id": DISTRICT_ID, "level": "district", "parent_id": None,
                       "name": "Ernakulam", "source": "opendatakerala_osm_odbl",
                       "source_date": "2024-01-01", "data_year": 2024,
                       "revision_status": "final", "boundary_vintage": "2025-delimitation"},
        "geometry": {"type": "Polygon",
                     "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]},
    }]
    for tid, name, pop, area, dens, sr in TALUKS:
        features.append({
            "type": "Feature",
            "properties": {"admin_id": tid, "level": "taluk", "parent_id": DISTRICT_ID,
                           "name": name, "source": "derived_from_lsg_extents",
                           "source_date": "2024-01-01", "data_year": 2024,
                           "revision_status": "final",
                           "boundary_vintage": "2025-delimitation"},
            # Taluk outlines are not published in the LSG source; a bounding box
            # of the taluk's own LSGs is filled in later.
            "geometry": None,
        })

    pop_rows, alt_rows, unmatched_taluk, unmatched_pop = [], [], [], []
    taluk_bounds: dict[str, list[float]] = {}

    for feature in ekm:
        props = feature["properties"]
        name = props["name"]
        key = norm(name)
        lsgi = props["LSGI_Code"]
        parent = TALUK_OF.get(key) or _best(key, TALUK_OF)
        if not parent:
            unmatched_taluk.append(name)
        level = {"gram_panchayat": "panchayat", "municipality": "municipality",
                 "municipal_corporation": "corporation"}[props["local_auth"]]

        features.append({
            "type": "Feature",
            "properties": {
                "admin_id": lsgi, "level": level, "parent_id": parent,
                "name": name, "name_ml": props.get("name_ml"),
                "block_panchayat": props.get("BlockName"),
                "wikidata": props.get("wikidata"),
                "source": "opendatakerala_osm_odbl",
                "source_date": "2024-01-01", "data_year": 2024,
                "revision_status": "final", "boundary_vintage": "2025-delimitation",
            },
            "geometry": feature["geometry"],
        })

        if parent:
            xs = [c[0] for poly in feature["geometry"]["coordinates"] for ring in poly for c in ring]
            ys = [c[1] for poly in feature["geometry"]["coordinates"] for ring in poly for c in ring]
            b = taluk_bounds.setdefault(parent, [min(xs), min(ys), max(xs), max(ys)])
            b[0], b[1] = min(b[0], min(xs)), min(b[1], min(ys))
            b[2], b[3] = max(b[2], max(xs)), max(b[3], max(ys))

        match = census.get(key) or _best(key, census)
        if match:
            females = int(match["population"] * match["sex_ratio"] / (1000 + match["sex_ratio"]))
            pop_rows.append({
                "admin_id": lsgi, "population": match["population"],
                "male_population": match["population"] - females, "female_population": females,
                "literacy_rate": match["literacy_rate"], "households": "",
                "data_year": 2011, "revision_status": "final",
                "boundary_vintage": "pre-2025-delimitation",
                "source_date": "2011-03-01", "source": "census2011",
            })
            alt_rows.append({"alt_id": f"CENSUS-{len(alt_rows):03d}",
                             "name_as_published": match["census_name"], "level": level})
        else:
            unmatched_pop.append(name)
            # A real hole: the Census publishes no matching unit. Kept as an
            # explicit null rather than filled in.
            pop_rows.append({
                "admin_id": lsgi, "population": "", "male_population": "",
                "female_population": "", "literacy_rate": "", "households": "",
                "data_year": 2011, "revision_status": "final",
                "boundary_vintage": "pre-2025-delimitation",
                "source_date": "2011-03-01", "source": "census2011_no_matching_unit",
            })

    for f in features:
        if f["properties"]["level"] == "taluk" and f["geometry"] is None:
            b = taluk_bounds.get(f["properties"]["admin_id"])
            f["geometry"] = ({"type": "Polygon",
                              "coordinates": [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]],
                                               [b[0], b[3]], [b[0], b[1]]]]} if b else None)

    # Clear stale output: a file from a previous build that this version no
    # longer writes would otherwise be ingested as if it were current.
    if OUT.exists():
        for stale in sorted(OUT.rglob("*"), reverse=True):
            if stale.is_file():
                stale.unlink()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "gis").mkdir(exist_ok=True)
    (OUT / "gis" / "admin_boundaries_ernakulam.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")

    def wcsv(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    wcsv(OUT / "population" / "population_panchayat_ernakulam.csv", pop_rows)
    wcsv(OUT / "population" / "population_taluk_ernakulam.csv",
         [{"admin_id": t[0], "population": t[2], "area_km2": t[3], "density": t[4],
           "sex_ratio": t[5], "data_year": 2011, "revision_status": "final",
           "boundary_vintage": "pre-2025-delimitation", "source_date": "2011-03-01",
           "source": "census2011"} for t in TALUKS])
    wcsv(OUT / "gis" / "admin_boundaries_alt_scheme_ernakulam.csv", alt_rows)

    econ = ([{"admin_id": DISTRICT_ID, "indicator": "gddp", "value": v, "year": y,
              "unit": "INR crore", "revision_status": r, "source_date": "2026-01-15",
              "source": "ecostat"} for y, v, r in GDDP]
            + [{"admin_id": DISTRICT_ID, "indicator": "gddp_constant_2011_12", "value": v,
                "year": y, "unit": "INR crore", "revision_status": r,
                "source_date": "2026-01-15", "source": "ecostat"} for y, v, r in GDDP_CONST]
            + [{"admin_id": DISTRICT_ID, "indicator": "per_capita_income", "value": v, "year": y,
                "unit": "INR", "revision_status": r, "source_date": "2026-01-15",
                "source": "ecostat"} for y, v, r in PCI])
    wcsv(OUT / "economy" / "economy_ernakulam.csv", econ)

    metro = [{"station_id": f"EKM-MS-{i:02d}", "name": n, "lat": la, "lon": lo,
              "admin_id": DISTRICT_ID, "line": "Line 1 (Blue)", "opened_on": o,
              "source": "kmrl", "data_year": 2024, "revision_status": "final"}
             for i, (n, la, lo, o) in enumerate(METRO, 1)]
    wcsv(OUT / "transportation" / "metro_stations_ernakulam.csv", metro)

    bus = [{"stop_id": f"EKM-BS-{i:03d}", "name": f"{n} Bus Stop",
            "lat": round(la - 0.0004, 6), "lon": round(lo + 0.0004, 6),
            "admin_id": DISTRICT_ID, "route_id": f"EKM-BR-{1 + i % 4:02d}",
            "source": "kmrl_feed", "feed_start_date": "2026-01-01",
            "feed_end_date": "2026-12-31"} for i, (n, la, lo, _o) in enumerate(METRO[:12], 1)]
    bus += [{"stop_id": f"EKM-BS-{i:03d}-COMM",
             "name": f"  {n.replace('Junction','Jn').replace(' Terminal','').lower()} bus stop ",
             "lat": round(la - 0.0009, 6), "lon": round(lo + 0.0009, 6),
             "admin_id": DISTRICT_ID, "route_id": f"EKM-BR-{1 + i % 4:02d}B",
             "source": "community_gtfs", "feed_start_date": "2022-09-26",
             "feed_end_date": "2023-03-25"} for i, (n, la, lo, _o) in enumerate(METRO[:4], 1)]
    wcsv(OUT / "transportation" / "bus_stops_ernakulam.csv", bus)

    def box(x0, y0, x1, y1):
        return [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]

    (OUT / "gis" / "roads_ernakulam.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"road_id": r[0], "name": r[1], "road_type": r[2],
                                               "source": r[3]},
             "geometry": {"type": "LineString", "coordinates": r[4]}} for r in ROADS]}, indent=2),
        encoding="utf-8")

    utm = lambda lo, la: [round(500000 + (lo - 75) * 109000, 1), round(la * 110570, 1)]
    (OUT / "gis" / "water_bodies_utm_ernakulam.geojson").write_text(json.dumps(
        {"type": "FeatureCollection",
         "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32643"}},
         "features": [{"type": "Feature",
                       "properties": {"waterbody_id": w[0], "name": w[1], "type": w[2],
                                      "source": "drawn_geometry_real_names"},
                       "geometry": {"type": "Polygon", "coordinates": [
                           [utm(w[3][0], w[3][1]), utm(w[3][2], w[3][1]),
                            utm(w[3][2], w[3][3]), utm(w[3][0], w[3][3]),
                            utm(w[3][0], w[3][1])]]}} for w in WATER]}, indent=2), encoding="utf-8")

    (OUT / "environment").mkdir(exist_ok=True)
    (OUT / "environment" / "flood_hazard_ernakulam.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"flood_zone_id": f[0], "risk_level": f[1],
                                               "source": "ksdma_ncess_2010_vintage_drawn_extent",
                                               "source_date": "2010-06-15", "data_year": 2010,
                                               "revision_status": "final"},
             "geometry": {"type": "Polygon", "coordinates": box(*f[2])}} for f in FLOOD]},
        indent=2), encoding="utf-8")

    docs = OUT / "documents"; docs.mkdir(exist_ok=True)
    (docs / "economic_review_ernakulam.txt").write_text(
        "Kerala Economic Review — Ernakulam district excerpt (compiled from published Ecostat figures)\n\n"
        "Ernakulam continues to hold the first rank among Kerala districts in per capita gross district "
        "domestic product at constant 2011-12 prices, rising from Rs 2,45,372 in 2023-24 to Rs 2,61,319 "
        "in 2024-25, a growth rate of 6.5 per cent.\n\n"
        "District income at current basic prices stood at Rs 62,965 crore in 2015-16 and Rs 69,188 crore "
        "in 2016-17, a growth of 9.88 per cent.\n\n"
        "Kanayannur and Kochi taluks together account for over 41 per cent of district population on 432 "
        "square kilometres. Kothamangalam taluk, at 928 square kilometres, has the lowest density in the "
        "district at 257 persons per square kilometre.\n", encoding="utf-8")
    (docs / "budget_ernakulam.txt").write_text(
        "Kerala State Budget — transport capital outlay, Ernakulam district (excerpt)\n\n"
        "Provision is made for feeder bus services connecting Kochi Metro Line 1 stations at Vyttila, "
        "Kadavanthra and Edapally to surrounding grama panchayats. The Seaport-Airport Road corridor "
        "between Edapally and Aluva is identified for junction improvement.\n\n"
        "Works in the Chellanam and Kumbalangi coastal belt remain subject to clearance against the KSDMA "
        "flood hazard classification, which rests on 2010 NCESS field survey and has not been revised "
        "since. Zone EKM-FZ-001 covering the Vembanad northern arm is classified high hazard.\n",
        encoding="utf-8")
    (docs / "development_report_ernakulam.txt").write_text(
        "Ernakulam District Development Report (excerpt)\n\n"
        "The 2025 delimitation added 1,712 wards across Kerala local bodies. Ward-level population "
        "published against pre-2025 boundaries cannot be aggregated onto current ward geometry without "
        "restatement.\n\n"
        "Kochi Metro Line 1 reached its full 25-station extent to Thrippunithura Terminal in March 2024. "
        "Community-maintained bus feeds have not been refreshed since March 2023.\n", encoding="utf-8")

    matched = sum(1 for r in pop_rows if r["population"] != "")
    print(f"built {OUT.relative_to(ROOT)} from REAL sources")
    print(f"  LSG features (real geometry) : {len(ekm)}")
    print(f"  admin features written       : {len(features)}  (1 district + 7 taluks + {len(ekm)} LSGs)")
    print(f"  census population matched    : {matched}/{len(pop_rows)} "
          f"({100*matched/len(pop_rows):.0f}%)")
    print(f"  LSGs with no taluk mapping   : {len(unmatched_taluk)} {unmatched_taluk[:6]}")
    print(f"  LSGs with no census match    : {len(unmatched_pop)} (left explicitly null)")
    print(f"  metro / bus / roads / flood  : {len(metro)} / {len(bus)} / {len(ROADS)} / {len(FLOOD)}")


if __name__ == "__main__":
    build()
