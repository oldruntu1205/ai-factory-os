-- owner: control
-- schema: control-plane
-- 角色: control_owner / control_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 control 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS control-plane;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='control_owner') THEN CREATE ROLE control_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='control_app') THEN CREATE ROLE control_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE control_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE control_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT control_owner TO control_app;
GRANT USAGE, CREATE ON SCHEMA control-plane TO control_owner;
ALTER SCHEMA control-plane OWNER TO control_owner;
CREATE TABLE IF NOT EXISTS control-plane.tasks (
    tenant_id text NOT NULL, task_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, task_id)
);
ALTER TABLE control-plane.tasks OWNER TO control_owner;
ALTER TABLE control-plane.tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE control-plane.tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY tasks_tenant_isolation ON control-plane.tasks USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS control-plane.approvals (
    tenant_id text NOT NULL, approval_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, approval_id)
);
ALTER TABLE control-plane.approvals OWNER TO control_owner;
ALTER TABLE control-plane.approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE control-plane.approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY approvals_tenant_isolation ON control-plane.approvals USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS control-plane.policy_evaluations (
    tenant_id text NOT NULL, evaluation_ref text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, evaluation_ref)
);
ALTER TABLE control-plane.policy_evaluations OWNER TO control_owner;
ALTER TABLE control-plane.policy_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE control-plane.policy_evaluations FORCE ROW LEVEL SECURITY;
CREATE POLICY policy_evaluations_tenant_isolation ON control-plane.policy_evaluations USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS control-plane.knowledge_answers (
    tenant_id text NOT NULL, task_id text NOT NULL, trace_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, task_id, trace_id)
);
ALTER TABLE control-plane.knowledge_answers OWNER TO control_owner;
ALTER TABLE control-plane.knowledge_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE control-plane.knowledge_answers FORCE ROW LEVEL SECURITY;
CREATE POLICY knowledge_answers_tenant_isolation ON control-plane.knowledge_answers USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA control-plane TO control_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA control-plane TO control_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA control-plane TO control_app;
REVOKE ALL ON SCHEMA control-plane FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA control-plane FROM PUBLIC;
