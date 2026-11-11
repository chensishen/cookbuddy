CREATE TABLE IF NOT EXISTS cookbuddy.memory_purge_outbox (
 user_id text PRIMARY KEY REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE cookbuddy.memory_purge_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.memory_purge_outbox FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='memory_purge_outbox' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.memory_purge_outbox USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
END $$;
INSERT INTO cookbuddy.schema_versions(version) VALUES(8) ON CONFLICT DO NOTHING;
