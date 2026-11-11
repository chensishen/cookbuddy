-- User-turn task state is independent of answer success or model output.
CREATE TABLE IF NOT EXISTS cookbuddy.task_states(
 user_id text NOT NULL,
 conversation_id text NOT NULL,
 user_message_id bigint NOT NULL REFERENCES cookbuddy.messages(id) ON DELETE CASCADE,
 job_id text NOT NULL,
 state jsonb,
 decision jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(user_id,user_message_id),
 UNIQUE(user_id,job_id),
 FOREIGN KEY(user_id,conversation_id) REFERENCES cookbuddy.conversations(user_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS task_states_conversation_idx ON cookbuddy.task_states(user_id,conversation_id,user_message_id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS messages_task_identity_idx ON cookbuddy.messages(user_id,conversation_id,id);
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='task_state_message_tenant') THEN
  ALTER TABLE cookbuddy.task_states ADD CONSTRAINT task_state_message_tenant
   FOREIGN KEY(user_id,conversation_id,user_message_id) REFERENCES cookbuddy.messages(user_id,conversation_id,id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.task_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.task_states FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='task_states' AND policyname='tenant_scope') THEN
  CREATE POLICY tenant_scope ON cookbuddy.task_states USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
END $$;
INSERT INTO cookbuddy.schema_versions(version) VALUES(4) ON CONFLICT DO NOTHING;
