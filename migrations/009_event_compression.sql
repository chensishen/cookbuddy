ALTER TABLE cookbuddy.episodic_memory ADD COLUMN IF NOT EXISTS compressed_into text;
INSERT INTO cookbuddy.schema_versions(version) VALUES(9) ON CONFLICT DO NOTHING;
