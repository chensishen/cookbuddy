"""多 Agent 新路径继续遵守完整问题、来源工具和失败隔离。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from langchain_multi_agent import ToolCollector, execute_multi_agent


class MultiAgentBoundaryTests(unittest.TestCase):
    def test_no_source_tool_is_execution_failure_not_knowledge_refusal(self):
        class FakeSupervisor:
            def invoke(self,*args,**kwargs):return {'messages':[]}
        with patch('langchain_multi_agent.build_agents',return_value=(FakeSupervisor(),[],[],lambda *_:None)):
            result=execute_multi_agent('不存在的菜怎么做？',model=object())
        self.assertEqual(result['status'],'agent_unavailable')
        self.assertEqual(result['outputs'],[])
        self.assertIn('尚未完成知识库查询',result['error'])
        self.assertEqual(result['stop_reason'],'agent_unavailable')

    def test_qa_cannot_shorten_original_inventory_question(self):
        calls = []
        def dispatch(name, arguments, progress, original_question=None):
            calls.append((name, arguments, original_question))
            return {'status':'verified','short_answer':'已核对','sources':[]}
        question = '我有黄瓜和大蒜，没有酱油，能做凉拌黄瓜吗？'
        collector = ToolCollector(question, lambda _: None, dispatch)
        collector.call('query_recipes', {'question':'推荐黄瓜菜'})
        self.assertEqual(calls[0][1]['question'], question)
        self.assertEqual(calls[0][2], question)
        self.assertIn('RAG恢复用户完整原始问题', collector.steps[0]['parameter_adjustments'])

    def test_source_worker_cannot_add_unrequested_section(self):
        calls = []
        def dispatch(name, arguments, progress, original_question=None):
            calls.append((name, arguments))
            return {'status':'verified_source','short_answer':'找到原文','sources':[]}
        collector = ToolCollector('请查看蒸水蛋原文', lambda _: None, dispatch)
        collector.call('read_recipe', {'title':'蒸水蛋','section':'成品'})
        self.assertEqual(calls, [('read_recipe', {'title':'蒸水蛋','section':''})])

    def test_image_worker_restores_recipe_name_and_requested_chapter(self):
        calls = []
        def dispatch(name, arguments, progress, original_question=None):
            calls.append((name, arguments))
            return {'status':'verified_source','short_answer':'找到图片','sources':[]}
        collector = ToolCollector('请给我看基础牛奶面包酵头的图片', lambda _: None, dispatch)
        collector.call('find_recipe_images', {'title':'基础牛奶面包酵头','section':'图片'})
        self.assertEqual(calls, [('find_recipe_images', {'title':'基础牛奶面包','section':'酵头'})])

    def test_source_worker_uses_explicit_chapter_for_original_text(self):
        calls = []
        def dispatch(name, arguments, progress, original_question=None):
            calls.append((name, arguments))
            return {'status':'verified_source','short_answer':'找到原文','sources':[]}
        collector = ToolCollector('请同时查看基础牛奶面包酵头的原文和图片', lambda _: None, dispatch)
        collector.call('read_recipe', {'title':'基础牛奶面包酵头','section':''})
        self.assertEqual(calls, [('read_recipe', {'title':'基础牛奶面包','section':'酵头'})])

    def test_source_worker_cannot_execute_factual_rag(self):
        calls = []
        def dispatch(name, arguments, progress, original_question=None):
            calls.append(name)
            return {'status':'verified','short_answer':'已核对','sources':[]}
        collector = ToolCollector('酸辣土豆丝原文需要哪些调料？', lambda _: None, dispatch)
        response = collector.call('read_recipe', {'title':'酸辣土豆丝','section':''}, worker='source_media')
        self.assertIn('wrong_specialist', response)
        self.assertEqual(calls, [])

    def test_same_tool_call_is_executed_once(self):
        calls = []
        def dispatch(name, arguments, progress, original_question=None):
            calls.append(name)
            return {'status':'verified_source','short_answer':'已核对','sources':[]}
        collector = ToolCollector('看蒸水蛋原文', lambda _: None, dispatch)
        collector.call('read_recipe', {'title':'蒸水蛋','section':''})
        repeated = collector.call('read_recipe', {'title':'蒸水蛋','section':''})
        self.assertEqual(calls, ['read_recipe'])
        self.assertIn('tool_limit', repeated)

    def test_run_exposes_only_collected_tool_result(self):
        def dispatch(name, arguments, progress, original_question=None):
            return {'status':'verified_source','short_answer':'原文已核对','sources':[]}
        class FakeSupervisor:
            def invoke(self, *args, **kwargs):
                collector.call('read_recipe', {'title':'蒸水蛋','section':''})
                return {'messages':[{'content':'未经核对的自由结论'}]}
        def build(question, created, model):
            nonlocal collector
            collector = created
            return FakeSupervisor(), ['source_media'], [], lambda *_: None
        collector = None
        with patch('langchain_multi_agent.build_agents', side_effect=build):
            result = execute_multi_agent('看蒸水蛋原文', model=object(), dispatch_fn=dispatch)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('未经核对', str(result))
        self.assertEqual(result['outputs'][0]['result']['short_answer'], '原文已核对')


if __name__ == '__main__':
    unittest.main()
