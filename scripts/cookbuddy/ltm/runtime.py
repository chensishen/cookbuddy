"""Orchestration adapter. Identities originate from the authenticated request."""
import copy
from dataclasses import asdict,replace
from .models import settings,stamp,now
from .profile_store import ProfileStore
from .extractor import rule_extract,reference_only
from .guard import current,request_conflict
from .episodic_store import EpisodicStore
from .retriever import personalize_result

class MemoryRuntime:
    def __init__(self,store,uid,sid,job,question):
        self.store=store;self.uid=uid;self.sid=sid;self.job=job;self.question=question
        self.available=hasattr(store,'connect');self.profile={'hard_constraints':[],'soft_prefs':[]}
        self.hard=[];self.events=[];self.confirmation=None;self.notice='';self.enabled=False
        self.session=None
        self.previous_task=None
        self.level=6
        self.management=None
        import re
        from .management import parse_command,handle_command
        command=parse_command(question)
        clearing=bool(re.fullmatch(r'(?:请)?(?:忘掉|忘记|删除|清空)(?:我(?:的)?)?(?:所有)?(?:长期)?记忆[。！! ]*',question.strip()))
        self.candidates=[] if clearing or command else rule_extract(question)
        self.hard=[asdict(c) for c in self.candidates if c.strength=='hard' and c.source=='explicit' and c.op=='set']
        if not self.available:return
        cfg=settings()['experiments']
        if cfg['level'] not in {'M'+str(n) for n in range(7)}:raise ValueError('invalid_memory_level')
        self.level=int(cfg['level'][1])
        if cfg['hard_filter_mode']!='code_enforced' or cfg['confirm_relax']!='on' or cfg['scope_detection']!='on':raise ValueError('unsafe_memory_policy_for_live_requests')
        self.prefs=ProfileStore(store);self.enabled=self.prefs.enabled(uid) and cfg['level']!='M0'
        if not self.enabled:
            try:self.session=store.session_context(uid,sid,job)
            except AssertionError:self.session=None
            if command:self.management=handle_command(command,self.prefs,uid,sid,job,enabled=False)
            return
        self.session=store.session_context(uid,sid,job)
        temporary=self.session.slots['temp_constraints']
        if 'hard_constraints' in temporary:
            temporary['hard_constraints']=[c for c in temporary['hard_constraints']
                if not (c.get('dimension')=='avoid_ingredient' and reference_only(c.get('value','')))]
        if re.fullmatch(r'(?:请告诉我)?我(?:现在|这次|刚才|家里|手头)?(?:有|告诉过你有)(?:什么|哪些)食材[。？? ]*',question.strip()):
            stock=self.session.slots['ingredients_on_hand'] if self.level>=2 else []
            self.management={'decision':'NOOP','message':('本次对话你提到的现有食材：'+'、'.join(stock)+'。这些食材只用于当前会话。')
                             if stock else '本次对话还没有记录现有食材，请告诉我现在有什么食材。',
                             'needs_confirmation':False}
            return
        inventory=[c.value for c in self.candidates if c.dimension=='inventory' and c.op=='set']
        if inventory and self.level>=2:self.session.slots['ingredients_on_hand']=list(dict.fromkeys(inventory))
        if command:
            self.management=handle_command(command,self.prefs,uid,sid,sid+':'+str(store.user_message_id(uid,job)))
            self.profile=self.prefs.load_profile(uid)
            self.hard=copy.deepcopy(self.profile['hard_constraints'])
            self.hard.extend(self.session.slots['temp_constraints'].get('hard_constraints',[]))
            if self.management.get('needs_confirmation'):self.notice=self.prefs.pending_message(uid,sid)
            return
        if self.level>=2:self.previous_task=store.task_context(uid,sid,job)
        if self.level==1:
            from ..session import slots_default
            self.session.slots=slots_default();self.session.summary=''
        if self.level<3:
            if self.level>=2:
                constraints=self.session.slots['temp_constraints'];constraints.setdefault('profile_overrides',{});constraints.setdefault('hard_constraints',[])
                for c in self.candidates:
                    if c.op=='set' and c.strength=='hard':constraints['hard_constraints'].append(asdict(c))
                    elif c.op=='set' and c.dimension!='inventory':constraints['profile_overrides'][c.dimension]=c.value
                self.hard=constraints['hard_constraints']
            self.hard.extend(asdict(c) for c in self.candidates if c.strength=='hard' and c.op=='set')
            return
        from .memory_routes import classify_session_confirmation
        self.confirmation=classify_session_confirmation(self.session,question)
        if self.confirmation:store.save_session_context(uid,sid,job,self.session)
        self.confirmation=self.confirmation or self.prefs.confirm(uid,sid,question)
        if self.confirmation:
            self.candidates=[]
            self.profile=self.prefs.load_profile(uid)
            self.hard=copy.deepcopy(self.profile['hard_constraints'])
            self.hard.extend(self.session.slots['temp_constraints'].get('hard_constraints',[]))
            self.management=self.confirmation
            self.notice=''
            return
        turn=sid+':'+str(store.user_message_id(uid,job))
        # Explicit stable preferences update synchronously; implicit extraction
        # remains queued. Inventory never enters the long-term profile.
        saved=[]
        recipe_request=bool(re.search(r'推荐|怎么|如何|多久|多少|能否|能不能|可以做|做法|原文|全文|步骤|图片|照片|配方|[?？]',question))
        for candidate in self.candidates:
            stable=(candidate.source=='explicit' and candidate.scope=='global' and
                    (candidate.dimension in ('flavor','spice_level','avoid_ingredient','allergy')
                     or candidate.dimension=='equipment' and candidate.value.startswith('有')))
            if stable or candidate.op=='revoke' or any(v in question for v in ('记住','以后都','忘掉')):
                if candidate.op=='revoke':
                    target=next((p for p in self.prefs.prefs(uid) if p.value==candidate.value),None)
                    if target:candidate=replace(candidate,dimension=target.dimension,polarity=target.polarity,strength=target.strength)
                outcome=self.prefs.apply(uid,sid,turn,candidate)
                if candidate.op=='set' and outcome['decision'] in ('ADD','UPDATE','NOOP'):
                    record=next((p for p in self.prefs.prefs(uid) if p.id==outcome.get('id') and p.status=='active'
                                and p.value==candidate.value and p.polarity==candidate.polarity),None)
                    if record:
                        from .management import describe
                        saved.append(record.id)
                if (outcome.get('needs_confirmation') or
                    stable and not recipe_request and (not self.previous_task or candidate.dimension=='flavor') or
                    any(v in question for v in ('记住','以后都','忘掉','不用管','不用提醒'))):
                    if outcome['decision']=='SESSION_ONLY':
                        outcome={**outcome,'message':'已记录在本次对话中，不会保存为长期偏好。'}
                    self.management=outcome
        if saved and self.management and not self.management.get('needs_confirmation'):
            current_saved=[describe(p) for p in self.prefs.prefs(uid) if p.id in saved and p.status=='active']
            if current_saved:
                self.management={**self.management,'message':'已记住并更新长期偏好：'+'；'.join(current_saved)+'。后续推荐会参考当前偏好。'
                                 +(' 现有食材仅在本次对话中保留。' if inventory else '')}
        temp=self.session.slots['temp_constraints']
        temp.setdefault('profile_overrides',{})
        temp.setdefault('hard_constraints',[])
        for c in self.candidates:
            if c.scope!='session' and c.op=='set':temp['profile_overrides'].pop(c.dimension,None)
            if c.scope=='session' and c.op=='set':
                if c.strength=='hard':
                    if not any(p['dimension']==c.dimension and p['value']==c.value for p in temp['hard_constraints']):temp['hard_constraints'].append(asdict(c))
                elif c.dimension!='inventory':temp['profile_overrides'][c.dimension]=c.value
            if c.op=='revoke':
                old=next((p for p in temp['hard_constraints'] if p['dimension']==c.dimension and p['value']==c.value),None)
                if old:
                    from datetime import timedelta
                    self.session.pending_confirmation={'action':'relax_session_hard','constraint':old,'expires_at':stamp(now()+timedelta(hours=24)),'turns_left':3}
        self.profile=self.prefs.load_profile(uid)
        self.hard=copy.deepcopy(self.profile['hard_constraints'])
        self.hard.extend(temp['hard_constraints'])
        # Cover the queue's pending/processing interval without committing a
        # synchronous preference write or trusting any assistant/tool text.
        with store.connect(uid) as db:
            rows=db.execute('''SELECT m.content FROM cookbuddy.memory_tasks q JOIN cookbuddy.messages m
                ON m.job_id=q.job_id AND m.user_id=q.user_id AND m.role='user'
                WHERE q.state IN ('pending','processing') AND q.epoch=%s AND q.profile_epoch=%s''',
                (store.memory_epoch(uid),self.prefs.epoch(uid))).fetchall()
        immediate=self.candidates+[c for row in rows for c in rule_extract(row['content']) if c.scope!='session']
        for c in immediate:
            if c.strength=='hard' and c.source=='explicit' and c.op=='set':
                self.hard.append(asdict(c))
        self.notice=self.prefs.pending_message(uid,sid)
        if self.session.pending_confirmation:
            c=self.session.pending_confirmation['constraint']
            self.notice='确认移除本会话的「'+c['value']+'」限制吗？（回复“确认”或“保留”）'
            if any(candidate.op=='revoke' for candidate in self.candidates):
                self.management={'decision':'ASK','message':self.notice,'needs_confirmation':True}
        from src.agent.turn_intents import alternative_request
        if (not self.management and not alternative_request(question) and self.candidates and not self.previous_task and not self.session.slots['last_recommendations']
                and not re.search(r'推荐|怎么|如何|多久|多少|能否|能不能|可以做|做法|原文|全文|步骤|图片|照片|配方|[?？]',question)
                and all(c.op=='set' for c in self.candidates)):
            self.management={'decision':'SESSION_ONLY' if all(c.scope=='session' for c in self.candidates) else 'QUEUED',
                             'message':('已记录本次对话的食材和临时要求，不会保存为长期偏好。' if all(c.scope=='session' for c in self.candidates)
                                        else '这次会按你提供的偏好和限制处理。需要长期保留的信息会在后台提取。'),
                             'needs_confirmation':False,'asynchronous':True}
    def context_items(self,recommend=False):
        if not self.enabled or self.level<3:return []
        items=[{'id':p['id'],'kind':'profile','content':p['dimension']+':'+p['value']+'; polarity='+p['polarity'],'version':p['version']} for p in self.profile['soft_prefs']]
        if self.level>=6 and (recommend or any(v in self.question for v in ('上次','之前','以前做过','我做过的'))):
            try:self.events=EpisodicStore(self.prefs).recall(self.uid,self.question)
            except Exception:self.events=[]
        return items
    def apply_soft(self,task,session):
        if not self.enabled or not task.get('state'):return
        state=task['state'];current={c.dimension:c for c in self.candidates if c.op=='set'}
        explicit_taste=any(e.get('slot')=='tastes' for e in task.get('edits',[]))
        if state.get('memory_tastes')==state['tastes'] and not explicit_taste and 'spice_level' not in current:
            state['tastes']=[]
        state.pop('memory_tastes',None)
        if 'spice_level' in current and task['action'] in ('clarify','recommend'):
            state['tastes']=[{'0':'不辣','1':'微辣','2':'辣味','3':'辣味'}[current['spice_level'].value]]
            task['action']='recommend'
        if task['action']!='recommend':return
        import re
        if not state['ingredients'] and re.search('现有|家里|手边|库存',self.question):
            inventory=session.slots['ingredients_on_hand']
            if inventory:state['ingredients']=inventory;state['mode']='inventory'
            elif state['mode']=='inventory':
                task.update(action='clarify',clarification='本次对话还没有记录现有食材，请告诉我现在有什么食材。')
                return
        temp=session.slots['temp_constraints'].get('profile_overrides',{})
        def inherit_spice(value):
            taste={'0':'不辣','1':'微辣','2':'辣味','3':'辣味'}[value]
            # The reducer drops spicy tastes when chili is excluded. Filling an
            # empty slot from memory must not undo that current task constraint.
            if '辣椒' in state.get('excluded_ingredients',[]) and taste!='不辣':return
            state['tastes']=[taste]
            state['memory_tastes']=list(state['tastes'])
        if 'spice_level' in temp and 'spice_level' not in current and not state['tastes']:
            inherit_spice(temp['spice_level'])
        if not state['tastes'] and 'spice_level' not in current:
            pref=next((p for p in self.profile['soft_prefs'] if p['dimension']=='spice_level'),None)
            if pref:inherit_spice(pref['value'])
        from ..task_context import task_question
        task['question']=task_question(state)
    def rank(self,data,state,session):
        if not self.enabled or self.level<6 or not data or not data.get('recommendations'):return data
        return personalize_result(data,state,self.profile,session)
