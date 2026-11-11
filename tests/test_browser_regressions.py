"""Regressions from the real Chrome session, without provider calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.task_context import advance_task
from cookbuddy.ltm.management import parse_command
from cookbuddy.meal_planning import meal_request,plan_meal
from recipe_agent import save_run
from recipe_fact_answer import source_fact
from recipe_tools import load_chunks
from run_recipe_job import export_record


class BrowserRegressions(unittest.TestCase):
    def test_preparation_timeout_keeps_request_id_elapsed_and_private_diagnostics(self):
        from job_queue import JobQueue
        import run_recipe_job
        with tempfile.TemporaryDirectory() as tmp:
            queue=JobQueue(Path(tmp)/'jobs.sqlite3')
            ident,_=queue.submit({'mode':'auto','user_id':'u','conversation_id':'s','question':'安排早餐','memory_epoch':0})
            queue.claim('test-worker')
            with patch.dict(os.environ,{'RECIPE_JOB_ID':ident}),patch('run_recipe_job.JobQueue',return_value=queue), \
                    patch('cookbuddy.pipeline.run',side_effect=TimeoutError('private-provider-body')),patch('cookbuddy.store.Store'):
                run_recipe_job.main()
            result=queue.get(ident)['result']
            self.assertEqual(result['request_id'],ident)
            self.assertGreater(result['elapsed_seconds'],0)
            self.assertIn(ident,result['service_notice'])
            self.assertIn(ident,queue.get(ident)['progress'])
            self.assertEqual(result['trace']['error_code'],'TimeoutError')
            self.assertNotIn('private-provider-body',json.dumps(result))

    def test_job_history_preserves_engine_stage_timings(self):
        from job_queue import JobQueue
        import run_recipe_job
        from unittest.mock import MagicMock
        stages=[{'phase':'understanding','elapsed_ms':1250}]
        result={'status':'completed','outputs':[],'trace':{'stages':stages,'tool_calls':0}}
        with tempfile.TemporaryDirectory() as tmp:
            queue=JobQueue(Path(tmp)/'jobs.sqlite3')
            ident,_=queue.submit({'mode':'auto','user_id':'u','conversation_id':'s','question':'你是谁','memory_epoch':0})
            queue.claim('test-worker')
            store=MagicMock()
            with patch.dict(os.environ,{'RECIPE_JOB_ID':ident}),patch('run_recipe_job.JobQueue',return_value=queue), \
                    patch('cookbuddy.pipeline.run',return_value=result),patch('run_recipe_job.export_record',return_value=None), \
                    patch('cookbuddy.store.Store',return_value=store):
                run_recipe_job.main()
            self.assertEqual(queue.get(ident)['state'],'done')
            record=store.update_record.call_args.args[-1]
            self.assertEqual(record['trace']['stages'],stages)
            self.assertEqual(record['trace']['state'],'done')
            self.assertEqual(record['trace']['tool_calls'],0)

    def test_missing_ingredients_is_output_and_recovers_old_task(self):
        task=advance_task('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。',None)
        self.assertEqual(task['state']['extra_constraints'],[])
        self.assertIn('missing_ingredients',task['state']['response_fields'])
        task['state']['extra_constraints']=['说明还缺什么']
        task['state']['response_fields']=[]
        recovered=advance_task('我有番茄和鸡蛋，推荐两道菜',task['state'])['state']
        self.assertEqual(recovered['extra_constraints'],[])
        self.assertIn('missing_ingredients',recovered['response_fields'])
        unknown=advance_task('推荐两道菜，说明热量',None)['state']
        self.assertTrue(unknown['extra_constraints'])

    def test_natural_memory_read_does_not_become_recipe_query(self):
        self.assertEqual(parse_command('我不吃什么？'),('read',''))
        self.assertEqual(parse_command('我喜欢吃什么菜？'),('read',''))
        self.assertIsNone(parse_command('我不吃什么都可以'))

    def test_meal_clarification_preserves_unknown_constraints(self):
        chunks=load_chunks()
        q='可以使用常备调料，不需要核实价格。请安排2人晚餐，30分钟，两菜一汤。'
        self.assertEqual(plan_meal(meal_request(q),chunks)['status'],'verified')
        for extra in ('预算50元','不要太咸','不可以使用常备调料'):
            question=q+extra
            self.assertEqual(plan_meal(meal_request(question),chunks)['status'],'insufficient')

    def test_newbie_recipe_is_complete_literal_procedure(self):
        q='我想做西红柿炒鸡蛋，我是新手。请根据菜谱原文说明用料和操作步骤。'
        result=source_fact(q,load_chunks())
        self.assertEqual(result['status'],'verified')
        self.assertIn('食用油',result['answer'])
        self.assertIn('鸡蛋液',result['answer'])
        self.assertTrue(result['evidence'])
        self.assertIsNone(source_fact(q+'不要放鸡蛋。',load_chunks()))

    def test_export_range_citation_and_optional_artifact_failure(self):
        result={'question':'原问题','mode':'langchain','status':'degraded','outputs':[{'tool':'search_recipes',
            'result':{'short_answer':'已核对摘录','sources':[{'title':'菜谱','section':'操作',
                'source':'data/HowToCook/a.md','line_range':'L10-L12'}]}}]}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data/processed').mkdir(parents=True)
            with patch('recipe_agent.ROOT',root):path=save_run(result)
            self.assertIn('L10-L12',path.with_suffix('.md').read_text())
        with patch('run_recipe_job.save_run',side_effect=OSError('private-path')):
            self.assertIsNone(export_record(result))
        self.assertEqual(result['record_export_error'],'OSError')
        self.assertNotIn('private-path',str(result))

    def test_http_proxy_reaches_generation_after_validation(self):
        import generate_test_answer as generator
        from check_embedding import ROOT
        from rag_dataset import FOLDER
        class ReachedProvider(BaseException):pass
        row=next(r for r in load_chunks() if r['title']=='西红柿炒鸡蛋')
        with tempfile.TemporaryDirectory(dir=FOLDER) as tmp:
            path=Path(tmp)/'input.json'
            path.write_text(json.dumps({'question':'西红柿炒鸡蛋可以搭配哪些饮料？',
                'results':[{**row,'rerank_rank':1}]}))
            settings={'base_url':'http://127.0.0.1:3425/v1','api_key':'test-only','model':'test-chat'}
            with patch.object(sys,'argv',['generate_test_answer.py',str(path)]), \
                    patch.object(generator,'require_llm',return_value=settings), \
                    patch.object(generator,'generation_with_fallback',side_effect=ReachedProvider) as call, \
                    patch.object(generator,'record_model_attempt'):
                with self.assertRaises(ReachedProvider):generator.main()
            self.assertEqual(call.call_args.args[0].full_url,settings['base_url']+'/chat/completions')


if __name__=='__main__':unittest.main()
