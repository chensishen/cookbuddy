import asyncio
import unittest

from dialogue_harness import DialogueHarness


class ExactPromptRegressionTests(unittest.TestCase):
    def test_source_questions_answer_the_requested_scope(self):
        async def run():
            chat=DialogueHarness()
            cases=[
                ('核对凉拌黄瓜用料。我只有黄瓜和醋，把已有、缺少和可选用料分开。',
                 'recipe_ingredients',('已经有','黄瓜','醋','可选用料')),
                ('凉拌黄瓜做五份，各种用料需要多少？请给出原文公式或依据。',
                 'source_fact',('1000克','27.5ml','17.5ml','15瓣','1.4克','15ml','固定量')),
                ('凉拌黄瓜做三份，白糖需要多少？原文没写换算方法的话请直说。',
                 'source_fact',('白糖5克','没有按份数的白糖公式')),
                ('宫保鸡丁里的花生需要多少？可以用腰果替换吗？原文没说明的请明确指出。',
                 'source_fact',('150g','没有提到','腰果')),
            ]
            for question,tool,expected in cases:
                _,result,_=await chat.turn(question)
                output=result['outputs'][0]
                self.assertEqual(output['tool'],tool,output)
                data=output['result']
                for phrase in expected:
                    self.assertIn(phrase,data['short_answer'])
                self.assertTrue(data['evidence'])
        asyncio.run(run())

    def test_section_list_and_selection_stay_with_recommended_recipe(self):
        async def run():
            chat=DialogueHarness()
            _,result,_=await chat.turn('我有鸡蛋，推荐一道菜。')
            recipe=result['outputs'][0]['result']['recommendations'][0]
            for _ in range(2):
                _,result,_=await chat.turn('这道有哪些章节？')
                self.assertIn('操作',result['outputs'][0]['result']['clarification_options'])
            _,result,_=await chat.turn('操作')
            data=result['outputs'][0]['result']
            self.assertTrue(data['sources'])
            self.assertTrue(all(row['title']==recipe['title'] for row in data['sources']))
        asyncio.run(run())

    def test_lunch_and_dinner_are_distinct_under_the_same_time_budget(self):
        from cookbuddy.meal_planning import meal_request, plan_meal
        from recipe_tools import load_chunks

        rows=load_chunks()
        menus=[]
        for meal in ('午餐','晚餐'):
            request=meal_request(f'帮我安排两个人的{meal}，30分钟，想做两菜一汤。')
            result=plan_meal(request,rows)
            self.assertTrue(result['meal_plan']['complete'])
            self.assertLessEqual(result['meal_plan']['estimated_minutes'],30)
            menus.append({r['source'] for r in result['recommendations']})
        self.assertNotEqual(*menus)

    def test_meal_revision_preserves_soup_and_changes_one_dish(self):
        async def run():
            chat=DialogueHarness()
            await chat.turn('帮我安排两个人的晚餐，想做三菜一汤。')
            prepared,result,_=await chat.turn('60分钟。')
            self.assertEqual(prepared['nlu']['path'],'fast')
            self.assertEqual(result['outputs'][0]['result']['meal_plan']['minutes'],60)
            before=result['outputs'][0]['result']['recommendations']
            _,result,_=await chat.turn('改成三个人，时间不变，只换一道菜，汤保留。')
            data=result['outputs'][0]['result']
            after=data['recommendations']
            self.assertEqual(len(before),len(after))
            self.assertEqual(sum(x['source']!=y['source'] for x,y in zip(before,after)),1)
            self.assertEqual([r['source'] for r in before if r['role']=='soup'],
                             [r['source'] for r in after if r['role']=='soup'])
            self.assertEqual((data['meal_plan']['people'],data['meal_plan']['minutes']),(3,60))
        asyncio.run(run())
