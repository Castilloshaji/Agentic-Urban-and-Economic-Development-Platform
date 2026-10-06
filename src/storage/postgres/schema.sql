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

DROP TABLE IF EXISTS audit_event, decision, constraint_result, agent_priority,
                     scenario_parameter, scenario_run, transit_feature,
                     admin_hazard_feature, admin_derived_feature,
                     admin_code_xref, bus_stop, metro_station, population_legacy_ward,
                     population_panchayat,
                     population_taluk,
                     flood_zone, water_body, road, economic_indicator, admin_boundary CASCADE;

-- NOTE: `implementation` is deliberately NOT in that list. Every other table
-- here is derived from files and can be rebuilt by re-running the pipeline.
-- The implementation ledger is the opposite: it records decisions a person
-- made, which exist nowhere else and cannot be regenerated. Dropping it on a
-- schema reapply would destroy the only copy, so it is created
-- IF NOT EXISTS below and left alone.


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


-- ===========================================================================
-- REVIEW 2 — real hazard and transit features, and the decision audit trail
-- ===========================================================================

-- Zonal statistics over the KSDMA flood return-probability rasters plus the GSI
-- landslide join. One row per local body. These are the measured inputs the
-- parameter engine normalises; keeping them in the database (rather than only as
-- CSVs) is what puts them under the cross-store verification.
CREATE TABLE admin_hazard_feature (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id               TEXT    NOT NULL,
    name                   TEXT,
    local_auth             TEXT,
    flood_share_hist_10yr  NUMERIC(8,6),
    flood_share_hist_25yr  NUMERIC(8,6),
    flood_share_hist_50yr  NUMERIC(8,6),
    flood_share_hist_100yr NUMERIC(8,6),
    flood_share_hist_200yr NUMERIC(8,6),
    flood_share_hist_500yr NUMERIC(8,6),
    flood_share_rcp85_10yr  NUMERIC(8,6),
    flood_share_rcp85_25yr  NUMERIC(8,6),
    flood_share_rcp85_50yr  NUMERIC(8,6),
    flood_share_rcp85_100yr NUMERIC(8,6),
    flood_share_rcp85_200yr NUMERIC(8,6),
    flood_share_rcp85_500yr NUMERIC(8,6),
    landslide_rank         SMALLINT,
    landslide_susceptibility TEXT,
    source                 TEXT,
    source_level           SMALLINT,
    data_year              INTEGER,
    dataset_edition        TEXT NOT NULL DEFAULT 'ernakulam',
    loaded_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT admin_hazard_feature_natural_key UNIQUE (admin_id, dataset_edition)
);
CREATE INDEX admin_hazard_feature_admin_ix ON admin_hazard_feature (admin_id);
CREATE INDEX admin_hazard_feature_flood_ix ON admin_hazard_feature (flood_share_hist_50yr);

-- Accessibility derived from the real Kochi GTFS feed. feed_months_stale is
-- carried so a conclusion can never silently rest on an expired feed.
CREATE TABLE transit_feature (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id            TEXT    NOT NULL,
    area_km2            NUMERIC(10,3),
    stop_count          INTEGER,
    trips_total         INTEGER,
    stop_density_per_km2 NUMERIC(10,3),
    trips_per_stop      NUMERIC(10,2),
    nearest_stop_m      NUMERIC(12,1),
    feed_end_date       DATE,
    feed_months_stale   INTEGER,
    source              TEXT,
    source_level        SMALLINT,
    data_year           INTEGER,
    dataset_edition     TEXT NOT NULL DEFAULT 'ernakulam',
    loaded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT transit_feature_natural_key UNIQUE (admin_id, dataset_edition)
);
CREATE INDEX transit_feature_admin_ix ON transit_feature (admin_id);

