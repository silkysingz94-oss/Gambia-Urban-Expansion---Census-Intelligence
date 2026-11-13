-- GBoS GEOAI Census Intelligence
-- PostgreSQL 15+ / PostGIS 3+
-- Example schema. Review field mapping and data governance before loading official microdata.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS census;

CREATE TABLE IF NOT EXISTS census.ea (
    ea_code             text PRIMARY KEY,
    ea_name             text,
    lga                 text,
    district            text,
    ward                text,
    geom                geometry(MultiPolygon, 4326),
    land_area_ha        double precision,
    population_2013     bigint,
    population_2023     bigint,
    households_2013     bigint,
    households_2023     bigint,
    source_name         text,
    source_year         integer,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ea_code_not_blank CHECK (length(trim(ea_code)) > 0),
    CONSTRAINT ea_population_nonnegative CHECK (
      (population_2013 IS NULL OR population_2013 >= 0) AND
      (population_2023 IS NULL OR population_2023 >= 0)
    ),
    CONSTRAINT ea_households_nonnegative CHECK (
      (households_2013 IS NULL OR households_2013 >= 0) AND
      (households_2023 IS NULL OR households_2023 >= 0)
    )
);

CREATE INDEX IF NOT EXISTS ea_geom_gix ON census.ea USING GIST (geom);
CREATE INDEX IF NOT EXISTS ea_lga_idx ON census.ea (lga);
CREATE INDEX IF NOT EXISTS ea_district_idx ON census.ea (district);
CREATE INDEX IF NOT EXISTS ea_ward_idx ON census.ea (ward);

CREATE TABLE IF NOT EXISTS census.change_log (
    event_id       bigserial PRIMARY KEY,
    table_name     text NOT NULL,
    record_key     text,
    operation      text NOT NULL,
    changed_at     timestamptz NOT NULL DEFAULT now(),
    changed_by     text NOT NULL DEFAULT current_user,
    changed_fields text[]
);

CREATE OR REPLACE FUNCTION census.capture_ea_change()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  k text;
  changed text[];
  op text := TG_OP;
BEGIN
  IF TG_OP = 'DELETE' THEN
    k := OLD.ea_code;
    changed := ARRAY['deleted'];
  ELSIF TG_OP = 'INSERT' THEN
    k := NEW.ea_code;
    changed := ARRAY['inserted'];
    NEW.updated_at := now();
  ELSE
    k := NEW.ea_code;
    SELECT array_agg(key_name) INTO changed
    FROM unnest(ARRAY[
      'ea_name','lga','district','ward','geom','land_area_ha',
      'population_2013','population_2023','households_2013',
      'households_2023','source_name','source_year'
    ]) AS fields(key_name)
    WHERE (to_jsonb(NEW)->key_name) IS DISTINCT FROM (to_jsonb(OLD)->key_name);
    NEW.updated_at := now();
  END IF;

  INSERT INTO census.change_log(table_name,record_key,operation,changed_fields)
  VALUES ('census.ea',k,op,COALESCE(changed,ARRAY[]::text[]));

  PERFORM pg_notify('census_data_changed',
    json_build_object('table','census.ea','key',k,'operation',op,
      'changed_fields',COALESCE(changed,ARRAY[]::text[]),
      'at',now())::text);

  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS ea_change_trigger ON census.ea;
CREATE TRIGGER ea_change_trigger
BEFORE INSERT OR UPDATE OR DELETE ON census.ea
FOR EACH ROW EXECUTE FUNCTION census.capture_ea_change();

-- A view for consistent dashboard summaries. Population growth is only computed
-- where the 2013 denominator is positive.
CREATE OR REPLACE VIEW census.ea_dashboard AS
SELECT ea_code, ea_name, lga, district, ward, geom, land_area_ha,
       population_2013, population_2023, households_2013, households_2023,
       CASE WHEN population_2013 > 0
         THEN 100.0 * (population_2023 - population_2013) / population_2013
         ELSE NULL END AS population_change_pct,
       CASE WHEN population_2023 > 0 AND households_2023 > 0
         THEN population_2023::double precision / households_2023
         ELSE NULL END AS average_household_size_2023,
       updated_at
FROM census.ea;

-- Create a restricted read-only account separately, for example:
-- CREATE ROLE census_reader LOGIN PASSWORD 'SET_A_STRONG_SECRET';
-- GRANT CONNECT ON DATABASE gambia_census TO census_reader;
-- GRANT USAGE ON SCHEMA census TO census_reader;
-- GRANT SELECT ON census.ea, census.ea_dashboard, census.change_log TO census_reader;
-- Do NOT grant INSERT/UPDATE/DELETE to the dashboard account.
