"""Regression guards for the shared SovereignOS desktop identity and shell mode."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DesktopShellContractTests(unittest.TestCase):
    def test_desktop_header_and_core_use_the_same_master_svg(self):
        source = (ROOT / "sovereign_os_ultimate.py").read_text(encoding="utf-8")
        self.assertIn('app.add_static_files("/sovereign-assets", str(MASTER_BRAND_ASSET))', source)
        self.assertIn('src="/sovereign-assets/sovereign-os-mark.svg"', source)
        self.assertIn('desktop-brand-mark" src="/sovereign-assets/sovereign-os-mark.svg"', source)

    def test_master_mark_preserves_adopted_layering(self):
        mark = (ROOT / "clients" / "web" / "public" / "sovereign-os-mark.svg").read_text(encoding="utf-8")
        self.assertIn("SovereignOS official SO monogram", mark)
        self.assertIn("O visibly overlaps the S", mark)
        self.assertIn("O is deliberately rendered second", mark)

    def test_web_and_installed_pwa_use_the_same_master_mark(self):
        index = (ROOT / "clients" / "web" / "index.html").read_text(encoding="utf-8")
        manifest = (ROOT / "clients" / "web" / "public" / "manifest.webmanifest").read_text(encoding="utf-8")
        self.assertIn('rel="icon" href="./sovereign-os-mark.svg"', index)
        self.assertIn('"src":"./sovereign-os-mark.svg"', manifest)

    def test_native_window_has_safe_browser_fallback(self):
        source = (ROOT / "sovereign_os_ultimate.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("SOVEREIGN_NATIVE_WINDOW", "auto")', source)
        self.assertIn("webview_available = importlib.util.find_spec(\"webview\") is not None", source)
        self.assertIn("native=native_mode", source)
        self.assertIn("show=not native_mode", source)


if __name__ == "__main__":
    unittest.main()
