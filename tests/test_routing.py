"""Offline tests for the migrated, deterministic task router."""
import unittest

from sovereign_core.routing import AgentRole, infer_role, route_instruction
from sovereign_core.models import PermissionLevel


class RoutingTests(unittest.TestCase):
    def test_specialist_roles(self):
        self.assertEqual(infer_role("research competitors"), AgentRole.RESEARCH)
        self.assertEqual(infer_role("build the checkout API"), AgentRole.ENGINEERING)
        self.assertEqual(infer_role("show the bank balance"), AgentRole.FINANCE)
        self.assertEqual(infer_role("prepare a campaign for new leads"), AgentRole.SALES)
        self.assertEqual(infer_role("plan product pricing"), AgentRole.PRODUCT)
        self.assertEqual(infer_role("schedule delivery workflow"), AgentRole.OPERATIONS)
        self.assertEqual(infer_role("help desk complaint"), AgentRole.SUPPORT)
        self.assertEqual(infer_role("prepare a daily brief"), AgentRole.EXECUTIVE)

    def test_read_only_work_does_not_need_approval(self):
        task = route_instruction("research public water sensor standards")
        self.assertEqual(task.permission_level, PermissionLevel.READ_ONLY.value)
        self.assertFalse(task.requires_approval)

    def test_local_changes_are_classified_reversible(self):
        task = route_instruction("edit the README and save the draft")
        self.assertEqual(task.permission_level, PermissionLevel.REVERSIBLE.value)
        self.assertFalse(task.requires_approval)

    def test_external_and_financial_actions_require_approval(self):
        for instruction in (
            "transfer money to a supplier",
            "publish the approved article",
            "delete the old database",
            "deploy the production service",
            "shut off the water valve",
        ):
            with self.subTest(instruction=instruction):
                task = route_instruction(instruction)
                self.assertEqual(task.permission_level, PermissionLevel.HIGH_IMPACT.value)
                self.assertTrue(task.requires_approval)

    def test_empty_instruction_is_rejected(self):
        with self.assertRaises(ValueError):
            route_instruction("   ")


if __name__ == "__main__":
    unittest.main()
