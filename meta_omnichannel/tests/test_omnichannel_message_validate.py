"""Offline unit tests for Omnichannel Message provider/channel consistency."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.ValidationError = type("ValidationError", (Exception,), {})

	def throw(msg, exc=None, *args, **kwargs):
		err = exc if isinstance(exc, type) and issubclass(exc, Exception) else frappe.ValidationError
		raise err(str(msg))

	frappe.throw = throw

	document_mod = types.ModuleType("frappe.model.document")

	class Document:
		def __init__(self, *args, **kwargs):
			pass

	document_mod.Document = Document
	model_mod = types.ModuleType("frappe.model")
	model_mod.document = document_mod

	sys.modules["frappe"] = frappe
	sys.modules["frappe.model"] = model_mod
	sys.modules["frappe.model.document"] = document_mod
	return frappe


class TestOmnichannelMessageValidate(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.meta_omnichannel.doctype.omnichannel_message.omnichannel_message as mod

		importlib.reload(mod)
		cls.mod = mod
		cls.OmnichannelMessage = mod.OmnichannelMessage

	def setUp(self):
		self.frappe.db.get_value.reset_mock()
		self.conversations = {
			"CONV-FB": {"name": "CONV-FB", "provider": "Facebook", "channel": "CH-FB"},
			"CONV-IG": {"name": "CONV-IG", "provider": "Instagram", "channel": "CH-IG"},
		}
		self.channels = {
			"CH-FB": {"name": "CH-FB", "provider": "Facebook"},
			"CH-IG": {"name": "CH-IG", "provider": "Instagram"},
		}

		def get_value(doctype, name, fields=None, as_dict=False):
			store = self.conversations if doctype == "Omnichannel Conversation" else self.channels
			row = store.get(name)
			if not row:
				return None
			if as_dict:
				return types.SimpleNamespace(**row)
			if isinstance(fields, (list, tuple)):
				return tuple(row.get(f) for f in fields)
			return row.get(fields) if fields else name

		self.frappe.db.get_value.side_effect = get_value

	def _msg(self, **overrides):
		doc = self.OmnichannelMessage()
		defaults = {
			"conversation": "CONV-FB",
			"channel": "CH-FB",
			"provider": "Facebook",
			"direction": "Inbound",
			"message_type": "Text",
		}
		defaults.update(overrides)
		for key, value in defaults.items():
			setattr(doc, key, value)
		return doc

	def test_valid_facebook_message(self):
		self._msg().validate()

	def test_valid_instagram_message(self):
		self._msg(
			conversation="CONV-IG",
			channel="CH-IG",
			provider="Instagram",
		).validate()

	def test_provider_mismatch(self):
		with self.assertRaises(self.frappe.ValidationError) as ctx:
			self._msg(provider="Instagram").validate()
		self.assertIn("does not match conversation provider", str(ctx.exception))

	def test_channel_mismatch(self):
		with self.assertRaises(self.frappe.ValidationError) as ctx:
			self._msg(channel="CH-IG").validate()
		self.assertIn("does not match conversation channel", str(ctx.exception))

	def test_channel_provider_mismatch(self):
		# Conversation and message agree on channel name, but channel row has wrong provider.
		self.conversations["CONV-FB"] = {
			"name": "CONV-FB",
			"provider": "Facebook",
			"channel": "CH-WEIRD",
		}
		self.channels["CH-WEIRD"] = {"name": "CH-WEIRD", "provider": "WhatsApp"}
		with self.assertRaises(self.frappe.ValidationError) as ctx:
			self._msg(channel="CH-WEIRD", provider="Facebook").validate()
		self.assertIn("Channel provider", str(ctx.exception))

	def test_missing_conversation(self):
		with self.assertRaises(self.frappe.ValidationError) as ctx:
			self._msg(conversation="MISSING").validate()
		self.assertIn("does not exist", str(ctx.exception))

	def test_missing_channel(self):
		self.conversations["CONV-FB"] = {
			"name": "CONV-FB",
			"provider": "Facebook",
			"channel": "CH-GONE",
		}
		with self.assertRaises(self.frappe.ValidationError) as ctx:
			self._msg(channel="CH-GONE").validate()
		self.assertIn("Omnichannel Channel", str(ctx.exception))
		self.assertIn("does not exist", str(ctx.exception))

	def test_empty_conversation_rejected(self):
		with self.assertRaises(self.frappe.ValidationError):
			self._msg(conversation="").validate()

	def test_validate_does_not_auto_correct_mismatched_fields(self):
		"""Offline check: validate() raises and leaves provider/channel/conversation unchanged.

		Does not call real Frappe ``insert(ignore_permissions=True)``; that path is
		documented on the controller (validate still runs under ignore_permissions).
		"""
		doc = self._msg(provider="Instagram")
		with self.assertRaises(self.frappe.ValidationError):
			doc.validate()
		self.assertEqual(doc.provider, "Instagram")
		self.assertEqual(doc.channel, "CH-FB")
		self.assertEqual(doc.conversation, "CONV-FB")


if __name__ == "__main__":
	unittest.main()
