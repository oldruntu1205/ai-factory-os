-- owner: decision
-- schema: decision-service
-- 角色: decision_owner / decision_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 decision 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS decision-service;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='decision_owner') THEN CREATE ROLE decision_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='decision_app') THEN CREATE ROLE decision_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE decision_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE decision_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT decision_owner TO decision_app;
GRANT USAGE, CREATE ON SCHEMA decision-service TO decision_owner;
ALTER SCHEMA decision-service OWNER TO decision_owner;
CREATE TABLE IF NOT EXISTS decision-service.decisions (
    tenant_id text NOT NULL, decision_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, decision_id)
);
ALTER TABLE decision-service.decisions OWNER TO decision_owner;
ALTER TABLE decision-service.decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision-service.decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY decisions_tenant_isolation ON decision-service.decisions USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA decision-service TO decision_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA decision-service TO decision_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA decision-service TO decision_app;
REVOKE ALL ON SCHEMA decision-service FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA decision-service FROM PUBLIC;
