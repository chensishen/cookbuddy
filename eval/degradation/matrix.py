"""Offline acceptance: real routing, reducers, corpus tools and isolated memory DBs.

Only the primary understanding response and failed remote boundaries are scripted.
The dialogue harness replaces tenant persistence, not recipe facts or filters.
"""
import asyncio
import copy
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'tests')]
from dialogue_harness import DialogueHarness
from eval.degradation.faults import FaultyModel
from src.capabilities import CAPABILITIES,STATES,validate_registry,degradation
from src.dialogue import public_reply
from src.nlu.router import understand

CASES={
    'meal_plan':('安排早餐','meal_plan',{'meal':{'meal_type':'breakfast'}}),
    'ingredient_recommend':('我有番茄和鸡蛋，推荐两道菜','ingredient_recommend',{'ingredients':['番茄','鸡蛋'],'mode':'inventory','limit':2}),
    'constraint_refine':('换一道，不要第二道','constraint_refine',{'exclude_refs':[{'type':'ordinal','value':2}],'limit':1,'replace_batch':True}),
    'recipe_lookup':('查看基础牛奶面包完整原文','recipe_lookup',{'dish':'基础牛奶面包','request_kind':'source'}),
    'recipe_section':('只看酵头制作部分','recipe_section',{'section':'酵头的制作','request_kind':'source'}),
    'recipe_images':('只看酵头制作的图片','recipe_section',{'section':'酵头的制作','request_kind':'images'}),
    'recipe_ingredients':('这道还需要准备哪些食材','recipe_lookup',{'request_kind':'ingredients'}),
    'follow_up':('第一道的做法','follow_up',{'selected_ref':1,'request_kind':'source'}),
    'identity_capability':('你是谁','identity_capability',{}),
    'chitchat':('你好','chitchat',{}),
    'preference_update':('请记住我喜欢微辣','preference_update',{}),
    'out_of_scope':('帮我查天气','out_of_scope',{}),
    'unclear':('随便那个然后弄一弄','unclear',{}),
    'recall_history':('上次做菜的反馈','follow_up',{}),
}


def semantic(plan):
    """Compare the full executable intent/slot contract, not confidence/notices."""
    return {'intent':plan['intent'],'slots':plan['slots']}


def check_evidence(outputs):
    for output in outputs:
        data=output['result']
        for row in data.get('recommendations',[]):
            assert row['source'] and row['evidence'],'recommendation_without_source'
        for ref in data.get('evidence',[]):
            original=(ROOT/ref['source']).read_text().splitlines()
            assert '\n'.join(original[ref['start_line']-1:ref['end_line']])==ref['quote'],'nonliteral_evidence'


