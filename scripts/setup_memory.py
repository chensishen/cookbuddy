"""Idempotent PostgreSQL schema and model-versioned Milvus memory collection."""
from cookbuddy.store import Store
from cookbuddy.ltm.episodic_store import MemoryVectors
if __name__=='__main__':
    store=Store();store.migrate();vectors=MemoryVectors();vectors.ensure()
    from cookbuddy.ltm.maintenance import migrate_legacy_profiles
    from cookbuddy.ltm.profile_store import ProfileStore
    with store.connect(admin=True) as db:users=db.execute('SELECT id FROM cookbuddy.tenants').fetchall()
    migrated=sum(migrate_legacy_profiles(ProfileStore(store),u['id']) for u in users)
    print('memory_schema_ready','legacy_records_migrated',migrated)
