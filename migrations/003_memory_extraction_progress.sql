ALTER TABLE cookbuddy.memory_tasks ADD COLUMN IF NOT EXISTS extraction_progress jsonb NOT NULL DEFAULT '{}'::jsonb;
INSERT INTO cookbuddy.schema_versions(version) VALUES(3) ON CONFLICT DO NOTHING;
