"""The /chat page head: a viewport meta (so a phone lays the page out at device width and multi-chat.js's
phone single-pane rule can fire) and an inline favicon (so the browser does not request /favicon.ico and log a 404).

Run: .venv/bin/python -m unittest test_chat_page -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402


class ChatPageHeadTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def _page(self):
        resp = self.client.get("/chat")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_viewport_meta_uses_device_width(self):
        html = self._page()
        tag = re.search(r'<meta\s+name="viewport"\s+content="([^"]*)"', html)
        self.assertIsNotNone(tag, "chat.html has no viewport meta")
        self.assertIn("width=device-width", tag.group(1))
        self.assertIn("initial-scale=1", tag.group(1))

    def test_inline_favicon_link_avoids_favicon_request(self):
        html = self._page()
        icon = re.search(r'<link\s+rel="icon"\s+href="([^"]*)"', html)
        self.assertIsNotNone(icon, "chat.html has no icon link")
        self.assertTrue(icon.group(1).startswith("data:"), "the icon must be an inline data: URI")

    def test_multi_panes_flag_script_comes_before_the_first_module_script(self):
        html = self._page()
        flag = html.find("window.__CHAT_MULTI_PANES__ = true")
        module = html.find('type="module"')
        self.assertNotEqual(flag, -1, "chat.html has no inline __CHAT_MULTI_PANES__ script")
        self.assertNotEqual(module, -1)
        self.assertLess(flag, module, "the flag must be set before the first module script runs")
        script_open = html.rfind("<script", 0, flag)
        self.assertNotIn("type=", html[script_open:flag], "the flag script must be a classic script, not a module")
        self.assertEqual(html.count('id="chat-panel"'), 1, "#chat-panel stays the single id")


if __name__ == "__main__":
    unittest.main()
