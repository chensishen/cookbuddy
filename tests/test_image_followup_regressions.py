"""A chapter-image follow-up must stay bound to the preceding recipe."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_tools import load_chunks,resolve_title
from cookbuddy.session import SessionState
from src.nlu.router import understand


class ImageFollowupTests(unittest.TestCase):
    def setUp(self):
        cache=patch('service_runtime.RuntimeStore');self.addCleanup(cache.stop)
        cache.start().return_value.get.return_value=None
        self.title,self.source=resolve_title('基础牛奶面包',load_chunks())
        self.context={'slots':{'selected_dish':{'dish_name':self.title,'source_path':self.source}}}

    def test_fallback_keeps_selected_recipe_for_chapter_images(self):
        plan=understand('只看酵头制作的图片。',self.context,use_model=False)
        self.assertNotIn('unmatched_dish',plan)
        self.assertEqual(plan['slots']['dish'],self.title)
        self.assertEqual(plan['slots']['request_kind'],'images')
        self.assertEqual(plan['slots']['section'],'酵头的制作')
        self.assertEqual(plan['answer_mode'],'grounded_recipe')

    def test_model_cannot_promote_image_instruction_to_dish_name(self):
        for intent in ('recipe_lookup','recipe_section'):
            with self.subTest(intent=intent):
                plan=understand('只看酵头制作的图片。',self.context,model_call=lambda *a:{
                    'intent':intent,'confidence':.95,'slots':{'dish':'只看酵头制作的图片',
                    'section':'酵头制作','request_kind':'images'}})
                self.assertNotIn('unmatched_dish',plan)
                self.assertEqual(plan['slots']['dish'],self.title)
                self.assertEqual(plan['slots']['section'],'酵头的制作')
                self.assertEqual(plan['slots']['request_kind'],'images')

    def test_explicit_new_recipe_is_not_rebound_to_old_recipe(self):
        plan=understand('查看西红柿炒鸡蛋的图片。',self.context,use_model=False)
        self.assertEqual(plan['slots']['dish'],'西红柿炒鸡蛋')

    def test_unknown_dish_or_missing_context_does_not_guess_bread(self):
        for question,context in [('查看火星蓝晶炖月光的图片。',self.context),('只看酵头制作的图片。',{})]:
            with self.subTest(question=question):
                plan=understand(question,context,use_model=False)
                self.assertNotEqual(plan['slots']['dish'],self.title)

    def test_complete_source_then_chapter_images_uses_only_verified_chapter(self):
        import langchain_service_worker as worker
        from src.agent.config import load_config
        from src.agent.engine import ChatEngine
        session=SessionState('u','s');store=MagicMock()
        store.session_context.return_value=session;store.task_context.return_value=None
        cfg=load_config()
        def runtime(*args):
            return SimpleNamespace(enabled=True,level=5,session=session,previous_task=None,hard=[],events=[],
                confirmation=None,management=None,notice='',candidates=[],apply_soft=lambda *a:None,context_items=lambda **kw:[])
        def offline(q,c,**kw):return understand(q,c,use_model=False)
        async def check():
            for i,q in enumerate(['查看基础牛奶面包的完整原文和图片。','只看酵头制作的图片。']):
                p=worker.prepare(dict(user_id='u',session_id='s',request_id='j'+str(i),question=q))
                async def service(op,args,timeout):
                    return worker.execute(dict(operation=op,arguments=args,user_id='u',session_id='s',
                        request_id='j'+str(i),hard=[],config=cfg.model_dump()))
                async def guard(compact,full):return compact,full
                async def emit(*args):pass
                run=SimpleNamespace(question=q,prepared=p,cfg=cfg,context=SimpleNamespace(remaining=lambda n:n),
                    tool_calls=0,evidence={},degraded=False,stages=[],service=service,guard=guard,emit=emit)
                self.assertTrue(await ChatEngine._direct(None,run))
                outputs=[{'tool':r['name'],'result':r['full']} for r in run.evidence.values()]
                if i==1:
                    self.assertEqual([r['tool'] for r in outputs],['find_recipe_images'])
                    result=outputs[0]['result']
                    self.assertEqual(result['answer_layout'],'images_only')
                    self.assertTrue(result['images'])
                    self.assertTrue(all(r['source']==self.source and '酵头的制作' in r['section'] for r in result['images']))
                worker.execute(dict(operation='persist',arguments={'question':q,'prepared':p,
                    'result':{'status':'completed','outputs':outputs},'text':''},user_id='u',session_id='s',
                    request_id='j'+str(i),hard=[],config=cfg.model_dump()))
        with patch('cookbuddy.store.Store',return_value=store),patch('cookbuddy.ltm.runtime.MemoryRuntime',side_effect=runtime),\
                patch('src.nlu.router.understand',side_effect=offline):
            asyncio.run(check())
