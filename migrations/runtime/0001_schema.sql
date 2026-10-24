-- owner: runtime
-- schema: hermes-runtime
-- 角色: runtime_owner / runtime_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 runtime 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS hermes-runtime;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='runtime_owner') THEN CREATE ROLE runtime_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='runtime_app') THEN CREATE ROLE runtime_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE runtime_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE runtime_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT runtime_owner TO runtime_app;
GRANT USAGE, CREATE ON SCHEMA hermes-runtime TO runtime_owner;
ALTER SCHEMA hermes-runtime OWNER TO runtime_owner;
CREATE TABLE IF NOT EXISTS hermes-runtime.runs (
    tenant_id text NOT NULL, run_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);
ALTER TABLE hermes-runtime.runs OWNER TO runtime_owner;
ALTER TABLE hermes-runtime.runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE hermes-runtime.runs FORCE ROW LEVEL SECURITY;
CREATE POLICY runs_tenant_isolation ON hermes-runtime.runs USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS hermes-runtime.checkpoints (
    tenant_id text NOT NULL, run_id text NOT NULL, version integer NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, version)
);
ALTER TABLE hermes-runtime.checkpoints OWNER TO runtime_owner;
ALTER TABLE hermes-runtime.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE hermes-runtime.checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY checkpoints_tenant_isolation ON hermes-runtime.checkpoints USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA hermes-runtime TO runtime_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hermes-runtime TO runtime_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hermes-runtime TO runtime_app;
REVOKE ALL ON SCHEMA hermes-runtime FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA hermes-runtime FROM PUBLIC;
