import copy
import unittest
from cookbuddy.meal_planning import meal_request,plan_meal
from cookbuddy.menu_planner import resolve_template,templates,validate_menu,MenuPlanner
from cookbuddy.recommendation_schema import new_state
from cookbuddy.ltm.guard import policy,source_allowed
from recipe_tools import load_chunks
from check_embedding import ROOT


class MenuTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.chunks=load_chunks()

    def test_templates_time_difficulty_category_and_literal_citations(self):
        for q,kind in [('安排早餐','breakfast'),('安排午饭','lunch'),('安排晚饭','dinner'),('安排夜宵','snack_late')]:
            with self.subTest(kind=kind):
                request=meal_request(q);result=plan_meal(request,self.chunks)
                self.assertEqual(result['status'],'verified')
                plan=result['meal_plan'];self.assertEqual(plan['meal_type'],kind)
                _,template,_=resolve_template(request)
                allowed={s['slot_id']:set(s['categories']) for s in template['slots']}
                primary=set()
                for row in result['recommendations']:
                    self.assertIn(row['category'],allowed[row['slot_id']])
                    self.assertLessEqual(row['difficulty'],template['max_difficulty'])
                    self.assertFalse(primary & set(row['primary_ingredients']))
                    primary.update(row['primary_ingredients'])
                    self.assertTrue(row['evidence']);self.assertTrue((ROOT/row['source']).is_file())
                    if kind in ('breakfast','snack_late'):
                        self.assertNotRegex((ROOT/row['source']).read_text(),templates()[kind]['exclude_pattern'])
                if template['budget']:self.assertLessEqual(plan['estimated_minutes'],template['budget'])
                for ref in result['evidence']:
                    original=(ROOT/ref['source']).read_text().splitlines()
                    self.assertEqual('\n'.join(original[ref['start_line']-1:ref['end_line']]),ref['quote'])

    def test_people_scale_and_explicit_counts_override_scaling(self):
        four=plan_meal(meal_request('4人60分钟安排午餐'),self.chunks)
        self.assertEqual(len(four['recommendations']),4)
        one=plan_meal(meal_request('1人60分钟安排午餐'),self.chunks)
        self.assertNotIn('soup',[r['role'] for r in one['recommendations']])
        explicit=plan_meal(meal_request('1人60分钟午餐安排两菜一汤'),self.chunks)
        self.assertEqual(len(explicit['recommendations']),3)
        self.assertEqual(explicit['meal_plan']['soups'],1)

    def test_inventory_is_preference_across_menu_and_reports_missing(self):
        request={**meal_request('安排午餐'),'ingredients':['鸡蛋','番茄'],'constraints':{},'mode':'inventory'}
        result=plan_meal(request,self.chunks)
        rows=result['recommendations']
        self.assertTrue(any(r['matched'] for r in rows))
        self.assertTrue(any(r['missing'] for r in rows))
        self.assertTrue(any(not {'鸡蛋','番茄'}<=set(r['matched']) for r in rows))

    def test_hard_avoid_postcheck_and_partial_menu_never_wrong_category(self):
        hard=[{'dimension':'avoid_ingredient','value':'鸡蛋','strength':'hard','polarity':'avoid'}]
        with policy(hard):result=plan_meal(meal_request('安排早餐'),self.chunks)
        for row in result.get('recommendations',[]):self.assertTrue(source_allowed(row['source'],hard))
        only_veg=[r for r in self.chunks if '/vegetable_dish/' in r['source']]
        result=plan_meal(meal_request('安排午餐'),only_veg)
        self.assertFalse(result['meal_plan']['complete'])
        self.assertIn('main',result['meal_plan']['missing_roles'])
        self.assertIn('soup',result['meal_plan']['missing_roles'])
        self.assertTrue(result['recommendations'])
        self.assertTrue(all(r['category']=='vegetable_dish' for r in result['recommendations']))
        self.assertIn('缺少',result['short_answer'])

    def test_clock_boundary_is_explicit_and_planner_is_deterministic(self):
        request={**meal_request('今天吃什么'),'local_hour':7,'constraints':{}}
        first=plan_meal(request,self.chunks);second=plan_meal(request,self.chunks)
        self.assertEqual(first,second)
        self.assertEqual(first['meal_plan']['meal_type'],'breakfast')
        self.assertIn('本地时间',first['short_answer'])
        self.assertIsNone(first['meal_plan']['people'])
        self.assertIsNone(first['meal_plan']['minutes'])

    def test_independent_validator_rejects_tampered_source_category_time_and_quote(self):
        request=meal_request('安排午餐');_,template,_=resolve_template(request)
        state=new_state();state['limit']=10
        rows,_=MenuPlanner().plan(request,self.chunks,state,template)
        grouped={}
        for chunk in self.chunks:grouped.setdefault(chunk['source'],[]).append(chunk)
        for mutation in ('quote','time','category','duplicate'):
            broken=copy.deepcopy(rows)
            if mutation=='quote':broken[0]['evidence'][0]['quote']='invented'
            if mutation=='time':broken[0]['duration']['minutes']=0
            if mutation=='category':broken[0]['slot_id']='soup_1'
            if mutation=='duplicate':broken.append(broken[0])
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_menu(broken,template,grouped,state)
