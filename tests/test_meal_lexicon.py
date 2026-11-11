import copy
import unittest
from unittest.mock import patch
from cookbuddy.lexicon import settings,meal_match
from cookbuddy.meal_planning import meal_request
from src.nlu.prompts import rendering
from src.nlu.router import understand


class MealLexiconTests(unittest.TestCase):
    def test_every_configured_alias_and_generated_variants(self):
        for kind,row in settings()['meal_types'].items():
            for alias in row['synonyms']:
                for question in ('安排'+alias,'请帮我安排'+alias+' 1',alias+'安排一下','安排下'+alias+'吧'):
                    with self.subTest(question=question):
                        plan=understand(question,{},use_model=False)
                        self.assertEqual(plan['intent'],'meal_plan')
                        self.assertEqual(plan['slots']['meal']['meal_type'],kind)
        self.assertEqual(meal_match('安排早参')['meal_type'],'breakfast')

    def test_config_addition_changes_prompt_and_parser_without_code_change(self):
        cfg=copy.deepcopy(settings());cfg['meal_types']['breakfast']['synonyms'].append('晨间餐')
        with patch('cookbuddy.lexicon.settings',return_value=cfg):
            self.assertEqual(meal_request('安排晨间餐')['meal_type'],'breakfast')
            self.assertIn('晨间餐',rendering())
        self.assertNotIn('晨间餐',rendering())

    def test_rule_source_contains_no_parallel_meal_alias_list(self):
        import ast
        from pathlib import Path
        aliases={alias for row in settings()['meal_types'].values() for alias in row['synonyms']}
        for path in ('scripts/cookbuddy/lexicon.py','scripts/cookbuddy/meal_planning.py','src/nlu/router.py'):
            tree=ast.parse(Path(path).read_text())
            for node in ast.walk(tree):
                if isinstance(node,(ast.List,ast.Set,ast.Tuple)):
                    literals={v.value for v in node.elts if isinstance(v,ast.Constant) and isinstance(v.value,str)}
                    self.assertFalse(literals & aliases,(path,literals & aliases))
