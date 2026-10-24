-- owner: evidence
-- schema: trajectory-eval
-- 角色: evidence_owner / evidence_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 evidence 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS trajectory-eval;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='evidence_owner') THEN CREATE ROLE evidence_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='evidence_app') THEN CREATE ROLE evidence_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE evidence_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE evidence_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT evidence_owner TO evidence_app;
GRANT USAGE, CREATE ON SCHEMA trajectory-eval TO evidence_owner;
ALTER SCHEMA trajectory-eval OWNER TO evidence_owner;
CREATE TABLE IF NOT EXISTS trajectory-eval.outbox (
    tenant_id text NOT NULL, event_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id)
);
ALTER TABLE trajectory-eval.outbox OWNER TO evidence_owner;
ALTER TABLE trajectory-eval.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE trajectory-eval.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY outbox_tenant_isolation ON trajectory-eval.outbox USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS trajectory-eval.inbox (
    tenant_id text NOT NULL, consumer_unit text NOT NULL, event_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, consumer_unit, event_id)
);
ALTER TABLE trajectory-eval.inbox OWNER TO evidence_owner;
ALTER TABLE trajectory-eval.inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE trajectory-eval.inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY inbox_tenant_isolation ON trajectory-eval.inbox USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS trajectory-eval.dead_letters (
    tenant_id text NOT NULL, dlq_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, dlq_id)
);
ALTER TABLE trajectory-eval.dead_letters OWNER TO evidence_owner;
ALTER TABLE trajectory-eval.dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE trajectory-eval.dead_letters FORCE ROW LEVEL SECURITY;
CREATE POLICY dead_letters_tenant_isolation ON trajectory-eval.dead_letters USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA trajectory-eval TO evidence_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA trajectory-eval TO evidence_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA trajectory-eval TO evidence_app;
REVOKE ALL ON SCHEMA trajectory-eval FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA trajectory-eval FROM PUBLIC;
