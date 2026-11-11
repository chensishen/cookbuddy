import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from answer_blocks import build_display_blocks,bind_block_images,build_source_display
from generate_test_answer import validate_answer


class AnswerBlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.source='data/HowToCook/recipe.md';self.path=self.root/self.source;self.path.parent.mkdir(parents=True)
        self.text='## 操作\n\n![混合酵头](one.jpg)\n1. 用30℃温水混合，静置5分钟。\n\n\n![盖上布发酵](two.jpg)\n2. 盖布发酵45分钟，直到体积增大。'
        self.path.write_text(self.text)
        self.refs=[{'reference_id':1,'source':self.source,'section':'操作 > 酵头的制作','content_start_line':1,'content':self.text}]
        self.payload={'claims':[
            {'kind':'step','text':'用30℃温水混合，静置5分钟。','evidence':[{'reference_id':1,'start_line':3,'end_line':4}]},
            {'kind':'step','text':'盖布发酵45分钟，直到体积增大。','evidence':[{'reference_id':1,'start_line':7,'end_line':8}]}]}
        self.patch=patch('answer_blocks.ROOT',self.root);self.patch.start()
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def image(self,line):return {'source':self.source,'source_line':line,'section':'操作 > 酵头的制作','image_path':'test.jpg'}

    def test_display_uses_exact_reviewed_text_but_keeps_narrow_anchors_before_evidence_expansion(self):
        text,evidence,_=validate_answer(json.dumps(self.payload),self.refs,require_blocks=True)
        self.assertEqual(evidence[0]['end_line'],8)
        blocks=build_display_blocks(self.payload,self.refs)
        self.assertEqual(blocks[0]['anchors'][0]['end_line'],4)
        for block in blocks:self.assertIn(block['text'],text)
        self.assertIn('1. 用30℃',text);self.assertIn('2. 盖布',text)
        self.assertEqual(blocks[0]['heading'],'酵头的制作')

    def test_images_before_steps_bind_by_explicit_citations_not_previous_step(self):
        blocks=build_display_blocks(self.payload,self.refs)
        images=bind_block_images(blocks,[self.image(3),self.image(7)])
        self.assertEqual([r['answer_block_id'] for r in images],['answer-1','answer-2'])
        self.assertEqual(bind_block_images(blocks,[self.image(6)]),[])

    def test_ambiguous_or_changed_sources_do_not_attach_images(self):
        blocks=build_display_blocks(self.payload,self.refs)
        duplicate=copy.deepcopy(blocks[0]);duplicate['id']='other'
        self.assertEqual(bind_block_images([blocks[0],duplicate],[self.image(3)]),[])
        self.path.write_text(self.text+'\nchanged')
        self.assertEqual(bind_block_images(blocks,[self.image(3)]),[])

    def test_typed_generation_requires_types_and_rejects_unreviewed_heading_or_duplicate_numbering(self):
        for changes in [{'kind':'unknown'},{'text':'### 未核实的标题\n结论'},{'text':'1. 静置5分钟'}]:
            payload=copy.deepcopy(self.payload);payload['claims'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):validate_answer(json.dumps(payload),self.refs,require_blocks=True)
        legacy=copy.deepcopy(self.payload)
        for claim in legacy['claims']:claim.pop('kind')
        validate_answer(json.dumps(legacy),self.refs)
        self.assertEqual(build_display_blocks(legacy,self.refs),[])
        with self.assertRaises(ValueError):validate_answer(json.dumps(legacy),self.refs,require_blocks=True)

    def test_typed_claims_still_reject_unsupported_parameters(self):
        self.payload['claims'][0]['text']='用90℃温水混合。'
        with self.assertRaises(ValueError):validate_answer(json.dumps(self.payload),self.refs,require_blocks=True)

    def test_phase_step_numbers_follow_source_so_cross_references_do_not_shift(self):
        self.text=self.text.replace('2. 盖布','4. 盖布');self.path.write_text(self.text);self.refs[0]['content']=self.text
        blocks=build_display_blocks(self.payload,self.refs)
        text,_,_=validate_answer(json.dumps(self.payload),self.refs,require_blocks=True)
        self.assertEqual(blocks[1]['step_number'],4)
        self.assertIn('4. 盖布',text)

    def test_exact_source_fallback_preserves_image_positions_and_rejects_modified_text(self):
        text=self.text.replace('![混合酵头](one.jpg)','').replace('![盖上布发酵](two.jpg)','').strip()
        payload={'claims':[{'text':text,'evidence':[{'reference_id':1,'start_line':1,'end_line':8}]}]}
        source=build_source_display(payload,self.refs)
        self.assertEqual(source[0]['quote'].splitlines()[6],'![盖上布发酵](two.jpg)')
        payload['claims'][0]['text']=text.replace('30℃','90℃')
        with self.assertRaises(ValueError):build_source_display(payload,self.refs)

    def test_split_claims_from_the_same_original_step_form_one_step_without_losing_conditions(self):
        continuation=copy.deepcopy(self.payload['claims'][0]);continuation['text']='保持30℃。'
        self.payload['claims'].insert(1,continuation)
        blocks=build_display_blocks(self.payload,self.refs)
        self.assertEqual(len(blocks),2)
        self.assertIn('静置5分钟',blocks[0]['text']);self.assertIn('保持30℃',blocks[0]['text'])
        text,_,_=validate_answer(json.dumps(self.payload),self.refs,require_blocks=True)
        self.assertEqual(text.count('1. '),1)


class CompleteRecipeMediaTests(unittest.TestCase):
    def test_source_recipe_fallback_keeps_late_step_images_but_excludes_optional_tool_pictures(self):
        from recipe_tools import read_recipe,attach_recipe_media
        from source_recipe_answer import prepare_full_recipe_answer
        source=read_recipe('基础牛奶面包')
        references=[{'reference_id':i+1,'source':e['source'],'section':e['section'],
            'content_start_line':e['start_line'],'content':e['quote']} for i,e in enumerate(source['evidence'])]
        payload=prepare_full_recipe_answer('基础牛奶面包的完整制作步骤和配方',references,source['sources'][0]['source'],compact=True)
        sections=build_source_display(payload,references)
        result=attach_recipe_media({**source,'status':'verified','display_source':sections})
        alts={i['alt'] for i in result['images']}
        self.assertIn('基础牛奶面包-预览图-9',alts)
        self.assertIn('基础牛奶面包-预览图-10',alts)
        self.assertNotIn('用具1',alts);self.assertNotIn('用具2',alts)


if __name__=='__main__':unittest.main()
