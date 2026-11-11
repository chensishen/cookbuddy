import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from ingredient_answer_checks import check_optional_ingredients, ingredient_list_answer, inventory_recommendation

class OptionalTests(unittest.TestCase):
    def check(self,text):
        return check_optional_ingredients(json.dumps({'claims':[{'text':text,'evidence':[{'reference_id':1}]}]}),
          [{'reference_id':1,'section':'必备原料和工具','content':'* 盐\n* 糖（可选）\n* 葱花（可选）'}], '可以做什么菜？')
    def test_optional_not_missing(self):
        with self.assertRaises(ValueError):self.check('还缺糖和葱花。')
    def test_optional_label_accepted(self):self.check('已有西红柿和鸡蛋，还缺油和盐；糖和葱花可选。')
    def test_mandatory_not_forced_optional(self):self.check('还缺盐。')

class RequiredTests(unittest.TestCase):
    def check(self,text):
        from ingredient_answer_checks import check_required_ingredients
        check_required_ingredients(json.dumps({'claims':[{'text':text,'evidence':[{'reference_id':1}]}]}),
          [{'reference_id':1,'title':'汤','section':'必备原料和工具','content':'- 西红柿\n- 鸡蛋\n- 盐\n- 葱、姜、蒜'}],
          '我只有西红柿和鸡蛋，没有其他食材，可以做什么菜？')
    def test_missing_salt_rejected(self):
        with self.assertRaises(ValueError):self.check('还缺葱、姜、蒜。')
    def test_full_required_passes(self):self.check('还缺盐、葱、姜、蒜。')


class ComparisonTests(unittest.TestCase):
    def table(self):return [{'reference_id':1,'title':'西红柿炒鸡蛋','section':'必备原料和工具','content_start_line':10,'content':'* 西红柿\n* 鸡蛋\n* 食用油\n* 盐\n* 糖（可选）\n* 葱花（可选）'}]
    def test_missing_and_optional_separated(self):
        from ingredient_answer_checks import inventory_recommendation
        row=inventory_recommendation('我只有西红柿和鸡蛋，可以做什么菜？',self.table())
        text=row['claims'][0]['text']
        self.assertIn('还缺：食用油、盐',text)
        self.assertIn('可选配料：糖（可选）；葱花（可选）',text)
    def test_all_required_present(self):
        from ingredient_answer_checks import inventory_recommendation
        text=inventory_recommendation('我有西红柿、鸡蛋、食用油和盐，可以考虑什么菜？',self.table())['claims'][0]['text']
        self.assertIn('所列必备食材已覆盖',text)
        self.assertNotIn('还缺',text)
    def test_allergy_uses_model_route(self):
        from ingredient_answer_checks import inventory_recommendation
        self.assertIsNone(inventory_recommendation('我有西红柿和鸡蛋，鸡蛋过敏，推荐一道菜。',self.table()))

    def test_named_recipe_inventory_keeps_unspecified_subtype_and_alternative(self):
        row=[{'reference_id':1,'title':'麻婆豆腐','source':'recipe.md','section':'必备原料和工具',
              'content_start_line':10,'content':'- 内脂豆腐\n- 五花肉（肉糜也行）\n- 大蒜\n- 小米椒（可选）\n- 水果刀'}]
        result=inventory_recommendation('我有豆腐和猪肉末，想做麻婆豆腐；按原方还需确认哪些配料？',row)
        text=result['claims'][0]['text']
        self.assertIn('未说明是否有：内脂豆腐、五花肉（肉糜也行）、大蒜',text)
        self.assertIn('可选配料：小米椒',text)
        self.assertIn('未说明的工具：水果刀',text)

    def test_condiment_query_separates_table_and_calculation(self):
        rows=[{'reference_id':1,'title':'酸辣土豆丝','source':'recipe.md','section':'必备原料和工具',
               'content_start_line':10,'content':'- 土豆\n- 青椒\n- 生抽\n- 陈醋\n- 盐'},
              {'reference_id':2,'title':'酸辣土豆丝','source':'recipe.md','section':'计算',
               'content_start_line':30,'content':'- 食用油 10-15ml'}]
        result=ingredient_list_answer('酸辣土豆丝原文需要哪些调料？',rows)
        text=result['claims'][0]['text']
        self.assertIn('调味料：生抽、陈醋、盐',text)
        self.assertIn('计算章节还列出：食用油 10-15ml',text)
        self.assertIn('其他配料：青椒',text)
        self.assertEqual(result['claims'][0]['evidence'][-1],{'reference_id':2,'start_line':30,'end_line':30})
        self.assertIsNone(ingredient_list_answer('其他菜原文需要哪些调料？',rows))

if __name__=='__main__':unittest.main()
