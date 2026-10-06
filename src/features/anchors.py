"""District-level published figures used to anchor the economic approximations.

Nothing in this file is measured at local-body level, and nothing in it may be
presented as if it were. Each entry is a published district aggregate with its
publisher, its as-on date and the URL it came from, held here so that a reader
auditing an approximation can check the number it rests on without reading the
code that spends it.

Why anchors at all: MSME registration, income and employment are not published
below district level in Kerala, and inventing a local figure would be worse than
reporting none. An anchored approximation is different from an invented one — it
is constrained to reproduce a real published total, and its allocator is stated
so the shape of the estimate can be argued with.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Anchor:
    value: float
    unit: str
    as_on: str
    publisher: str
    source_level: int
    data_year: int
    url: str
    note: str = ""


# Udyam + Udyam Assist registrations in Ernakulam district. Quoted in a Lok
# Sabha unstarred-question answer; the per-size split is 160,865 micro, 4,877
# small and 458 medium. Registration count is not the same as operating
# enterprises — Udyam has no deregistration flow — so this is an upper bound on
# enterprise presence and is labelled as such wherever it is spent.
MSME_REGISTRATIONS = Anchor(
    value=166_200, unit="registered enterprises", as_on="2024-12-15",
    publisher="Ministry of MSME, reply to Lok Sabha unstarred question",
    source_level=1, data_year=2024,
    url="https://eparlib.sansad.in/bitstream/123456789/2987130/1/AU3956_Rzq5jY.pdf",
    note="Udyam + Udyam Assist Platform; 160,865 micro / 4,877 small / 458 medium")

# Ernakulam per-capita income, already carried in the project's own economy
# table from Kerala Ecostat. Restated here because the approximation is
# constrained to reproduce it, which makes it part of the method rather than
# just an input.
PER_CAPITA_INCOME = Anchor(
    value=261_319, unit="INR per person per year", as_on="2024-03-31",
    publisher="Kerala State Planning Board / Ecostat, district domestic product",
    source_level=1, data_year=2024,
    url="https://www.ecostat.kerala.gov.in/",
    note="highest of Kerala's 14 districts; held in data/.../economy_ernakulam.csv")

DISTRICT_GDDP_CRORE = Anchor(
    value=167_661.9, unit="INR crore", as_on="2024-03-31",
    publisher="Kerala Ecostat, gross district domestic product (current prices)",
    source_level=1, data_year=2024,
    url="https://www.ecostat.kerala.gov.in/",
    note="provisional then finalised; held in data/.../economy_ernakulam.csv")

# Census 2011 district total. The local-body population figures the allocators
# use come from the same census, so allocation shares are internally consistent
# even though the census is now old.
DISTRICT_POPULATION = Anchor(
    value=3_282_388, unit="persons", as_on="2011-03-01",
    publisher="Census of India 2011",
    source_level=1, data_year=2011,
    url="https://www.census2011.co.in/census/district/278-ernakulam.html",
    note="literacy 95.89%; sum of the 97 local-body figures differs slightly "
         "because of pre-2025 boundary vintage")

# ---------------------------------------------------------------------------
# Health, education and fiscal anchors, added with the Healthcare, Education and
# Budget agents.
# ---------------------------------------------------------------------------

# Government health facilities in Ernakulam. The district total is published;
# the per-local-body distribution is not, so facility *location* comes from OSM
# (measured) and this total is only used to sanity-check that extraction.
HEALTH_FACILITIES = Anchor(
    value=87, unit="government health facilities", as_on="2020-04-07",
    publisher="District administration, reported via PTI",
    source_level=4, data_year=2020,
    url="https://www.business-standard.com/article/pti-stories/"
        "every-panchayat-in-ernakulam-district-braces-up-for-virus-war-120040701843_1.html",
    note="10 community health centres, 12 block PHCs, 65 mini PHCs. Government "
         "facilities only, so it excludes the large private sector that OSM maps")

# Schools in Ernakulam, all managements.
SCHOOLS = Anchor(
    value=323, unit="schools", as_on="2024-01-01",
    publisher="District education office listings, via Wikipedia compilation",
    source_level=4, data_year=2024,
    url="https://en.wikipedia.org/wiki/List_of_schools_in_Ernakulam_district",
    note="88 government, 178 aided, 57 unaided. Counts of schools by management "
         "vary by source and by whether higher-secondary sections are counted "
         "separately, so this is an order-of-magnitude check on the OSM extract, "
         "not a target to reproduce")

# The fiscal rule the Budget agent implements. This is the real distribution
# formula, not an invention: Kerala's State Finance Commission distributes the
# basic grant among local governments on population, area and inverse own
# income. Implementing it is what makes the budget split arguable rather than
# arbitrary.
DEVOLUTION_FORMULA = Anchor(
    value=0.80, unit="weight on 2011 population", as_on="2021-04-01",
    publisher="Kerala State Finance Commission, basic grant distribution",
    source_level=1, data_year=2021,
    url="https://keralaeconomy.com/admin/pdfs/5th%20SFC%20recomendation.pdf",
    note="80% population (Census 2011), 10% area, 10% inverse of own income. "
         "Own income is not published per local body, so the engine substitutes "
         "a measured proxy and says so")

# What the state actually devolves, for sizing a plausible local envelope.
DEVOLVED_PLAN_SHARE = Anchor(
    value=28.09, unit="percent of the State Plan outlay", as_on="2024-04-01",
    publisher="Government of Kerala, Development Fund to local governments",
    source_level=1, data_year=2024,
    url="https://en.wikipedia.org/wiki/Local_government_in_Kerala",
    note="2024-25 Development Fund share. 2023-24 was 27%, about INR 8,258 "
         "crore. Separate from the General Purpose Fund (4% of state own tax "
         "revenue) and the Maintenance Fund (6.5%)")

ALL = {
    "msme_registrations": MSME_REGISTRATIONS,
    "per_capita_income": PER_CAPITA_INCOME,
    "district_gddp_crore": DISTRICT_GDDP_CRORE,
    "district_population": DISTRICT_POPULATION,
    "health_facilities": HEALTH_FACILITIES,
    "schools": SCHOOLS,
    "devolution_formula": DEVOLUTION_FORMULA,
    "devolved_plan_share": DEVOLVED_PLAN_SHARE,
}


def table() -> list[dict]:
    """The anchors as rows, for the data dictionary and the API."""
    return [{"anchor": key, **vars(anchor)} for key, anchor in ALL.items()]


if __name__ == "__main__":
    for row in table():
        print(f"{row['anchor']:>22}  {row['value']:>12,.1f} {row['unit']}")
        print(f"{'':>22}  as on {row['as_on']} · L{row['source_level']} · {row['publisher']}")
        print(f"{'':>22}  {row['url']}")
        if row["note"]:
            print(f"{'':>22}  {row['note']}")
        print()
