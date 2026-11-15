"""Tests for the provider-neutral workspace/memory service."""
import tempfile
import unittest
from pathlib import Path

from sovereign_core.workspaces import WorkspaceService


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = WorkspaceService(Path(self.temp.name) / "sovereign.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_default_workspaces_are_created_idempotently(self):
        first = self.service.ensure_default_workspaces()
        second = self.service.ensure_default_workspaces()
        self.assertEqual([w.id for w in first], [w.id for w in second])
        self.assertTrue({"General", "Nikolai Creative Studio", "Yard Truths",
                         "SmartFlow BW", "SovereignOS"}.issubset({w.name for w in second}))

    def test_create_workspace_is_case_insensitive_and_idempotent(self):
        one = self.service.create_workspace("Project X", "First purpose")
        two = self.service.create_workspace("project x", "Ignored duplicate purpose")
        self.assertEqual(one.id, two.id)
        self.assertEqual(len([w for w in self.service.list_workspaces()
                              if w.name.casefold() == "project x"]), 1)

    def test_select_options_use_workspace_ids_as_values(self):
        workspaces = self.service.ensure_default_workspaces()
        options = self.service.select_options()
        self.assertEqual(set(options), {workspace.id for workspace in workspaces})
        self.assertEqual(options[next(w.id for w in workspaces if w.name == "SovereignOS")], "SovereignOS")

    def test_memory_is_scoped_to_workspace(self):
        a = self.service.create_workspace("Alpha")
        b = self.service.create_workspace("Beta")
        saved = self.service.remember(a.id, "Alpha uses SQLite", source="user")
        self.assertEqual([m.id for m in self.service.search(a.id, "SQLite")], [saved.id])
        self.assertEqual(self.service.search(b.id, "SQLite"), [])

    def test_correct_history_and_export_stay_in_scope(self):
        a = self.service.create_workspace("Alpha")
        b = self.service.create_workspace("Beta")
        old = self.service.remember(a.id, "Old deployment target")
        new = self.service.correct(a.id, old.id, "New deployment target")
        self.assertEqual(new.supersedes_id, old.id)
        self.assertEqual(len(self.service.history(a.id, old.id)), 2)
        with self.assertRaises(ValueError):
            self.service.history(b.id, old.id)
        self.assertIn('"workspace_id": "' + a.id + '"', self.service.export_json(a.id))

    def test_forget_rejects_cross_workspace_memory_id(self):
        a = self.service.create_workspace("Alpha")
        b = self.service.create_workspace("Beta")
        saved = self.service.remember(a.id, "Private Alpha detail")
        with self.assertRaises(ValueError):
            self.service.forget(b.id, saved.id)
        self.assertIsNotNone(self.service.memory.get(saved.id))

    def test_verify_is_workspace_scoped(self):
        a = self.service.create_workspace("Alpha")
        b = self.service.create_workspace("Beta")
        saved = self.service.remember(a.id, "Confirm this detail")
        verified = self.service.verify(a.id, saved.id)
        self.assertTrue(verified.verified)
        with self.assertRaises(ValueError):
            self.service.verify(b.id, saved.id)

    def test_unknown_workspace_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.search("not-a-workspace", "anything")


if __name__ == "__main__":
    unittest.main()
