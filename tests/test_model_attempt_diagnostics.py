"""模型调用诊断只保存阶段信息，不带输入、回复或密钥。"""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from generate_test_answer import record_model_attempt


class ModelAttemptDiagnosticsTests(unittest.TestCase):
    def test_success_and_failure_are_separate_and_anonymous(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'attempts.jsonl'
            question = '我的密钥不应出现在记录里：secret-question'
            started = time.monotonic() - 1
            record_model_attempt(path, question, 'rerank-results/example.json',
                                 'generation', 45, started, 'ok')
            record_model_attempt(path, question, 'rerank-results/example.json',
                                 'repair_generation', 45, started, 'timeout')
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row['phase'] for row in rows], ['generation', 'repair_generation'])
            self.assertEqual([row['outcome'] for row in rows], ['ok', 'timeout'])
            self.assertTrue(all(row['elapsed_seconds'] >= 1 for row in rows))
            self.assertNotIn(question, path.read_text())
            self.assertNotIn('secret-question', path.read_text())


if __name__ == '__main__':
    unittest.main()
