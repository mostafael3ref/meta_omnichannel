"""Unified Inbox sync_inbox API tests (frappe mocked; no live connector)."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	frappe.session = MagicMock(user="agent@example.com")
	frappe.get_roles = MagicMock(return_value=["Omnichannel Agent"])
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	frappe.get_list = MagicMock(return_value=[])
	frappe.get_all = MagicMock(return_value=[])
	frappe.get_doc = MagicMock()
	frappe.db = MagicMock()
	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	return frappe


class TestSyncInboxApi(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()

		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()  # type: ignore[attr-defined]
		perm.has_conversation_permission = MagicMock(return_value=True)  # type: ignore[attr-defined]
		perm.user_is_omnichannel_admin = MagicMock(return_value=True)  # type: ignore[attr-defined]
		sys.modules["meta_omnichannel.permissions"] = perm

		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(inbox_mod)
		cls.inbox = inbox_mod

	def test_sync_inbox_runs_both_channels(self):
		fb = {"ok": True, "processed": 2}
		ig = {"ok": True, "processed": 1}

		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		) as fb_sync, patch.object(
			self.inbox, "_sync_instagram_conversations", return_value=ig
		) as ig_sync:
			result = self.inbox.sync_inbox(limit=25)

		fb_sync.assert_called_once_with(25)
		ig_sync.assert_called_once_with(25)
		self.assertTrue(result["ok"])
		self.assertFalse(result["partial"])
		self.assertEqual(result["facebook"], fb)
		self.assertEqual(result["instagram"], ig)
		self.assertIsNone(result["error"])

	def test_sync_inbox_partial_failure_reports_both(self):
		fb = {"ok": True, "processed": 3}
		ig = {"ok": False, "error": "Instagram rate limited", "sync_blocked": False}

		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		) as fb_sync, patch.object(
			self.inbox, "_sync_instagram_conversations", return_value=ig
		) as ig_sync:
			result = self.inbox.sync_inbox(limit=10)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertTrue(result["facebook"]["ok"])
		self.assertFalse(result["instagram"]["ok"])
		self.assertIn("Instagram rate limited", result["error"])
		fb_sync.assert_called_once()
		ig_sync.assert_called_once()

	def test_sync_inbox_exception_isolated_per_channel(self):
		fb = {"ok": True, "processed": 1}

		with patch.object(self.inbox, "_ensure_can_trigger_connector_sync"), patch.object(
			self.inbox, "_sync_facebook_conversations", return_value=fb
		), patch.object(
			self.inbox,
			"_sync_instagram_conversations",
			side_effect=RuntimeError("boom"),
		):
			result = self.inbox.sync_inbox(limit=10)

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertTrue(result["facebook"]["ok"])
		self.assertFalse(result["instagram"]["ok"])
		self.assertIn("boom", result["instagram"]["error"])
		self.assertIn("boom", result["error"])


if __name__ == "__main__":
	unittest.main()
