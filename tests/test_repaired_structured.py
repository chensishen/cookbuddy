import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from evaluate_repaired_recipes import compare_inventory
from extract_recipe_pilot import boundary_checks
from recipe_schema import validate

FOLDER = ROOT / 'data/structured/repaired_pilot'
MANIFEST = {r['recipe_id']: r for r in json.loads(
    (ROOT / 'data/structured/extraction-pilot-manifest.json').read_text())['recipes']}


class RepairedStructuredTests(unittest.TestCase):
    def recipe(self, ident):
        return json.loads((FOLDER / (ident + '.json')).read_text())

    def test_two_revisions_are_source_checked_and_separate_from_originals(self):
        for ident in ('auto_cola_wings', 'auto_steamed_egg'):
            recipe = self.recipe(ident)
            self.assertTrue(validate(recipe)['quotes_match'])
            self.assertEqual(boundary_checks(recipe, MANIFEST[ident]), [])
            self.assertFalse(recipe['activation']['enabled'])
            self.assertIsNone(recipe['answer_policy'])
            original = json.loads((ROOT / recipe['extraction']['derived_from']).read_text())
            self.assertEqual(original['extraction']['method'], 'model_extraction')

    def test_cola_alternative_and_step_extras_are_distinct(self):
        recipe = self.recipe('auto_cola_wings')
        enough = compare_inventory(recipe, ['鸡翅中', '可乐', '白糖', '生抽', '盐', '生姜', '小葱', '啤酒'])
        less = compare_inventory(recipe, ['鸡翅中', '可乐', '白糖', '生抽', '盐', '生姜', '小葱'])
        self.assertEqual(enough['material_alternative_groups'][0]['status'], 'named')
        self.assertEqual(less['material_alternative_groups'][0]['status'], 'not_listed')
        self.assertEqual(set(enough['step_only_materials']), {'水', '油'})
        self.assertFalse(any('剩余姜片' in issue['note'] and issue['kind']=='quantity_discrepancy'
                             for issue in recipe['issues']))
        self.assertEqual(enough['readiness'], 'not_assessed')

    def test_steamed_egg_does_not_assume_freshness_or_meld_water_uses(self):
        recipe = self.recipe('auto_steamed_egg')
        generic = compare_inventory(recipe, ['鸡蛋', '热水', '盐'])
        self.assertIn('新鲜鸡蛋', [row['name'] for row in generic['required_names']
                              if row['status']=='not_listed'])
        water = next(e for e in recipe['entities'] if e['canonical_name']=='水')
        self.assertTrue({24,26}.issubset({o['start_line'] for o in water['observations']}))
        self.assertTrue(any(o['start_line']==26 and o['purpose']=='锅中用水' for o in water['observations']))
        self.assertFalse(any(g['group_id']=='optional_seasonings' for g in recipe['alternative_groups']))
        self.assertEqual(generic['readiness'], 'not_assessed')

    def test_stale_model_draft_lineage_is_rejected(self):
        recipe = self.recipe('auto_cola_wings')
        recipe['extraction']['draft_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            compare_inventory(recipe, ['可乐'])


if __name__ == '__main__':
    unittest.main()
