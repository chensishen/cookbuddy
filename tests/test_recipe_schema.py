import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from recipe_schema import validate
from extract_recipe_pilot import materialize, boundary_checks
from extract_recipe_pilot import call, ModelOutputError, APICallError, mark_failure, extract
from audit_structured_drafts import audit


class RecipeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.recipe = json.loads((ROOT / 'data/structured/recipe_pilot/gongbao_chicken.json').read_text())

    def test_existing_samples_share_schema(self):
        for name in ('gongbao_chicken','milk_bread','cucumber_salad'):
            recipe = json.loads((ROOT / 'data/structured/recipe_pilot' / (name+'.json')).read_text())
            result = validate(recipe)
            self.assertTrue(result['quotes_match'])
            self.assertEqual(recipe['schema_version'], '0.2')

    def test_changed_source_hash_and_external_path_rejected(self):
        self.recipe['source_sha256'] = '0'*64
        with self.assertRaises(ValueError): validate(self.recipe)
        self.recipe['source'] = '../.env'
        with self.assertRaises(ValueError): validate(self.recipe)

    def test_fabricated_quote_and_boolean_line_rejected(self):
        observation = self.recipe['entities'][0]['observations'][0]
        original = copy.deepcopy(observation)
        observation['quote'] = '没有这句原文'
        with self.assertRaises(ValueError): validate(self.recipe)
        observation.update(original); observation['start_line'] = True
        with self.assertRaises(ValueError): validate(self.recipe)

    def test_group_reverse_reference_and_duplicate_ids_rejected(self):
        self.recipe['entities'][0]['alternative_group'] = None
        with self.assertRaises(ValueError): validate(self.recipe)
        self.setUp()
        self.recipe['entities'][1]['entity_id'] = self.recipe['entities'][0]['entity_id']
        with self.assertRaises(ValueError): validate(self.recipe)

    def test_tools_cannot_be_procurement_ingredients(self):
        tool = next(e for e in self.recipe['entities'] if e['category']=='tool')
        self.recipe['version_preparation'][0]['entries'].append({'entity_id':tool['entity_id'],'name':tool['canonical_name'],
            'alternative_group':None,'evidence_lines':[tool['observations'][0]['start_line']]})
        with self.assertRaises(ValueError): validate(self.recipe)

    def test_auto_draft_cannot_be_enabled(self):
        self.recipe['extraction']['method'] = 'model_extraction'
        with self.assertRaises(ValueError): validate(self.recipe)

    def test_overlapping_alternative_groups_remain_separate(self):
        self.recipe['alternative_groups'].append({'group_id':'version_meat','members':['手枪腿','鸡胸脯肉'],
            'satisfaction_rule':'any_one','note':'另一版本观察，不合并成新的同时采购要求。'})
        for entity in self.recipe['entities']:
            if entity['canonical_name'] in ['手枪腿','鸡胸脯肉']:
                entity['alternative_group_ids'].append('version_meat')
        self.assertTrue(validate(self.recipe)['relationships_valid'])


class OfflineDraftAuditTests(unittest.TestCase):
    def test_steamed_egg_recheck_detects_multiple_independent_omissions(self):
        manifest=json.loads((ROOT/'data/structured/extraction-pilot-manifest.json').read_text())['recipes'][1]
        recipe=json.loads((ROOT/'data/structured/extraction_pilot/auto_steamed_egg/recipe.json').read_text())
        failures=boundary_checks(recipe,manifest)
        self.assertTrue(any('水/热水/温水 L24' in item for item in failures))
        self.assertTrue(any('水/热水/温水 L26' in item for item in failures))
        self.assertTrue(any('缺少条目：盘子' == item for item in failures))
        self.assertTrue(any('19,24' in item for item in failures))

    def test_offline_audit_keeps_model_drafts_disabled_and_source_cards_checkable(self):
        result=audit()
        self.assertEqual(result['summary']['current_drafts'],2)
        self.assertEqual(result['summary']['no_current_draft'],3)
        self.assertEqual(result['summary']['activated'],0)
        for card in result['review_cards']:
            lines=(ROOT/card['source']).read_text().splitlines()
            for quote in card['quotes']:
                self.assertEqual(quote['text'],lines[quote['line']-1])


class ExtractionMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.item = json.loads((ROOT / 'data/structured/extraction-pilot-manifest.json').read_text())['recipes'][2]
        self.path = ROOT / self.item['source']
        self.raw = {'entities':[
            {'name':'面包片','aliases':[],'category':'ingredient','review_note':'','observations':[
                {'start_line':11,'end_line':11,'requirement':'listed_required'},
                {'start_line':18,'end_line':18,'requirement':'listed_required','quantity_raw':'两片'}]},
            {'name':'空气炸锅','aliases':[],'category':'tool','review_note':'','observations':[
                {'start_line':12,'end_line':12,'requirement':'listed_required'}]}
        ],'alternative_groups':[],'issues':[]}
        lines=self.path.read_text().splitlines()
        for entity in self.raw['entities']:
            for obs in entity['observations']:
                obs['evidence_text']='\n'.join(lines[obs['start_line']-1:obs['end_line']])

    def test_quotes_and_section_generated_from_source(self):
        result = materialize(self.raw,self.item,self.path)
        self.assertEqual(result['entities'][0]['observations'][0]['quote'],'- 面包片')
        self.assertFalse(result['activation']['enabled'])
        self.assertEqual(boundary_checks(result,self.item),[])

    def test_hallucinated_quantity_or_alias_rejected(self):
        self.raw['entities'][0]['observations'][1]['quantity_raw'] = '400g'
        with self.assertRaises(ValueError): materialize(self.raw,self.item,self.path)
        self.raw['entities'][0]['observations'][1]['quantity_raw'] = None
        self.raw['entities'][0]['aliases'] = ['苹果']
        with self.assertRaises(ValueError): materialize(self.raw,self.item,self.path)

    def test_omitted_tool_detected_independently(self):
        self.raw['entities'] = self.raw['entities'][:1]
        result = materialize(self.raw,self.item,self.path)
        self.assertIn('缺少条目：空气炸锅',boundary_checks(result,self.item))

    def test_unique_exact_anchor_corrects_model_line_numbers(self):
        self.raw['entities'][0]['observations'][0]['start_line'] = 999
        self.raw['entities'][0]['observations'][0]['end_line'] = 999
        result = materialize(self.raw,self.item,self.path)
        self.assertEqual(result['entities'][0]['observations'][0]['start_line'],11)
        self.assertEqual(result['extraction']['alignment_notes'][0]['model_lines'],[999,999])

    def test_rewritten_anchor_is_rejected(self):
        self.raw['entities'][0]['observations'][0]['evidence_text'] = '- 苹果切片'
        with self.assertRaises(ValueError): materialize(self.raw,self.item,self.path)

    def test_quantity_from_other_chapter_is_separate_fact(self):
        self.raw['entities'][0]['observations'][0]['quantity_raw']='两片'
        result=materialize(self.raw,self.item,self.path)
        observations=result['entities'][0]['observations']
        self.assertIsNone(observations[0]['quantity_raw'])
        self.assertTrue(any(o['start_line']==18 and o['quantity_raw']=='两片' and o['requirement']=='mentioned' for o in observations))

    def test_process_time_cannot_be_bread_quantity(self):
        self.raw['entities'][0]['observations'][0]['quantity_raw']='5 分钟'
        with self.assertRaises(ValueError): materialize(self.raw,self.item,self.path)

    def test_review_outage_preserves_boundary_failures(self):
        result={'recipe':materialize(self.raw,self.item,self.path),'boundary_failures':['遗漏步骤水']}
        mark_failure(result,APICallError('HTTP 429'),'review')
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['error_stage'],'review')
        self.assertEqual(result['recipe']['validation']['semantic_status'],'review_unavailable')
        self.assertIn('遗漏步骤水',result['recipe']['validation']['warnings'])
        self.assertFalse(result['recipe']['activation']['enabled'])

    def test_repair_outage_and_invalid_output_never_leave_pass_status(self):
        result={'recipe':materialize(self.raw,self.item,self.path),'boundary_failures':[]}
        mark_failure(result,APICallError('HTTP 429'),'extract')
        self.assertEqual(result['recipe']['validation']['semantic_status'],'repair_unavailable')
        mark_failure(result,ValueError('非法字段'),'materialize')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['recipe']['validation']['semantic_status'],'needs_review')

    def test_new_draft_cannot_inherit_older_review_after_api_outage(self):
        trace={'model':'Qwen/Qwen3-32B','finish_reason':'stop','raw_content':'{}'}
        old_review={'verdict':'needs_review','findings':[{'kind':'分类','note':'旧版待核对','lines':[12]}]}
        responses=[(copy.deepcopy(self.raw),trace),(old_review,trace),
                   (copy.deepcopy(self.raw),trace),APICallError('HTTP 429')]
        with tempfile.TemporaryDirectory() as directory, patch('extract_recipe_pilot.OUT',Path(directory)), patch('extract_recipe_pilot.call',side_effect=responses):
            result=extract(self.item)
            saved=json.loads((Path(directory)/self.item['recipe_id']/'result.json').read_text())
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(saved['draft_attempt'],2)
        self.assertIsNone(saved['semantic_review'])
        self.assertIsNone(saved['semantic_review_attempt'])
        self.assertEqual(saved['error_stage'],'review')
        self.assertNotIn('旧版待核对',saved['recipe']['validation']['warnings'])


