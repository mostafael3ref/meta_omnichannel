# Copyright (c) 2026, Meta Omnichannel and contributors
# License: MIT. See license.txt
"""Frappe integration tests for Omnichannel Settings.

These tests require a Linux Frappe Bench site with ``meta_omnichannel`` installed.
They are skipped when ``frappe`` is not available (e.g. Windows source checkout).
"""

from __future__ import annotations

import unittest

try:
	import frappe
	from frappe.tests.utils import FrappeTestCase

	FRAPPE_AVAILABLE = True
except Exception:
	FRAPPE_AVAILABLE = False
	FrappeTestCase = unittest.TestCase  # type: ignore[misc,assignment]


@unittest.skipUnless(FRAPPE_AVAILABLE, "Requires Frappe Bench environment")
class TestOmnichannelSettings(FrappeTestCase):
	def test_doctype_is_single(self):
		meta = frappe.get_meta("Omnichannel Settings")
		self.assertTrue(meta.issingle)
		fieldnames = {df.fieldname for df in meta.fields}
		for name in (
			"connector_base_url",
			"connector_tenant_id",
			"connector_api_key",
			"connection_status",
			"last_successful_test",
			"last_error_summary",
		):
			self.assertIn(name, fieldnames)

	def test_api_key_field_is_password(self):
		meta = frappe.get_meta("Omnichannel Settings")
		df = meta.get_field("connector_api_key")
		self.assertEqual(df.fieldtype, "Password")
