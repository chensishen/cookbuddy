import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from check_embedding import ROOT
from production_ablation_runtime import install,audit_summary


class ProductionAblationTests(unittest.TestCase):
    def test_alias_removal_is_local_and_does_not_remove_source_evidence(self):
        import ingredient_recommendation as ingredients
        from retrieval_candidates import match_titles
        from cookbuddy.ltm.guard import source_allowed
        original=ingredients.ALIASES.copy();guard=source_allowed
        with install('P-A5',[]):
            self.assertEqual(ingredients.normalize_ingredient('马铃薯'),'马铃薯')
            self.assertEqual(match_titles('番茄炒蛋要哪些原料？',{'西红柿炒鸡蛋'}),[])
            from cookbuddy.ltm.guard import source_allowed as unchanged
            self.assertIs(guard,unchanged)
        self.assertEqual(ingredients.ALIASES,original)
        self.assertEqual(ingredients.normalize_ingredient('马铃薯'),'土豆')
        self.assertEqual(match_titles('番茄炒蛋要哪些原料？',{'西红柿炒鸡蛋'}),['西红柿炒鸡蛋'])

    def test_filter_removal_reaches_fast_recommendation_and_restores_context(self):
        import ingredient_recommendation as ingredients
        import search_filters as filters
        with patch.object(ingredients,'recommend',return_value={'ok':True}) as recommend:
            with filters.filter_policy(category='soup',ingredients_all=['鸡蛋']):
                with install('P-A4',[]):
                    self.assertEqual(filters.current_filters(),{})
                    ingredients.recommend(['鸡蛋'],[],category='soup',max_difficulty=2)
                    self.assertEqual(recommend.call_args.kwargs,{})
                self.assertEqual(filters.current_filters(),{'category':'soup','ingredients_all':['鸡蛋']})

    def test_source_fact_intervention_keeps_other_fast_paths(self):
        import recipe_fact_answer as facts
        import ingredient_recommendation as ingredients
        recommendation=ingredients.recommendation_answer
        with patch.object(facts,'source_fact',return_value={'answer_method':'source_quantity_table'}) as fact:
            audit=[]
            with install('E9',audit):self.assertEqual(facts.source_fact('q',[])['answer_method'],'source_quantity_table')
            self.assertTrue(audit_summary(audit)['source_fact_matched'])
            with install('P-A7',[]):
                self.assertIsNone(facts.source_fact('q',[]))
                self.assertIs(ingredients.recommendation_answer,recommendation)
            self.assertEqual(fact.call_count,1)

    def test_child_stage_inherits_deadline_and_hard_constraints_unchanged(self):
        env={'RECIPE_DEADLINE_EPOCH':'123','RECIPE_HARD_FILTERS':'original','RECIPE_PRODUCTION_ABLATION':'P-A5'}
        with patch('subprocess.run') as process:
            with install('P-A5',[]):
                subprocess.run(['/python',str(ROOT/'scripts/search_test_recipes.py'),'q'],env=env,capture_output=True)
                command=process.call_args.args[0]
                self.assertEqual(command[1:], [str(ROOT/'scripts/production_ablation_worker.py'),'--stage',str(ROOT/'scripts/search_test_recipes.py'),'q'])
                self.assertIs(process.call_args.kwargs['env'],env)
                subprocess.run(['echo','unchanged'])
                self.assertEqual(process.call_args.args[0],['echo','unchanged'])


if __name__=='__main__':unittest.main()
