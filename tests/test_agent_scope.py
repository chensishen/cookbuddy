import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_agent import execute,normalize_tool_request


def decision(name,args):
    return {'role':'assistant','content':None,'tool_calls':[{'id':'test_call','type':'function',
            'function':{'name':name,'arguments':json.dumps(args,ensure_ascii=False)}}]}, {'model':'fake_for_test'}


class ScopeTests(unittest.TestCase):
    def test_literal_preparation_needs_only_one_real_planning_decision(self):
        question='做响油鳝丝之前，需要准备哪些食材和器具？'
        calls=[]
        def model(messages):
            calls.append(messages)
            if len(calls)>1:raise AssertionError('不应复查已完整核对的原文字面答案')
            return decision('query_recipes',{'question':question})
        result=execute(question,model_call=model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(calls),1)
        self.assertEqual(result['outputs'][0]['result']['answer_method'],'source_ingredient_table')

    def test_historical_before_still_allows_additional_tools(self):
        from recipe_agent import query_scope_complete
        outputs=[{'tool':'query_recipes','result':{'status':'verified','short_answer':'已核对'}}]
        self.assertFalse(query_scope_complete('之前说过的响油鳝丝要哪些食材？',outputs))

    def test_planner_failure_returns_literal_source_as_explicit_degradation(self):
        from recipe_agent import AgentAPIError
        def failing(_):raise AgentAPIError('HTTP 429')
        result=execute('做响油鳝丝之前，需要准备哪些食材和器具？',model_call=failing)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['stop_reason'],'agent_unavailable')
        self.assertEqual(result['fallback'],'literal_source_after_planning_failure')
        self.assertEqual(result['model_calls'],[])
        answer=result['outputs'][0]['result']
        self.assertEqual(answer['status'],'degraded')
        self.assertEqual(answer['degraded_from'],'verified')
        self.assertIn('规划服务暂时不可用',answer['short_answer'])
        self.assertTrue(answer['evidence'])

    def test_literal_fallback_still_respects_hard_source_constraint(self):
        from recipe_agent import AgentAPIError
        from cookbuddy.ltm.guard import policy
        def failing(_):raise AgentAPIError('HTTP 429')
        with policy([{'dimension':'allergy','value':'大蒜','polarity':'avoid','strength':'hard'}]):
            result=execute('做响油鳝丝之前，需要准备哪些食材和器具？',model_call=failing)
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(result['outputs'],[])

    def test_completed_query_does_not_need_another_planning_request(self):
        with patch('recipe_agent.dispatch',return_value={'status':'verified','short_answer':'已核对','sources':[]}):
            calls=[]
            def model(messages):
                calls.append(messages.copy())
                if len(calls)>1:raise AssertionError('完整问答不应再次请求规划')
                return decision('query_recipes',{'question':'番茄炒蛋要哪些原料？'})
            result=execute('番茄炒蛋要哪些原料？',model_call=model)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['stop_reason'],'scope_complete')
        self.assertEqual(len(result['model_calls']),1)

    def test_query_does_not_skip_explicit_memory_or_image_request(self):
        from recipe_agent import query_scope_complete
        outputs=[{'tool':'query_recipes','result':{'status':'verified','short_answer':'已核对'}}]
        for question in ('番茄炒蛋怎么做并给我图片','推荐菜并记住我喜欢番茄','查看原文'):
            self.assertFalse(query_scope_complete(question,outputs))
        outputs[0]['result']['status']='degraded'
        self.assertFalse(query_scope_complete('番茄炒蛋怎么做',outputs))

    def test_unrequested_filter_removed_and_audited(self):
        calls=iter([decision('find_recipe_images',{'title':'宫保鸡丁','section':'成品'}),({'content':'完成'},{})])
        result=execute('查看宫保鸡丁的原图。',model_call=lambda _:next(calls))
        output=result['outputs'][0]
        self.assertEqual(output['requested_arguments']['section'],'成品')
        self.assertEqual(output['arguments']['section'],'')
        self.assertEqual(output['result']['total_images'],1)
        self.assertTrue(output['parameter_adjustments'])

    def test_fact_question_not_nonexistent_chapter(self):
        calls=iter([decision('read_recipe',{'title':'蒸水蛋','section':'成品'}),({'content':'完成'},{})])
        with patch('recipe_agent.dispatch',return_value={'status':'insufficient','short_answer':'原文未给中心温度','sources':[]}) as tool:
            result=execute('这份蒸水蛋原文明确写了成品中心温度是多少吗？',model_call=lambda _:next(calls))
        self.assertEqual(tool.call_args.args[0],'query_recipes')
        self.assertIn('中心温度',tool.call_args.args[1]['question'])
        self.assertEqual(result['outputs'][0]['requested_tool'],'read_recipe')
        self.assertEqual(result['status'],'insufficient')

    def test_explicit_image_keyword_preserved(self):
        name,args,changes=normalize_tool_request('find_recipe_images',{'title':'基础牛奶面包','section':'酵头'},'查看基础牛奶面包酵头图片')
        self.assertEqual(args['section'],'酵头');self.assertEqual(changes,[])

    def test_explicit_read_section_preserved(self):
        name,args,changes=normalize_tool_request('read_recipe',{'title':'蒸水蛋','section':'操作'},'只看蒸水蛋操作章节原文')
        self.assertEqual(name,'read_recipe');self.assertEqual(args['section'],'操作')
        self.assertEqual(changes,[])

    def test_illegal_arguments_not_hidden_by_rerouting(self):
        original={'title':'蒸水蛋','section':True,'path':'.env'}
        name,args,changes=normalize_tool_request('read_recipe',original,'蒸水蛋中心温度？')
        self.assertEqual((name,args,changes),('read_recipe',original,[]))
        self.assertEqual(normalize_tool_request('query_recipes',{'question':True},'原问题'),
                         ('query_recipes',{'question':True},[]))

    def test_normalized_repeat_deduplicated(self):
        index=[0]
        def model(_):
            index[0]+=1
            return decision('find_recipe_images',{'title':'宫保鸡丁','section':'成品' if index[0]==1 else ''})
        result=execute('查看宫保鸡丁原图',model_call=model)
        self.assertEqual(len(result['outputs']),1)
        self.assertEqual(result['status'],'partial')

    def test_direct_mode_respects_explicit_filter(self):
        result=execute('查询图片',mode='images',title='宫保鸡丁',section='成品')
        self.assertEqual(result['outputs'][0]['arguments']['section'],'成品')
        self.assertEqual(result['status'],'insufficient')

    def test_fact_does_not_route_to_photos(self):
        name,args,changes=normalize_tool_request('find_recipe_images',{'title':'蒸水蛋','section':'成品'},'蒸水蛋成品中心温度是多少？')
        self.assertEqual(name,'query_recipes');self.assertIn('中心温度',args['question'])

    def test_explicit_local_intent_does_not_become_factual_rag(self):
        name,args,_=normalize_tool_request('read_recipe',{'title':'宫保鸡丁','section':'成品'},'查看宫保鸡丁原图')
        self.assertEqual(name,'find_recipe_images');self.assertEqual(args['section'],'')
        name,args,_=normalize_tool_request('find_recipe_images',{'title':'蒸水蛋','section':'操作'},'看蒸水蛋操作原文')
        self.assertEqual(name,'read_recipe');self.assertEqual(args['section'],'操作')

    def test_feedback_reports_executed_tool(self):
        histories=[]
        def model(messages):
            histories.append(messages.copy())
            if len(histories)==1:return decision('find_recipe_images',{'title':'宫保鸡丁','section':'成品'})
            return {'content':'完成'},{}
        execute('查看宫保鸡丁原图',model_call=model)
        feedback=json.loads(histories[1][-1]['content'])
        self.assertEqual(feedback['executed_tool'],'find_recipe_images')
        self.assertTrue(feedback['parameter_adjustments'])


if __name__=='__main__':unittest.main()
