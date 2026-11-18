import random
import unittest

from cookbuddy.ltm.retriever import personalize
from dialogue_harness import DialogueHarness


class VariedRecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_request_varies_without_reusing_old_inventory(self):
        chat = DialogueHarness()
        await chat.turn('我只有茄子，推荐两道菜。')
        batches = []
        for _ in range(3):
            prepared, result, _ = await chat.turn('给我推荐几个菜')
            self.assertEqual(prepared['nlu']['path'], 'fast')
            self.assertEqual(prepared['task']['state']['ingredients'], [])
            rows = result['outputs'][0]['result']['recommendations']
            self.assertEqual(len(rows), 3)
            batches.append({row['source'] for row in rows})
        self.assertNotEqual(batches[0], batches[1])

    def test_random_ties_never_displace_verified_preference_matches(self):
        rows = [
            {'title': '清淡菜甲', 'description': '口味清淡', 'reason': ''},
            {'title': '清淡菜乙', 'description': '口味清淡', 'reason': ''},
            {'title': '重口菜', 'description': '咸香浓郁', 'reason': ''},
        ]
        profile = {'soft_prefs': [{'dimension': 'flavor', 'value': '清淡', 'polarity': 'like'}]}
        for seed in range(5):
            result = personalize(rows, profile, k=2, rng=random.Random(seed))
            self.assertEqual({row['title'] for row in result}, {'清淡菜甲', '清淡菜乙'})
