import copy
import unittest
from src.capabilities import CAPABILITIES,STATES,validate_registry
from eval.degradation.matrix import evaluate,CASES


class CapabilityMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_registered_capability_and_fault_executes_correctly(self):
        rows=await evaluate()
        failures=[r for r in rows if not r['correct'] or r.get('parity') is False]
        self.assertFalse(failures,failures)
        self.assertEqual(len(rows),len(CAPABILITIES)*len(STATES))

    def test_missing_declaration_test_or_public_tool_fails_ci(self):
        from types import SimpleNamespace
        from src.agent.tools import make_tools
        run=SimpleNamespace(cfg=SimpleNamespace(memory=SimpleNamespace(mode='M6')),prepared={})
        validate_registry(public_tools=[tool.name for tool in make_tools(run)])
        registry=copy.deepcopy(CAPABILITIES)
        registry['meal_plan'].degraded_paths.pop('timeout')
        with self.assertRaisesRegex(ValueError,'capability_degradation_missing'):validate_registry(registry)
        covered={(name,state) for name in CASES for state in STATES}
        covered.remove(('meal_plan','breaker_open'))
        with self.assertRaisesRegex(ValueError,'capability_test_missing'):validate_registry(covered=covered)
        with self.assertRaisesRegex(ValueError,'capability_tool_missing'):validate_registry(public_tools=['new_user_feature'])
