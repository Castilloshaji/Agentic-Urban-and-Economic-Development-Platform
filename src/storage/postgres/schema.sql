-- Ernakulam Digital Twin — Stage 3 PostGIS schema.
--
-- Three conventions run through every table:
--
--   1. VINTAGE COLUMNS. source, source_date, data_year, revision_status and
--      (where the entity is tied to an administrative delimitation)
--      boundary_vintage. Nothing here is a single current truth: the 2010 flood
--      layer and the 2026 road layer both live in this database, and a query
--      that does not say which vintage it wants is a query with a bug in it.
--
--   2. SIDECAR CONFIDENCE. match_confidence plus soft_check_flags carry Stage 2's
--      soft findings into the database instead of leaving them in a JSON file
--      nobody joins against. A row flagged as a near-duplicate still loads — it
--      loads *labelled*, so downstream agents can weight it.
--
--   3. SURROGATE KEYS. Natural ids repeat across dataset editions (an official
--      layer and a community layer both call a place DEMO-P-01), so every table
--      has a synthetic pk and a natural-key UNIQUE that includes dataset_edition.
--      Deduplication is the digital twin layer's job, not the loader's.
--
-- Apply with:
--   psql "$DATABASE_URL" -f src/storage/postgres/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

-- Stage 2 emits exactly these three levels; anything else is a loader bug.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'match_confidence_t') THEN
        CREATE TYPE match_confidence_t AS ENUM ('exact', 'crosswalked', 'unmatched-estimate');
    END IF;
END
$$;

DROP TABLE IF EXISTS admin_code_xref, bus_stop, metro_station, population_legacy_ward,
                     population_panchayat,
                     population_taluk,
                     flood_zone, water_body, road, economic_indicator, admin_boundary CASCADE;


-- ---------------------------------------------------------------------------
-- Administrative boundaries
-- ---------------------------------------------------------------------------
CREATE TABLE admin_boundary (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id          TEXT        NOT NULL,
    level             TEXT        NOT NULL,
    parent_id         TEXT,
    name              TEXT,
    normalized_name   TEXT,
    geom              GEOMETRY(MultiPolygon, 4326) NOT NULL,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER,
    revision_status   TEXT,
    boundary_vintage  TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT admin_boundary_natural_key UNIQUE (admin_id, boundary_vintage, dataset_edition)
);

CREATE INDEX admin_boundary_geom_gist   ON admin_boundary USING GIST (geom);
CREATE INDEX admin_boundary_admin_id_ix ON admin_boundary (admin_id);
CREATE INDEX admin_boundary_parent_ix   ON admin_boundary (parent_id);
CREATE INDEX admin_boundary_vintage_ix  ON admin_boundary (boundary_vintage);


