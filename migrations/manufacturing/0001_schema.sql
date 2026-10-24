-- owner: manufacturing
-- schema: manufacturing-core
-- 角色: manufacturing_owner / manufacturing_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 manufacturing 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS manufacturing-core;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='manufacturing_owner') THEN CREATE ROLE manufacturing_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='manufacturing_app') THEN CREATE ROLE manufacturing_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE manufacturing_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE manufacturing_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT manufacturing_owner TO manufacturing_app;
GRANT USAGE, CREATE ON SCHEMA manufacturing-core TO manufacturing_owner;
ALTER SCHEMA manufacturing-core OWNER TO manufacturing_owner;
CREATE TABLE IF NOT EXISTS manufacturing-core.fact_projections (
    tenant_id text NOT NULL, projection_ref text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, projection_ref)
);
ALTER TABLE manufacturing-core.fact_projections OWNER TO manufacturing_owner;
ALTER TABLE manufacturing-core.fact_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE manufacturing-core.fact_projections FORCE ROW LEVEL SECURITY;
CREATE POLICY fact_projections_tenant_isolation ON manufacturing-core.fact_projections USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS manufacturing-core.source_snapshots (
    tenant_id text NOT NULL, snapshot_ref text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, snapshot_ref)
);
ALTER TABLE manufacturing-core.source_snapshots OWNER TO manufacturing_owner;
ALTER TABLE manufacturing-core.source_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE manufacturing-core.source_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY source_snapshots_tenant_isolation ON manufacturing-core.source_snapshots USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA manufacturing-core TO manufacturing_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA manufacturing-core TO manufacturing_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA manufacturing-core TO manufacturing_app;
REVOKE ALL ON SCHEMA manufacturing-core FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA manufacturing-core FROM PUBLIC;
