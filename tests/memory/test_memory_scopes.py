"""Stock belongs to a conversation; stable preferences can be revised."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from cookbuddy.ltm.extractor import extract
from cookbuddy.ltm.models import Candidate
from cookbuddy.ltm.profile_store import ProfileStore


class MemoryScopeTests(unittest.TestCase):
    def test_rejected_recipe_reference_never_becomes_ingredient_avoidance(self):
        for text in ('换一套，不要刚才的。','不要刚才那道菜','不要上次的菜','我不要重复刚才的菜'):
            self.assertEqual(extract(text),[],text)
        self.assertEqual([c.value for c in extract('不要香菜')],['香菜'])
        text='我想换掉刚才的菜'
        model=Mock(return_value={'candidates':[dict(dimension='avoid_ingredient',value='刚才',
            polarity='avoid',strength='hard',source='explicit',scope='session',op='set',confidence=.7,evidence=text)]})
        self.assertEqual(extract(text,model=model),[])
        model.assert_called_once()

    def test_legacy_session_reference_is_removed_but_real_allergy_remains(self):
        from cookbuddy.session import SessionState
        from cookbuddy.ltm.runtime import MemoryRuntime
        session=SessionState('u','s')
        allergy={'dimension':'allergy','value':'花生','strength':'hard'}
        session.slots['temp_constraints']['hard_constraints']=[
            {'dimension':'avoid_ingredient','value':'刚才','strength':'hard'},allergy]
        store=Mock();store.session_context.return_value=session
        with patch('cookbuddy.ltm.runtime.ProfileStore') as prefs:
            prefs.return_value.enabled.return_value=True
            MemoryRuntime(store,'u','s','j','我有哪些食材？')
        self.assertEqual(session.slots['temp_constraints']['hard_constraints'],[allergy])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.prefs=ProfileStore(path=Path(self.tmp.name)/'memory.db')

    def test_meal_parameters_never_become_long_term_preferences(self):
        for dimension,value in (('max_difficulty','1'),('household_size','2'),('time_budget','30')):
            for scope in ('global','temporary','session'):
                with self.subTest(dimension=dimension,scope=scope):
                    c=Candidate(dimension,value,scope=scope)
                    self.assertEqual(c.scope,'session')
                    self.assertIsNone(c.valid_to)
                    # The persistence boundary must also reject a stale worker's scope.
                    c.scope='global'
                    for i in range(3):
                        self.assertEqual(self.prefs.apply('u','s'+str(i),dimension+scope+str(i),c)['decision'],'SESSION_ONLY')
        self.assertEqual(self.prefs.prefs('u'),[])

    def test_rules_and_model_keep_meal_parameters_in_session(self):
        for text in ('我们家2人','请记住：30分钟以内'):
            rows=extract(text)
            self.assertTrue(rows)
            self.assertTrue(all(c.scope=='session' for c in rows))
        text='请记住我的难度上限1，用餐人数2，时间预算30分钟'
        model=Mock(return_value={'candidates':[dict(dimension=dimension,value=value,
            polarity='must',strength='hard',source='explicit',scope='global',op='set',
            confidence=.7,evidence=text) for dimension,value in
            (('max_difficulty','1'),('household_size','2'),('time_budget','30'))]})
        rows=extract(text,model=model)
        model.assert_called_once()
        self.assertEqual(len(rows),3)
        self.assertTrue(all(c.scope=='session' and c.valid_to is None for c in rows))

    def test_legacy_meal_parameters_do_not_enter_profile_or_memory_read(self):
        from cookbuddy.ltm.models import item
        from cookbuddy.ltm.management import handle_command
        for dimension,value in (('max_difficulty','1'),('household_size','2'),('time_budget','30')):
            old=item(Candidate(dimension,value),uid='u')
            old.scope='global'
            with self.prefs.connect('u') as db:self.prefs._insert(db,old)
        self.prefs.apply('u','s','s:1',Candidate('spice_level','1'))
        profile=self.prefs.load_profile('u')
        self.assertEqual(profile['hard_constraints'],[])
        self.assertEqual([p['dimension'] for p in profile['soft_prefs']],['spice_level'])
        self.assertEqual(handle_command(('read',''),self.prefs,'u','s','s:2')['message'],
                         '目前记得的偏好和限制：\n- 辣度：微辣')

    def test_stock_is_session_only_even_when_user_says_remember(self):
        for text in ('我有番茄和鸡蛋','请记住：我家里有番茄和鸡蛋'):
            rows=extract(text)
            self.assertEqual({c.value for c in rows},{'番茄','鸡蛋'})
            self.assertTrue(all(c.dimension=='inventory' and c.scope=='session' and c.valid_to is None for c in rows))
        c=Candidate('inventory','鸡蛋',scope='global')
        self.assertEqual(self.prefs.apply('u','s','s:1',c)['decision'],'SESSION_ONLY')
        self.assertEqual(self.prefs.prefs('u'),[])

    def test_model_cannot_promote_stock_to_long_term(self):
        model=Mock(return_value={'candidates':[dict(dimension='inventory',value='菠菜',polarity='must',strength='soft',
            source='explicit',scope='global',op='set',confidence=.7,evidence='我买到了菠菜')]})
        rows=extract('我买到了菠菜',model=model)
        self.assertEqual(rows[0].scope,'session')

    def test_rule_and_model_flavor_aliases_share_one_long_term_preference(self):
        model=Mock(return_value={'candidates':[dict(dimension='flavor',value='重口味',polarity='like',strength='soft',
            source='explicit',scope='global',op='set',confidence=.7,evidence='我以后的口味是重口味')]})
        self.prefs.apply('u','s','s:1',Candidate('flavor','重口'))
        c=extract('我以后的口味是重口味',model=model)[0]
        self.assertEqual(c.value,'重口')
        self.prefs.apply('u','s','s:2',c)
        self.assertEqual([p.value for p in self.prefs.prefs('u')],['重口'])

    def test_taste_change_supersedes_only_conflicting_flavor(self):
        for i,text in enumerate(('我喜欢清淡口味','我喜欢少油','我的口味改成重口味了'),1):
            rows=extract(text);self.assertTrue(rows,text)
            for c in rows:self.prefs.apply('u','s','s:'+str(i),c)
        active=self.prefs.load_profile('u')['soft_prefs']
        self.assertEqual({p['value'] for p in active},{'少油','重口'})
        old=next(p for p in self.prefs.prefs('u',('superseded',)) if p.value=='清淡')
        new=next(p for p in self.prefs.prefs('u') if p.value=='重口')
        self.assertEqual((new.supersedes,new.version),(old.id,old.version+1))
        for c in extract('我不再喜欢重口味了'):self.prefs.apply('u','s','s:4',c)
        active=self.prefs.load_profile('u')['soft_prefs']
        self.assertFalse(any(p['value']=='重口' and p['polarity']=='like' for p in active))
        self.assertTrue(any(p['value']=='重口' and p['polarity']=='dislike' for p in active))

    def test_current_meal_does_not_replace_long_term_taste_or_relax_allergy(self):
        self.prefs.apply('u','s','s:1',Candidate('flavor','清淡'))
        for c in extract('今天想吃重口味'):self.prefs.apply('u','s','s:2',c)
        self.assertEqual([p.value for p in self.prefs.prefs('u')],['清淡'])
        for c in extract('我对花生过敏'):self.prefs.apply('u','s','s:3',c)
        result=self.prefs.apply('u','s','s:4',extract('我对花生不过敏')[0])
        self.assertEqual(result['decision'],'ASK')
        self.assertTrue(self.prefs.load_profile('u')['hard_constraints'])

    def test_older_taste_extraction_cannot_restore_superseded_flavor(self):
        self.prefs.apply('u','s','s:20',Candidate('flavor','重口'))
        self.prefs.apply('u','s','s:10',Candidate('flavor','清淡'))
        self.assertEqual([p.value for p in self.prefs.prefs('u')],['重口'])

    def test_legacy_stock_is_retained_but_excluded_from_long_term_context(self):
        from dataclasses import replace
        from cookbuddy.ltm.models import item
        from cookbuddy.ltm.management import handle_command
        old=replace(item(Candidate('inventory','鸡蛋'),uid='u'),scope='temporary')
        with self.prefs.connect('u') as db:self.prefs._insert(db,old)
        self.prefs.apply('u','s','s:1',Candidate('flavor','清淡'))
        self.assertFalse(any(p['dimension']=='inventory' for p in self.prefs.load_profile('u')['soft_prefs']))
        self.assertNotIn('鸡蛋',handle_command(('read',''),self.prefs,'u','s','s:2')['message'])
        self.assertTrue(any(p.id==old.id for p in self.prefs.prefs('u')))
        with self.assertRaisesRegex(ValueError,'inventory_is_session_memory'):
            self.prefs.apply('u','s','s:3',Candidate('inventory','豆腐'),target_id=old.id)

    def test_old_stock_idempotency_record_cannot_claim_long_term_write(self):
        import hashlib
        import json
        from cookbuddy.ltm.models import stamp
        c=Candidate('inventory','鸡蛋');turn='s:1'
        signature={k:getattr(c,k) for k in ('dimension','value','polarity','op')}
        key=turn+':'+hashlib.sha256(json.dumps(signature,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.prefs.connect('u') as db:
            db.execute('INSERT INTO memory_events(id,user_id,decision,reason,candidate_json,source_turn_ref,created_at,idem_key) VALUES(?,?,?,?,?,?,?,?)',
                       ('legacy-stock','u','ADD','legacy','{}',turn,stamp(),key))
        self.assertEqual(self.prefs.apply('u','s',turn,c)['decision'],'SESSION_ONLY')

    def test_disliked_flavor_is_not_described_as_a_positive_preference(self):
        from cookbuddy.ltm.management import describe
        from cookbuddy.ltm.models import item
        self.assertEqual(describe(item(Candidate('flavor','清淡',polarity='dislike'))),'不喜欢清淡口味')


if __name__=='__main__':unittest.main()
