import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from recipe_tools import dispatch,query_recipes,load_chunks,_query_recipes_with_cache,source_candidates
from search_filters import current_filters,filter_policy,filter_chunks
from recipe_agent import execute
from langchain_multi_agent import ToolCollector,build_agents
from ask_test_recipes import run_stage,StageFailure


class AgentSearchFilterTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'RECIPE_SEARCH_FILTERS':'{}','RECIPE_ANSWER_CACHE':'0'})
        env.start();self.addCleanup(env.stop)
        rows,_,_=filter_chunks(load_chunks(),category='soup',max_difficulty=2)
        self.source=rows[0]['source']

    def test_dispatch_preserves_original_question_and_filters(self):
        with patch('recipe_tools.query_recipes',return_value={}) as query:
            dispatch('query_recipes',{'question':'改写','category':'soup','max_difficulty':2},original_question='原始问题')
        query.assert_called_once()
        self.assertEqual(query.call_args.args[0],'原始问题')
        self.assertEqual(query.call_args.kwargs,{'category':'soup','max_difficulty':2})

    def test_invalid_tool_values_stop_before_retrieval(self):
        for key,value in (('max_difficulty',True),('max_difficulty','2'),('max_difficulty',6),('category','invalid')):
            with patch('recipe_tools._query_recipes_checked') as query:
                with self.assertRaises(ValueError):dispatch('query_recipes',{'question':'鸡蛋',key:value})
            query.assert_not_called()

    def test_literal_answer_respects_filters_and_context_is_restored(self):
        with patch('recipe_tools.run_stage',side_effect=AssertionError('literal answer should not call API')):
            allowed=query_recipes('金针菇汤需要哪些原料？',category='soup',max_difficulty=2)
            denied=query_recipes('金针菇汤需要哪些原料？',category='meat_dish',max_difficulty=1)
        self.assertIn(allowed['status'],('verified','verified_source'))
        self.assertEqual(allowed['answer_method'],'source_ingredient_table')
        self.assertTrue(all('/soup/' in row['source'] for row in allowed['sources']))
        self.assertEqual(denied['status'],'insufficient')
        self.assertEqual(denied['sources'],[])
        self.assertEqual(current_filters(),{})

    def test_return_guard_blocks_sources_and_block_anchors_outside_filter(self):
        wrong=next(r['source'] for r in load_chunks() if '/meat_dish/' in r['source'])
        for result in ({'sources':[{'source':wrong}]},
                       {'sources':[{'source':self.source}],'display_blocks':[{'anchors':[{'source':wrong}]}]}):
            with patch('recipe_tools._query_recipes_checked',return_value={'status':'verified','short_answer':'private-out-of-scope',**result}):
                answer=query_recipes('鸡蛋',category='soup',max_difficulty=2)
            self.assertEqual(answer['answer_method'],'metadata_filter_guard')
            self.assertNotIn('private-out-of-scope',str(answer))

    def test_generation_blocks_with_allowed_anchors_are_retained(self):
        result={'status':'verified','short_answer':'已核对','sources':[{'source':self.source}],
                'display_blocks':[{'text':'已核对','anchors':[{'source':self.source}]}]}
        with patch('recipe_tools._query_recipes_checked',return_value=result):
            answer=query_recipes('鸡蛋',category='soup',max_difficulty=2)
        self.assertEqual(answer['display_blocks'],result['display_blocks'])

    def test_empty_filter_result_never_calls_answer_chain(self):
        with patch('recipe_tools.load_chunks',return_value=[]),patch('recipe_tools._query_recipes_checked') as query:
            answer=query_recipes('鸡蛋',category='soup')
        query.assert_not_called();self.assertEqual(answer['status'],'insufficient')

    def test_filter_context_does_not_leak_between_concurrent_requests_or_after_error(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier=Barrier(2)
        def call(category):
            with filter_policy(category=category):
                barrier.wait(timeout=5)
                return current_filters()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(call,('soup','meat_dish')))
        self.assertEqual(results,[{'category':'soup'},{'category':'meat_dish'}])
        with self.assertRaises(RuntimeError),filter_policy(category='soup'):
            raise RuntimeError('injected')
        self.assertEqual(current_filters(),{})

    def test_verified_answer_without_source_is_rejected_under_filter(self):
        with patch('recipe_tools._query_recipes_checked',return_value={'status':'verified','short_answer':'unsupported'}):
            answer=query_recipes('鸡蛋',category='soup')
        self.assertEqual(answer['answer_method'],'metadata_filter_guard')

    def test_fallback_candidates_respect_filter_after_stage_failure(self):
        with filter_policy(category='soup',max_difficulty=2),patch('recipe_fact_answer.source_fact',return_value=None),patch('ingredient_recommendation.recommendation_answer',return_value=None),patch('recipe_tools._query_recipes',side_effect=StageFailure('injected')):
            answer=_query_recipes_with_cache('鸡蛋汤的差异')
        self.assertEqual(answer['status'],'degraded')
        self.assertTrue(answer['sources'])
        rows,_,_=filter_chunks(load_chunks(),'soup',2);allowed={r['source'] for r in rows}
        self.assertTrue(all(row['source'] in allowed for row in answer['sources']))

    def test_cache_keys_include_filters(self):
        with TemporaryDirectory() as directory,patch.dict(os.environ,{'RECIPE_ANSWER_CACHE':'1','RECIPE_RUNTIME_DB':str(Path(directory)/'runtime.sqlite')}):
            result={'status':'verified','short_answer':'核对','sources':[{'source':self.source}],
                    'evidence':[{'source':self.source}]}
            with patch('recipe_fact_answer.source_fact',return_value=None),patch('ingredient_recommendation.recommendation_answer',return_value=None),patch('recipe_tools._query_recipes',return_value=result) as chain:
                for filters in ({'category':'soup','max_difficulty':2},{'category':'soup','max_difficulty':1},{'category':'soup','max_difficulty':2}):
                    with filter_policy(**filters):answer=_query_recipes_with_cache('对比鸡蛋汤')
            self.assertEqual(chain.call_count,2)
            self.assertTrue(answer['cache_hit'])

    def test_subprocess_stage_receives_context_and_invalid_environment_is_rejected(self):
        with TemporaryDirectory() as directory:
            folder=Path(directory);output=folder/'search-results';output.mkdir()
            preview=output/'test.md';preview.write_text('test');preview.with_suffix('.json').write_text('{}')
            completed=subprocess.CompletedProcess([],0,'完整结果：'+str(preview)+'\n','')
            with patch('ask_test_recipes.FOLDER',folder),patch('ask_test_recipes.subprocess.run',return_value=completed) as run,filter_policy(category='soup',max_difficulty=2):
                run_stage('search_test_recipes.py','鸡蛋','search-results')
                env=run.call_args.kwargs['env']
            self.assertEqual(json.loads(env['RECIPE_SEARCH_FILTERS']),{'category':'soup','max_difficulty':2})
            child=subprocess.check_output([sys.executable,'-c',"import sys,json;sys.path.insert(0,'scripts');from search_filters import current_filters;print(json.dumps(current_filters()))"],cwd=ROOT,env=env,text=True)
            self.assertEqual(json.loads(child),{'category':'soup','max_difficulty':2})
        with patch.dict(os.environ,{'RECIPE_SEARCH_FILTERS':'{"max_difficulty":true}'}):
            with self.assertRaises(ValueError):current_filters()

    def test_classic_agent_passes_filters_to_real_local_tool(self):
        question='我有鸡蛋，推荐汤羹，难度不超过2星'
        def model(_):
            return {'role':'assistant','content':None,'tool_calls':[{'id':'call','type':'function','function':{
                'name':'query_recipes','arguments':json.dumps({'question':question,'category':'soup','max_difficulty':2},ensure_ascii=False)}}]},{}
        answer=execute(question,model_call=model)
        self.assertEqual(answer['status'],'completed')
        self.assertEqual(answer['outputs'][0]['result']['filters'],{'category':'soup','max_difficulty':2})

    def test_multi_agent_collector_keeps_filters_while_restoring_question(self):
        with patch('recipe_tools.query_recipes',return_value={'status':'insufficient','short_answer':'无候选','sources':[]}) as query:
            collector=ToolCollector('仅汤羹，难度不超过2星',lambda _:None,dispatch)
            collector.call('query_recipes',{'question':'改写','category':'soup','max_difficulty':2},worker='recipe_qa')
        self.assertEqual(query.call_args.args[0],'仅汤羹，难度不超过2星')
        self.assertEqual(query.call_args.kwargs,{'category':'soup','max_difficulty':2})

    def test_langchain_tool_schema_and_real_invocation(self):
        captured={}
        class FakeAgent:pass
        def create(**kwargs):
            captured[kwargs['name']]=kwargs
            return FakeAgent()
        with TemporaryDirectory() as directory,patch.dict(os.environ,{'RECIPE_CHECKPOINT_DB':str(Path(directory)/'checkpoint.sqlite')}),patch('langchain.agents.create_agent',side_effect=create):
            collector=ToolCollector('我有鸡蛋，推荐汤羹，难度不超过2星',lambda _:None,dispatch)
            supervisor,*_=build_agents(collector.question,collector,object())
            try:
                tool=captured['recipe_qa']['tools'][0]
                response=json.loads(tool.invoke({'question':collector.question,'category':'soup','max_difficulty':2}))
                self.assertIn(response['status'],('verified','verified_source'))
                self.assertEqual(collector.outputs[0]['result']['filters'],{'category':'soup','max_difficulty':2})
                with self.assertRaises(ValueError):tool.invoke({'question':collector.question,'max_difficulty':True})
            finally:supervisor.recipe_checkpoint_connection.close()


if __name__=='__main__':unittest.main()
