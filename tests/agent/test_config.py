import sys
from pathlib import Path
import unittest
from src.agent.config import load_config,ROOT,Config
sys.path.insert(0,str(ROOT/'scripts'))
from experiment_profiles import load_core_profiles


class ConfigTests(unittest.TestCase):
    def test_named_memory_modes_and_missing_context(self):
        from src.agent.context import AppContext
        for name,expected in [('off','M0'),('window','M1'),('slots','M2'),('profile','M3'),('profile_conflict','M5'),('full','M6')]:
            self.assertEqual(Config.model_validate({'memory':{'mode':name}}).memory.mode,expected)
        with self.assertRaises(ValueError):AppContext('','session')
    def test_online_experiments_are_config_only(self):
        folder=ROOT/'config/experiments/online'
        for path in folder.glob('*.yaml'):load_config(experiment=path)
        self.assertFalse(load_config(experiment=folder/'P-A1.yaml').retrieval.rerank.enabled)
        self.assertEqual(load_config(experiment=folder/'P-A2.yaml').retrieval.mode,'dense')
        self.assertFalse(load_config(experiment=folder/'P-A6.yaml').agent.enabled)

    def test_core_profiles_preserve_original_protocol(self):
        actual=load_core_profiles(ROOT/'config/experiments/core')
        base={'corpus':'h2','prefix':False,'overview':False,'normalize':False,'hybrid':False,'filter':False,'rerank':False,'agent':False}
        expected={'E0':{**base,'corpus':'closed'},'E1':{**base,'corpus':'fixed512'}}
        profile=base.copy()
        for name,extra in [('E2',{}),('E3',{'prefix':True}),('E4',{'overview':True,'normalize':True}),('E5',{'hybrid':True}),('E6',{'filter':True}),('E7',{'rerank':True}),('E8',{'agent':True})]:
            profile={**profile,**extra};expected[name]=profile.copy()
        for name,key in [('A-1','rerank'),('A-2','hybrid'),('A-3','prefix'),('A-4','filter'),('A-5','normalize'),('A-6','agent')]:expected[name]={**expected['E8'],key:False}
        self.assertEqual(actual,expected)
