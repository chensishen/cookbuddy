import unittest
from langgraph.checkpoint.memory import InMemorySaver
from src.agent.context import AppContext
from src.agent.engine import ChatEngine
from src.agent.tools import compact_result
from .fakes import ScriptedModel,tool_call
from .test_online import Services,configuration


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inherited_chapter_images_do_not_replay_full_original(self):
        self.services.nlu['slots'].update(request_kind='images',section='操作')
        original=self.services.call
        async def call(op,args,context,**kwargs):
            result=await original(op,args,context,**kwargs)
            if op=='prepare':result.update(named_recipe={'dish_name':'蒸水蛋','source_path':self.services.source},inherited_recipe=True)
            return result
        self.services.call=call
        engine=ChatEngine(configuration(),self.services,InMemorySaver(),model=ScriptedModel(replies=[RuntimeError()]))
        self.engines.append(engine)
        result=await engine.answer('只看操作的图片。',AppContext('A','only-images'))
        self.assertEqual({o['tool'] for o in result['outputs']},{'find_recipe_images'})
        self.assertEqual(next(c[1] for c in self.services.calls if c[0]=='images')['section'],'操作')
        self.assertNotIn('detail',[c[0] for c in self.services.calls])

    async def test_specific_fact_uses_current_question_not_full_original(self):
        original=self.services.call
        question='酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。'
        self.services.task={'action':'none','question':'基础牛奶面包的'+question}
        async def call(op,args,context,**kwargs):
            result=await original(op,args,context,**kwargs)
            if op=='prepare':result.update(named_recipe={'dish_name':'基础牛奶面包','source_path':self.services.source},inherited_recipe=True)
            return result
        self.services.call=call
        engine=ChatEngine(configuration(),self.services,InMemorySaver(),model=ScriptedModel(replies=[RuntimeError()]))
        self.engines.append(engine)
        result=await engine.answer(question,AppContext('A','specific-fact'))
        self.assertEqual({o['tool'] for o in result['outputs']},{'search_recipes'})
        search=next(c[1] for c in self.services.calls if c[0]=='search')
        self.assertIn(question,search['query'])
        self.assertEqual(search['source_path'],self.services.source)
        self.assertNotIn('detail',[c[0] for c in self.services.calls])

    async def asyncSetUp(self):
        self.services=Services();self.engines=[]

    async def asyncTearDown(self):
        for engine in self.engines:await engine.memory_queue.close()

    def engine(self,replies,**changes):
        engine=ChatEngine(configuration(agent={'direct':False},**changes),self.services,InMemorySaver(),
                          model=ScriptedModel(replies=replies))
        self.engines.append(engine);return engine

    async def test_early_model_stop_still_retrieves_verified_answer(self):
        result=await self.engine(['未经工具核实的回答']).answer('菜谱问题',AppContext('A','early'))
        self.assertIn('search',[c[0] for c in self.services.calls])
        self.assertIn('answer',[c[0] for c in self.services.calls])
        self.assertNotIn('未经工具核实的回答',result['text'])
        self.assertFalse(result['degraded'])

    async def test_failed_generation_does_not_erase_verified_detail(self):
        self.services.fail.add('answer')
        result=await self.engine([tool_call('get_recipe_detail',{'dish_name':'蒸水蛋'}),
            tool_call('search_recipes',{'query':'问题'},'second'),'结束']).answer('问题',AppContext('A','partial'))
        self.assertTrue(result['degraded'])
        detail=next(o['result'] for o in result['outputs'] if o['tool']=='get_recipe_detail')
        self.assertEqual(detail['status'],'verified_source')
        self.assertGreater(len(detail['short_answer']),300)
        self.assertIn('原文事实',result['text'])

    async def test_named_full_bundle_is_direct_and_not_truncated(self):
        self.services.nlu['slots'].update(request_kind='bundle',section='')
        original=self.services.call
        async def call(op,args,context,**kwargs):
            r=await original(op,args,context,**kwargs)
            if op=='prepare':r['named_recipe']={'dish_name':'蒸水蛋','source_path':self.services.source}
            if op in ('detail','images'):
                self.assertEqual(kwargs['timeout'],configuration().retrieval.tool_timeout_s)
            if op=='detail':
                r['evidence'][0]['quote']='逐字原文。'*1200;r['complete']=True
            return r
        self.services.call=call
        engine=ChatEngine(configuration(),self.services,InMemorySaver(),model=ScriptedModel(replies=[RuntimeError()]))
        self.engines.append(engine)
        result=await engine.answer('查看蒸水蛋完整原文和图片',AppContext('A','bundle'))
        self.assertEqual({o['tool'] for o in result['outputs']},{'get_recipe_detail','find_recipe_images'})
        self.assertNotIn('model',[c[0] for c in self.services.calls])
        detail=next(o['result'] for o in result['outputs'] if o['tool']=='get_recipe_detail')
        self.assertGreater(len(detail['short_answer']),4000)
        self.assertNotIn('已截断',detail['short_answer'])

    async def test_named_chapter_images_keep_literal_section_without_planner(self):
        self.services.nlu['slots'].update(request_kind='images',section='操作')
        original=self.services.call
        async def call(op,args,context,**kwargs):
            result=await original(op,args,context,**kwargs)
            if op=='prepare':result['named_recipe']={'dish_name':'蒸水蛋','source_path':self.services.source}
            return result
        self.services.call=call
        engine=ChatEngine(configuration(),self.services,InMemorySaver(),model=ScriptedModel(replies=[RuntimeError()]))
        self.engines.append(engine)
        result=await engine.answer('查看蒸水蛋操作的图片',AppContext('A','chapter'))
        args=next(c[1] for c in self.services.calls if c[0]=='images')
        self.assertEqual(args['section'],'操作')
        self.assertFalse(result['degraded'])

    async def test_named_substitution_uses_one_search_and_keeps_question(self):
        original=self.services.call
        async def call(op,args,context,**kwargs):
            result=await original(op,args,context,**kwargs)
            if op=='prepare':result['named_recipe']={'dish_name':'蒸水蛋','source_path':self.services.source}
            return result
        self.services.call=call
        engine=ChatEngine(configuration(),self.services,InMemorySaver(),model=ScriptedModel(replies=[RuntimeError()]))
        self.engines.append(engine)
        question='蒸水蛋可以换成别的原料吗？原文没说明的请明确指出。'
        result=await engine.answer(question,AppContext('A','substitution'))
        searches=[c for c in self.services.calls if c[0]=='search']
        self.assertEqual(len(searches),1)
        self.assertEqual(searches[0][1]['query'],question)
        self.assertIn('answer',[c[0] for c in self.services.calls])
        self.assertFalse(result['degraded'])

    async def test_named_search_scope_is_not_model_controlled(self):
        original=self.services.call
        async def call(op,args,context,**kwargs):
            r=await original(op,args,context,**kwargs)
            if op=='prepare':r['named_recipe']={'dish_name':'蒸水蛋','source_path':self.services.source}
            return r
        self.services.call=call
        await self.engine([tool_call('search_recipes',{'query':'另一道菜','source_path':'wrong'}),'结束']).answer(
            '蒸水蛋问题',AppContext('A','named'))
        args=next(c[1] for c in self.services.calls if c[0]=='search')
        self.assertEqual(args['source_path'],self.services.source)

    async def test_literal_fast_path_cannot_drop_extra_constraints(self):
        original=self.services.call
        async def call(op,args,context,**kwargs):
            r=await original(op,args,context,**kwargs)
            if op=='prepare':r['named_recipe']={'dish_name':'蒸水蛋','source_path':self.services.source}
            return r
        self.services.call=call
        model=ScriptedModel(replies=[tool_call('search_recipes',{'query':'蒸水蛋'}),'结束'])
        engine=ChatEngine(configuration(),self.services,InMemorySaver(),model=model)
        self.engines.append(engine)
        await engine.answer('查看蒸水蛋原文，不要放鸡蛋',AppContext('A','restricted'))
        self.assertEqual(model.position,0)
        search=next(c[1] for c in self.services.calls if c[0]=='search')
        self.assertIn('不要放鸡蛋',search['query'])
        self.assertIn('search',[c[0] for c in self.services.calls])
        self.assertNotIn('detail',[c[0] for c in self.services.calls])

    def test_compact_evidence_matches_its_section(self):
        result={'status':'verified_source','sources':[{'source':'a','section':'计算','line':5},
            {'source':'a','section':'操作','line':10}],
            'evidence':[{'source':'a','section':'计算','quote':'鸡蛋两个'},
                        {'source':'a','section':'操作','quote':'蒸十分钟'}]}
        items=compact_result(result)['items']
        self.assertEqual([r['snippet'] for r in items],['鸡蛋两个','蒸十分钟'])


if __name__=='__main__':unittest.main()
