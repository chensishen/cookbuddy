import unittest

from dialogue_harness import DialogueHarness


class RecommendationContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_uses_session_stock_and_excludes_the_last_batch(self):
        chat = DialogueHarness()
        _, result, _ = await chat.turn('我只有茄子，推荐俩道菜')
        before = result['outputs'][0]['result']['recommendations']
        self.assertNotIn('茄子数量', before[0]['missing'])
        chat.state = None  # The separate task checkpoint is missing.
        prepared, result, _ = await chat.turn('再推荐几个')
        after = result['outputs'][0]['result']['recommendations']
        self.assertEqual(prepared['nlu']['path'], 'fast')
        self.assertEqual(len(after), 2)
        self.assertTrue(all('茄子' in row['matched'] for row in after))
        self.assertFalse({r['source'] for r in before} & {r['source'] for r in after})

    async def test_legacy_session_preserves_stock_and_first_choice(self):
        chat = DialogueHarness()
        await chat.turn('我有番茄和马铃薯，推荐两道菜，分别说说还缺什么。')
        await chat.turn('马铃薯用完了，加上鸡蛋。还是两道，推荐理由一句话就好。')
        before = chat.session.slots['last_recommendations'].copy()
        chat.state = None
        chat.session.slots.pop('recommendation_state')
        prepared, result, _ = await chat.turn('换一道，不要刚才第二道。1')
        self.assertEqual(prepared['task']['state']['ingredients'], ['番茄', '鸡蛋'])
        self.assertEqual(prepared['task']['replacement_index'], 2)
        rows = result['outputs'][0]['result']['recommendations']
        self.assertEqual(len(rows), 1)
        self.assertTrue({'番茄', '鸡蛋'} & set(rows[0]['matched']))
        self.assertEqual(chat.session.slots['last_recommendations'][0], before[0])
        self.assertNotEqual(chat.session.slots['last_recommendations'][1], before[1])

    async def test_more_without_previous_context_asks_for_conditions(self):
        _, result, _ = await DialogueHarness().turn('再推荐几个')
        self.assertEqual(result['outputs'][0]['tool'], 'clarify')

    async def test_comma_separated_inventory_survives_attached_request(self):
        chat = DialogueHarness()
        for question, stock in (
            ('我有冷饭，鸡蛋，火腿给我推荐几道菜', {'冷饭', '鸡蛋', '火腿'}),
            ('我有鸡蛋，火腿给我推荐几道菜', {'鸡蛋', '火腿'}),
            ('手头有冷饭, 鸡蛋, 火腿，帮我推荐两道菜', {'冷饭', '鸡蛋', '火腿'}),
        ):
            with self.subTest(question=question):
                prepared, result, _ = await chat.turn(question)
                state = prepared['task']['state']
                self.assertEqual(state['mode'], 'inventory')
                self.assertEqual(set(state['ingredients']), stock)
                first = result['outputs'][0]['result']['recommendations'][0]
                self.assertEqual(first['title'], '蛋炒饭')
                self.assertEqual(set(first['matched']), stock)

    async def test_rice_and_egg_recommend_both_fried_rice_recipes(self):
        _, result, _ = await DialogueHarness().turn('我有冷饭和鸡蛋推荐几个菜')
        dishes = result['outputs'][0]['result']['recommendations']
        self.assertEqual([dish['title'] for dish in dishes[:2]], ['蛋炒饭', '扬州炒饭'])
        self.assertEqual(set(dishes[1]['matched']), {'冷饭', '鸡蛋'})
        self.assertIn('午餐肉罐头', dishes[1]['missing'])
        self.assertTrue(all('coverage' not in dish for dish in dishes))
