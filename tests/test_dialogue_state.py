"""Semantic paraphrases and durable state across an actually trimmed history."""
import asyncio
import copy
import unittest
from dialogue_harness import DialogueHarness
from src.nlu.router import validate


def model_plan(intent='follow_up',**slots):
    return lambda *args:validate({'intent':intent,'confidence':.98,'slots':slots})


class DialogueStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_price_waiver_clears_old_condition_for_later_turns(self):
        chat=DialogueHarness()
        await chat.turn('两个人30分钟两菜一汤，预算50元')
        p,r,_=await chat.turn('价格先不用核实，可以用常备调料，按两菜一汤搭配。',
            model_plan('meal_plan',meal={'people':2,'minutes':30},constraints={}))
        self.assertEqual(r['outputs'][0]['result']['status'],'verified')
        self.assertEqual(chat.session.slots['meal_request']['constraints']['unresolved'],[])
        p,r,_=await chat.turn('换一套',model_plan('meal_plan'))
        self.assertEqual(r['outputs'][0]['result']['status'],'verified')

    async def test_price_waiver_continues_pending_meal_after_budget_clarification(self):
        for reply in ('价格不用核实','可以按常备调料选菜，不需要核实价格，其他没有特殊要求'):
            with self.subTest(reply=reply):
                chat=DialogueHarness()
                await chat.turn('两个人60分钟安排晚餐，两菜一汤，预算50元。')
                p,r,_=await chat.turn(reply)
                request=p['task']['meal_request']
                self.assertEqual(p['task']['action'],'meal_plan')
                self.assertEqual(request['constraints'].get('unresolved',[]),[])
                self.assertEqual((request['people'],request['minutes'],request['dishes'],request['soups']),(2,60,2,1))
                self.assertEqual(r['outputs'][0]['result']['status'],'verified')

    async def test_meal_model_omitted_slots_inherit_active_menu(self):
        chat=DialogueHarness()
        await chat.turn('两个人60分钟安排晚餐，两菜一汤')
        previous={r['source_path'] for r in chat.session.slots['last_recommendations']}
        p,r,_=await chat.turn('换一套',model_plan('meal_plan'))
        self.assertEqual(p['task']['action'],'meal_plan')
        data=r['outputs'][0]['result']
        self.assertEqual((data['meal_plan']['people'],data['meal_plan']['minutes']),(2,60))
        self.assertFalse(previous & {v['source'] for v in data['recommendations']})

    async def test_missing_optional_meal_inputs_do_not_trigger_model_clarification(self):
        chat=DialogueHarness()
        model=lambda *args:validate({'intent':'meal_plan','confidence':.95,
            'slots':{'meal':{}},'needs_clarification':True,'clarify_question':'几个人、多少分钟？'})
        p,r,_=await chat.turn('帮我安排晚餐。',model)
        self.assertEqual(p['task']['action'],'meal_plan')
        self.assertEqual(r['outputs'][0]['result']['status'],'verified')

    async def test_meal_optional_inputs_and_short_followups_survive_model_outage(self):
        chat=DialogueHarness()
        for question,people,minutes in [('帮我安排晚餐。',None,None),
                ('两个人，想做两菜一汤。',2,None),('60分钟',2,60),('换一套',2,60)]:
            previous={r['source_path'] for r in chat.session.slots['last_recommendations']}
            p,r,_=await chat.turn(question)
            self.assertEqual(p['task']['action'],'meal_plan')
            data=r['outputs'][0]['result']
            self.assertEqual(data['status'],'verified')
            self.assertEqual((data['meal_plan']['people'],data['meal_plan']['minutes']),(people,minutes))
            if question=='换一套':
                self.assertFalse(previous & {v['source'] for v in data['recommendations']})

    async def test_explicit_replace_and_keep_survives_missing_model_edit(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。')
        original=chat.session.slots['last_recommendations'].copy()
        p,r,_=await chat.turn('第一项换一道，第二项保留。',model_plan('constraint_refine'))
        self.assertEqual(p['task'].get('replacement_index'),1)
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),1)
        self.assertEqual(chat.session.slots['last_recommendations'][1]['source_path'],original[1]['source_path'])
        self.assertNotEqual(chat.session.slots['last_recommendations'][0]['source_path'],original[0]['source_path'])

    async def test_replace_keep_invalid_or_conflicting_ordinals_preserve_list(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。')
        original=chat.session.slots['last_recommendations'].copy()
        for question in ('第一项换一道，第九项保留。','第一项换一道，第一项保留。'):
            p,r,_=await chat.turn(question,model_plan('constraint_refine'))
            self.assertEqual(p['task']['action'],'clarify')
            self.assertEqual(chat.session.slots['last_recommendations'],original)

    async def test_explicit_ordinal_overrides_model_omission_after_replacement(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。')
        expected=chat.session.slots['last_recommendations'][1]['source_path']
        wrong=chat.session.slots['last_recommendations'][0]['dish_name']
        p,r,_=await chat.turn('第二道还是刚才保留的那道吗？给我操作原文。',model_plan('recipe_section',dish=wrong,section='操作',request_kind='source'))
        self.assertEqual(p['named_recipe']['source_path'],expected)
        self.assertTrue(all(e['source']==expected for e in r['outputs'][0]['result']['evidence']))

    async def test_known_ordinal_source_request_does_not_ask_which_list(self):
        from src.nlu.router import validate
        chat=DialogueHarness()
        await chat.turn('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。')
        model=lambda *a:validate({'intent':'follow_up','confidence':.85,'slots':{'request_kind':'source'},'needs_clarification':True,'clarify_question':'你说的第一项指哪个列表？'})
        p,r,_=await chat.turn('这次第一项叫什么？给我原文。',model)
        self.assertNotEqual(p['task']['action'],'clarify')
        self.assertTrue(r['outputs'])

    async def test_ordinal_missing_ingredients_survives_understanding_outage(self):
        chat=DialogueHarness()
        _,initial,_=await chat.turn('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。')
        source=chat.session.slots['last_recommendations'][1]['source_path']
        expected_missing=initial['outputs'][0]['result']['recommendations'][1]['missing']
        def unavailable(*args):raise TimeoutError('injected understanding outage')
        for question in ('第二道还需要准备哪些食材？','第二项还缺什么？'):
            with self.subTest(question=question):
                p,r,_=await chat.turn(question,unavailable)
                self.assertEqual(p['task']['action'],'ingredients')
                self.assertEqual(p['task']['selected_source'],source)
                self.assertEqual(r['outputs'][0]['tool'],'recipe_ingredients')
                self.assertEqual(r['outputs'][0]['result']['missing'],expected_missing)

    async def test_section_fact_questions_after_images_use_qa_instead_of_original(self):
        from unittest.mock import patch
        import langchain_service_worker as worker
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包的完整原文和图片。',model_plan('recipe_lookup',dish='基础牛奶面包',request_kind='bundle'))
        await chat.turn('只看酵头制作的图片。',model_plan('recipe_section',section='酵头的制作',request_kind='images'))
        execute=worker.execute
        questions=[('酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。','酵头的制作'),
                   ('面包的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？','操作'),
                   ('面包的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？','')]
        for question,section in questions:
            calls=[]
            def service(payload):
                if payload['operation']=='fact':
                    calls.append(payload['arguments']['question'])
                    return {'result':{'status':'verified','short_answer':'已核实的针对性回答','evidence':[],'sources':[]}}
                return execute(payload)
            with patch.object(worker,'execute',side_effect=service):
                p,r,handled=await chat.turn(question,model_plan('recipe_section',section=section,request_kind='facts'))
            self.assertTrue(handled)
            self.assertEqual([o['tool'] for o in r['outputs']],['source_fact'])
            self.assertIn(question,calls[0])
            self.assertEqual(p['slots']['selected_section'],section)

    async def test_image_evidence_limit_survives_model_outage(self):
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包原文。',model_plan('recipe_lookup',dish='基础牛奶面包',request_kind='source'))
        def unavailable(*args):raise TimeoutError('injected understanding outage')
        p,r,_=await chat.turn('只根据面包图片判断内部是否已经完全熟透。',unavailable)
        self.assertEqual(p['nlu']['path'],'fast')
        self.assertEqual(r['outputs'][0]['tool'],'find_recipe_images')
        self.assertIn('不能单独证明',r['outputs'][0]['result']['short_answer'])
        self.assertIn('熟度',r['outputs'][0]['result']['short_answer'])

    async def test_thanks_with_retention_words_returns_no_recipe_sources(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。')
        def unavailable(*args):raise TimeoutError('injected understanding outage')
        p,r,_=await chat.turn('谢谢，先保留这两道。',unavailable)
        self.assertEqual(p['nlu']['path'],'fast')
        self.assertEqual(p['nlu']['intent'],'chitchat')
        self.assertEqual(r['outputs'][0]['result']['short_answer'],'不客气，随时可以继续问我。')
        self.assertFalse(r['outputs'][0]['result']['sources'])

    async def test_full_source_and_images_do_not_require_a_chapter(self):
        chat=DialogueHarness()
        p,r,handled=await chat.turn('查看基础牛奶面包的完整原文和图片。',model_plan('recipe_section',dish='基础牛奶面包',section='',request_kind='bundle'))
        self.assertEqual(p['nlu']['answer_mode'],'grounded_recipe')
        self.assertTrue(handled)
        self.assertEqual({o['tool'] for o in r['outputs']},{'get_recipe_detail','find_recipe_images'})
        self.assertTrue(next(o['result']['images'] for o in r['outputs'] if o['tool']=='find_recipe_images'))

    async def test_section_resolves_list_ordinal_before_requesting_dish(self):
        chat=DialogueHarness()
        await chat.turn('挑两道鸡蛋菜',model_plan('ingredient_recommend',ingredients=['番茄','鸡蛋'],mode='inventory',limit=2))
        expected=chat.session.slots['last_recommendations'][1]['source_path']
        p,r,_=await chat.turn('第二道的做法展开看看',model_plan('recipe_section',selected_ref=2,section='操作',request_kind='source'))
        self.assertEqual(p['nlu']['answer_mode'],'grounded_recipe')
        self.assertEqual(p['named_recipe']['source_path'],expected)
        self.assertTrue(r['outputs'][0]['result']['evidence'])
        p,r,_=await chat.turn('第二道的做法展开看看')
        self.assertEqual(p['named_recipe']['source_path'],expected)
        self.assertTrue(r['outputs'][0]['result']['evidence'])
        p,r,_=await chat.turn('第二道的做法展开看看',model_plan('recipe_section',selected_ref=2,section='',request_kind='source'))
        self.assertEqual(p['nlu']['answer_mode'],'grounded_recipe')
        self.assertEqual(p['named_recipe']['source_path'],expected)
        self.assertTrue(r['outputs'][0]['result']['evidence'])

    async def test_inventory_memory_notice_does_not_consume_recipe_request(self):
        chat=DialogueHarness()
        p,r,_=await chat.turn('我有番茄鸡蛋，挑两道菜，缺什么列出来',model_plan('ingredient_recommend',ingredients=['番茄','鸡蛋'],mode='inventory',limit=2),management={'decision':'SESSION_ONLY','message':'已记录本次食材'})
        self.assertIsNone(p['management'])
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),2)

    def test_ordinal_objects_still_reject_invalid_types_and_fields(self):
        for ref in ({'type':'ordinal','value':True},{'type':'ordinal','value':'1'},{'type':'ordinal','value':1,'extra':2}):
            with self.assertRaises(ValueError):validate({'intent':'follow_up','confidence':.9,'slots':{'selected_ref':ref}})

    async def test_free_recommendation_with_missing_stock_is_not_followup(self):
        chat=DialogueHarness()
        p,r,_=await chat.turn('冰箱就剩番茄鸡蛋啦，帮我挑俩菜，缺啥一块告诉我',model_plan('ingredient_recommend',ingredients=['番茄','鸡蛋'],mode='inventory',limit=2,request_kind='ingredients',response_fields=['missing_ingredients']))
        self.assertEqual(p['task']['action'],'recommend')
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),2)

    async def test_stock_followup_keeps_model_resolved_ordinal(self):
        chat=DialogueHarness()
        await chat.turn('挑两道鸡蛋菜',model_plan('ingredient_recommend',ingredients=['番茄','鸡蛋'],mode='inventory',limit=2))
        expected=chat.session.slots['last_recommendations'][1]['source_path']
        p,r,_=await chat.turn('第二道到底是哪道，缺什么也再说一下',model_plan(selected_ref=2,request_kind='ingredients'))
        self.assertEqual(p['task']['action'],'ingredients')
        self.assertEqual(p['task']['selected_source'],expected)

    async def test_single_replacement_does_not_display_extra_candidates(self):
        chat=DialogueHarness()
        await chat.turn('挑两道鸡蛋菜',model_plan('ingredient_recommend',ingredients=['番茄','鸡蛋'],mode='inventory',limit=2))
        retained=copy.deepcopy(chat.session.slots['last_recommendations'][1])
        p,r,_=await chat.turn('第一道换掉吧1，另一个留着',model_plan('constraint_refine',replace_ref={'type':'ordinal','value':1},limit=2))
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),1)
        self.assertEqual(chat.session.slots['last_recommendations'][1],retained)

    async def test_hundred_turns_trim_history_keep_stock_source_and_restart(self):
        chat=DialogueHarness()
        p,r,handled=await chat.turn('冰箱里现在就番茄鸡蛋，帮我挑两样，缺的也列一下',
            model_plan('ingredient_recommend',ingredients=['番茄','鸡蛋'],mode='inventory',limit=2,response_fields=['missing_ingredients']))
        self.assertTrue(handled);self.assertEqual(len(r['outputs'][0]['result']['recommendations']),2)
        p,r,_=await chat.turn('排前面的那个，给我看完整做法',model_plan(selected_ref=1))
        source=p['named_recipe']['source_path']
        for i in range(96):
            q='多谢啦' if i%3==0 else '我接着问刚才那个：用料呢' if i%3==1 else '把刚才那份制作步骤展开'
            model=model_plan('chitchat') if i%3==0 else model_plan(request_kind='ingredients') if i%3==1 else model_plan('recipe_section',section='操作',request_kind='source')
            p,r,handled=await chat.turn(q,model)
            self.assertTrue(handled,(i,p))
            self.assertNotEqual(p['task']['action'],'clarify')
            if i%3:self.assertEqual(p['named_recipe']['source_path'],source)
            self.assertEqual(chat.session.slots['ingredients_on_hand'],['番茄','鸡蛋'])
        # A fresh process reads only serialized state; old transcript is absent.
        from cookbuddy.session import SessionState
        resumed=DialogueHarness();resumed.state=copy.deepcopy(chat.state)
        resumed.session=SessionState('u','s');resumed.session.slots=copy.deepcopy(chat.session.slots)
        self.assertLess(len(chat.session.messages),30)
        for q in ('还缺哪些要买的','再看看做的时候那一段'):
            p,r,handled=await resumed.turn(q,model_plan(request_kind='ingredients'))
            self.assertTrue(handled);self.assertEqual(p['task']['selected_source'],source)
            self.assertTrue(r['outputs'])
        self.assertEqual(chat.count+resumed.count,100)

    async def test_semantic_inventory_edits_and_filters_survive_unrelated_chat(self):
        chat=DialogueHarness()
        await chat.turn('先给我两道鸡肉菜',model_plan('ingredient_recommend',ingredients=['鸡肉'],mode='inventory',limit=2,constraints={'max_difficulty':2}))
        await chat.turn('还有一块豆腐也放进去',model_plan('constraint_refine',ingredients=['豆腐'],ingredient_operation='add'))
        await chat.turn('谢啦',model_plan('chitchat'))
        p,_,_=await chat.turn('豆腐刚发现坏了，只用剩下的',model_plan('constraint_refine',ingredients=['豆腐'],ingredient_operation='remove'))
        self.assertEqual(p['task']['state']['ingredients'],['鸡肉'])
        self.assertEqual(p['task']['filters'],{'max_difficulty':2})
        self.assertEqual(p['task']['state']['limit'],2)

    async def test_simple_recommendation_executes_with_difficulty_filter(self):
        chat=DialogueHarness()
        p,result,_=await chat.turn('推荐两道简单的鸡肉菜')
        self.assertEqual(p['task']['filters'],{'max_difficulty':2})
        rows=result['outputs'][0]['result']['recommendations']
        self.assertEqual(len(rows),2)
        self.assertTrue(all(1<=row['difficulty']<=2 for row in rows))

    async def test_ambiguous_reference_never_guesses_first_recipe(self):
        chat=DialogueHarness()
        await chat.turn('推荐两道鸡蛋菜',model_plan('ingredient_recommend',ingredients=['鸡蛋'],limit=2))
        p,_,_=await chat.turn('它还缺什么',model_plan(request_kind='ingredients'))
        self.assertEqual(p['task']['action'],'clarify')
        self.assertIsNone(p['named_recipe'])
        self.assertTrue(p['task']['clarification_options'])

    async def test_unsupported_condition_is_retained_until_user_explicitly_drops_it(self):
        chat=DialogueHarness()
        p,r,_=await chat.turn('两道鸡肉菜，便宜点别超过三块',model_plan('ingredient_recommend',ingredients=['鸡肉'],limit=2,constraints={'unresolved':['总预算三元']}))
        self.assertIn('总预算三元',p['task']['state']['extra_constraints'])
        self.assertFalse(r['outputs'][0]['result'].get('recommendations'))
        p,r,_=await chat.turn('先按已理解条件推荐')
        self.assertEqual(p['task']['state']['extra_constraints'],[])
        self.assertTrue(r['outputs'][0]['result'].get('recommendations'))

    async def test_condition_clarification_keeps_one_dish_after_model_count_drift(self):
        chat=DialogueHarness()
        p,r,_=await chat.turn('推荐一道清淡的菜。')
        self.assertEqual(p['task']['state']['limit'],1)
        self.assertTrue(r['outputs'][0]['result']['clarification_options'])
        p,r,_=await chat.turn('先按已理解条件推荐',
            model_plan('constraint_refine',ingredients=['番茄'],limit=3))
        self.assertEqual(p['task']['state']['limit'],1)
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),1)

    async def test_independent_recommendation_clears_prior_inventory(self):
        chat=DialogueHarness()
        await chat.turn('我有番茄和土豆，推荐两道菜。')
        p,r,_=await chat.turn('取消后还能继续吗？推荐一道快手菜。',
            model_plan('constraint_refine',ingredients=['番茄','土豆'],limit=5))
        self.assertEqual(p['task']['state']['ingredients'],[])
        self.assertEqual(p['task']['state']['mode'],'preference')
        self.assertEqual(p['task']['state']['limit'],1)
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),1)

    async def test_model_outage_still_supports_contextual_chapter_images(self):
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包的完整原文和图片。')
        def unavailable(*a):raise TimeoutError()
        p,r,_=await chat.turn('只看酵头制作的图片。',unavailable)
        self.assertEqual(p['nlu']['path'],'rule')
        self.assertEqual(len(r['outputs'][0]['result']['images']),3)
        self.assertEqual(p['named_recipe']['dish_name'],'基础牛奶面包')

    async def test_chapter_survives_images_then_procedure_followup(self):
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包的完整原文和图片。')
        await chat.turn('只看酵头制作的图片。')
        p,r,_=await chat.turn('具体怎么做')
        self.assertEqual(p['nlu']['slots']['section'],'酵头的制作')
        self.assertTrue(all('酵头的制作' in e['section'] for e in r['outputs'][0]['result']['evidence']))

    async def test_inventory_can_be_explicitly_cleared(self):
        chat=DialogueHarness()
        await chat.turn('我有鸡蛋，推荐两道菜')
        p,_,_=await chat.turn('都用完了，重新选吧',model_plan('constraint_refine',ingredient_operation='clear'))
        self.assertEqual(p['task']['state']['ingredients'],[])
        self.assertEqual(chat.session.slots['ingredients_on_hand'],[])

    async def test_invalid_count_is_atomic(self):
        chat=DialogueHarness()
        await chat.turn('推荐两道鸡蛋菜')
        previous=copy.deepcopy(chat.state)
        p,_,_=await chat.turn('一道都不要',model_plan('constraint_refine',limit=0))
        self.assertEqual(p['task']['action'],'clarify')
        self.assertEqual(chat.state,previous)

    async def test_free_expression_meal_uses_structured_conditions(self):
        chat=DialogueHarness()
        p,r,_=await chat.turn('我俩下班就吃，四十五分钟搞定两个菜配个汤吧',
            model_plan('meal_plan',meal={'meal_type':'dinner','people':2,'minutes':45,'dishes':2,'soups':1,'explicit_composition':True}))
        self.assertEqual(p['task']['action'],'meal_plan')
        self.assertEqual(r['outputs'][0]['result']['status'],'verified')
        self.assertEqual(len(r['outputs'][0]['result']['recommendations']),3)

    async def test_provider_cooldown_is_not_reported_as_user_misunderstanding(self):
        from src.dialogue import public_reply
        from service_runtime import BudgetExceeded
        calls=[]
        def limited(*args):
            calls.append(args)
            error=BudgetExceeded('provider cooldown');error.reason='provider_retry_after'
            raise error
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包原文')
        source=copy.deepcopy(chat.session.slots['selected_dish'])
        p,r,_=await chat.turn('可以随便聊两句吗',limited)
        self.assertEqual(len(calls),1)
        self.assertIn('provider_rate_limited',p['nlu']['errors'])
        self.assertIn('理解服务暂时不可用',public_reply(p['nlu']))
        self.assertEqual(chat.session.slots['selected_dish'],source)
