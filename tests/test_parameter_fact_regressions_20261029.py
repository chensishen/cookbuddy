import unittest
from dialogue_harness import DialogueHarness
from recipe_tools import load_chunks,local_file
from recipe_fact_answer import source_fact


class ParameterFactRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_starter_parameters_after_images_are_complete_and_source_bound(self):
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包的完整原文和图片。')
        await chat.turn('只看酵头制作的图片。')
        _,result,handled=await chat.turn('酵头需要多少酵母、水温多少、发酵多久？请区分静置时间和发酵时间，并给出依据。')
        self.assertTrue(handled,'Parameter question fell through to remote generation')
        data=result['outputs'][-1]['result']
        self.assertEqual(data['status'],'verified')
        for value in ('2 g','30','静置','5 分钟','45','60 分钟'):
            self.assertIn(value,data['short_answer'])
        self.assertTrue(data['evidence'])
        self.assertEqual(len({ref['source'] for ref in data['evidence']}),1)
        for ref in data['evidence']:
            self.assertEqual(ref['quote'],'\n'.join(local_file(ref['source']).read_text().splitlines()[ref['start_line']-1:ref['end_line']]))

    async def test_baking_adjustments_after_starter_images(self):
        chat=DialogueHarness()
        await chat.turn('查看基础牛奶面包的完整原文和图片。')
        await chat.turn('只看酵头制作的图片。')
        _,result,_=await chat.turn('面包的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？')
        data=result['outputs'][-1]['result']
        self.assertEqual(data['status'],'verified')
        for value in ('180','165','30 分钟','10 分钟','温度过低','温度过高','焦糖色','锡纸'):
            self.assertIn(value,data['short_answer'])

    def test_starter_path_rejects_modified_recipe_and_hard_conflicts(self):
        from cookbuddy.ltm.guard import policy
        q='基础牛奶面包的酵头需要多少酵母、水温多少、发酵多久？'
        chunks=load_chunks()
        self.assertIsNotNone(source_fact(q,chunks))
        self.assertIsNone(source_fact(q+'不放盐。',chunks))
        self.assertIsNone(source_fact(q+'给3人吃。',chunks))
        with policy([{'dimension':'allergy','value':'鸡蛋','strength':'hard','polarity':'avoid'}]):
            self.assertIsNone(source_fact(q,chunks))

    def test_missing_adjustment_notes_do_not_count_as_complete_fact_answer(self):
        q='空气炸锅面包片的烘烤温度和时间是多少？原文有没有根据烤箱表现调整的方法？'
        self.assertIsNone(source_fact(q,load_chunks()))

    def test_starter_values_follow_source_changes_instead_of_constants(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from unittest.mock import patch
        from recipe_tools import resolve_title
        chunks=load_chunks();source=resolve_title('基础牛奶面包',chunks)[1]
        text=local_file(source).read_text().replace('酵母 2 g','酵母 5 g').replace('30 ℃','32 ℃').replace('30℃','32℃').replace('静置 5 分钟','静置 7 分钟').replace('45 ~ 60 分钟','50 ~ 70 分钟')
        with TemporaryDirectory() as folder:
            path=Path(folder)/'changed.md';path.write_text(text)
            with patch('recipe_tools.local_file',return_value=path):
                data=source_fact('基础牛奶面包的酵头需要多少酵母、水温多少、发酵多久？',chunks)
        for value in ('酵母 5 g','32 ℃','静置 7 分钟','50 ~ 70 分钟'):
            self.assertIn(value,data['short_answer'])
        for ref in data['evidence']:
            self.assertEqual(ref['quote'],'\n'.join(text.splitlines()[ref['start_line']-1:ref['end_line']]))
