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
MANIFEST = {item['recipe_id']: item for item in json.loads(
    (ROOT / 'data/structured/extraction-pilot-manifest.json').read_text())['recipes']}


def recipe(ident):
    return json.loads((FOLDER / (ident + '.json')).read_text())


class SourceFirstStructuredTests(unittest.TestCase):
    def test_three_failed_model_cases_have_disabled_source_first_candidates(self):
        for ident in ('auto_airfryer_toast', 'auto_tomato_egg', 'auto_liangpi'):
            row = recipe(ident)
            self.assertTrue(validate(row)['quotes_match'])
            self.assertEqual(boundary_checks(row, MANIFEST[ident]), [])
            self.assertEqual(row['curation'], 'assistant_source_first_draft')
            self.assertFalse(row['activation']['enabled'])
            self.assertIsNone(row['answer_policy'])

    def test_airfryer_time_temperature_and_nutrition_do_not_become_materials(self):
        row = recipe('auto_airfryer_toast')
        self.assertEqual({e['canonical_name'] for e in row['entities']}, {'面包片', '空气炸锅'})
        preview = compare_inventory(row, ['面包片'])
        self.assertEqual(preview['tools_not_assessed'], ['空气炸锅'])
        self.assertEqual(preview['readiness'], 'not_assessed')

    def test_tomato_optional_additions_stay_optional(self):
        row = recipe('auto_tomato_egg')
        preview = compare_inventory(row, ['西红柿', '鸡蛋', '食用油', '盐'])
        self.assertEqual([r['name'] for r in preview['required_names'] if r['status']=='not_listed'], [])
        self.assertTrue({'糖', '葱花', '醋', '番茄酱', '水', '生抽'} <= set(preview['optional_materials']))
        self.assertEqual(next(e['category'] for e in row['entities'] if e['canonical_name']=='鸡蛋液'), 'intermediate')

    def test_liangpi_list_is_split_and_water_purposes_are_separate(self):
        row = recipe('auto_liangpi')
        names = {e['canonical_name'] for e in row['entities']}
        self.assertTrue({'绿豆芽', '蒜臼', '盘子', '辣椒油', '水'} <= names)
        self.assertFalse(any('、' in name for name in names))
        water = next(e for e in row['entities'] if e['canonical_name']=='水')
        self.assertTrue({26,29,36,44,45} <= {o['start_line'] for o in water['observations']})
        preview = compare_inventory(row, ['凉皮', '面筋', '黄瓜', '大蒜', '绿豆芽'])
        self.assertIn('盐', [r['name'] for r in preview['required_names'] if r['status']=='not_listed'])
        self.assertIn('辣椒油', preview['optional_materials'])
        self.assertIn('水', preview['step_only_materials'])


if __name__ == '__main__':
    unittest.main()
