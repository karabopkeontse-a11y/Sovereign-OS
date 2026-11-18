import tempfile
import unittest
from pathlib import Path

from sovereign_core.agent_tools import (
    execute_workspace_tool, validate_tool_input, verify_workspace_tool, workspace_root,
)


class AgentWorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "sovereign.sqlite3"
        self.root = workspace_root(self.database, "workspace-a")

    def tearDown(self):
        self.temp.cleanup()

    def test_workspace_roots_are_isolated(self):
        other = workspace_root(self.database, "workspace-b")
        self.assertNotEqual(self.root, other)
        self.assertEqual(self.root.parent, other.parent)

    def test_list_files_only_returns_supported_text_files(self):
        (self.root / "brief.md").write_text("hello", encoding="utf-8")
        (self.root / "secret.env").write_text("not listed", encoding="utf-8")
        result = execute_workspace_tool("workspace.list_files", {}, self.root)
        self.assertEqual(result["files"], ["brief.md"])
        self.assertEqual(result["count"], 1)
        self.assertTrue(verify_workspace_tool("workspace.list_files", {}, result, self.root)[0])

    def test_read_file_is_confined_to_workspace(self):
        (self.root / "brief.md").write_text("workspace content", encoding="utf-8")
        result = execute_workspace_tool("workspace.read_file", {"path": "brief.md"}, self.root)
        self.assertEqual(result["content"], "workspace content")
        with self.assertRaises(ValueError):
            validate_tool_input("workspace.read_file", {"path": "../outside.md"})
        with self.assertRaises(ValueError):
            validate_tool_input("workspace.read_file", {"path": "..\\outside.md"})

    def test_create_note_is_new_only_and_readback_verified(self):
        args = {"filename": "approved-draft.md", "content": "# Approved draft\nLocal only."}
        result = execute_workspace_tool("workspace.create_note", args, self.root)
        self.assertTrue(result["created"])
        self.assertTrue(verify_workspace_tool("workspace.create_note", args, result, self.root)[0])
        self.assertEqual((self.root / "approved-draft.md").read_text(encoding="utf-8"), args["content"])
        with self.assertRaises(FileExistsError):
            execute_workspace_tool("workspace.create_note", args, self.root)

    def test_unknown_tools_and_unsafe_note_names_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_tool_input("shell.execute", {"command": "whoami"})
        with self.assertRaises(ValueError):
            validate_tool_input("workspace.create_note", {
                "filename": "../outside.md", "content": "not allowed"
            })

    def test_oversized_file_is_not_read(self):
        (self.root / "large.txt").write_text("x" * 1_000_001, encoding="utf-8")
        with self.assertRaises(ValueError):
            execute_workspace_tool("workspace.read_file", {"path": "large.txt"}, self.root)


if __name__ == "__main__":
    unittest.main()
