import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from cookbuddy.meal_planning import meal_request,plan_meal,duration_estimate
from cookbuddy.task_context import advance_task
from cookbuddy.router import IntentRouter,DEFAULT_INTENTS
from cookbuddy.ltm.guard import policy,source_allowed
from recipe_tools import load_chunks
from check_embedding import ROOT


class MealPlanningTests(unittest.TestCase):
    question='两个人吃晚饭，30分钟内做好，推荐两菜一汤。'

    def test_composition_has_precedence_and_dedicated_route(self):
        task=advance_task(self.question,None)
        self.assertEqual(task['action'],'meal_plan')
        self.assertEqual({k:task['meal_request'][k] for k in ('people','minutes','dishes','soups')},
                         {'people':2,'minutes':30,'dishes':2,'soups':1})
        store=Mock();store.registry.return_value=[{'id':r[0],'target':r[3],'version':1} for r in DEFAULT_INTENTS]
        route=IntentRouter(store).route_task(task)
        self.assertEqual((route['intent'],route['target']),('meal_planning','meal_planner'))
        self.assertIn('两菜一汤',task['question'])

    def test_real_menu_composition_time_and_literal_sources(self):
        result=plan_meal(meal_request(self.question),load_chunks())
        self.assertEqual(result['status'],'verified')
        plan=result['meal_plan'];self.assertEqual((plan['dishes'],plan['soups'],plan['people']),(2,1,2))
        self.assertLessEqual(plan['estimated_minutes'],30)
        rows=result['recommendations'];self.assertEqual(len(rows),3)
        self.assertEqual(sum('/soup/' in r['source'] for r in rows),1)
        self.assertEqual(len({r['source'] for r in rows}),3)
        for ref in result['evidence']:
            actual=(ROOT/ref['source']).read_text().splitlines()[ref['start_line']-1:ref['end_line']]
            self.assertEqual('\n'.join(actual),ref['quote'])
        self.assertIn('时间安排',result['short_answer']);self.assertIn('份量',result['short_answer'])
        # The HTTP job exports both JSON and Markdown after pipeline completion.
        # Exercise that contract as well as the in-process answer.
        from recipe_agent import save_run
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'data/processed').mkdir(parents=True)
            with patch('recipe_agent.ROOT',root):
                path=save_run({'question':self.question,'mode':'meal_planning','status':'completed',
                               'outputs':[{'tool':'plan_meal','result':result}]})
            self.assertTrue(path.with_suffix('.md').is_file())

    def test_variants_missing_slots_and_unsupported_limits(self):
        mixed=meal_request('两人30分钟安排一荤一素一汤')
        self.assertEqual((mixed['meat'],mixed['vegetables'],mixed['soups']),(1,1,1))
        self.assertEqual(plan_meal(mixed,load_chunks())['status'],'verified')
        for q in ('两个人20分钟晚餐怎么安排？','两个人1小时晚餐怎么安排？'):
            self.assertEqual(plan_meal(meal_request(q),load_chunks())['status'],'verified')
        for q,needed in [('两个人30分钟两菜一汤，预算50元','采购价格'),
                         ('两个人30分钟两菜一汤，不要太咸','条件')]:
            r=plan_meal(meal_request(q),load_chunks())
            self.assertEqual(r['status'],'insufficient');self.assertIn(needed,r['short_answer'])
        self.assertIsNone(meal_request('推荐3道鸡肉菜'));self.assertIsNone(meal_request('基础牛奶面包怎么做'))

    def test_optional_people_and_time_give_reference_menu_without_inventing_values(self):
        for q,people in [('帮我安排晚餐。',None),('两个人晚餐怎么安排',2)]:
            result=plan_meal(meal_request(q),load_chunks())
            self.assertEqual(result['status'],'verified')
            self.assertEqual(result['meal_plan']['people'],people)
            self.assertIsNone(result['meal_plan']['minutes'])
            self.assertEqual(len(result['recommendations']),3)
            self.assertNotIn('None',result['short_answer'])
            self.assertNotIn('请在同一句',result['short_answer'])

    def test_menu_varies_and_avoids_previous_menu_within_budget(self):
        request=meal_request('两个人60分钟安排晚餐，两菜一汤')
        first=plan_meal(request,load_chunks(),seed=1)
        sources={r['source'] for r in first['recommendations']}
        second=plan_meal({**request,'previous_sources':list(sources)},load_chunks(),seed=2)
        self.assertFalse(sources & {r['source'] for r in second['recommendations']})
        self.assertLessEqual(second['meal_plan']['estimated_minutes'],60)
        menus={tuple(r['source'] for r in plan_meal(request,load_chunks(),seed=n)['recommendations']) for n in range(6)}
        self.assertGreater(len(menus),1)

    def test_soup_course_does_not_treat_catalogued_porridge_as_soup(self):
        for seed in range(10):
            result=plan_meal(meal_request('帮我安排晚餐'),load_chunks(),seed=seed)
            soup=next(r for r in result['recommendations'] if '/soup/' in r['source'])
            self.assertNotIn('粥',soup['title'])

    def test_explicit_permissions_are_not_unresolved_conditions(self):
        request=meal_request('价格先不用核实，可以用常备调料，按两菜一汤搭配。')
        request['constraints']={'unresolved':['价格先不用核实','可用常备调料','采购预算先不核实','采购总预算不超过50元']}
        self.assertEqual(plan_meal(request,load_chunks())['status'],'verified')
        request['constraints']['unresolved'].append('采购预算50元必须保证不超，同时不要太咸')
        self.assertEqual(plan_meal(request,load_chunks())['status'],'insufficient')

    def test_inherited_budget_clarification_names_an_actionable_waiver(self):
        request=meal_request('两个人60分钟安排晚餐，两菜一汤')
        request.update(question='换一套',constraints={'unresolved':['预算50元']})
        result=plan_meal(request,load_chunks())
        self.assertEqual(result['status'],'insufficient')
        self.assertIn('价格不用核实',result['short_answer'])
        self.assertNotIn('请补充这些条件',result['short_answer'])

    def test_total_time_is_not_single_step_time_or_parallel_assumption(self):
        r=plan_meal(meal_request('两个人1分钟吃晚饭，推荐两菜一汤'),load_chunks())
        self.assertEqual(r['status'],'insufficient');self.assertIn('整餐耗时',r['short_answer'])
        def row(text,section='简介'):return [{'section':section,'source_start_line':3,'content':text}]
        self.assertEqual(duration_estimate(row('全程耗时约8小时15分钟。'))['minutes'],495)
        self.assertEqual(duration_estimate(row('全程约20–30分钟。'))['minutes'],30)
        self.assertEqual(duration_estimate(row('腌制15分钟，全程约20分钟。'))['minutes'],35)
        self.assertIsNone(duration_estimate(row('烹饪时焖煮5分钟。','操作')))
        self.assertIsNone(duration_estimate(row('全程至少30分钟。')))
        self.assertIsNone(duration_estimate(row('全程半小时。')))

    def test_hard_constraints_and_current_exclusions_reach_all_courses(self):
        hard=[{'dimension':'avoid_ingredient','value':'鸡蛋','strength':'hard','polarity':'avoid'}]
        with policy(hard):r=plan_meal(meal_request(self.question),load_chunks())
        for row in r.get('recommendations',[]):self.assertTrue(source_allowed(row['source'],hard))
        r=plan_meal(meal_request('两个人30分钟晚饭，推荐两菜一汤，不要辣椒'),load_chunks())
        self.assertEqual(r['status'],'verified')
        from preference_recommendation import exclusion_pattern
        import re
        for row in r['recommendations']:
            self.assertIsNone(re.search(exclusion_pattern('辣椒'),(ROOT/row['source']).read_text()))
