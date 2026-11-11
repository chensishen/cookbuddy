import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from pilot_media_ocr import checked_asset, candidate
from search_media_ocr import search


class MediaOcrPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / 'data/processed/media_ocr_pilot/results.json').read_text())

    def test_candidates_are_not_recipe_evidence(self):
        for row in self.data['records']:
            self.assertFalse(row['enabled_for_recipe_answers'])
            self.assertEqual(row['evidence_type'], 'image_ocr_candidate')
            checked_asset({'path': row['image_path'], 'sha256': row['image_sha256'],
                           'references': row['references']})

    def test_literal_search_keeps_image_and_source(self):
        found = search('玉米油', self.data['records'], title='戚风蛋糕')
        self.assertEqual(len(found), 1)
        self.assertIn('IMG_1516.jpg', found[0]['image_path'])
        self.assertFalse(found[0]['enabled_for_recipe_answers'])
        self.assertEqual(found[0]['references'][0]['source_line'], 142)
        self.assertEqual(search('玉米油', self.data['records'], title='基础牛奶面包'), [])

    def test_changed_image_is_rejected_at_query_time(self):
        stale = copy.deepcopy(self.data['records'][0])
        stale['image_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            search('玉米油', [stale])

    def test_empty_ocr_is_not_searchable(self):
        asset = {'path': 'photo.jpg', 'sha256': 'abc', 'references': []}
        row = candidate(asset, {'observations': []})
        self.assertEqual(row['review_status'], 'no_text_detected')
        self.assertEqual(search('photo', [row]), [])


if __name__ == '__main__':
    unittest.main()
