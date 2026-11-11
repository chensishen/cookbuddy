"""Consumer for the existing durable queue; never receives recipe/tool text."""
import json
import uuid
from .profile_store import ProfileStore
from .models import stamp,settings
from .extractor import extract,anchor_expiry
from ..models import json_model


def process_memory_task(store,row):
    if int(settings()['experiments']['level'][1])<3:return 0
    uid=row['user_id'];prefs=ProfileStore(store)
    if not prefs.enabled(uid) or prefs.epoch(uid)!=row.get('profile_epoch',0):return 0
    with store.connect(uid) as db:
        current=db.execute("SELECT id,conversation_id,content,created_at FROM cookbuddy.messages WHERE job_id=%s AND role='user'",(row['job_id'],)).fetchone()
        if not current:return 0
        previous=db.execute("SELECT role,content FROM cookbuddy.messages WHERE conversation_id=%s AND id<%s AND role='user' ORDER BY id DESC LIMIT 2",(current['conversation_id'],current['id'])).fetchall()
    sid=current['conversation_id'];turn=sid+':'+str(current['id'])
    try:candidates=extract(current['content'],list(reversed(previous)),json_model)
    except Exception:
        # Do not log prompts, utterances, preference values, model output or errors.
        with prefs.connect(uid) as db:
            db.execute('INSERT INTO memory_events(id,user_id,decision,reason,candidate_json,source_turn_ref,created_at,idem_key) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,idem_key) DO NOTHING',
                (uuid.uuid4().hex,uid,'REJECT','invalid_extraction','{}',turn,stamp(),'reject:'+turn))
        raise ValueError('memory_extract_rejected') from None
    stored=0
    for c in candidates:
        c=anchor_expiry(c,current['created_at'])
        if store.memory_epoch(uid)!=row['epoch']:return stored
        result=prefs.apply(uid,sid,turn,c,expected_epoch=row.get('profile_epoch',0))
        stored+=result['decision'] in {'ADD','UPDATE'}
    from .episodic_store import EpisodicStore,event_from_user
    event=event_from_user(current['content'])
    if event and store.memory_epoch(uid)==row['epoch']:
        EpisodicStore(prefs).write(uid,event,turn,expected_epoch=row.get('profile_epoch',0))
    return stored