async def exercise(name,state):
    from recipe_tools import load_chunks
    from cookbuddy.ltm.profile_store import ProfileStore
    from cookbuddy.ltm.extractor import rule_extract
    from cookbuddy.ltm.management import handle_command
    from cookbuddy.ltm.models import Candidate
    from cookbuddy.ltm.episodic_store import EpisodicStore
    question,intent,slots=CASES[name]
    oracle={'intent':intent,'confidence':.99,'slots':copy.deepcopy(slots)}
    model_state=state if state in ('ok','timeout','5xx','429','invalid_json','breaker_open','slow','partial') else 'ok'
    fault=FaultyModel(model_state,oracle);chat=DialogueHarness();management=None
    bread=next(r for r in load_chunks() if r['title']=='基础牛奶面包')
    selected={'idx':1,'dish_name':bread['title'],'source_path':bread['source']}
    if name in ('recipe_section','recipe_images','recipe_ingredients'):
        chat.session.slots['selected_dish']=selected
    if name=='follow_up':chat.session.slots['last_recommendations']=[selected]
    if name=='constraint_refine':await chat.turn('推荐两道鸡蛋菜')
    # Trap each remote boundary: local capability paths must neither depend on
    # it nor claim that a failure occurred when the boundary was never called.
    bomb=Mock(side_effect=TimeoutError('injected_remote_boundary'))
    target={'embedding_down':'cookbuddy.models.embed','rerank_down':'ask_test_recipes.run_stage',
            'milvus_down':'insert_test_recipes.milvus_api'}.get(state)
    with tempfile.TemporaryDirectory() as tmp:
        prefs=ProfileStore(path=Path(tmp)/'memory.sqlite') if name in ('preference_update','recall_history') else None
        if name=='preference_update':
            candidates=rule_extract(question);assert candidates
            for candidate in candidates:
                outcome=prefs.apply('matrix_user','matrix_session','remember',candidate)
                assert outcome['decision']=='ADD'
            assert any(p.dimension=='spice_level' and p.value=='1' for p in prefs.prefs('matrix_user'))
            prefs.apply('matrix_user','matrix_session','seed',Candidate('avoid_ingredient','香菜','dislike','soft'))
            deleted=handle_command(('forget','香菜'),prefs,'matrix_user','matrix_session','forget')
            assert deleted['decision']=='DELETE'
            assert [p.value for p in prefs.prefs('matrix_user')]==['1']
            management={'message':'已记住微辣偏好；已忘记香菜偏好。','decision':'ADD'}
        if name=='recall_history':
            events=EpisodicStore(prefs,vectors=Mock(search=Mock(side_effect=TimeoutError())),embedding=lambda *a:[1.,0.])
            events.write('matrix_user',{'text':'用户反馈蒸水蛋：好吃。','type':'feedback','dish_refs':['蒸水蛋'],'importance':.8},'seed')
            if state=='embedding_down':events.embedding=bomb
            try:
                rows=events.recall('matrix_user','蒸水蛋')
                assert rows and '蒸水蛋' in rows[0]['text']
                outputs=[{'tool':'recall_history','result':{'status':'verified_source','short_answer':rows[0]['text'],'sources':[],'evidence':[]}}]
            except TimeoutError:
                outputs=[{'tool':'recall_history','result':{'status':'tool_failed','short_answer':'现在无法查询历史反馈，可以先告诉我菜名查做法。','sources':[],'evidence':[]}}]
            plan={'intent':'follow_up','slots':{},'errors':[],'path':'model'}
            meta=degradation(plan,outputs,'matrix-request',tool_degraded=state=='embedding_down',tool_failed=state=='embedding_down')
            return {'capability':name,'state':state,'correct':True,'parity':None,'meta':meta,'path':'isolated_history_store'}
        from src.nlu.config import settings as nlu_settings
        router_config=copy.deepcopy(nlu_settings());router_config['fast_path']['enabled']=name=='chitchat'
        # Inject faults into model routes; exact greetings keep their template path.
        # Complete literal no-call behavior has separate latency tests.
        with patch('src.nlu.router.settings',return_value=router_config):
            if target:
                with patch(target,bomb):prepared,result,handled=await chat.turn(question,fault,management=management)
            else:prepared,result,handled=await chat.turn(question,fault,management=management)
        plan=prepared['nlu'];outputs=result['outputs']
        assert handled and outputs,'capability_not_executed'
        # Explicit memory management is deterministic and intentionally does
        # not call the remote understanding model.
        if model_state not in ('ok','partial') and name not in ('chitchat','preference_update'):
            assert fault.calls>0 and plan['path'] in ('rule','general_chat'),'fault_not_exercised'
        if target:assert bomb.call_count==0,'declared_local_path_called_remote_dependency'
        check_evidence(outputs)
        data=outputs[0]['result']
        if name=='meal_plan':
            assert data['meal_plan']['meal_type']=='breakfast'
            assert data['meal_plan']['estimated_minutes']<=25
            assert all(r['category'] in {'breakfast','staple','vegetable_dish','soup','drink'} for r in data['recommendations'])
        elif name in ('recipe_section','recipe_images'):
            assert data.get('evidence') or data.get('images')
            assert all('酵头' in r['section'] for r in data.get('evidence',[]))
            if name=='recipe_images':assert data['images']
        elif name=='constraint_refine':
            assert data.get('recommendations')
            rejected=chat.state.get('excluded_dishes',[]) if chat.state else []
            assert not any(r['title'] in rejected for r in data['recommendations'])
        elif name=='ingredient_recommend':assert data.get('recommendations')
        elif name in ('recipe_lookup','follow_up'):assert data.get('evidence')
        elif name=='recipe_ingredients':assert data.get('ingredient_check') or data.get('inventory_check') or data.get('evidence')
        elif name=='identity_capability':assert 'cookbuddy' in data['short_answer']
        elif name=='out_of_scope':assert '菜谱' in data['short_answer']
        elif name=='preference_update':assert '微辣' in data['short_answer']
        meta=degradation(plan,outputs,'matrix-request')
        assert not meta['service_notice'] or name=='unclear'
        if name=='unclear' and meta['degraded']:
            assert meta['unsupported_in_fallback'] and meta['request_id'] in meta['service_notice']
            assert public_reply(plan)
        return {'capability':name,'state':state,'correct':True,'semantic':semantic(plan),
                'meta':meta,'path':plan['path'],'fault_calls':fault.calls}


async def evaluate():
    assert set(CASES)==set(CAPABILITIES),'capability_case_missing'
    rows=[]
    for name in CASES:
        primary=None
        for state in STATES:
            try:
                row=await exercise(name,state)
                if state=='ok':primary=row.get('semantic')
                row['parity']=None if name in ('unclear','recall_history') else row.get('semantic')==primary
                row.pop('semantic',None)
            except Exception as error:
                row={'capability':name,'state':state,'correct':False,'error':type(error).__name__+':'+str(error)[:180]}
            rows.append(row)
    validate_registry(covered={(r['capability'],r['state']) for r in rows})
    return rows


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--out',type=Path,required=True);args=parser.parse_args()
    if args.out.exists():raise SystemExit('refuse_to_overwrite_report')
    rows=asyncio.run(evaluate());args.out.write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    failed=[r for r in rows if not r['correct'] or r.get('parity') is False]
    print(json.dumps({'total':len(rows),'failures':failed},ensure_ascii=False))
    raise SystemExit(bool(failed))