-- One row per scenario execution. run_id is the join key everything else uses,
-- so a decision can be reconstructed from the database alone.
CREATE TABLE scenario_run (
    run_id            TEXT PRIMARY KEY,
    admin_id          TEXT NOT NULL,
    admin_name        TEXT,
    scenario_key      TEXT NOT NULL,
    scenario_label    TEXT,
    budget_inr_crore  NUMERIC(14,2),
    formula_version   TEXT NOT NULL,
    llm_involved      BOOLEAN NOT NULL DEFAULT FALSE,
    suitability_score NUMERIC(6,4),
    stance            TEXT,
    leading_domain    TEXT,
    constraint_verdict TEXT,
    blocking_count    INTEGER NOT NULL DEFAULT 0,
    dataset_edition   TEXT NOT NULL DEFAULT 'ernakulam',
    generated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX scenario_run_admin_ix    ON scenario_run (admin_id);
CREATE INDEX scenario_run_scenario_ix ON scenario_run (scenario_key);

-- Every parameter as it stood for that run, with provenance. This is what makes
-- a weight reproducible months later: the inputs are stored, not just the output.
CREATE TABLE scenario_parameter (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES scenario_run(run_id) ON DELETE CASCADE,
    parameter      TEXT NOT NULL,
    domain         TEXT NOT NULL,
    raw_value      NUMERIC(18,6),
    normalized     NUMERIC(8,4),
    confidence     NUMERIC(5,3) NOT NULL,
    status         TEXT NOT NULL,
    source         TEXT,
    source_level   SMALLINT,
    data_year      INTEGER,
    note           TEXT,
    CONSTRAINT scenario_parameter_key UNIQUE (run_id, parameter)
);
CREATE INDEX scenario_parameter_run_ix ON scenario_parameter (run_id);

-- The weight audit: every term of the formula, per domain, per run.
CREATE TABLE agent_priority (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES scenario_run(run_id) ON DELETE CASCADE,
    domain             TEXT NOT NULL,
    scenario_relevance NUMERIC(5,3) NOT NULL,
    relevance_term     NUMERIC(8,4) NOT NULL,
    evidence_confidence NUMERIC(6,4) NOT NULL,
    parameter_signal   NUMERIC(6,4) NOT NULL,
    signal_modulator   NUMERIC(6,4) NOT NULL,
    raw_weight         NUMERIC(10,6) NOT NULL,
    final_weight       NUMERIC(7,4) NOT NULL,
    rank               SMALLINT NOT NULL,
    parameters_used    INTEGER NOT NULL DEFAULT 0,
    parameters_skipped INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT agent_priority_key UNIQUE (run_id, domain)
);

-- Constraint outcomes. overridable_by_model is stored FALSE so the record itself
-- carries the rule that a model may not clear a block.
CREATE TABLE constraint_result (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES scenario_run(run_id) ON DELETE CASCADE,
    code          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    domain        TEXT NOT NULL,
    parameter     TEXT,
    measured_value NUMERIC(18,6),
    threshold     NUMERIC(18,6),
    message       TEXT NOT NULL,
    source        TEXT,
    source_level  SMALLINT,
    data_year     INTEGER,
    overridable_by_model BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT constraint_result_key UNIQUE (run_id, code)
);
CREATE INDEX constraint_result_severity_ix ON constraint_result (severity);

CREATE TABLE decision (
    run_id        TEXT PRIMARY KEY REFERENCES scenario_run(run_id) ON DELETE CASCADE,
    stance        TEXT NOT NULL,
    headline      TEXT NOT NULL,
    conditions    JSONB NOT NULL DEFAULT '[]'::jsonb,
    advisories    JSONB NOT NULL DEFAULT '[]'::jsonb,
    agent_outputs JSONB,
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_event (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     TEXT REFERENCES scenario_run(run_id) ON DELETE CASCADE,
    stage      TEXT NOT NULL,
    event      TEXT NOT NULL,
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_event_run_ix ON audit_event (run_id);


-- ---------------------------------------------------------------------------
-- Derived feature layers: measured values the Phase-1 tables do not carry, and
-- the estimates allocated from published district figures.
--
-- Kept in one table rather than four because every column is keyed on the same
-- thing — one local body — and because the twin reads them together. The
-- `*_is_estimate` and `econ_population_imputed` flags travel with the values so
-- a reader (or an agent) can never mistake an allocation for a measurement.
-- ---------------------------------------------------------------------------
CREATE TABLE admin_derived_feature (
    id                        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_id                  TEXT    NOT NULL,

    -- measured: OSM Southern-Zone clip
    road_density_km_per_km2   NUMERIC(10,3),
    max_road_class            SMALLINT,
    row_narrow_share          NUMERIC(6,4),
    row_arterial_km_per_km2   NUMERIC(10,3),
    row_lane_tag_coverage     NUMERIC(6,4),
    water_area_share          NUMERIC(8,5),
    water_distance_m          NUMERIC(12,1),

    -- measured: OSM healthcare and education facilities
    health_count              INTEGER,
    health_capacity           NUMERIC(10,2),
    health_nearest_m          NUMERIC(12,1),
    health_per_km2            NUMERIC(12,3),
    health_per_1000           NUMERIC(12,3),
    education_count           INTEGER,
    education_capacity        NUMERIC(10,2),
    education_nearest_m       NUMERIC(12,1),
    education_per_km2         NUMERIC(12,3),
    education_per_1000        NUMERIC(12,3),

    -- measured: KMRL station list
    nearest_metro_m           NUMERIC(12,1),
    nearest_metro_station     TEXT,
    metro_stations_in_unit    SMALLINT,

    -- estimated: district anchors allocated down (never a measurement)
    msme_estimated_count      NUMERIC(12,1),
    msme_per_1000_people      NUMERIC(10,2),
    msme_density_per_km2      NUMERIC(12,1),
    income_per_capita_estimate NUMERIC(14,1),
    gddp_share_estimate_crore NUMERIC(14,2),
    employment_capacity_index NUMERIC(6,4),
    econ_allocation           TEXT,
    econ_population_imputed   SMALLINT NOT NULL DEFAULT 0,
    econ_is_estimate          SMALLINT NOT NULL DEFAULT 1,

    source                    TEXT,
    source_level              SMALLINT,
    data_year                 INTEGER,
    dataset_edition           TEXT NOT NULL DEFAULT 'ernakulam',
    loaded_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT admin_derived_feature_natural_key UNIQUE (admin_id, dataset_edition)
);
CREATE INDEX admin_derived_feature_admin_ix ON admin_derived_feature (admin_id);


-- ---------------------------------------------------------------------------
-- Implementation ledger: what someone decided to actually do.
--
-- Not derived data. Every row is a human commitment, so this table survives a
-- schema reapply and is never truncated by a loader.
--
-- The important design point is what this table does NOT do: it never alters a
-- measured parameter. When drainage work completes, `flood_risk` still reads
-- what KSDMA measured, because the alternative — editing the measurement to
-- reflect the intervention — would fabricate data and destroy the provenance
-- the whole engine rests on. Instead the ledger is consulted alongside the
-- measurements: the shortfall is still real, and it is now also "being
-- addressed by X, in progress since Y". A re-measurement is the only thing
-- that may change a measurement.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS implementation (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title             TEXT        NOT NULL,
    domain            TEXT        NOT NULL,
    -- The shortfall this is meant to close, by parameter name. This is the join
    -- that stops the system re-proposing work already under way.
    addresses         TEXT,
    admin_ids         TEXT[]      NOT NULL,
    est_cost_inr_crore NUMERIC(12,2),
    status            TEXT        NOT NULL DEFAULT 'planned',
    feasibility       TEXT,
    conditions        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    detail            TEXT,
    evidence          TEXT,
    -- Where it came from: an ideation run, a reviewed proposal, or entered by hand.
    origin            TEXT        NOT NULL DEFAULT 'manual',
    origin_id         TEXT,
    note              TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    CONSTRAINT implementation_status_check CHECK (status IN
        ('planned', 'in_progress', 'completed', 'on_hold', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS implementation_status_ix ON implementation (status);
CREATE INDEX IF NOT EXISTS implementation_addresses_ix ON implementation (addresses);
CREATE INDEX IF NOT EXISTS implementation_admin_ix ON implementation USING GIN (admin_ids);

-- Every status change, kept forever. "What is in progress" is a question about
-- now; "when did this stall" is a question about the past, and a single
-- mutable status column cannot answer the second one.
CREATE TABLE IF NOT EXISTS implementation_event (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    implementation_id BIGINT      NOT NULL REFERENCES implementation(id) ON DELETE CASCADE,
    from_status       TEXT,
    to_status         TEXT        NOT NULL,
    note              TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS implementation_event_impl_ix
    ON implementation_event (implementation_id, created_at);
