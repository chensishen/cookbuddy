import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from recipe_fact_answer import baking_fact,ingredient_fact,procedure_fact,quantity_table_fact
from recipe_tools import load_chunks,query_recipes,local_file


class QuantityTableFactTests(unittest.TestCase):
    def test_unchanged_quantities_preserve_original_portions_ranges_and_optional_items(self):
        from acceptance_closeout import citation_metadata
        for question,terms in [('手抓饼的配方材料分别要准备多少？',('1~2','200 克','100 毫升','50 毫升')),
                ('冬瓜茶原料各要多少？',('4-5','1000g','300g')),
                ('米粥的原配方用量是多少？',('60ml-110ml','9-12','T1','Tr','可选'))]:
            with self.subTest(question=question),patch('recipe_tools.run_stage',side_effect=AssertionError('literal quantity called remote stage')):
                result=query_recipes(question)
            self.assertEqual(result['status'],'verified');self.assertEqual(result['answer_method'],'source_quantity_table')
            for term in terms:self.assertIn(term,result['short_answer'])
            self.assertEqual(citation_metadata(result,load_chunks())['rate'],1)

    def test_scaled_modified_or_combined_requests_are_not_shortcut(self):
        for question in ('手抓饼两人份材料各要多少？','手抓饼的配方材料分别要准备多少？不放盐。',
                '冬瓜茶原料各要多少？用蜂蜜替代冰糖。','米粥的原配方用量是多少？给我图片。',
                '冬瓜茶和米粥的材料各要多少？','冬瓜茶糖最少需要多少？','冬瓜茶怎么换算成10人份？'):
            self.assertIsNone(quantity_table_fact(question,load_chunks()),question)

    def test_changed_source_and_hard_constraints_are_not_bypassed(self):
        with patch('recipe_tools.checked_chunks',side_effect=ValueError('source changed')):
            with self.assertRaisesRegex(ValueError,'source changed'):quantity_table_fact('冬瓜茶原料各要多少？',load_chunks())
        with patch('cookbuddy.ltm.guard.current',return_value={'allergy':['冰糖']}),patch('cookbuddy.ltm.guard.source_allowed',return_value=False):
            self.assertIsNone(quantity_table_fact('冬瓜茶原料各要多少？',load_chunks()))

    def test_missing_calculation_is_not_filled_from_knowledge(self):
        chunks=load_chunks()
        with patch('recipe_tools.checked_chunks',return_value=[r for r in chunks if '计算' not in r['section']]):
            self.assertIsNone(quantity_table_fact('冬瓜茶原料各要多少？',chunks))

