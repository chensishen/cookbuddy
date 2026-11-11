SET LOCAL search_path=cookbuddy,public;
CREATE TABLE IF NOT EXISTS user_prefs (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,dimension TEXT NOT NULL,value TEXT NOT NULL,polarity TEXT NOT NULL,
 strength TEXT NOT NULL,strength_score REAL,confidence REAL NOT NULL,source TEXT NOT NULL,scope TEXT NOT NULL,
 status TEXT NOT NULL,evidence_count INTEGER NOT NULL DEFAULT 1,session_count INTEGER NOT NULL DEFAULT 1,
 valid_from TEXT,valid_to TEXT,supersedes TEXT,version INTEGER NOT NULL DEFAULT 1,sensitive INTEGER NOT NULL DEFAULT 0,
 source_turn_ref TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_confirmed_at TEXT,last_used_at TEXT);
CREATE INDEX IF NOT EXISTS idx_prefs_user_status ON user_prefs(user_id,status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prefs_idem ON user_prefs(user_id,dimension,value,source_turn_ref);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prefs_single_active ON user_prefs(user_id,dimension)
 WHERE status='active' AND dimension IN ('spice_level','max_difficulty','time_budget','household_size','diet_type');
CREATE TABLE IF NOT EXISTS memory_events (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,pref_id TEXT,decision TEXT NOT NULL,reason TEXT,candidate_json TEXT,
 source_turn_ref TEXT,created_at TEXT NOT NULL,idem_key TEXT NOT NULL,UNIQUE(user_id,idem_key));
CREATE TABLE IF NOT EXISTS pending_confirmations (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT NOT NULL,action TEXT NOT NULL,payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,expires_at TEXT NOT NULL,turns_left INTEGER NOT NULL DEFAULT 3);
CREATE TABLE IF NOT EXISTS memory_settings (user_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 1,epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS episodic_memory (
 mem_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,text TEXT NOT NULL,type TEXT NOT NULL,dish_refs TEXT NOT NULL,
 importance REAL NOT NULL,created_at TEXT NOT NULL,last_accessed TEXT NOT NULL,expire_at TEXT,version INTEGER NOT NULL DEFAULT 1,
 vector TEXT,status TEXT NOT NULL DEFAULT 'active',source_turn_ref TEXT,UNIQUE(user_id,source_turn_ref));
CREATE TABLE IF NOT EXISTS memory_vector_outbox (
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,mem_id TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(user_id,mem_id));
CREATE TABLE IF NOT EXISTS memory_session_flush (
 user_id TEXT NOT NULL,session_id TEXT NOT NULL,last_message_id BIGINT NOT NULL,flushed_at TEXT NOT NULL,
 PRIMARY KEY(user_id,session_id));

ALTER TABLE cookbuddy.user_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.user_prefs FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='user_prefs' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.user_prefs USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='user_prefs_tenant_fk') THEN
 ALTER TABLE cookbuddy.user_prefs ADD CONSTRAINT user_prefs_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.memory_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.memory_events FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='memory_events' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.memory_events USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='memory_events_tenant_fk') THEN
 ALTER TABLE cookbuddy.memory_events ADD CONSTRAINT memory_events_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.pending_confirmations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.pending_confirmations FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='pending_confirmations' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.pending_confirmations USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='pending_confirmations_tenant_fk') THEN
 ALTER TABLE cookbuddy.pending_confirmations ADD CONSTRAINT pending_confirmations_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.memory_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.memory_settings FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='memory_settings' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.memory_settings USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='memory_settings_tenant_fk') THEN
 ALTER TABLE cookbuddy.memory_settings ADD CONSTRAINT memory_settings_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.episodic_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.episodic_memory FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='episodic_memory' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.episodic_memory USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='episodic_memory_tenant_fk') THEN
 ALTER TABLE cookbuddy.episodic_memory ADD CONSTRAINT episodic_memory_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.memory_vector_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.memory_vector_outbox FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='memory_vector_outbox' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.memory_vector_outbox USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='memory_vector_outbox_tenant_fk') THEN
 ALTER TABLE cookbuddy.memory_vector_outbox ADD CONSTRAINT memory_vector_outbox_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
ALTER TABLE cookbuddy.memory_session_flush ENABLE ROW LEVEL SECURITY;
ALTER TABLE cookbuddy.memory_session_flush FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='cookbuddy' AND tablename='memory_session_flush' AND policyname='tenant_scope') THEN
 CREATE POLICY tenant_scope ON cookbuddy.memory_session_flush USING(user_id=current_setting('cookbuddy.user_id',true)) WITH CHECK(user_id=current_setting('cookbuddy.user_id',true));
 END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='memory_session_flush_tenant_fk') THEN
 ALTER TABLE cookbuddy.memory_session_flush ADD CONSTRAINT memory_session_flush_tenant_fk FOREIGN KEY(user_id) REFERENCES cookbuddy.tenants(id) ON DELETE CASCADE;
 END IF;
END $$;
INSERT INTO cookbuddy.schema_versions(version) VALUES(6) ON CONFLICT DO NOTHING;
