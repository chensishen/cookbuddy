import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_tools import attach_recipe_media,find_recipe_images,query_recipes,read_recipe,refresh_history_media

class SearchMediaTests(unittest.TestCase):
    def test_failed_answers_do_not_fetch_or_display_recipe_media(self):
        for status in ('degraded','validation_failed','insufficient','tool_failed'):
            with patch('recipe_tools.find_recipe_images',side_effect=AssertionError('failed answer fetched media')):
                result=attach_recipe_media({'status':status,'sources':[{'source':'bread.md'}],'images':[{'image_path':'tool.jpg'}]})
            self.assertEqual(result['images'],[])
    def test_all_indexed_recipe_images_accompany_displayed_source_ranges(self):
        index=json.loads((Path(__file__).resolve().parents[1]/'data/processed/media_pilot/asset-index.json').read_text())
        grouped={}
        for asset in index['assets']:
            if asset['media_type']!='image':continue
            for ref in asset['references']:grouped.setdefault(ref['source'],[]).append({**ref,'image_path':asset['path']})
        for source,refs in grouped.items():
            for result in (read_recipe(source),find_recipe_images(source,limit=1)):
                evidence=result.get('evidence',[])
                expected={(r['image_path'],r['source_line']) for r in refs if any(e['start_line']<=r['source_line']<=e['end_line'] for e in evidence)}
                actual={(r['image_path'],r['source_line']) for r in result.get('images',[])}
                self.assertEqual(expected,actual,source)
    def test_old_history_is_repaired_only_when_its_source_still_matches(self):
        current=read_recipe('基础牛奶面包');old={**current,'images':current['images'][:8]}
        data={'messages':[{'record':{'outputs':[{'tool':'read_recipe','result':old}]}}]}
        repaired=refresh_history_media(data)['messages'][0]['record']['outputs'][0]['result']
        self.assertEqual(len(repaired['images']),10);self.assertEqual(len(old['images']),8)
        old['evidence']=[{**old['evidence'][0],'quote':'被更改的正文'}]
        self.assertEqual(refresh_history_media(data)['messages'][0]['record']['outputs'][0]['result']['images'],[])
    def test_complete_source_includes_the_ninth_and_tenth_images_at_their_original_lines(self):
        result=read_recipe('基础牛奶面包')
        self.assertTrue(result['complete']);self.assertEqual(len(result['images']),10)
        for number,name in ((9,'4-6成品面包.jpg'),(10,'5-1成品.jpg')):
            image=result['images'][number-1]
            self.assertTrue(image['image_path'].endswith(name))
            self.assertEqual(image['alt'],f'基础牛奶面包-预览图-{number}')
            quote=next(row for row in result['evidence'] if row['start_line']<=image['source_line']<=row['end_line'])
            self.assertEqual(quote['quote'].splitlines()[image['source_line']-quote['start_line']],image['source_quote'])
        self.assertEqual(len(find_recipe_images('基础牛奶面包')['images']),8)
    def setUp(self):
        media=find_recipe_images('基础牛奶面包','酵头')
        self.source=media['images'][0]['source']
        self.text={'status':'verified','answer':'酵头制作请参考原文。','sources':[{'source':self.source,'title':'基础牛奶面包','section':media['images'][0]['section']}],'evidence':[]}
    def test_ordinary_qa_automatically_returns_matching_verified_images(self):
        with patch('recipe_tools._query_recipes_with_cache',return_value=self.text):
            result=query_recipes('酵头怎么做？')
        self.assertEqual(result['answer'],self.text['answer'])
        self.assertEqual(len(result['images']),3)
        self.assertTrue(all(image['source']==self.source for image in result['images']))
    def test_recommendations_include_bounded_images_per_recipe(self):
        result=attach_recipe_media({**self.text,'recommendations':[{'source':self.source,'title':'基础牛奶面包'}]})
        self.assertEqual(len(result['images']),2)
        self.assertTrue(all(image['source']==self.source for image in result['images']))
    def test_broken_media_does_not_break_text_or_reuse_cached_images(self):
        with patch('recipe_tools.find_recipe_images',side_effect=ValueError('source_hash_changed')):
            result=attach_recipe_media({**self.text,'images':[{'image_path':'stale'}]})
        self.assertEqual(result['answer'],self.text['answer'])
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['images'],[])
        self.assertEqual(result['media']['errors'][0]['error_code'],'ValueError')
    def test_other_recipe_or_section_is_not_attached(self):
        result=attach_recipe_media({**self.text,'sources':[{'source':self.source,'title':'基础牛奶面包','section':'虚构章节'}]})
        self.assertEqual(result['images'],[])
        wrong={'source':'another-recipe','source_line':1,'image_path':'wrong.jpg'}
        with patch('recipe_tools.find_recipe_images',return_value={'images':[wrong]}):
            self.assertEqual(attach_recipe_media(self.text)['images'],[])
    def test_ambiguous_source_does_not_choose_a_picture(self):
        with patch('recipe_tools.find_recipe_images') as lookup:
            result=attach_recipe_media({**self.text,'source_choices':[{'source':self.source}]})
        lookup.assert_not_called()
        self.assertNotIn('images',result)

if __name__=='__main__':unittest.main()
