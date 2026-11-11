import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_metadata import enrich
from ingredient_recommendation import ingredient_fields
from search_filters import filter_chunks,scalar_filter,ingredient_terms,filter_policy,current_filters
from search_filters import explicit_question_filters


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source='data/HowToCook/dishes/soup/test.md'
        self.path=self.root/self.source;self.path.parent.mkdir(parents=True)
        self.path.write_text('预估烹饪难度：★★\n## 必备原料和工具\n- 西红柿150克\n- 葱（可选）\n- 锅\n- 鸡蛋或者豆腐')
        self.rows=[{'chunk_id':'a','title':'汤','section':'简介','source':self.source,'source_start_line':1,'source_end_line':1,'content':'预估烹饪难度：★★','text':'unchanged'},
            {'chunk_id':'b','title':'汤','section':'必备原料和工具','source':self.source,'source_start_line':2,'source_end_line':6,'content':'\n'.join(self.path.read_text().splitlines()[1:]),'text':'unchanged'}]

    def test_metadata_is_source_bound_and_vectors_input_unchanged(self):
        rows,metadata=enrich(self.rows,self.root,commit='a'*40)
        for before,after in zip(self.rows,rows):
            for key,value in before.items():self.assertEqual(after[key],value)
            self.assertEqual(after['ingredients'],['番茄'])
            self.assertEqual(after['optional_ingredients'],['葱'])
            self.assertEqual(after['tools'],['锅'])
            self.assertEqual(after['difficulty'],2)
            self.assertEqual(after['category'],'soup')
            self.assertEqual(len(after['doc_hash']),64)
            self.assertIn('ingredients_uncertain',json.loads(after['flags']))
            self.assertLess(after['quality_score'],1)
        self.assertEqual(rows[1]['chunk_type'],'ingredients')

    def test_changed_source_or_path_traversal_is_rejected(self):
        self.path.write_text('changed')
        with self.assertRaises(ValueError):enrich(self.rows,self.root,commit='a'*40)
        for row in self.rows:row['source']='../outside.md'
        with self.assertRaises(ValueError):enrich(self.rows,self.root,commit='a'*40)

    def test_missing_metadata_is_flagged_not_invented(self):
        row={**self.rows[0],'content':'没有声明难度'};self.path.write_text(row['content'])
        rows,_=enrich([row],self.root,commit='a'*40)
        self.assertEqual(rows[0]['difficulty'],0)
        self.assertEqual(rows[0]['ingredients'],[])
        self.assertEqual(set(json.loads(rows[0]['flags'])),{'difficulty_unknown','ingredient_table_missing','ingredients_empty'})

    def test_shared_ingredient_parser_retains_uncertainty(self):
        required,optional,tools,uncertain,evidence=ingredient_fields(self.rows[1:],self.path.read_text().splitlines())
        self.assertEqual(required,{'番茄'});self.assertEqual(optional,{'葱'});self.assertEqual(tools,{'锅'})
        self.assertEqual(uncertain,['鸡蛋或者豆腐']);self.assertEqual(evidence[0]['start_line'],2)

    def test_terminal_newline_does_not_change_source_line_identity(self):
        self.rows[0]['content']+='\n'
        rows,_=enrich(self.rows,self.root,commit='a'*40)
        self.assertEqual(rows[0]['content'],self.rows[0]['content'])

    def test_array_filters_use_normalized_required_items_only(self):
        rows,_=enrich(self.rows,self.root,commit='a'*40)
        accepted,_,_=filter_chunks(rows,ingredients_all=['西红柿'])
        self.assertEqual(len(accepted),2)
        rejected,_,_=filter_chunks(rows,ingredients_any=['葱','鸡蛋'])
        self.assertEqual(rejected,[])
        rejected,_,_=filter_chunks(rows,ingredients_all=['西红柿','鸡蛋'])
        self.assertEqual(rejected,[])
        with filter_policy(ingredients_any=['西红柿','番茄']):
            self.assertEqual(current_filters()['ingredients_any'],['番茄'])
        expression=scalar_filter({'category':'soup','max_difficulty':2,'ingredients_all':['西红柿']})
        self.assertIn('array_contains_all(ingredients, ["番茄"])',expression)
        self.assertIn('difficulty >= 1',expression)

    def test_invalid_array_conditions_are_rejected(self):
        for value in ([],True,'鸡蛋',['鸡蛋2'],['锅'],['鸡蛋']*17,[True]):
            with self.assertRaises(ValueError):ingredient_terms(value)

    def test_filter_evaluation_does_not_reward_empty_answers_for_nonempty_gold(self):
        from evaluate_metadata_filters import compliance
        self.assertEqual(compliance([],{'a','b'}),0)
        self.assertEqual(compliance([],set()),1)
        self.assertEqual(compliance([{'chunk_id':'x'}],set()),0)
        self.assertEqual(compliance([{'chunk_id':'a'},{'chunk_id':'x'}],{'a','b'}),.5)

    def test_embedding_batch_boundaries_preserve_query_order(self):
        from evaluate_metadata_filters import query_vectors
        calls=[]
        def request(req,**kwargs):
            payload=json.loads(req.data);calls.append(payload['input'])
            return {'data':[{'index':index,'embedding':[int(value),1]} for index,value in reversed(list(enumerate(payload['input'])))],'usage':{'total_tokens':1}}
        with patch('evaluate_metadata_filters.request_json',side_effect=request):
            vectors,usages=query_vectors([str(i) for i in range(12)],{'api_base_url':'https://example.invalid','requested_model':'test','dimensions':2},{'EMBEDDING_API_KEY':'private'})
        self.assertEqual([len(batch) for batch in calls],[8,4])
        self.assertEqual([row[0] for row in vectors],list(range(12)))
        self.assertEqual(len(usages),2)

    def test_explicit_constraints_are_completed_conservatively(self):
        question='只推荐必备原料至少包含土豆和菠菜中的一种的素菜。'
        self.assertEqual(explicit_question_filters(question),{'category':'vegetable_dish'})
        self.assertEqual(explicit_question_filters('推荐难度不超过2星的汤羹'),{'category':'soup','max_difficulty':2})
        for question in ('推荐简单一点的菜','推荐除了荤菜以外的菜','不是难度不超过2星，难度不要限制；推荐菜谱','推荐汤羹和荤菜','素菜难度不超过2星是真的吗？'):
            self.assertEqual(explicit_question_filters(question),{})
        self.assertEqual(explicit_question_filters('推荐素菜甲怎么做',[{'title':'素菜甲'}]),{})
        from ingredient_recommendation import recommendation_filters
        for threshold in ('2星半','2 星 半','2.5星','2点五星','2/5星'):
            self.assertEqual(explicit_question_filters('推荐难度不超过'+threshold+'的菜'),{})
            self.assertIsNone(recommendation_filters('我有鸡蛋，推荐难度不超过'+threshold+'的菜'))

    def test_observed_missing_category_is_completed_at_tool_boundary(self):
        from recipe_tools import query_recipes,load_chunks
        rows,_,_=filter_chunks(load_chunks(),category='vegetable_dish',ingredients_any=['土豆','菠菜'])
        source=rows[0]['source']
        with patch('recipe_tools._query_recipes_checked',return_value={'status':'verified','short_answer':'已核对','sources':[{'source':source}]}):
            result=query_recipes('只推荐必备原料至少包含土豆和菠菜中的一种的素菜。',ingredients_any=['土豆','菠菜'])
        self.assertEqual(result['filters']['category'],'vegetable_dish')
        self.assertEqual(result['explicit_question_filters'],{'category':'vegetable_dish'})

    def test_explicit_completion_never_weakens_inherited_filter(self):
        from recipe_tools import query_recipes
        with filter_policy(max_difficulty=1),patch('recipe_tools._query_recipes_checked') as chain:
            result=query_recipes('推荐难度不超过2星的汤羹')
        self.assertEqual(result['answer_method'],'metadata_filter_guard')
        chain.assert_not_called()


if __name__=='__main__':unittest.main()
