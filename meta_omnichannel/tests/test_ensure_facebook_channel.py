"""Focused tests for ``_ensure_facebook_channel`` page-name updates."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	sys.modules["frappe"] = frappe
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value="2026-11-18 12:00:00")  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils
	return frappe


class TestEnsureFacebookChannel(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def setUp(self):
		self.frappe.db.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.frappe.db.set_value.side_effect = None
		doc = MagicMock()
		doc.name = "CHAN-NEW"
		doc.insert = MagicMock(return_value=doc)
		self.frappe.get_doc.return_value = doc

	def test_creates_new_channel_with_page_name(self):
		self.frappe.db.get_value.return_value = None
		name = self.sync._ensure_facebook_channel("PAGE-1", "Acme Page")
		self.assertEqual(name, "CHAN-NEW")
		fields = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(fields["channel_name"], "Acme Page")
		self.assertEqual(fields["external_account_name"], "Acme Page")
		self.assertEqual(fields["external_account_id"], "PAGE-1")
		self.assertEqual(fields["provider"], "Facebook")
		self.frappe.db.set_value.assert_not_called()

	def test_creates_new_channel_fallback_name_when_page_name_blank(self):
		self.frappe.db.get_value.return_value = None
		name = self.sync._ensure_facebook_channel("PAGE-9", None)
		self.assertEqual(name, "CHAN-NEW")
		fields = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(fields["channel_name"], "Facebook Page PAGE-9")
		self.assertIsNone(fields["external_account_name"])

	def test_updates_existing_channel_when_page_name_differs(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CHAN-1",
			channel_name="Facebook Page PAGE-1",
			external_account_name=None,
		)
		name = self.sync._ensure_facebook_channel("PAGE-1", "Real Page Name")
		self.assertEqual(name, "CHAN-1")
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.set_value.assert_called_once_with(
			"Omnichannel Channel",
			"CHAN-1",
			{
				"channel_name": "Real Page Name",
				"external_account_name": "Real Page Name",
			},
			update_modified=False,
		)

	def test_unchanged_names_skip_set_value(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CHAN-1",
			channel_name="Real Page Name",
			external_account_name="Real Page Name",
		)
		name = self.sync._ensure_facebook_channel("PAGE-1", "Real Page Name")
		self.assertEqual(name, "CHAN-1")
		self.frappe.db.set_value.assert_not_called()
		self.frappe.get_doc.assert_not_called()

	def test_missing_or_blank_page_name_leaves_existing_unchanged(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CHAN-1",
			channel_name="Old Name",
			external_account_name="Old Name",
		)
		for blank in (None, "", "   "):
			self.frappe.db.set_value.reset_mock()
			name = self.sync._ensure_facebook_channel("PAGE-1", blank)
			self.assertEqual(name, "CHAN-1")
			self.frappe.db.set_value.assert_not_called()

	def test_updates_only_fields_that_differ(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CHAN-1",
			channel_name="Real Page Name",
			external_account_name="Stale External",
		)
		self.sync._ensure_facebook_channel("PAGE-1", "Real Page Name")
		self.frappe.db.set_value.assert_called_once_with(
			"Omnichannel Channel",
			"CHAN-1",
			{"external_account_name": "Real Page Name"},
			update_modified=False,
		)

	def test_preserves_existing_document_id(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CHAN-KEEP",
			channel_name="Old",
			external_account_name="Old",
		)
		name = self.sync._ensure_facebook_channel("PAGE-1", "New")
		self.assertEqual(name, "CHAN-KEEP")
		# Never insert a replacement channel document.
		self.frappe.get_doc.assert_not_called()


if __name__ == "__main__":
	unittest.main()