-- ---------------------------------------------------------------------------
-- Admin code crosswalk
-- ---------------------------------------------------------------------------
-- Stage 2 reconciles LGD / LSG / Census codes onto one canonical admin_id by
-- normalized name. That mapping is the answer to "which of these ids is the same
-- place", so it belongs in the database where an agent can join against it —
-- not only in a CSV nobody queries. Unmatched rows are loaded too: a code with
-- no canonical match is a fact worth being able to look up.
CREATE TABLE admin_code_xref (
    id                 BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_name    TEXT,
    match_status       TEXT        NOT NULL,
    match_confidence   match_confidence_t NOT NULL DEFAULT 'exact',
    match_method       TEXT,
    canonical_admin_id TEXT,
    canonical_name     TEXT,
    canonical_level    TEXT,
    alt_id             TEXT,
    alt_name_as_published TEXT,
    alt_level          TEXT,
    alt_source_file    TEXT,
    dataset_edition    TEXT       NOT NULL DEFAULT '',
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX admin_code_xref_canonical_ix ON admin_code_xref (canonical_admin_id);
CREATE INDEX admin_code_xref_alt_ix       ON admin_code_xref (alt_id);
CREATE INDEX admin_code_xref_status_ix    ON admin_code_xref (match_status);


-- ---------------------------------------------------------------------------
-- Economic indicators (GDDP, per-capita income, ...)
-- ---------------------------------------------------------------------------
-- revision_status is part of the natural key on purpose: a provisional estimate
-- and its later final revision are two published facts, and Stage 2's rule is to
-- keep both. Downstream reads should filter to 'final' rather than expect one row.
CREATE TABLE economic_indicator (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id          TEXT        NOT NULL,
    indicator         TEXT        NOT NULL,
    value             NUMERIC,
    value_unit        TEXT,        -- unit extracted from the value cell ("Cr")
    unit              TEXT,        -- unit as declared by the source ("INR crore")
    year              INTEGER     NOT NULL,

    source            TEXT,
    source_date       DATE,
    revision_status   TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT economic_indicator_natural_key
        UNIQUE (admin_id, indicator, year, revision_status, dataset_edition)
);

CREATE INDEX economic_indicator_admin_ix     ON economic_indicator (admin_id);
CREATE INDEX economic_indicator_series_ix    ON economic_indicator (indicator, year);


-- ---------------------------------------------------------------------------
-- Roads
-- ---------------------------------------------------------------------------
CREATE TABLE road (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    road_id           TEXT        NOT NULL,
    name              TEXT,
    road_type         TEXT,
    geom              GEOMETRY(MultiLineString, 4326) NOT NULL,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER,
    revision_status   TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT road_natural_key UNIQUE (road_id, dataset_edition)
);

CREATE INDEX road_geom_gist ON road USING GIST (geom);
CREATE INDEX road_type_ix   ON road (road_type);


-- ---------------------------------------------------------------------------
-- Water bodies
-- ---------------------------------------------------------------------------
CREATE TABLE water_body (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    waterbody_id      TEXT        NOT NULL,
    name              TEXT,
    water_body_type   TEXT,       -- "type" in the source; reserved-ish word here
    geom              GEOMETRY(MultiPolygon, 4326) NOT NULL,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER,
    revision_status   TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT water_body_natural_key UNIQUE (waterbody_id, dataset_edition)
);

CREATE INDEX water_body_geom_gist ON water_body USING GIST (geom);


-- ---------------------------------------------------------------------------
-- Flood hazard zones
-- ---------------------------------------------------------------------------
-- data_year matters more here than anywhere else: KSDMA's classification rests on
-- 2010 NCESS fieldwork, so this layer is a static historical hazard map and must
-- never be read as a current-conditions feed.
CREATE TABLE flood_zone (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    flood_zone_id     TEXT        NOT NULL,
    risk_level        TEXT,
    geom              GEOMETRY(MultiPolygon, 4326) NOT NULL,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER,
    revision_status   TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT flood_zone_natural_key UNIQUE (flood_zone_id, dataset_edition)
);

CREATE INDEX flood_zone_geom_gist  ON flood_zone USING GIST (geom);
CREATE INDEX flood_zone_risk_ix    ON flood_zone (risk_level);
CREATE INDEX flood_zone_year_ix    ON flood_zone (data_year);


-- ---------------------------------------------------------------------------
-- Population — current panchayat level
-- ---------------------------------------------------------------------------
-- literacy_rate and every count are nullable by design. A missing observation
-- stays NULL; Stage 2 never coerces it to 0, and neither does this table.
CREATE TABLE population_panchayat (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id          TEXT        NOT NULL,
    population        INTEGER,
    male_population   INTEGER,
    female_population INTEGER,
    literacy_rate     NUMERIC(5, 2),
    households        INTEGER,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER     NOT NULL,
    revision_status   TEXT,
    boundary_vintage  TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT population_panchayat_natural_key
        UNIQUE (admin_id, data_year, revision_status, boundary_vintage, dataset_edition)
);

CREATE INDEX population_panchayat_admin_ix   ON population_panchayat (admin_id);
CREATE INDEX population_panchayat_vintage_ix ON population_panchayat (boundary_vintage);


-- ---------------------------------------------------------------------------
-- Population — taluk level
-- ---------------------------------------------------------------------------
-- Kept separate from the panchayat table rather than unioned into it. Taluk
-- figures are published directly by the Census, while panchayat figures in this
-- project are derived from them — merging the two would let a derived number sit
-- in the same column as the measurement it was derived from, indistinguishable.
CREATE TABLE population_taluk (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id          TEXT        NOT NULL,
    population        INTEGER,
    area_km2          NUMERIC(10, 2),
    density           INTEGER,
    sex_ratio         INTEGER,
    households        INTEGER,
    literacy_rate     NUMERIC(5, 2),

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER     NOT NULL,
    revision_status   TEXT,
    boundary_vintage  TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT population_taluk_natural_key
        UNIQUE (admin_id, data_year, revision_status, boundary_vintage, dataset_edition)
);

CREATE INDEX population_taluk_admin_ix   ON population_taluk (admin_id);
CREATE INDEX population_taluk_vintage_ix ON population_taluk (boundary_vintage);


-- ---------------------------------------------------------------------------
-- Population — legacy ward level
-- ---------------------------------------------------------------------------
-- Kept in its own table rather than unioned with the panchayat figures: the
-- pre-2025 ward split does not nest inside the current one, so any join between
-- these two tables is an estimate and should be written as one.
CREATE TABLE population_legacy_ward (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    legacy_ward_id    TEXT        NOT NULL,
    panchayat_id      TEXT,
    population        INTEGER,
    note              TEXT,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER     NOT NULL,
    revision_status   TEXT,
    boundary_vintage  TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT population_legacy_ward_natural_key
        UNIQUE (legacy_ward_id, data_year, revision_status, dataset_edition)
);

CREATE INDEX population_legacy_ward_parent_ix  ON population_legacy_ward (panchayat_id);
CREATE INDEX population_legacy_ward_vintage_ix ON population_legacy_ward (boundary_vintage);


-- ---------------------------------------------------------------------------
-- Metro stations
-- ---------------------------------------------------------------------------
CREATE TABLE metro_station (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id        TEXT        NOT NULL,
    name              TEXT,
    line              TEXT,
    admin_id          TEXT,
    geom              GEOMETRY(Point, 4326) NOT NULL,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER,
    revision_status   TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT metro_station_natural_key UNIQUE (station_id, dataset_edition)
);

CREATE INDEX metro_station_geom_gist ON metro_station USING GIST (geom);
CREATE INDEX metro_station_admin_ix  ON metro_station (admin_id);


-- ---------------------------------------------------------------------------
-- Bus stops
-- ---------------------------------------------------------------------------
-- feed_start_date / feed_end_date are the GTFS service window. A stop whose feed
-- ended years ago still loads, flagged stale — absence of a stop is a worse lie
-- than a stop with a known-expired feed.
CREATE TABLE bus_stop (
    id                BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stop_id           TEXT        NOT NULL,
    name              TEXT,
    route_id          TEXT,
    admin_id          TEXT,
    geom              GEOMETRY(Point, 4326) NOT NULL,
    feed_start_date   DATE,
    feed_end_date     DATE,

    source            TEXT,
    source_date       DATE,
    data_year         INTEGER,
    revision_status   TEXT,

    dataset_edition   TEXT        NOT NULL DEFAULT '',
    source_file       TEXT,
    match_confidence  match_confidence_t NOT NULL DEFAULT 'exact',
    soft_check_flags  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    loaded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT bus_stop_natural_key UNIQUE (stop_id, dataset_edition)
);

CREATE INDEX bus_stop_geom_gist    ON bus_stop USING GIST (geom);
CREATE INDEX bus_stop_admin_ix     ON bus_stop (admin_id);
CREATE INDEX bus_stop_feed_end_ix  ON bus_stop (feed_end_date);