class StreamingResponseTests(unittest.TestCase):
    def response(self, content, finish):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def __iter__(self):
                yield ('data: '+json.dumps({'choices':[{'delta':{'reasoning_content':'内部推理，不是JSON'}}]} )+'\n').encode()
                yield ('data: '+json.dumps({'model':'Qwen/Qwen3-8B','choices':[{'delta':{'content':content},'finish_reason':finish}]})+'\n').encode()
                yield b'data: [DONE]\n'
        return Response()

    def test_reasoning_ignored_and_sampling_parameters_not_greedy(self):
        with patch('extract_recipe_pilot.config',return_value={'EMBEDDING_BASE_URL':'https://example.invalid/v1','EMBEDDING_API_KEY':'unit-test-token'}), patch('extract_recipe_pilot.urlopen',return_value=self.response('{"ok":true}','stop')) as api:
            result, trace = call([{'role':'user','content':'测试'}])
        self.assertEqual(result,{'ok':True})
        self.assertGreater(trace['request_parameters']['temperature'],0)
        self.assertNotIn('messages',trace['request_parameters'])
        self.assertEqual(api.call_count,1)

    def test_truncated_output_never_passes_and_raw_is_preserved(self):
        with patch('extract_recipe_pilot.config',return_value={'EMBEDDING_BASE_URL':'https://example.invalid/v1','EMBEDDING_API_KEY':'unit-test-token'}), patch('extract_recipe_pilot.urlopen',return_value=self.response('{"partial":','length')):
            with self.assertRaises(ModelOutputError) as error: call([])
        self.assertEqual(error.exception.trace['raw_content'],'{"partial":')
        self.assertEqual(error.exception.trace['finish_reason'],'length')

    def test_invalid_json_never_passes(self):
        with patch('extract_recipe_pilot.config',return_value={'EMBEDDING_BASE_URL':'https://example.invalid/v1','EMBEDDING_API_KEY':'unit-test-token'}), patch('extract_recipe_pilot.urlopen',return_value=self.response('{bad}','stop')):
            with self.assertRaises(ModelOutputError) as error: call([])
        self.assertEqual(error.exception.trace['raw_content'],'{bad}')


if __name__ == '__main__': unittest.main()
