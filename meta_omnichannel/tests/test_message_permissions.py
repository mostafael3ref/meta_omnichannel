"""Conversation-scoped Omnichannel Message has_permission hook (mocked frappe)."""

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
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.session = MagicMock(user="agent@example.com")
	frappe.get_roles = MagicMock(return_value=["Omnichannel Agent"])
	frappe.db = MagicMock()
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	sys.modules["frappe"] = frappe
	# meta_connectors.permissions imports frappe at load time
	return frappe


class TestHasMessagePermission(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		# Ensure connector admin helper uses the same fake frappe.
		import meta_omnichannel.meta_connectors.permissions as admin_perm

		importlib.reload(admin_perm)
		import meta_omnichannel.permissions as perm_mod

		importlib.reload(perm_mod)
		cls.perm = perm_mod
		cls.admin_perm = admin_perm

	def setUp(self):
		self.frappe.session.user = "agent@example.com"
		self.frappe.get_roles.return_value = ["Omnichannel Agent"]
		self.frappe.db.get_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1",
			assigned_to="agent@example.com",
		)

	def _message_doc(self, conversation="CONV-1"):
		"""Minimal stand-in for a Frappe Document (``.get`` API)."""
		data = {"conversation": conversation}

		class Doc:
			def get(self, key, default=None):
				return data.get(key, default)

		return Doc()

	def test_admin_allowed(self):
		self.frappe.session.user = "admin@example.com"
		self.frappe.get_roles.return_value = ["Omnichannel Administrator"]
		self.assertTrue(
			self.perm.has_message_permission(self._message_doc(), "read", user="admin@example.com")
		)
		self.assertTrue(
			self.perm.has_message_permission(self._message_doc(), "write", user="admin@example.com")
		)

	def test_system_manager_administrator_user(self):
		self.assertTrue(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="Administrator"
			)
		)

	def test_assigned_agent_allowed(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="agent@example.com"
		)
		self.assertTrue(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		)

	def test_unassigned_conversation_allowed_for_agent(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to=None
		)
		self.assertTrue(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to=""
		)
		self.assertTrue(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		)

	def test_other_agent_denied(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="other@example.com"
		)
		self.assertFalse(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		)

	def test_non_omnichannel_user_denied(self):
		self.frappe.get_roles.return_value = ["Employee"]
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to=""
		)
		self.assertFalse(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="employee@example.com"
			)
		)

	def test_missing_conversation_field_denied(self):
		self.assertFalse(
			self.perm.has_message_permission(self._message_doc(conversation=""), "read")
		)
		self.frappe.db.get_value.assert_not_called()

	def test_invalid_conversation_reference_denied(self):
		self.frappe.db.get_value.return_value = None
		self.assertFalse(
			self.perm.has_message_permission(
				self._message_doc(conversation="CONV-GONE"), "read"
			)
		)

	def test_reuses_has_conversation_permission(self):
		"""Direct document check delegates assignment rules to conversation helper."""
		calls = []

		def spy(doc, ptype, user=None, debug=False):
			name = doc.get("name") if hasattr(doc, "get") else getattr(doc, "name", None)
			calls.append((name, ptype, user, debug))
			return True

		original = self.perm.has_conversation_permission
		self.perm.has_conversation_permission = spy
		try:
			ok = self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		finally:
			self.perm.has_conversation_permission = original
		self.assertTrue(ok)
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][0], "CONV-1")
		self.assertEqual(calls[0][1], "read")
		self.assertEqual(calls[0][2], "agent@example.com")
		self.assertFalse(calls[0][3])

	def test_read_and_write_both_scoped_to_conversation(self):
		"""Hook scopes access for any ptype; DocType roles still gate write grants."""
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="agent@example.com"
		)
		self.assertTrue(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		)
		self.assertTrue(
			self.perm.has_message_permission(
				self._message_doc(), "write", user="agent@example.com"
			)
		)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="other@example.com"
		)
		self.assertFalse(
			self.perm.has_message_permission(
				self._message_doc(), "read", user="agent@example.com"
			)
		)
		self.assertFalse(
			self.perm.has_message_permission(
				self._message_doc(), "write", user="agent@example.com"
			)
		)

	def test_hook_registered_in_hooks(self):
		from pathlib import Path

		text = (
			Path(__file__).resolve().parents[1] / "hooks.py"
		).read_text(encoding="utf-8")
		self.assertIn(
			'"Omnichannel Message": "meta_omnichannel.permissions.has_message_permission"',
			text,
		)

	def test_does_not_call_frappe_has_permission(self):
		"""Avoid recursion through frappe.has_permission on the parent conversation."""
		self.frappe.has_permission = MagicMock()
		self.perm.has_message_permission(self._message_doc(), "read")
		self.frappe.has_permission.assert_not_called()
		self.frappe.db.get_value.assert_called_once()
		args = self.frappe.db.get_value.call_args
		self.assertEqual(args.args[0], "Omnichannel Conversation")
		self.assertEqual(args.args[1], "CONV-1")

	def test_private_file_download_follows_message_read_not_weakened(self):
		"""Document Frappe File auth for Omnichannel attachments (no File hook).

		Frappe ``File.has_permission`` / ``download_private_file`` /
		``frappe.handler.download_file`` authorize private Files via
		``attached_to`` document read. For our Files that means Omnichannel
		Message read (role table + ``has_message_permission`` conversation
		scope). We intentionally do **not** register a File ``has_permission``
		hook and must not weaken File permissions.
		"""
		import json
		from pathlib import Path

		hooks = Path(__file__).resolve().parents[1] / "hooks.py"
		text = hooks.read_text(encoding="utf-8")
		self.assertNotIn('"File"', text)
		self.assertIn(
			'"Omnichannel Message": "meta_omnichannel.permissions.has_message_permission"',
			text,
		)

		msg_json = (
			Path(__file__).resolve().parents[1]
			/ "meta_omnichannel"
			/ "doctype"
			/ "omnichannel_message"
			/ "omnichannel_message.json"
		)
		meta = json.loads(msg_json.read_text(encoding="utf-8"))
		agent = next(p for p in meta["permissions"] if p["role"] == "Omnichannel Agent")
		self.assertEqual(agent.get("read"), 1)

		# Assigned agent may read the message → File download may proceed via Frappe.
		self.assertTrue(self.perm.has_message_permission(self._message_doc(), "read"))
		# Other agent on assigned thread is denied → File download must also fail closed.
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="other@example.com"
		)
		self.assertFalse(self.perm.has_message_permission(self._message_doc(), "read"))

	def test_frappe_hook_kwargs_debug_accepted_allow_and_deny(self):
		"""Frappe calls hooks as frappe.call(..., ptype=..., user=..., debug=...)."""
		conv = {"name": "CONV-1", "assigned_to": "agent@example.com"}
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="agent@example.com"
		)
		for debug in (False, True):
			self.assertTrue(
				self.perm.has_conversation_permission(
					conv, "read", user="agent@example.com", debug=debug
				)
			)
			self.assertTrue(
				self.perm.has_message_permission(
					self._message_doc(),
					"read",
					user="agent@example.com",
					debug=debug,
				)
			)

		denied_conv = {"name": "CONV-1", "assigned_to": "other@example.com"}
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="CONV-1", assigned_to="other@example.com"
		)
		for debug in (False, True):
			self.assertFalse(
				self.perm.has_conversation_permission(
					denied_conv, "read", user="agent@example.com", debug=debug
				)
			)
			self.assertFalse(
				self.perm.has_message_permission(
					self._message_doc(),
					"read",
					user="agent@example.com",
					debug=debug,
				)
			)
			self.assertFalse(
				self.perm.has_message_permission(
					self._message_doc(),
					"write",
					user="agent@example.com",
					debug=debug,
				)
			)


if __name__ == "__main__":
	unittest.main()
