"""Tests for the local, provenance-aware memory store."""
import json
import tempfile
import unittest
from pathlib import Path

from sovereign_core.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "memory.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_remember_and_workspace_isolation(self):
        saved = self.store.remember("project-a", "Use SQLite as the local memory store",
                                    source="user instruction", confidence=1.0)
        self.assertEqual(saved.revision, 1)
        self.assertEqual(len(self.store.search("project-a", "SQLite memory")), 1)
        self.assertEqual(self.store.search("project-b", "SQLite"), [])

    def test_revisions_preserve_prior_content_and_provenance(self):
        saved = self.store.remember("p1", "Initial project preference", source="conversation")
        updated = self.store.revise(saved.id, "Updated project preference",
                                    source="user correction", confidence=0.95, verified=True)
        self.assertEqual(updated.revision, 2)
        history = self.store.history(saved.id)
        self.assertEqual([item.content for item in history],
                         ["Initial project preference", "Updated project preference"])
        self.assertEqual(history[1].source, "user correction")
        self.assertTrue(history[1].verified)

    def test_supersede_hides_old_record_but_retains_history(self):
        old = self.store.remember("p1", "The old deployment target")
        new = self.store.supersede(old.id, "The new deployment target", source="user correction")
        self.assertEqual(new.supersedes_id, old.id)
        self.assertEqual(self.store.get(old.id).status, MemoryStore.SUPERSEDED)
        self.assertEqual([item.id for item in self.store.list_memories("p1")], [new.id])
        self.assertEqual(self.store.history(old.id)[-1].status, MemoryStore.SUPERSEDED)

    def test_search_can_require_verified_memories(self):
        self.store.remember("p1", "The water sensor uses a float switch",
                            source="unverified note", verified=False)
        confirmed = self.store.remember("p1", "The water sensor uses a pressure sensor",
                                        source="bench test", verified=True, confidence=0.99)
        results = self.store.search("p1", "water sensor", verified_only=True)
        self.assertEqual([item.id for item in results], [confirmed.id])

    def test_retract_removes_from_retrieval(self):
        saved = self.store.remember("p1", "Temporary instruction")
        self.store.retract(saved.id)
        self.assertEqual(self.store.search("p1", "Temporary"), [])
        self.assertEqual(self.store.get(saved.id).status, MemoryStore.RETRACTED)

    def test_delete_permanently_removes_record_and_history(self):
        saved = self.store.remember("p1", "Sensitive draft")
        self.assertTrue(self.store.delete(saved.id))
        self.assertIsNone(self.store.get(saved.id))
        self.assertEqual(self.store.history(saved.id), [])
        self.assertFalse(self.store.delete(saved.id))

    def test_delete_superseded_record_preserves_successor(self):
        old = self.store.remember("p1", "Old fact")
        new = self.store.supersede(old.id, "Corrected fact", source="user correction")
        self.assertTrue(self.store.delete(old.id))
        self.assertIsNone(self.store.get(old.id))
        self.assertEqual(self.store.get(new.id).supersedes_id, None)

    def test_export_is_valid_json_and_includes_revisions(self):
        saved = self.store.remember("p1", "Exportable memory")
        payload = json.loads(self.store.export_json("p1"))
        self.assertEqual(payload["workspace_id"], "p1")
        self.assertEqual(payload["memories"][0]["id"], saved.id)
        self.assertEqual(len(payload["revisions"]), 1)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.remember("p1", " ")
        with self.assertRaises(ValueError):
            self.store.remember("p1", "fact", confidence=1.5)
        with self.assertRaises(ValueError):
            self.store.search("p1", "fact", limit=0)


if __name__ == "__main__":
    unittest.main()
