import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from conversation_memory import contextual_question, validate_memory
from job_queue import JobQueue

class ConversationTests(unittest.TestCase):
    def test_context_uses_preferences_and_prior_dish_as_data(self):
        captured=[]
        def call(req,**kwargs):
            captured.append(json.loads(req.data))
            return {'choices':[{'message':{'content':'{"question":"蒸水蛋还需要什么调料？偏好清淡口味。"}'}}]}
        with patch('conversation_memory.require_llm',return_value={'base_url':'https://api.deepseek.com/v1','api_key':'dummy','model':'deepseek-flash'}):
            result=contextual_question('它还需要什么调料？',[{'role':'user','content':'蒸水蛋怎么做？'}],['清淡口味'],call)
        self.assertIn('蒸水蛋',result)
        data=json.loads(captured[0]['messages'][1]['content'])
        self.assertEqual(data['current_question'],'它还需要什么调料？')
        self.assertEqual(data['saved_preferences'],['清淡口味'])
        self.assertEqual(captured[0]['thinking'],{'type':'disabled'})
    def test_no_memory_skips_model(self):
        self.assertEqual(contextual_question('蒸水蛋',[],[],lambda *a,**k:self.fail('model used')),'蒸水蛋')
    def test_reject_system_roles_unbounded_memories_and_bad_types(self):
        for history,memories in [([{'role':'system','content':'x'}],[]),([],['x']*13),([],['x'*201]),([{'role':'user','content':123}],[])]:
            with self.assertRaises(ValueError):validate_memory(history,memories)
    def test_bad_context_response_fails_without_guessing(self):
        with patch('conversation_memory.require_llm',return_value={'base_url':'https://api.deepseek.com/v1','api_key':'dummy','model':'deepseek-flash'}):
            with self.assertRaises(ValueError):
                contextual_question('它',[],['清淡'],lambda *a,**k:{'choices':[{'message':{'content':'{"question":""}'}}]})
    def test_progress_events_survive_queue_connection_and_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.sqlite';q=JobQueue(path)
            ident,_=q.submit({'question':'蒸水蛋'})
            q.claim('worker');q.progress(ident,'正在检索');q.progress(ident,'正在核验')
            q.finish(ident,'done',{'outputs':[]})
            result=JobQueue(path).get(ident)
            self.assertEqual([e['message'] for e in result['events']],['正在检索','正在核验'])
            self.assertLessEqual(result['events'][0]['at'],result['events'][1]['at'])
            self.assertGreaterEqual(result['finished'],result['events'][-1]['at'])
            q.progress(ident,'不应追加')
            self.assertEqual(len(q.get(ident)['events']),2)

if __name__=='__main__':unittest.main()
