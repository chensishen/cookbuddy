import unittest
from eval.degradation.faults import FaultyModel
from src.nlu.router import understand


class IncidentReproduction(unittest.TestCase):
    def test_breakfast_is_routable_while_breaker_open(self):
        for question in ('安排早餐','安排午餐','安排早饭','安排晚餐'):
            with self.subTest(question=question):
                result=understand(question,{},model_call=FaultyModel('breaker_open',{}))
                self.assertEqual(result['intent'],'meal_plan')
