import json
from pathlib import Path
import sys
from threading import Thread
from tempfile import TemporaryDirectory
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from recipe_agent import execute,AgentAPIError
from recipe_tools import dispatch,read_recipe,find_recipe_images,query_recipes
from serve_recipe_app import RecipeServer


def tool_message(name,args,ident='call_1'):
    return {'role':'assistant','content':None,'tool_calls':[{'id':ident,'type':'function',
          'function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]},{'model':'fake_for_test'}


class RecipeToolsTests(unittest.TestCase):
    def test_original_recipe_quotes_match_actual_source(self):
        result=read_recipe('蒸水蛋','操作')
        self.assertEqual(result['status'],'verified_source')
        for row in result['evidence']:
            lines=(ROOT/row['source']).read_text().splitlines()
            self.assertEqual(row['quote'],'\n'.join(lines[row['start_line']-1:row['end_line']]))

    def test_images_include_surrounding_steps_with_source_line_alignment(self):
        result=find_recipe_images('基础牛奶面包','酵头')
        for image in result['images']:
            chunks=[e for e in result['evidence'] if e['source']==image['source'] and e['start_line']<=image['source_line']<=e['end_line']]
            self.assertTrue(chunks)
            chunk=chunks[0]
            self.assertEqual(chunk['quote'].splitlines()[image['source_line']-chunk['start_line']],image['source_quote'])
        self.assertTrue(any(len(e['quote'].splitlines())>1 for e in result['evidence']))

    def test_recipe_original_includes_its_verified_section_images(self):
        result=read_recipe('基础牛奶面包','酵头')
        self.assertEqual(len(result['images']),3)
        self.assertTrue(all(any(e['source']==image['source'] and e['start_line']<=image['source_line']<=e['end_line'] for e in result['evidence']) for image in result['images']))

    def test_ambiguity_and_unknown_title_do_not_guess(self):
        self.assertEqual(read_recipe('宫保鸡丁和凉拌黄瓜')['status'],'insufficient')
        self.assertEqual(read_recipe('../.env')['status'],'insufficient')

    def test_image_lookup_preserves_source_and_excludes_draft_facts(self):
        result=find_recipe_images('基础牛奶面包','酵头')
        self.assertEqual(len(result['images']),3)
        self.assertTrue(all(row['source_line'] in (84,88,92) for row in result['images']))
        self.assertTrue(all('visual_description' not in row and 'ocr' not in row for row in result['images']))

    def test_reviewed_image_words_find_images_without_becoming_recipe_facts(self):
        result=find_recipe_images('戚风蛋糕','玉米油')
        self.assertEqual(result['status'],'verified_source')
        self.assertEqual(len(result['images']),1)
        image=result['images'][0]
        self.assertTrue(image['image_path'].endswith('IMG_1516.jpg'))
        self.assertEqual(image['reviewed_image_text'][0]['text'],'玉米油')
        self.assertEqual(image['reviewed_image_text'][0]['usage'],'image_lookup_only')
        self.assertEqual(len(result['evidence']),1)
        self.assertNotIn('玉米油',result['evidence'][0]['quote'])
        self.assertEqual(find_recipe_images('戚风蛋糕','150')['status'],'insufficient')
        self.assertEqual(find_recipe_images('基础牛奶面包','硅油纸')['status'],'insufficient')

    def test_reviewed_text_must_match_indexed_image_hash(self):
        from reviewed_media_text import load_reviewed_texts
        changed=load_reviewed_texts()
        changed['data/HowToCook/dishes/dessert/戚风蛋糕/IMG_1516.jpg']['image_sha256']='0'*64
        with patch('recipe_tools.load_reviewed_texts',return_value=changed):
            with self.assertRaises(ValueError):find_recipe_images('戚风蛋糕','玉米油')

    def test_unknown_section_and_missing_images_are_explicit(self):
        self.assertEqual(read_recipe('蒸水蛋','虚构章节')['status'],'insufficient')
        self.assertEqual(find_recipe_images('蒸水蛋')['status'],'insufficient')

    def test_remote_image_is_visible_as_unverified_link_and_respects_filter(self):
        result=find_recipe_images('完美水煮蛋')
        self.assertEqual(result['status'],'insufficient')
        self.assertEqual(result['images'],[])
        self.assertEqual(len(result['remote_links']),1)
        self.assertEqual(result['remote_links'][0]['status'],'not_downloaded')
        self.assertIn('内容未核验',result['short_answer'])
        self.assertEqual(find_recipe_images('完美水煮蛋','不存在的图注')['remote_links'],[])

    def test_tool_whitelist_and_arguments_are_checked(self):
        for name,args in [('exec',{'question':'hi'}),('read_recipe',{'title':'蒸水蛋','path':'.env'}),
                          ('read_recipe',{'title':True}),('query_recipes',{})]:
            with self.assertRaises(ValueError):dispatch(name,args)

    def test_rag_adapter_preserves_citation_ids_and_lineage(self):
        from rag_dataset import FOLDER
        search=FOLDER/'search-results/fixture.json';rerank=FOLDER/'rerank-results/fixture.json';answer=FOLDER/'answer-results/fixture.json'
        record={'question':'测试','source_rerank':str(rerank.relative_to(ROOT)),'status':'verified',
                'short_answer':'依据[7]','evidence':[{'reference_id':7,'quote':'原文','start_line':11,'end_line':11}],
                'references':[{'reference_id':7,'title':'蒸水蛋','section':'原料','source':'recipe.md','source_start_line':9}],
                'cited_reference_ids':[7],'answer':'完整回答','answer_method':'model_generation','validation':'checked','generation_model':'Qwen/Qwen3-8B'}
        stages=[(search,{'question':'测试','model':'embedding'}),
                (rerank,{'question':'测试','source_search':str(search.relative_to(ROOT)),'rerank_model':'reranker'}),
                (answer,record)]
        with patch('recipe_tools.run_stage',side_effect=stages):result=query_recipes('测试')
        self.assertEqual(result['sources'][0]['reference_id'],7)
        self.assertEqual(result['evidence'][0]['source'],'recipe.md')
        self.assertEqual(result['evidence'][0]['reference_id'],7)
        self.assertEqual(result['models']['generation'],'Qwen/Qwen3-8B')

    def test_rag_stops_when_stage_question_does_not_match(self):
        from rag_dataset import FOLDER
        with patch('recipe_tools.run_stage',return_value=(FOLDER/'search-results/fixture.json',{'question':'另一个问题'})) as stage:
            with self.assertRaises(ValueError):query_recipes('测试')
        self.assertEqual(stage.call_count,1)


class AgentTests(unittest.TestCase):
    def test_real_local_tool_and_model_conversation_linkage(self):
        messages=[]
        def model(history):
            messages.append(history.copy())
            if len(messages)==1:return tool_message('find_recipe_images',{'title':'基础牛奶面包','section':'酵头'})
            return {'content':'没有依据的模型自由结论：温度100度'},{}
        result=execute('看基础牛奶面包酵头图片',model_call=model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['outputs'][0]['result']['images']),3)
        self.assertEqual(messages[1][-1]['role'],'tool')
        self.assertEqual(messages[1][-1]['tool_call_id'],'call_1')
        self.assertNotIn('温度100度',json.dumps(result,ensure_ascii=False))

    def test_original_inventory_question_cannot_be_rewritten(self):
        model=iter([tool_message('query_recipes',{'question':'推荐鸡蛋'}),({'content':'完成'}, {})])
        with patch('recipe_agent.dispatch',return_value={'status':'verified','short_answer':'已核对','sources':[]}) as tool:
            execute('我有黄瓜和大蒜，没有酱油',model_call=lambda _:next(model))
        self.assertEqual(tool.call_args.args[1]['question'],'我有黄瓜和大蒜，没有酱油')

    def test_api_failure_is_explicit_without_fake_offline_agent(self):
        def failing(_):raise AgentAPIError('HTTP 429')
        result=execute('看蒸水蛋原文',model_call=failing)
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(result['outputs'],[])
        self.assertEqual(result['model_calls'],[])

    def test_completed_tool_survives_later_api_failure(self):
        calls=iter([tool_message('read_recipe',{'title':'蒸水蛋'}),AgentAPIError('HTTP 429')])
        def model(_):
            response=next(calls)
            if isinstance(response,Exception):raise response
            return response
        result=execute('看蒸水蛋原文',model_call=model)
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['outputs'][0]['result']['evidence'])

    def test_unknown_tool_never_executes_code(self):
        calls=iter([tool_message('os.system',{'command':'anything'}),({'content':''},{})])
        result=execute('测试',model_call=lambda _:next(calls))
        self.assertEqual(result['status'],'partial_failure')
        self.assertEqual(result['outputs'][0]['result']['status'],'tool_failed')

    def test_repeated_calls_execute_only_once(self):
        result=execute('看原文',model_call=lambda _:tool_message('read_recipe',{'title':'蒸水蛋'}))
        self.assertEqual(len(result['outputs']),1)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(len(result['model_calls']),3)

    def test_no_tool_means_no_unsupported_answer(self):
        result=execute('配方是什么',model_call=lambda _:({'content':'凭常识回答'},{}))
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(result['outputs'],[])
        self.assertEqual(result['stop_reason'],'no_tool_selected')
        self.assertIn('尚未完成知识库查询',result['error'])
        self.assertNotIn('凭常识回答',str(result))

    def test_direct_mode_never_calls_planning_model(self):
        with patch('recipe_agent.call_model') as model:
            result=execute('看蒸水蛋原文',mode='recipe',title='蒸水蛋')
        model.assert_not_called();self.assertIsNone(result['agent_model'])

    def test_direct_tool_failure_is_recordable(self):
        with patch('recipe_agent.dispatch',side_effect=ValueError('重排步骤未完成')):
            result=execute('测试问题',mode='rag')
        self.assertEqual(result['status'],'partial_failure')
        self.assertIn('重排',result['outputs'][0]['result']['short_answer'])


class LocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=TemporaryDirectory();cls.env=patch.dict(os.environ,{'RECIPE_JOBS_DB':str(Path(cls.temp.name)/'jobs.sqlite')});cls.env.start()
        cls.server=RecipeServer(('127.0.0.1',0));cls.thread=Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.env.stop();cls.temp.cleanup()

    def test_status_and_whitelisted_image_served(self):
        with urlopen(self.base+'/api/status') as response:status=json.load(response)
        self.assertEqual(status['recipe_count'],372)
        self.assertEqual(status['image_count'],354)
        from urllib.parse import quote
        with urlopen(self.base+'/image?path='+quote('data/HowToCook/dishes/staple/基础牛奶面包/4-1酵头1.jpg')) as response:
            self.assertEqual(response.headers['Content-Type'],'image/jpeg')
            self.assertGreater(len(response.read()),100)
        with urlopen(self.base+'/source?path='+quote('data/HowToCook/dishes/breakfast/蒸水蛋.md')) as response:
            text=response.read().decode()
            self.assertIn('1: # 蒸水蛋的做法',text)
            self.assertIn('24: ',text)

    def test_config_and_path_escape_cannot_be_served(self):
        for endpoint in ('/source?path=.env','/image?path=../.env'):
            with self.assertRaises(HTTPError) as error:urlopen(self.base+endpoint)
            self.assertEqual(error.exception.code,404)

    def test_cross_origin_submission_and_wrong_host_rejected(self):
        request=Request(self.base+'/api/ask',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://outside.invalid'})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,403)
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/',headers={'Host':'outside.invalid'}))
        self.assertEqual(error.exception.code,403)

    def test_invalid_mode_rejected_before_any_job(self):
        request=Request(self.base+'/api/ask',data=json.dumps({'question':'测试','mode':'shell'}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:urlopen(request)
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.server.queue.metrics()['states'],{})


if __name__=='__main__':unittest.main()
