-- owner: transaction
-- schema: transaction-service
-- 角色: transaction_owner / transaction_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 transaction 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS transaction-service;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='transaction_owner') THEN CREATE ROLE transaction_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='transaction_app') THEN CREATE ROLE transaction_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE transaction_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE transaction_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT transaction_owner TO transaction_app;
GRANT USAGE, CREATE ON SCHEMA transaction-service TO transaction_owner;
ALTER SCHEMA transaction-service OWNER TO transaction_owner;
CREATE TABLE IF NOT EXISTS transaction-service.commands (
    tenant_id text NOT NULL, command_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, command_id)
);
ALTER TABLE transaction-service.commands OWNER TO transaction_owner;
ALTER TABLE transaction-service.commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction-service.commands FORCE ROW LEVEL SECURITY;
CREATE POLICY commands_tenant_isolation ON transaction-service.commands USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS transaction-service.authority_receipts (
    tenant_id text NOT NULL, receipt_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, receipt_id)
);
ALTER TABLE transaction-service.authority_receipts OWNER TO transaction_owner;
ALTER TABLE transaction-service.authority_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction-service.authority_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY authority_receipts_tenant_isolation ON transaction-service.authority_receipts USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS transaction-service.saga_steps (
    tenant_id text NOT NULL, saga_id text NOT NULL, step_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, saga_id, step_id)
);
ALTER TABLE transaction-service.saga_steps OWNER TO transaction_owner;
ALTER TABLE transaction-service.saga_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction-service.saga_steps FORCE ROW LEVEL SECURITY;
CREATE POLICY saga_steps_tenant_isolation ON transaction-service.saga_steps USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS transaction-service.command_idempotency (
    tenant_id text NOT NULL, idempotency_key text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idempotency_key)
);
ALTER TABLE transaction-service.command_idempotency OWNER TO transaction_owner;
ALTER TABLE transaction-service.command_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE transaction-service.command_idempotency FORCE ROW LEVEL SECURITY;
CREATE POLICY command_idempotency_tenant_isolation ON transaction-service.command_idempotency USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA transaction-service TO transaction_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA transaction-service TO transaction_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA transaction-service TO transaction_app;
REVOKE ALL ON SCHEMA transaction-service FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA transaction-service FROM PUBLIC;
