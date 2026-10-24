-- owner: tools
-- schema: tool-gateway
-- 角色: tools_owner / tools_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 tools 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS tool-gateway;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tools_owner') THEN CREATE ROLE tools_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tools_app') THEN CREATE ROLE tools_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE tools_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE tools_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT tools_owner TO tools_app;
GRANT USAGE, CREATE ON SCHEMA tool-gateway TO tools_owner;
ALTER SCHEMA tool-gateway OWNER TO tools_owner;
CREATE TABLE IF NOT EXISTS tool-gateway.tool_specs (
    tool_id text NOT NULL, version integer NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tool_id, version)
);
ALTER TABLE tool-gateway.tool_specs OWNER TO tools_owner;
CREATE TABLE IF NOT EXISTS tool-gateway.tool_executions (
    tenant_id text NOT NULL, execution_ref text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, execution_ref)
);
ALTER TABLE tool-gateway.tool_executions OWNER TO tools_owner;
ALTER TABLE tool-gateway.tool_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool-gateway.tool_executions FORCE ROW LEVEL SECURITY;
CREATE POLICY tool_executions_tenant_isolation ON tool-gateway.tool_executions USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA tool-gateway TO tools_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tool-gateway TO tools_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tool-gateway TO tools_app;
REVOKE ALL ON SCHEMA tool-gateway FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA tool-gateway FROM PUBLIC;
