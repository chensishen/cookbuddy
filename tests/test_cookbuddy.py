import json
import sys
import uuid
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.router import combine_scores, IntentRouter
from cookbuddy.memory import validated_extractions, MemoryService
from cookbuddy.models import cosine
from cookbuddy.store import valid_id
from src.nlu.router import understand
from job_queue import JobQueue
import tempfile


class RouterTests(unittest.TestCase):
    def setUp(self):self.registry={'recipe_qa':{},'recipe_images':{},'unclear':{}}
    def test_agreement_accepts(self):
        r=combine_scores('recipe_images',.98,{'recipe_images':.88,'recipe_qa':.3},self.registry)
        self.assertEqual(r['intent'],'recipe_images');self.assertFalse(r['requires_clarification'])
    def test_low_confidence_requests_clarification(self):
        r=combine_scores('recipe_qa',.45,{'recipe_qa':.4},self.registry)
        self.assertEqual(r['intent'],'unclear')
    def test_disagreement_requires_strong_llm_evidence(self):
        r=combine_scores('recipe_qa',.8,{'recipe_images':.99,'recipe_qa':.35},self.registry)
        self.assertTrue(r['requires_clarification'])
    def test_unknown_id_nan_and_bool_rejected(self):
        for ident,score in [('fake',.9),('recipe_qa',float('nan')),('recipe_qa',True),('recipe_qa',1.1)]:
            with self.assertRaises(ValueError):combine_scores(ident,score,{},self.registry)
    def test_embedding_failure_explicitly_degrades(self):
        store=type('Registry',(),{'registry':lambda self:[{'id':'recipe_qa','version':2,'target':'rag','description':'问答','examples':['怎么做']},{'id':'unclear','version':1,'target':'clarify','description':'歧义','examples':['那个']} ]})()
        with patch('cookbuddy.router.local_recipe_intent',return_value=None),patch('cookbuddy.router.json_model',return_value={'intent':'recipe_qa','confidence':.98,'entities':{'title':'蒸水蛋'}}),patch('cookbuddy.router.embed',side_effect=TimeoutError):
            r=IntentRouter(store).route('蒸水蛋怎么做')
        self.assertEqual(r['target'],'rag');self.assertTrue(r['degraded']);self.assertFalse(r['confidence_calibrated'])
    def test_registry_target_dynamically_used(self):
        store=type('Registry',(),{'registry':lambda self:[{'id':'custom_oven','version':3,'target':'agent','description':'烤箱','examples':['烤箱怎么用']},{'id':'unclear','version':1,'target':'clarify','description':'歧义','examples':['那个']} ]})()
        with patch('cookbuddy.router.json_model',return_value={'intent':'custom_oven','confidence':1,'entities':{}}),patch('cookbuddy.router.embed',return_value=[1,0]),patch('cookbuddy.router.RuntimeStore') as cache:
            cache.return_value.get.return_value=[1,0]
            r=IntentRouter(store).route('烤箱怎么用')
        self.assertEqual(r['target'],'agent');self.assertEqual(r['registry_versions']['custom_oven'],3)
    def test_section_entities_grounded_in_real_heading_names(self):
        from recipe_tools import resolve_title,load_chunks
        title,source=resolve_title('基础牛奶面包',load_chunks())
        context={'slots':{'selected_dish':{'dish_name':title,'source_path':source}}}
        for q,section in [('只看操作章节','操作'),('只看酵头制作章节','酵头的制作')]:
            plan=understand(q,context,use_model=False)
            self.assertEqual(plan['slots']['dish'],title)
            self.assertEqual(plan['slots']['section'],section)
        self.assertEqual(understand('查看基础牛奶面包原文',context,use_model=False)['slots']['section'],'')
    def test_cosine_space_checks(self):
        self.assertEqual(cosine([1,0],[1,0]),1)
        for x,y in [([0,0],[1,0]),([1],[1,2])]:
            with self.assertRaises(ValueError):cosine(x,y)

    def test_job_is_not_claimable_until_user_message_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=JobQueue(Path(tmp)/'jobs.sqlite')
            ident,_=q.submit({'question':'你好'},ready=False)
            self.assertIsNone(q.claim('worker'))
            q.release(ident)
            self.assertEqual(q.claim('worker')['id'],ident)


