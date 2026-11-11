"""Regressions from the 2026-11-09 full browser walkthrough."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.ltm.extractor import rule_extract
from cookbuddy.ltm.management import handle_command
from cookbuddy.ltm.profile_store import ProfileStore
from cookbuddy.meal_planning import meal_conditions
from cookbuddy.task_context import advance_task
from src.dialogue import public_reply
from src.nlu.router import understand


class BrowserIssueTests(unittest.TestCase):
    def test_combined_positive_and_negative_preferences_are_both_saved(self):
        rows=rule_extract('请记住：我喜欢黑胡椒，不喜欢芹菜。')
        self.assertEqual(
            {(row.dimension,row.value,row.polarity) for row in rows},
            {('flavor','黑胡椒','like'),('avoid_ingredient','芹菜','dislike')}
        )
        with tempfile.TemporaryDirectory() as folder:
            prefs=ProfileStore(path=Path(folder)/'memory.db')
            for index,row in enumerate(rows):
                prefs.apply('u','s','s:'+str(index),row)
            message=handle_command(('read',''),prefs,'u','s','read')['message']
            self.assertIn('口味：黑胡椒',message)
            self.assertIn('不太喜欢芹菜',message)

    def test_inventory_edits_keep_the_literal_requested_count(self):
        first=advance_task('我有番茄和鸡蛋，推荐两道菜，说明还缺什么。',None)['state']
        second=advance_task('这次加上土豆，挑三道，分别说特点、推荐理由和缺料。',first)
        self.assertEqual(second['action'],'recommend')
        self.assertEqual(second['state']['limit'],3)
        self.assertEqual(second['state']['ingredients'],['番茄','鸡蛋','土豆'])
        updated=advance_task('鸡蛋已经用完了，这轮别按有鸡蛋算。',second['state'])
        self.assertEqual(updated['action'],'recommend')
        self.assertEqual(updated['state']['limit'],3)
        self.assertEqual(updated['state']['ingredients'],['番茄','土豆'])
        self.assertEqual(updated['state']['extra_constraints'],[])

    def test_short_reason_request_is_an_output_field(self):
        task=advance_task('按我的口味推荐两道菜，说明理由。',None)
        self.assertEqual(task['action'],'recommend')
        self.assertEqual(task['state']['limit'],2)
        self.assertEqual(task['state']['extra_constraints'],[])
        self.assertIn('reason',task['state']['response_fields'])

    def test_out_of_range_five_item_list_returns_all_five_choices(self):
        choices=[{'dish_name':'菜'+str(i),'source_path':str(i)+'.md'} for i in range(1,6)]
        result=understand('只看第九道。',{
            'slots':{'last_recommendations':choices},
            'previous_task':{'choices':choices,'limit':5}
        },use_model=False)
        self.assertEqual(result['answer_mode'],'clarify')
        self.assertEqual(len(result['options']),5)
        self.assertIn('5. 菜5',result['options'])

    def test_thanks_with_extra_words_keeps_a_thanks_reply(self):
        reply=public_reply({
            'answer_mode':'general_chat',
            'intent':'chitchat',
            'normalized':'谢谢，刚才的回答先留着。'
        })
        self.assertTrue(reply.startswith('不客气'))

    def test_price_waiver_does_not_leave_structural_word(self):
        question='价格先不用核实，可以用常备调料，按两菜一汤搭配。'
        _,residual=meal_conditions(question)
        self.assertEqual(residual,'')
        result=understand(question,{
            'slots':{'meal_request':{
                'meal_type':'dinner','people':2,'minutes':30,'dishes':2,'soups':1,
                'explicit_composition':True
            }}
        },use_model=False)
        self.assertEqual(result['intent'],'meal_plan')
        self.assertEqual(result['slots']['constraints'].get('unresolved',[]),[])

    def test_meal_person_change_is_local_and_preserves_other_slots(self):
        question='改成三个人，时间仍然30分钟，其他要求不变。'
        result=understand(question,{
            'slots':{'meal_request':{
                'meal_type':'dinner','people':2,'minutes':30,'dishes':2,'soups':1,
                'explicit_composition':True
            }}
        },use_model=False)
        self.assertEqual(result['intent'],'meal_plan')
        self.assertEqual(result['slots']['meal']['people'],3)
        self.assertEqual(result['slots']['meal']['minutes'],30)
        self.assertEqual(result['slots']['meal']['dishes'],2)
        self.assertEqual(result['slots']['meal']['soups'],1)

    def test_budget_addition_is_local_and_kept_for_clarification(self):
        question='采购预算总共50元，必须保证不超。'
        result=understand(question,{
            'slots':{'meal_request':{
                'meal_type':'dinner','people':2,'minutes':30,'dishes':2,'soups':1,
                'explicit_composition':True
            }}
        },use_model=False)
        self.assertEqual(result['intent'],'meal_plan')
        unresolved=result['slots']['constraints'].get('unresolved',[])
        self.assertTrue(unresolved)
        self.assertIn('50',unresolved[0])

    def test_substitution_boundary_is_answered_from_exact_source(self):
        from recipe_fact_answer import substitution_fact
        from recipe_tools import query_recipes
        from recipe_tools import load_chunks
        question='基础牛奶面包里的黄油能用橄榄油替换吗？原文没说明的请明确指出。'
        result=substitution_fact(question,load_chunks())
        self.assertEqual(result['status'],'verified')
        self.assertIn('没有提到',result['short_answer'])
        self.assertIn('橄榄油',result['short_answer'])
        self.assertIn('黄油或玉米油',result['short_answer'])
        self.assertTrue(result['evidence'])
        with patch('recipe_tools.run_stage',side_effect=AssertionError('substitution called model')):
            direct=query_recipes(question)
        self.assertEqual(direct['answer_method'],'literal_substitution_boundary')

    def test_unmatched_dish_name_excludes_request_wrapping(self):
        result=understand('给我火星蓝晶炖月光的完整菜谱和来源。',use_model=False)
        self.assertEqual(result.get('unmatched_dish'),'火星蓝晶炖月光')

    def test_meal_followup_chain_uses_local_parser_without_model(self):
        from dialogue_harness import DialogueHarness

        async def run():
            chat=DialogueHarness()
            await chat.turn('两个人，30分钟，想做两菜一汤。')
            prepared,result,_=await chat.turn('改成三个人，时间仍然30分钟，其他要求不变。')
            data=result['outputs'][-1]['result']
            self.assertEqual(prepared['nlu']['path'],'fast')
            self.assertEqual((data['meal_plan']['people'],data['meal_plan']['minutes']),(3,30))
            prepared,result,_=await chat.turn('采购预算总共50元，必须保证不超。')
            self.assertEqual(prepared['nlu']['path'],'fast')
            self.assertEqual(result['outputs'][-1]['result']['status'],'insufficient')
            prepared,result,_=await chat.turn('价格先不用核实，可以用常备调料，按两菜一汤搭配。')
            data=result['outputs'][-1]['result']
            self.assertEqual(prepared['nlu']['path'],'fast')
            self.assertEqual(data['status'],'verified')
            self.assertEqual(data['meal_plan']['people'],3)

        asyncio.run(run())


if __name__=='__main__':unittest.main()
