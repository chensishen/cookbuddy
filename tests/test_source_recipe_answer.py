import json
import re
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from source_recipe_answer import prepare_full_recipe_answer, verbatim_recipe_requested
from recipe_tools import read_recipe
from generate_test_answer import validate_answer
from source_process_answer import prepare_section_answer
from answer_blocks import build_source_display

class FullRecipeTests(unittest.TestCase):
    def test_complete_recipe_defaults_to_summary_unless_verbatim_is_requested(self):
        self.assertFalse(verbatim_recipe_requested('请提供完整配方和步骤'))
        self.assertFalse(verbatim_recipe_requested('请提供完整步骤，不用逐字复制'))
        self.assertTrue(verbatim_recipe_requested('完整步骤，逐字展示，不删减'))
    def setUp(self):
        result=read_recipe('基础牛奶面包');self.source=result['sources'][0]['source']
        self.refs=[{'reference_id':i+1,'source':e['source'],'section':e['section'],'content':e['quote'],'content_start_line':e['start_line']} for i,e in enumerate(result['evidence'])]
    def test_full_recipe_preserves_conditions_and_all_stages_with_exact_evidence(self):
        result=prepare_full_recipe_answer('基础牛奶面包怎么制作？我是新手，请提供完整制作步骤和配方。',self.refs,self.source)
        self.assertIsNotNone(result)
        text,evidence,cited=validate_answer(json.dumps(result,ensure_ascii=False),self.refs)
        for term in ('4 天','30 分钟','45 ~ 60 分钟','将剩下的原料全部与酵头','面团','烘烤'):
            self.assertIn(term,text)
        self.assertTrue(evidence);self.assertTrue(cited)
        self.assertNotRegex(text,r'(?m)^#{1,6} .* > ')
        for title in ('面团的制作','酵头的制作','可选材料、工具及材料、工具的介绍'):
            self.assertEqual(len(re.findall(r'(?m)^#{1,6} '+title+r'\s*$',text)),1,title)
        for item in evidence:
            original=Path(self.source).read_text().splitlines()
            self.assertEqual(item['quote'],'\n'.join(original[item['start_line']-1:item['end_line']]))
    def test_changes_constraints_and_narrow_questions_still_require_qa(self):
        for q in ('完整配方，可以不放糖吗？','只有面粉，给我完整做法','不含鸡蛋的完整配方','预算有限，请调整完整做法','过敏人群适合的完整做法','清淡口味的完整步骤','两人份的完整配方，面粉多少克？','完整配方改用空气炸锅','酵头怎么制作？','只要完整步骤中的烘烤时间'):
            self.assertIsNone(prepare_full_recipe_answer(q,self.refs,self.source),q)
    def test_stage_fallback_only_includes_matching_quantities_and_main_steps(self):
        refs=[dict(r,title='基础牛奶面包') for r in self.refs]
        result=prepare_section_answer('基础牛奶面包的酵头怎么制作？',refs,self.source)
        text,evidence,_=validate_answer(json.dumps(result,ensure_ascii=False),refs)
        for term in ('面粉 1 cup','盐 2 g','30℃','45 ~ 60','体积明显增大'):self.assertIn(term,text)
        for term in ('工具的介绍','永久','面粉 2½','面团的制作'):self.assertNotIn(term,text)
        self.assertTrue(build_source_display(result,refs))
        for item in evidence:
            self.assertEqual(item['quote'],'\n'.join(Path(self.source).read_text().splitlines()[item['start_line']-1:item['end_line']]))
        for q in ('基础牛奶面包的酵头怎么制作？不放盐','基础牛奶面包的酵头怎么制作？可以减半吗？','酵头怎么制作？','基础牛奶面包的酵头为什么失败？'):
            self.assertIsNone(prepare_section_answer(q,refs,self.source),q)
    def test_named_recipe_materials_and_steps_request_has_source_fallback_but_substep_does_not(self):
        refs=[dict(r,title='基础牛奶面包') for r in self.refs]
        self.assertIsNotNone(prepare_full_recipe_answer('基础牛奶面包怎么做？请给我用料和编号步骤。',refs,self.source,compact=True))
        self.assertIsNotNone(prepare_full_recipe_answer('基础牛奶面包怎么做？\n指定菜谱原文来源：'+self.source,refs,self.source,compact=True))
        self.assertIsNone(prepare_full_recipe_answer('基础牛奶面包的酵头怎么制作？请给我用料和步骤。',refs,self.source,compact=True))
        self.assertIsNone(prepare_full_recipe_answer('基础牛奶面包怎么做？不放糖，给我用料和步骤。',refs,self.source,compact=True))
    def test_summary_modifiers_have_complete_source_fallback_without_swallowing_changes(self):
        refs=[dict(r,title='基础牛奶面包') for r in self.refs]
        for q in ('基础牛奶面包怎么做？简洁说明，但保留用量、温度、时间和关键注意事项。',
                  '基础牛奶面包怎么做？请简要说明，包含材料和步骤。'):
            result=prepare_full_recipe_answer(q,refs,self.source,compact=True)
            self.assertIsNotNone(result,q)
            text,evidence,_=validate_answer(json.dumps(result,ensure_ascii=False),refs)
            for term in ('30℃','45 ~ 60','180','165','30 分钟','硅油纸'):self.assertIn(term,text)
        for q in ('基础牛奶面包怎么做？简洁说明，改用空气炸锅。',
                  '基础牛奶面包怎么做？简洁说明，但不放糖。',
                  '基础牛奶面包怎么做？简洁说明，酵母最少多少？'):
            self.assertIsNone(prepare_full_recipe_answer(q,refs,self.source,compact=True),q)
    def test_ambiguous_or_incomplete_section_set_does_not_take_fast_path(self):
        q='完整做法和配方'
        self.assertIsNone(prepare_full_recipe_answer(q,self.refs,None))
        self.assertIsNone(prepare_full_recipe_answer(q,self.refs[:1],self.source))
        self.assertIsNone(prepare_full_recipe_answer(q,self.refs+[dict(self.refs[0],source='other.md')],self.source))
    def test_compact_source_keeps_all_recipe_conditions_but_drops_tool_encyclopedia(self):
        full=prepare_full_recipe_answer('完整配方和步骤',self.refs,self.source)
        compact=prepare_full_recipe_answer('完整配方和步骤',self.refs,self.source,compact=True)
        text,evidence,_=validate_answer(json.dumps(compact,ensure_ascii=False),self.refs)
        self.assertLess(len(text),sum(len(c['text']) for c in full['claims']))
        self.assertNotIn('烘焙是独立的美食制作门类',text)
        for term in ('30℃','45 ~ 60','180','165','奶粉','刮壁','硅油纸'):
            self.assertIn(term,text)
        self.assertTrue(evidence)

if __name__=='__main__':unittest.main()
