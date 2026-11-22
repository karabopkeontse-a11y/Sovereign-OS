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

    def test_2050_chat_surface_has_fixed_composer_and_desktop_rail(self):
        source = (ROOT / "sovereign_os_ultimate.py").read_text(encoding="utf-8")
        self.assertIn("2050 intelligence workspace: visual system upgrade", source)
        self.assertIn(".chat-input-row{flex:0 0 auto!important", source)
        self.assertIn(".sovereign-desktop-dock{left:18px!important", source)
        self.assertIn(".chat-bubble-assistant{", source)
        self.assertIn(".desktop-brand-mark{", source)

    def test_native_window_has_safe_browser_fallback(self):
        source = (ROOT / "sovereign_os_ultimate.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("SOVEREIGN_NATIVE_WINDOW", "auto")', source)
        self.assertIn("webview_available = importlib.util.find_spec(\"webview\") is not None", source)
        # Native mode must launch through PyWebView.
        self.assertIn("if native_mode:", source)
        self.assertIn("webview.create_window(", source)
        self.assertIn("webview.start(", source)

        # Missing PyWebView or invalid configuration must fall back safely.
        self.assertIn("if native_mode and not webview_available:", source)
        self.assertIn("Unrecognised SOVEREIGN_NATIVE_WINDOW", source)

        # Browser mode must explicitly use NiceGUI's non-native browser mode.
        self.assertIn('print("Launching SovereignOS in local browser mode.")', source)
        self.assertIn("ui.run(", source)
        self.assertIn("native=False", source)
        self.assertIn("show=True", source)


if __name__ == "__main__":
    unittest.main()
