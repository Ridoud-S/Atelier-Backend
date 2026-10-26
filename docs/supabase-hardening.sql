-- Atelier: hardening for a Supabase database when FastAPI is the ONLY database client.
--
-- Why: Supabase exposes tables of the `public` schema through its auto-generated REST API to the
-- `anon` and `authenticated` roles. Without Row Level Security those roles could read/write the
-- tables with the project's public (anon) key, completely bypassing FastAPI. This script closes
-- that door: RLS ON with NO policies (deny by default) + no privileges for those roles.
-- The backend must connect with a role that owns the tables (e.g. `postgres`), which bypasses RLS.
--
-- Run once after each migration that creates tables (it is idempotent).
-- STATUS: syntax and effect verified against a local PostgreSQL with simulated `anon` /
-- `authenticated` roles; NOT yet verified against a real Supabase project.

DO $$
DECLARE
    t record;
    r text;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t.tablename);
    END LOOP;

    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', r);
            -- Tables created by future migrations stay closed too.
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I', r);
        END IF;
    END LOOP;
END
$$;
