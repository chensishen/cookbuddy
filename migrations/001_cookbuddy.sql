CREATE SCHEMA IF NOT EXISTS cookbuddy;
CREATE TABLE IF NOT EXISTS cookbuddy.schema_versions(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cookbuddy.tenants(id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(), memory_epoch integer NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cookbuddy.conversations(
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES cookbuddy.tenants(id), title text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cookbuddy.messages(
 id bigserial PRIMARY KEY, user_id text NOT NULL REFERENCES cookbuddy.tenants(id), conversation_id text NOT NULL REFERENCES cookbuddy.conversations(id),
 role text NOT NULL CHECK(role IN ('user','assistant')), job_id text NOT NULL, content text NOT NULL, record jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,job_id,role));
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON cookbuddy.messages(user_id,conversation_id,id);
CREATE TABLE IF NOT EXISTS cookbuddy.memories(
 id text PRIMARY KEY, user_id text NOT NULL REFERENCES cookbuddy.tenants(id), kind text NOT NULL CHECK(kind IN ('profile','task','knowledge')),
 key text NOT NULL, content text NOT NULL, evidence jsonb NOT NULL, confidence real NOT NULL CHECK(confidence BETWEEN 0 AND 1),
 importance real NOT NULL CHECK(importance BETWEEN 0 AND 1), version integer NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','forgotten','compressed')),
 expires_at timestamptz, last_accessed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(user_id,kind,key));
CREATE INDEX IF NOT EXISTS memories_active_idx ON cookbuddy.memories(user_id,kind,updated_at DESC) WHERE status='active';
CREATE TABLE IF NOT EXISTS cookbuddy.memory_revisions(
 id bigserial PRIMARY KEY, user_id text NOT NULL, memory_id text NOT NULL, version integer NOT NULL,
 action text NOT NULL, previous jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cookbuddy.outbox(
 id bigserial PRIMARY KEY, user_id text NOT NULL, memory_id text NOT NULL, version integer NOT NULL,
 operation text NOT NULL CHECK(operation IN ('upsert','delete')), state text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz, error_code text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(memory_id,version,operation));
CREATE TABLE IF NOT EXISTS cookbuddy.memory_tasks(
 job_id text PRIMARY KEY, user_id text NOT NULL, epoch integer NOT NULL DEFAULT 0, question text NOT NULL, record jsonb NOT NULL,
 state text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz, error_code text,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cookbuddy.intent_registry(
 id text PRIMARY KEY, description text NOT NULL, examples jsonb NOT NULL, target text NOT NULL,
 enabled boolean NOT NULL DEFAULT true, version integer NOT NULL DEFAULT 1, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cookbuddy.intent_audit(
 id bigserial PRIMARY KEY, user_id text NOT NULL, job_id text NOT NULL, decision jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['conversations','messages','memories','memory_revisions','intent_audit','outbox','memory_tasks'] LOOP
  EXECUTE format('ALTER TABLE cookbuddy.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE cookbuddy.%I FORCE ROW LEVEL SECURITY',t);
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename=t AND policyname='tenant_scope') THEN
   EXECUTE format('CREATE POLICY tenant_scope ON cookbuddy.%I USING (user_id=current_setting(''cookbuddy.user_id'',true)) WITH CHECK (user_id=current_setting(''cookbuddy.user_id'',true))',t);
  END IF;
 END LOOP;
END $$;
INSERT INTO cookbuddy.schema_versions(version) VALUES(1) ON CONFLICT DO NOTHING;
