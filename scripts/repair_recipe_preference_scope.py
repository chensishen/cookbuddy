"""Archive only the proven, single-turn legacy bread-parser mistake; keep audit/history."""
import argparse
import json
import uuid
from cookbuddy.ltm.models import stamp
from cookbuddy.ltm.profile_store import ProfileStore

QUESTION='基础牛奶面包不要放鸡蛋，给我完整原文和做法。'
REASON='recipe_parser_scope_repair_20261022'


def repair(store,user_id,*,apply=False):
    prefs=ProfileStore(store);ids=[]
    with prefs.connect(user_id) as db:
        rows=db.execute("""SELECT p.id,p.version,p.source_turn_ref FROM user_prefs p
            JOIN cookbuddy.messages m ON m.user_id=p.user_id AND m.role='user'
                AND m.id::text=split_part(p.source_turn_ref,':',2)
            WHERE p.user_id=? AND p.dimension='avoid_ingredient' AND p.value='放鸡蛋'
                AND p.scope='global' AND p.status='active' AND p.evidence_count=1
                AND p.session_count=1 AND m.content=?""",(user_id,QUESTION)).fetchall()
        for row in rows:
            ids.append(row['id'])
            if not apply:continue
            changed=db.execute("UPDATE user_prefs SET status='archived',version=version+1,updated_at=? WHERE user_id=? AND id=? AND version=? AND status='active'",
                (stamp(),user_id,row['id'],row['version']))
            if changed.rowcount!=1:raise ValueError('memory_version_conflict')
            db.execute('INSERT INTO memory_events(id,user_id,pref_id,decision,reason,candidate_json,source_turn_ref,created_at,idem_key) VALUES(?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,user_id,row['id'],'ARCHIVE',REASON,
                 json.dumps({'dimension':'avoid_ingredient','scope':'session','source':'explicit'}),
                 row['source_turn_ref'],stamp(),REASON+':'+row['id']))
    return {'eligible':len(ids),'archived':len(ids) if apply else 0,'preference_ids':ids}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    from cookbuddy.store import Store
    print(json.dumps(repair(Store(),args.user_id,apply=args.apply)))
