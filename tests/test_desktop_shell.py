"""Regression checks for the native-feeling desktop shell and shared brand asset."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "sovereign_os_ultimate.py"
MASTER_MARK = ROOT / "clients" / "web" / "public" / "sovereign-os-mark.svg"


class DesktopShellTests(unittest.TestCase):
    def test_master_so_mark_is_shared_with_desktop(self):
        self.assertTrue(MASTER_MARK.is_file(), "The single approved SO SVG must exist.")
        svg = MASTER_MARK.read_text(encoding="utf-8")
        self.assertIn("<svg", svg.lower())
        source = DESKTOP.read_text(encoding="utf-8")
        self.assertIn('BASE_DIR / "clients" / "web" / "public"', source)
        self.assertIn('"/sovereign-assets"', source)
        self.assertIn('src="/sovereign-assets/sovereign-os-mark.svg"', source)

    def test_desktop_has_os_command_surface(self):
        source = DESKTOP.read_text(encoding="utf-8")
        self.assertIn("sovereign-desktop-dock", source)
        self.assertIn("sovereign-command-palette", source)
        self.assertIn("desktop-command", source)
        self.assertIn("window.sovereignDesktopClock", source)


if __name__ == "__main__":
    unittest.main()
