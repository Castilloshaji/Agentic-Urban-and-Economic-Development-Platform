// Ernakulam Digital Twin — Stage 3 Neo4j schema.
//
// IDENTITY PROPERTY. The brief asks for uniqueness on (admin_id, dataset_edition)
// for every label, but only the four administrative labels actually carry an
// admin_id. A Road has road_id, a BusStop has stop_id, and roads_messy.geojson
// has no admin_id column at all — constraining on it would create one giant
// (null, edition) collision class and silently permit duplicates. Each label is
// therefore keyed on its own natural id, always paired with dataset_edition:
//
//     District, Taluk, Panchayat, Ward -> admin_id
//     Road         -> road_id
//     WaterBody    -> waterbody_id
//     FloodZone    -> flood_zone_id
//     MetroStation -> station_id
//     BusStop      -> stop_id
//
// dataset_edition is in every key for the same reason it is in the PostGIS
// natural keys: DEMO-P-01 exists in both the demo and messy editions, and they
// are different assertions about the same place, not a duplicate to collapse.
//
// Composite uniqueness (not NODE KEY) is used deliberately: NODE KEY is an
// Enterprise feature, and this project targets neo4j:5-community.
//
// Apply with:
//   cat src/storage/neo4j/schema.cypher | cypher-shell -u neo4j -p <password>

// --- administrative hierarchy -------------------------------------------------
CREATE CONSTRAINT district_key IF NOT EXISTS
FOR (n:District) REQUIRE (n.admin_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT taluk_key IF NOT EXISTS
FOR (n:Taluk) REQUIRE (n.admin_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT panchayat_key IF NOT EXISTS
FOR (n:Panchayat) REQUIRE (n.admin_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT ward_key IF NOT EXISTS
FOR (n:Ward) REQUIRE (n.admin_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT municipality_key IF NOT EXISTS
FOR (n:Municipality) REQUIRE (n.admin_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT corporation_key IF NOT EXISTS
FOR (n:Corporation) REQUIRE (n.admin_id, n.dataset_edition) IS UNIQUE;

// --- physical / network features ---------------------------------------------
CREATE CONSTRAINT road_key IF NOT EXISTS
FOR (n:Road) REQUIRE (n.road_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT water_body_key IF NOT EXISTS
FOR (n:WaterBody) REQUIRE (n.waterbody_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT flood_zone_key IF NOT EXISTS
FOR (n:FloodZone) REQUIRE (n.flood_zone_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT metro_station_key IF NOT EXISTS
FOR (n:MetroStation) REQUIRE (n.station_id, n.dataset_edition) IS UNIQUE;

CREATE CONSTRAINT bus_stop_key IF NOT EXISTS
FOR (n:BusStop) REQUIRE (n.stop_id, n.dataset_edition) IS UNIQUE;

// --- lookup indexes -----------------------------------------------------------
// parent_id drives the LOCATED_IN build; the vintage properties are what an agent
// filters on when it needs one delimitation rather than whatever loaded last.
CREATE INDEX district_parent_ix     IF NOT EXISTS FOR (n:District)     ON (n.parent_id);
CREATE INDEX taluk_parent_ix        IF NOT EXISTS FOR (n:Taluk)        ON (n.parent_id);
CREATE INDEX panchayat_parent_ix    IF NOT EXISTS FOR (n:Panchayat)    ON (n.parent_id);
CREATE INDEX ward_parent_ix         IF NOT EXISTS FOR (n:Ward)         ON (n.parent_id);
CREATE INDEX municipality_parent_ix IF NOT EXISTS FOR (n:Municipality) ON (n.parent_id);
CREATE INDEX corporation_parent_ix  IF NOT EXISTS FOR (n:Corporation)  ON (n.parent_id);

CREATE INDEX district_vintage_ix    IF NOT EXISTS FOR (n:District)     ON (n.boundary_vintage);
CREATE INDEX taluk_vintage_ix       IF NOT EXISTS FOR (n:Taluk)        ON (n.boundary_vintage);
CREATE INDEX panchayat_vintage_ix   IF NOT EXISTS FOR (n:Panchayat)    ON (n.boundary_vintage);
CREATE INDEX ward_vintage_ix        IF NOT EXISTS FOR (n:Ward)         ON (n.boundary_vintage);

CREATE INDEX flood_zone_year_ix     IF NOT EXISTS FOR (n:FloodZone)    ON (n.data_year);
CREATE INDEX flood_zone_risk_ix     IF NOT EXISTS FOR (n:FloodZone)    ON (n.risk_level);
CREATE INDEX road_type_ix           IF NOT EXISTS FOR (n:Road)         ON (n.road_type);
CREATE INDEX bus_stop_feed_end_ix   IF NOT EXISTS FOR (n:BusStop)      ON (n.feed_end_date);
CREATE INDEX metro_station_admin_ix IF NOT EXISTS FOR (n:MetroStation) ON (n.admin_id);
CREATE INDEX bus_stop_admin_ix      IF NOT EXISTS FOR (n:BusStop)      ON (n.admin_id);
