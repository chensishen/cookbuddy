ALTER TABLE cookbuddy.memory_tasks ADD COLUMN IF NOT EXISTS profile_epoch integer NOT NULL DEFAULT 0;
ALTER TABLE cookbuddy.memory_settings ADD COLUMN IF NOT EXISTS cleared_message_id bigint NOT NULL DEFAULT 0;
INSERT INTO cookbuddy.schema_versions(version) VALUES(7) ON CONFLICT DO NOTHING;
