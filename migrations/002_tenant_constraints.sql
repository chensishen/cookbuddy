ALTER TABLE cookbuddy.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.tenants FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='tenants' AND policyname='tenant_scope') THEN
  CREATE POLICY tenant_scope ON cookbuddy.tenants USING(id=current_setting('cookbuddy.user_id',true)) WITH CHECK(id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='conversation_tenant_pair') THEN
  ALTER TABLE cookbuddy.conversations ADD CONSTRAINT conversation_tenant_pair UNIQUE(user_id,id);
  ALTER TABLE cookbuddy.messages ADD CONSTRAINT message_conversation_tenant FOREIGN KEY(user_id,conversation_id) REFERENCES cookbuddy.conversations(user_id,id);
 END IF;
END $$;
INSERT INTO cookbuddy.schema_versions(version) VALUES(2) ON CONFLICT DO NOTHING;
