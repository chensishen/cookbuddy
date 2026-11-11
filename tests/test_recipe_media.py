import copy
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from process_recipe_media import reference_locations, local_file, check_ocr, make_chunks, parse_sample


class MediaTests(unittest.TestCase):
    def test_reference_uses_actual_image_line_and_matching_caption(self):
        lines=['路径 ./a.jpg','![前](./a.jpg)','![后](./a.jpg)',
               '![前][pic]','[pic]: ./a.jpg']
        self.assertEqual(reference_locations(lines,{'alt':'前','target':'./a.jpg'}),[2,4])

    def test_html_and_markdown_title_are_located(self):
        self.assertEqual(reference_locations(['<img src="./a.jpg">'],{'alt':'','target':'./a.jpg'}),[1])
        self.assertEqual(reference_locations(['![图](./a.jpg "title")'],{'alt':'图','target':'./a.jpg'}),[1])

    def test_external_path_rejected(self):
        with self.assertRaises(ValueError):local_file('../.env')
        with self.assertRaises(ValueError):local_file('AGENTS.md')

    def test_invalid_ocr_coordinates_and_confidence_rejected(self):
        region={'text':'①','confidence':0.3,'bbox':{'x':0.1,'y':0.2,'width':0.2,'height':0.1}}
        check_ocr({'observations':[region]})
        bad=copy.deepcopy(region);bad['confidence']=True
        with self.assertRaises(ValueError):check_ocr({'observations':[bad]})
        bad=copy.deepcopy(region);bad['bbox']['width']=0.95
        with self.assertRaises(ValueError):check_ocr({'observations':[bad]})

    def test_source_and_image_provenance_stay_separate(self):
        record={'image_path':'photo.jpg','image_sha256':'abc','visual_description':'浅色面团',
            'limitations':['不判断温度'],'warnings':[],'ocr':{'observations':[{'text':'①'}]},
            'references':[{'source':'recipe.md','source_sha256':'def','title':'面包','section':'操作',
            'source_line':10,'source_quote':'![酵头](photo.jpg)','chunk_ids':['recipe::001']}]}
        chunks=make_chunks(record)
        self.assertEqual({c['evidence_type'] for c in chunks},{'visual_description','image_ocr'})
        self.assertTrue(all(not c['enabled'] for c in chunks))
        self.assertTrue(all(c['source_quote']=='![酵头](photo.jpg)' for c in chunks))
        self.assertTrue(all(c['image_sha256']=='abc' and c['source_sha256']=='def' for c in chunks))
        self.assertEqual(len({c['chunk_id'] for c in chunks}),2)

    def test_empty_ocr_produces_no_invented_text_chunk(self):
        record={'image_path':'photo.jpg','image_sha256':'abc','visual_description':'浅色面团',
            'limitations':[],'warnings':[],'ocr':{'observations':[]},'references':[]}
        self.assertEqual(make_chunks(record),[])

    def test_changed_image_invalidates_visual_description_before_ocr(self):
        sample=json.loads((ROOT/'data/processed/media_pilot/sample-descriptions.json').read_text())['samples'][0]
        sample['image_sha256']='0'*64
        with self.assertRaises(ValueError):parse_sample(sample,{'asset_id':'0'*64})


if __name__=='__main__':unittest.main()
