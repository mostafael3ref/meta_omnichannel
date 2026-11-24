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


def _ensure_sync_module_stubs():
	"""Install lightweight sync stubs so API tests can patch without loading Frappe deps."""
	for pkg in (
		"meta_omnichannel.facebook_messenger",
		"meta_omnichannel.instagram",
	):
		if pkg not in sys.modules:
			sys.modules[pkg] = types.ModuleType(pkg)

	fb_sync = types.ModuleType("meta_omnichannel.facebook_messenger.sync")
	fb_sync.sync_conversation_messages = MagicMock()  # type: ignore[attr-defined]
	sys.modules["meta_omnichannel.facebook_messenger.sync"] = fb_sync

	ig_sync = types.ModuleType("meta_omnichannel.instagram.sync")
	ig_sync.sync_instagram_conversation_messages = MagicMock()  # type: ignore[attr-defined]
	sys.modules["meta_omnichannel.instagram.sync"] = ig_sync
	return fb_sync, ig_sync


class TestSyncConversationApi(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()

		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()  # type: ignore[attr-defined]
		perm.has_conversation_permission = MagicMock(return_value=True)  # type: ignore[attr-defined]
		perm.user_is_omnichannel_admin = MagicMock(return_value=True)  # type: ignore[attr-defined]
		sys.modules["meta_omnichannel.permissions"] = perm
		cls.perm = perm

		cls.fb_sync_mod, cls.ig_sync_mod = _ensure_sync_module_stubs()

		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(inbox_mod)
		cls.inbox = inbox_mod

	def setUp(self):
		self.perm.ensure_omnichannel_agent.reset_mock()
		self.perm.has_conversation_permission.reset_mock()
		self.perm.has_conversation_permission.return_value = True
		self.frappe.throw.reset_mock(side_effect=True)
		self.frappe.throw.side_effect = Exception("throw")
		self.fb_sync_mod.sync_conversation_messages.reset_mock()
		self.ig_sync_mod.sync_instagram_conversation_messages.reset_mock()
		self.fb_sync_mod.sync_conversation_messages.side_effect = None
		self.ig_sync_mod.sync_instagram_conversation_messages.side_effect = None

	def test_instagram_sync_conversation_returns_counts(self):
		conv = MagicMock()
		conv.provider = "Instagram"
		self.frappe.get_doc.return_value = conv
		ig_result = {
			"ok": True,
			"partial": False,
			"inserted": 3,
			"duplicates": 1,
			"skipped": 0,
			"pages": 1,
			"processed": 4,
			"error": None,
			"sync_log": "LOG-IG",
		}
		self.ig_sync_mod.sync_instagram_conversation_messages.return_value = ig_result

		result = self.inbox.sync_conversation("CONV-IG")

		self.perm.ensure_omnichannel_agent.assert_called_once()
		self.perm.has_conversation_permission.assert_called_once_with(conv, "write")
		self.ig_sync_mod.sync_instagram_conversation_messages.assert_called_once_with(
			"CONV-IG"
		)
		self.fb_sync_mod.sync_conversation_messages.assert_not_called()
		self.assertTrue(result["ok"])
		self.assertEqual(result["inserted"], 3)
		self.assertEqual(result["duplicates"], 1)
		self.assertFalse(result["partial"])

	def test_instagram_partial_result_passthrough(self):
		conv = MagicMock()
		conv.provider = "Instagram"
		self.frappe.get_doc.return_value = conv
		ig_result = {
			"ok": False,
			"partial": True,
			"inserted": 2,
			"duplicates": 0,
			"pages": 1,
			"processed": 2,
			"error": "repeated_offset",
		}
		self.ig_sync_mod.sync_instagram_conversation_messages.return_value = ig_result

		result = self.inbox.sync_conversation("CONV-IG")

		self.assertFalse(result["ok"])
		self.assertTrue(result["partial"])
		self.assertEqual(result["inserted"], 2)
		self.assertEqual(result["error"], "repeated_offset")

	def test_facebook_sync_conversation_unchanged(self):
		conv = MagicMock()
		conv.provider = "Facebook"
		self.frappe.get_doc.return_value = conv
		fb_result = {"ok": True, "processed": 5, "pages": 1, "sync_log": "LOG-FB"}
		self.fb_sync_mod.sync_conversation_messages.return_value = fb_result

		result = self.inbox.sync_conversation("CONV-FB")

		self.fb_sync_mod.sync_conversation_messages.assert_called_once_with("CONV-FB")
		self.ig_sync_mod.sync_instagram_conversation_messages.assert_not_called()
		self.assertEqual(result, fb_result)

	def test_whatsapp_remains_pending_contract(self):
		conv = MagicMock()
		conv.provider = "WhatsApp"
		self.frappe.get_doc.return_value = conv

		result = self.inbox.sync_conversation("CONV-WA")

		self.fb_sync_mod.sync_conversation_messages.assert_not_called()
		self.ig_sync_mod.sync_instagram_conversation_messages.assert_not_called()
		self.assertFalse(result["ok"])
		self.assertTrue(result["pending_contract"])
		self.assertIn("WhatsApp", result["error"])

	def test_permission_denied(self):
		conv = MagicMock()
		conv.provider = "Instagram"
		self.frappe.get_doc.return_value = conv
		self.perm.has_conversation_permission.return_value = False

		with self.assertRaises(Exception):
			self.inbox.sync_conversation("CONV-IG")

		self.ig_sync_mod.sync_instagram_conversation_messages.assert_not_called()
		self.frappe.throw.assert_called()


if __name__ == "__main__":
	unittest.main()
