-- owner: security
-- schema: security-gateway
-- 角色: security_owner / security_app
-- version: 0001
--
-- 本文件由 scripts/p2_migrations.py 从 services/_kernel/storage.py **生成**。
-- 不要手工编辑：门禁会重新生成并逐字节比对，任何手改都会报红。
-- 需要变更时：新增下一个版本号的文件，并在其中写差异（§13：不改已应用迁移）。
--
-- 隔离要点（上游 §13）：本 schema 只授权给 security 自己的角色；
-- 不生成任何跨 owner 的 GRANT / REFERENCES；租户表的 RLS 一律 ENABLE + FORCE。
CREATE SCHEMA IF NOT EXISTS security-gateway;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='security_owner') THEN CREATE ROLE security_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='security_app') THEN CREATE ROLE security_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; END IF; END $$;
ALTER ROLE security_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE security_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT security_owner TO security_app;
GRANT USAGE, CREATE ON SCHEMA security-gateway TO security_owner;
ALTER SCHEMA security-gateway OWNER TO security_owner;
CREATE TABLE IF NOT EXISTS security-gateway.classification_records (
    tenant_id text NOT NULL, classification_id text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, classification_id)
);
ALTER TABLE security-gateway.classification_records OWNER TO security_owner;
ALTER TABLE security-gateway.classification_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE security-gateway.classification_records FORCE ROW LEVEL SECURITY;
CREATE POLICY classification_records_tenant_isolation ON security-gateway.classification_records USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS security-gateway.egress_audits (
    tenant_id text NOT NULL, audit_ref text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, audit_ref)
);
ALTER TABLE security-gateway.egress_audits OWNER TO security_owner;
ALTER TABLE security-gateway.egress_audits ENABLE ROW LEVEL SECURITY;
ALTER TABLE security-gateway.egress_audits FORCE ROW LEVEL SECURITY;
CREATE POLICY egress_audits_tenant_isolation ON security-gateway.egress_audits USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE TABLE IF NOT EXISTS security-gateway.token_vault_mappings (
    tenant_id text NOT NULL, mapping_ref text NOT NULL,
    payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mapping_ref)
);
ALTER TABLE security-gateway.token_vault_mappings OWNER TO security_owner;
ALTER TABLE security-gateway.token_vault_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE security-gateway.token_vault_mappings FORCE ROW LEVEL SECURITY;
CREATE POLICY token_vault_mappings_tenant_isolation ON security-gateway.token_vault_mappings USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT USAGE ON SCHEMA security-gateway TO security_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA security-gateway TO security_owner;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA security-gateway TO security_app;
REVOKE ALL ON SCHEMA security-gateway FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA security-gateway FROM PUBLIC;