class ProcedureFactTests(unittest.TestCase):
    def test_literal_flow_paraphrases_keep_source_warnings_without_generation(self):
        for q in ('简易版炒糖色从开始制作到完成，应该按什么顺序操作？',
                  '简易版炒糖色的制作流程是什么？','简易版炒糖色按怎样的顺序操作？'):
            with patch('recipe_tools.run_stage',side_effect=AssertionError('literal flow called model')):
                result=query_recipes(q)
            self.assertEqual(result['answer_method'],'source_compact_recipe')
            for text in ('必须','400ml','5-10 秒','小火','发苦'):
                self.assertIn(text,result['short_answer'])
            from acceptance_closeout import citation_metadata
            self.assertEqual(citation_metadata(result,load_chunks())['rate'],1)
        for q in ('简易版炒糖色的制作流程是什么？不放油。',
                  '简易版炒糖色按什么顺序操作？改用蜂蜜。',
                  '简易版炒糖色从开始制作到完成，应该按什么顺序操作？给我图片。'):
            self.assertIsNone(procedure_fact(q,load_chunks()),q)

    def test_ingredient_preparation_paraphrases_preserve_tools_and_reject_modifiers(self):
        for q in ('做响油鳝丝之前，需要准备哪些食材和器具？',
                  '响油鳝丝的食材清单有哪些？','制作响油鳝丝前要备齐什么材料和工具？'):
            with patch('recipe_tools.run_stage',side_effect=AssertionError('literal ingredients called model')):
                result=query_recipes(q)
            self.assertEqual(result['answer_method'],'source_ingredient_table')
            self.assertIn('猪油',result['short_answer']);self.assertIn('鳝丝',result['short_answer'])
        for q in ('做响油鳝丝之前，需要准备哪些食材和器具？不能用猪油。',
                  '做响油鳝丝之前，需要准备哪些食材和器具？我只有鸡蛋。',
                  '做响油鳝丝之前，需要准备哪些食材和器具？同时推荐替代食材。'):
            self.assertIsNone(ingredient_fact(q,load_chunks()),q)
        with patch('cookbuddy.ltm.guard.current',return_value={'allergy':['鱼']}),patch('cookbuddy.ltm.guard.source_allowed',return_value=False):
            self.assertIsNone(ingredient_fact('响油鳝丝的食材清单有哪些？',load_chunks()))

    def test_original_read_has_complete_citation_metadata_even_when_used_for_fallback(self):
        from recipe_tools import read_recipe
        from acceptance_closeout import citation_metadata
        result=read_recipe('响油鳝丝')
        self.assertTrue(result['evidence'])
        self.assertEqual(citation_metadata(result,load_chunks())['rate'],1)

    def test_ordinary_procedures_do_not_wait_for_model_and_keep_exact_evidence(self):
        for q in ('番茄炒蛋怎么做？请给出用量和步骤。',
                  '基础牛奶面包怎么做？简洁说明，但保留用量、温度、时间和关键注意事项。'):
            with patch('recipe_tools.run_stage',side_effect=AssertionError('procedure waited for remote stage')):
                result=query_recipes(q)
            self.assertEqual(result['status'],'verified')
            self.assertEqual(result['answer_method'],'source_compact_recipe')
            self.assertTrue(result['display_source'])
            for e in result['evidence']:
                self.assertEqual(e['quote'],'\n'.join(local_file(e['source']).read_text().splitlines()[e['start_line']-1:e['end_line']]))
            if '面包' in q:
                for value in ('30℃','45 ~ 60','180','165','30 分钟','硅油纸','奶粉','可选'):
                    self.assertIn(value,result['short_answer'])
                self.assertNotIn('烘焙是独立的美食制作门类',result['short_answer'])
            else:self.assertEqual({r['title'] for r in result['sources']},{'西红柿炒鸡蛋'})

    def test_modifications_unknown_requirements_and_combined_requests_stay_in_qa(self):
        for q in ('基础牛奶面包怎么做？不放糖。','基础牛奶面包怎么做？低卡，给我用料和步骤。',
                  '基础牛奶面包怎么做？每份不超过300千卡。','基础牛奶面包怎么做？请推荐替代配料。',
                  '基础牛奶面包怎么做？展示全部图片。','基础牛奶面包的酵头怎么制作？',
                  '基础牛奶面包怎么做？西红柿炒鸡蛋怎么做？','未知面包怎么做？'):
            self.assertIsNone(procedure_fact(q,load_chunks()),q)

    def test_current_source_and_hard_constraints_are_not_bypassed(self):
        q='基础牛奶面包怎么做？'
        with patch('recipe_tools.checked_chunks',side_effect=ValueError('source changed')):
            with self.assertRaisesRegex(ValueError,'source changed'):procedure_fact(q,load_chunks())
        with patch('cookbuddy.ltm.guard.current',return_value={'allergy':['牛奶']}),patch('cookbuddy.ltm.guard.source_allowed',return_value=False):
            with patch('recipe_tools.checked_chunks',side_effect=AssertionError('blocked source read')):
                self.assertIsNone(procedure_fact(q,load_chunks()))

    def test_source_bound_request_only_removes_its_exact_source_hint(self):
        from recipe_tools import resolve_title
        source=resolve_title('基础牛奶面包',load_chunks())[1]
        result=procedure_fact('基础牛奶面包怎么做？\n指定菜谱原文来源：'+source,load_chunks())
        self.assertTrue(result)
        self.assertEqual({r['source'] for r in result['sources']},{source})
        self.assertIsNone(procedure_fact('基础牛奶面包怎么做？\n指定菜谱原文来源：other.md',load_chunks()))

class BakingFactTests(unittest.TestCase):
    def test_natural_temperature_then_time_question_is_literal(self):
        result=baking_fact('空气炸锅面包片应该用多少度、烤多久？',load_chunks())
        self.assertIsNotNone(result)
        self.assertIn('200',result['short_answer'])
        self.assertIn('5',result['short_answer'])

    def test_literal_answer_retains_core_numbers_notes_and_exact_source(self):
        with patch('recipe_tools.run_stage',side_effect=AssertionError('literal lookup called model stage')):
            result=query_recipes('基础牛奶面包要烤多久，温度是多少？')
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['answer_method'],'literal_baking_fact')
        for text in ('180','30 分钟','165','10 分钟','温度过低','温度过高','焦糖色','锡纸'):
            self.assertIn(text,result['short_answer'])
        for e in result['evidence']:
            self.assertEqual(e['quote'],'\n'.join(local_file(e['source']).read_text().splitlines()[e['start_line']-1:e['end_line']]))
    def test_modified_recipes_and_additional_questions_do_not_use_literal_path(self):
        for q in ('基础牛奶面包不放糖要烤多久，温度是多少？','基础牛奶面包用空气炸锅烤多久，温度是多少？',
                  '基础牛奶面包温度是多少，为什么？','基础牛奶面包要烤多久，温度是多少？可以省略预热吗？'):
            self.assertIsNone(baking_fact(q,load_chunks()))
    def test_excluded_source_is_not_used(self):
        self.assertIsNone(baking_fact('基础牛奶面包要烤多久，温度是多少？',[]))

    def test_hard_restriction_prevents_fact_from_forbidden_recipe(self):
        from cookbuddy.ltm.guard import policy
        with policy([{'dimension':'allergy','value':'鸡蛋','strength':'hard','polarity':'avoid'}]):
            self.assertIsNone(baking_fact('基础牛奶面包要烤多久，温度是多少？',load_chunks()))