class MemoryTests(unittest.TestCase):
    def row(self,**kw):
        return {'kind':'profile','key':'taste','content':'模型生成的额外内容','quote':'我喜欢清淡口味','source':'','confidence':.95,'importance':.8,**kw}
    def test_profiles_store_literal_evidence_not_model_hallucination(self):
        r=validated_extractions({'memories':[self.row()]},'我喜欢清淡口味，请看原文',[])
        self.assertEqual(r[0]['content'],'我喜欢清淡口味')
    def test_unsupported_question_hypothetical_and_invented_profile_rejected(self):
        for row,question in [(self.row(),'只问做法'),(self.row(quote='如果我喜欢清淡'),'如果我喜欢清淡'),(self.row(quote='我喜欢清淡吗？'),'我喜欢清淡吗？'),(self.row(key='password'),'我喜欢清淡口味')]:
            self.assertEqual(validated_extractions({'memories':[row]},question,[]),[])
    def test_knowledge_requires_current_exact_source_quote(self):
        source='data/HowToCook/dishes/breakfast/蒸水蛋.md'
        row=self.row(kind='knowledge',quote='蒸 4 分钟',source=source)
        evidence=[{'source':source,'quote':'转最小的火继续蒸 4 分钟','start_line':10,'end_line':12}]
        result=validated_extractions({'memories':[row]},'怎么做',evidence)
        self.assertEqual(result[0]['evidence']['origin'],'verified_recipe_source')
        self.assertEqual(validated_extractions({'memories':[dict(row,source='fake.md')]},'怎么做',evidence),[])
    def test_low_confidence_not_persisted(self):
        self.assertEqual(validated_extractions({'memories':[self.row(confidence=.5)]},'我喜欢清淡口味',[]),[])
    def test_vector_candidates_must_match_pg_revision_and_expiry(self):
        uid=uuid.uuid4().hex
        class FakeStore:
            def active(self,*a):return []
            def memory(self,u,ident):
                return {'id':ident,'version':2,'status':'active','expires_at':None,'kind':'task','content':'历史','evidence':{}}
        class FakeVectors:
            def search(self,*a):return [{'id':'old','version':1}]
        with patch('cookbuddy.memory.embed',return_value=[1]),patch('cookbuddy.memory.rerank') as rr:
            data=MemoryService(FakeStore(),FakeVectors()).retrieve(uid,'什么菜')
        self.assertEqual(data['items'],[]);rr.assert_not_called()
    def test_uuid_filter_rejects_injection(self):
        self.assertFalse(valid_id('" or user_id != "'))
        self.assertTrue(valid_id(uuid.uuid4().hex))

if __name__=='__main__':unittest.main()


class RouterLatencyTests(unittest.TestCase):
    def test_semantic_phase_has_shared_budget_and_versioned_day_cache(self):
        from service_runtime import remaining
        rows=[{'id':'recipe_qa','description':'问答','examples':['怎么做'],'target':'rag','version':1},
              {'id':'unclear','description':'澄清','examples':['这个'],'target':'clarify','version':1}]
        class Store:
            def registry(self):return rows
        seen=[]
        def embedding(*args):seen.append(remaining());return [1.,0.]
        from unittest.mock import MagicMock
        cache=MagicMock();cache.get.return_value=None
        with patch('cookbuddy.router.json_model',return_value={'intent':'recipe_qa','confidence':.99,'entities':{}}),patch('cookbuddy.router.embed',side_effect=embedding),patch('cookbuddy.router.embedding_spec',return_value=({},'space')),patch('cookbuddy.router.RuntimeStore',return_value=cache):
            result=IntentRouter(Store()).route('怎么做')
        self.assertEqual(result['target'],'rag')
        self.assertTrue(all(0<s<=6 for s in seen))
        self.assertEqual(cache.put.call_count,2)
        self.assertTrue(all(c.kwargs['ttl']==86400 for c in cache.put.call_args_list))
        self.assertNotEqual(cache.put.call_args_list[0].args[0],cache.put.call_args_list[1].args[0])
