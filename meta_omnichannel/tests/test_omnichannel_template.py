"""Regression: Frappe safe_render rejects templates containing the substring '.__'."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "www" / "omnichannel.html"
FRONTEND_JS = ROOT / "public" / "frontend" / "omnichannel.js"
VUE_API = ROOT.parents[0] / "frontend" / "src" / "api.js"


class TestOmnichannelInboxTemplate(unittest.TestCase):
	def test_template_has_no_forbidden_dunder_attribute_substring(self):
		text = TEMPLATE.read_text(encoding="utf-8")
		self.assertNotIn(
			".__",
			text,
			"Frappe frappe.render_template(safe_render=True) rejects templates containing '.__'",
		)
		self.assertNotIn("__OMNICHANNEL_BOOT__", text)

	def test_template_assigns_omnichannel_boot(self):
		text = TEMPLATE.read_text(encoding="utf-8")
		self.assertIn("window.omnichannelBoot = {{ boot | tojson }};", text)

	def test_frontend_reads_same_boot_variable(self):
		js = FRONTEND_JS.read_text(encoding="utf-8")
		self.assertIn("window.omnichannelBoot", js)
		self.assertNotIn("__OMNICHANNEL_BOOT__", js)

		api = VUE_API.read_text(encoding="utf-8")
		self.assertIn("window.omnichannelBoot", api)
		self.assertNotIn("__OMNICHANNEL_BOOT__", api)


if __name__ == "__main__":
	unittest.main()
