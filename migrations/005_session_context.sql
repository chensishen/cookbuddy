CREATE TABLE IF NOT EXISTS cookbuddy.session_states (
 user_id text NOT NULL,
 conversation_id text NOT NULL,
 user_message_id bigint NOT NULL,
 state jsonb NOT NULL,
 expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
 PRIMARY KEY(user_id,user_message_id),
 FOREIGN KEY(user_id,conversation_id,user_message_id)
 REFERENCES cookbuddy.messages(user_id,conversation_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS session_states_lookup ON cookbuddy.session_states(user_id,conversation_id,user_message_id DESC);
ALTER TABLE cookbuddy.session_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.session_states FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='session_states' AND policyname='tenant_scope') THEN
  CREATE POLICY tenant_scope ON cookbuddy.session_states USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
END $$;
INSERT INTO cookbuddy.schema_versions(version) VALUES(5) ON CONFLICT DO NOTHING;
