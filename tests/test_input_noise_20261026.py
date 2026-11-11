"""Do not let a stray numeric suffix become a recipe constraint or preference."""
import copy
import unittest
import test_ordinal_followups_20261023 as ordinal_tests


class InputNoiseTests(unittest.TestCase):
    def fixture(self):
        case=ordinal_tests.OrdinalFollowupTests();case.setUp();return case

    def test_screenshot_tail_does_not_pollute_task_or_memory(self):
        from cookbuddy.ltm.extractor import rule_extract
        case=self.fixture()
        q='换一道，不要刚才第二道。1'
        p,_=case.prepare(q,copy.deepcopy(case.state))
        self.assertEqual(p['task']['action'],'recommend')
        self.assertEqual(p['task']['state']['extra_constraints'],[])
        self.assertEqual(p['task']['state']['limit'],1)
        self.assertEqual(set(p['task']['exclude_dishes']),{'西红柿炒鸡蛋','炒滑蛋'})
        self.assertEqual(rule_extract(q),[])
        self.assertIn('末尾',p['notice'])
        self.assertIn('1',p['notice'])

    def test_equivalent_numeric_suffixes_keep_real_constraints(self):
        from cookbuddy.ltm.extractor import rule_extract
        for q in ('换一道，不要鸡蛋。1','换一道，不要鸡蛋。 １','换一道，不要鸡蛋. 1','换两道菜。2'):
            with self.subTest(q=q):
                case=self.fixture();p,_=case.prepare(q,copy.deepcopy(case.state))
                self.assertEqual(p['task']['action'],'recommend')
                self.assertEqual(p['task']['state']['extra_constraints'],[])
                if '鸡蛋' in q:
                    self.assertIn('鸡蛋',p['task']['state']['excluded_ingredients'])
                    self.assertEqual([(c.value,c.scope) for c in rule_extract(q)],[('鸡蛋','session')])
                else:self.assertEqual(p['task']['state']['limit'],2)

    def test_conflicting_or_ambiguous_number_requires_clarification(self):
        from cookbuddy.ltm.extractor import rule_extract
        for q in ('换一道，不要刚才第二道。2','换两道菜。1','换一批。1','换一道，不要鸡蛋。2'):
            with self.subTest(q=q):
                case=self.fixture();state=copy.deepcopy(case.state)
                p,session=case.prepare(q,state)
                self.assertEqual(p['task']['action'],'clarify')
                self.assertEqual(p['task']['state'],state)
                self.assertIn('末尾',p['task']['clarification'])
                self.assertEqual(rule_extract(q),[])
                self.assertEqual(len(session.slots['last_recommendations']),2)

    def test_measurements_and_meaningful_restrictions_are_not_stripped(self):
        from src.agent.turn_intents import alternative_request
        for q in ('换一道，不要鸡蛋，30分钟内','换一道，不要鸡蛋。180度','换一道，不要刚才第二道。1人份',
                  '换一道，不要刚才第二道。1克','换一道，不要刚才第二道。1不要辣'):
            with self.subTest(q=q):
                a=alternative_request(q)
                self.assertTrue(a)
                self.assertNotIn('ignored_suffix',a)
                self.assertNotIn('input_notice',a)

    def test_replace_recent_ordinal_accepts_matching_numeric_suffix(self):
        from src.agent.turn_intents import alternative_request
        result=alternative_request('换掉刚才第一道，不要重复。1')
        self.assertEqual(result['index'],1)
        self.assertEqual(result['constraint'],'')
        self.assertEqual(result['ignored_suffix'],'1')

    def test_previous_failed_suffix_does_not_block_retry(self):
        from cookbuddy.session import append_message,SessionState,update_slots
        case=self.fixture();state=copy.deepcopy(case.state)
        state['limit']=1;state['choices']=[]
        state['extra_constraints']=['不要刚才第二道','1']
        session=SessionState('u','s');update_slots(session,{'state':case.state})
        append_message(session,'user','换一道，不要刚才第二道。1')
        p,_=case.prepare('换一道，不要刚才第二道。1',state,session)
        self.assertEqual(p['task']['state']['extra_constraints'],[])
        self.assertEqual(set(p['task']['exclude_dishes']),{'西红柿炒鸡蛋','炒滑蛋'})

    def test_residue_without_matching_user_evidence_is_not_deleted(self):
        case=self.fixture();state=copy.deepcopy(case.state)
        state['extra_constraints']=['不要刚才第二道','1']
        p,_=case.prepare('换一道，不要刚才第二道。1',state)
        self.assertEqual(p['task']['state']['extra_constraints'],state['extra_constraints'])
