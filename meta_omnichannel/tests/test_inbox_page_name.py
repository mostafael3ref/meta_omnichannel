"""Focused tests: conversation serialization exposes Channel page_name."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock


def _install_inbox_frappe() -> MagicMock:
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
	utils.__path__ = []  # type: ignore[attr-defined]
	utils.cint = lambda v: int(v or 0)  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	fm = types.ModuleType("frappe.utils.file_manager")
	fm.save_file = MagicMock()  # type: ignore[attr-defined]
	fm.get_file_path = MagicMock(return_value="/tmp/x")  # type: ignore[attr-defined]
	sys.modules["frappe.utils.file_manager"] = fm
	return frappe


class TestInboxPageName(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_inbox_frappe()
		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()  # type: ignore[attr-defined]
		perm.has_conversation_permission = MagicMock(return_value=True)  # type: ignore[attr-defined]
		perm.user_is_omnichannel_admin = MagicMock(return_value=True)  # type: ignore[attr-defined]
		sys.modules["meta_omnichannel.permissions"] = perm
		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(inbox_mod)
		cls.inbox = inbox_mod
		cls.perm = perm

	def setUp(self):
		self.frappe.get_list.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_all.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_list.return_value = []
		self.frappe.get_all.return_value = []
		self.frappe.db.count.return_value = 0
		self.frappe.db.get_value.return_value = None
		self.perm.ensure_omnichannel_agent.reset_mock()
		self.perm.has_conversation_permission.return_value = True

	def _conv_row(self, **kwargs):
		base = dict(
			name="CONV-1",
			provider="Facebook",
			channel="CHAN-1",
			participant_name="Customer",
			participant_id="PSID-1",
			status="Open",
			assigned_to=None,
			unread_count=0,
			last_message_at=None,
			last_message_preview="",
			last_inbound_at=None,
			crm_contact=None,
			crm_lead=None,
			page_id="327138907153012",
		)
		base.update(kwargs)
		return SimpleNamespace(**base)

	def test_prefers_external_account_name(self):
		self.assertEqual(
			self.inbox._page_name_from_channel_fields("White box", "Fallback"),
			"White box",
		)

	def test_falls_back_to_channel_name(self):
		self.assertEqual(
			self.inbox._page_name_from_channel_fields("", "White box"),
			"White box",
		)
		self.assertIsNone(self.inbox._page_name_from_channel_fields(None, "  "))

	def test_list_conversations_batches_channel_lookup(self):
		rows = [
			self._conv_row(name="CONV-1", channel="CHAN-1"),
			self._conv_row(name="CONV-2", channel="CHAN-1", page_id="327138907153012"),
			self._conv_row(name="CONV-3", channel="CHAN-2", page_id="999"),
		]
		self.frappe.get_list.return_value = rows
		self.frappe.db.count.return_value = 3
		self.frappe.get_all.return_value = [
			SimpleNamespace(
				name="CHAN-1",
				external_account_name="White box",
				channel_name="White box",
			),
			SimpleNamespace(
				name="CHAN-2",
				external_account_name=None,
				channel_name="Other Page",
			),
		]
		result = self.inbox.list_conversations(limit=30, offset=0)
		self.assertEqual(self.frappe.get_all.call_count, 1)
		kwargs = self.frappe.get_all.call_args.kwargs
		self.assertEqual(kwargs["filters"]["name"], ["in", ["CHAN-1", "CHAN-2"]])
		by_name = {i["name"]: i for i in result["items"]}
		self.assertEqual(by_name["CONV-1"]["page_name"], "White box")
		self.assertEqual(by_name["CONV-2"]["page_name"], "White box")
		self.assertEqual(by_name["CONV-3"]["page_name"], "Other Page")
		self.assertEqual(by_name["CONV-1"]["page_id"], "327138907153012")

	def test_get_conversation_includes_page_name(self):
		doc = self._conv_row()
		self.frappe.get_doc.return_value = doc
		self.frappe.db.get_value.return_value = SimpleNamespace(
			external_account_name="White box",
			channel_name="White box",
		)
		out = self.inbox.get_conversation("CONV-1")
		self.assertEqual(out["page_name"], "White box")
		self.assertEqual(out["page_id"], "327138907153012")
		self.frappe.db.get_value.assert_called()

	def test_serialize_without_page_name_is_null(self):
		out = self.inbox._serialize_conversation(self._conv_row(), page_name=None)
		self.assertIsNone(out["page_name"])
		self.assertIn("page_id", out)


if __name__ == "__main__":
	unittest.main()
