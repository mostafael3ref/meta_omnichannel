"""Outbound Facebook send: idempotency, unique-key races, Unknown vs Failed."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pathlib import Path

from meta_omnichannel.meta_connectors.errors import (
	ConnectorAuthError,
	ConnectorError,
	ConnectorServerError,
	ConnectorUnreachableError,
)
from meta_omnichannel.utils.idempotency import build_outbound_idempotency_key

ROOT = Path(__file__).resolve().parents[2]
MESSAGE_JSON = (
	ROOT
	/ "meta_omnichannel"
	/ "meta_omnichannel"
	/ "doctype"
	/ "omnichannel_message"
	/ "omnichannel_message.json"
)


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe._ = lambda x: x
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	frappe.UniqueValidationError = type("UniqueValidationError", (Exception,), {})
	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = MagicMock(return_value="2026-11-22 12:00:00")  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	frappe.utils = utils
	return frappe


class TestOutboundSendIdempotency(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		# Ensure a clean import against the fake frappe module.
		for name in list(sys.modules):
			if name.startswith("meta_omnichannel.facebook_messenger.outbound"):
				del sys.modules[name]
		cls.outbound = importlib.import_module(
			"meta_omnichannel.facebook_messenger.outbound"
		)

	def setUp(self):
		f = self.frappe
		f.db.reset_mock()
		f.get_doc.reset_mock()
		f.throw.reset_mock()
		f.throw.side_effect = Exception("throw")
		f.db.commit = MagicMock()
		f.db.rollback = MagicMock()
		f.db.set_value = MagicMock()
		f.db.exists = MagicMock(return_value=False)
		f.db.get_value = MagicMock(return_value=None)

		self.conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CHAN-1",
			page_id="PAGE-1",
			participant_id="PSID-1",
		)
		f.get_doc.side_effect = self._get_doc

		self.adapter = MagicMock()
		self.adapter.send_text.return_value = {"message_id": "mid-1"}

	def _get_doc(self, arg, name=None):
		if arg == "Omnichannel Conversation" or (
			isinstance(arg, str) and arg == "Omnichannel Conversation"
		):
			return self.conv
		if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
			doc = MagicMock()
			doc.name = "MSG-NEW"
			doc.insert = MagicMock()
			for k, v in arg.items():
				setattr(doc, k, v)
			return doc
		if isinstance(arg, str) and name:
			return self.conv
		raise AssertionError(f"unexpected get_doc {arg!r} {name!r}")

	def _nonce_key(self, text="hello", nonce="n-1"):
		return build_outbound_idempotency_key(
			conversation_name="CONV-1", text=text, client_nonce=nonce
		)

	def test_success_marks_sent(self):
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertTrue(out["ok"])
		self.assertFalse(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Sent")
		self.assertFalse(out["deduplicated"])
		self.adapter.send_text.assert_called_once()
		kwargs = self.adapter.send_text.call_args.kwargs
		self.assertEqual(kwargs["idempotency_key"], self._nonce_key())

	def test_same_nonce_returns_existing_without_meta(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OLD", delivery_status="Sent", error_summary=None
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out["deduplicated"])
		self.adapter.send_text.assert_not_called()
		self.frappe.get_doc.assert_called()  # conversation load only

	def test_same_nonce_unknown_is_uncertain_no_retry(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OLD",
			delivery_status="Unknown",
			error_summary="Connector request timed out",
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Unknown")
		self.assertTrue(out["deduplicated"])
		self.adapter.send_text.assert_not_called()

	def test_same_nonce_failed_is_definitive_no_meta(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OLD",
			delivery_status="Failed",
			error_summary="auth failed",
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"])
		self.assertFalse(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Failed")
		self.adapter.send_text.assert_not_called()

	def test_concurrent_unique_insert_does_not_call_meta(self):
		"""Loser of the insert race re-reads the winner and skips Meta."""
		self.frappe.db.get_value.side_effect = [
			None,
			SimpleNamespace(
				name="MSG-WIN", delivery_status="Queued", error_summary=None
			),
		]

		def _boom_insert(*_a, **_k):
			raise self.frappe.UniqueValidationError("Duplicate entry for idempotency_key")

		def _get_doc(arg, name=None):
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
				doc = MagicMock()
				doc.name = "MSG-LOSE"
				doc.insert = MagicMock(side_effect=_boom_insert)
				return doc
			return self.conv

		self.frappe.get_doc.side_effect = _get_doc
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertTrue(out["deduplicated"])
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Queued")
		self.adapter.send_text.assert_not_called()
		self.frappe.db.rollback.assert_called()
		# Failed insert never reached commit — only the loser's txn is rolled back.
		self.frappe.db.commit.assert_not_called()

	def test_same_nonce_queued_is_uncertain_no_meta(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-Q",
			delivery_status="Queued",
			error_summary=None,
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"], "Queued must not look like confirmed success")
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Queued")
		self.assertTrue(out["deduplicated"])
		self.adapter.send_text.assert_not_called()

	def test_unreachable_marks_unknown_not_failed(self):
		self.adapter.send_text.side_effect = ConnectorUnreachableError(
			"Connector request timed out"
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Unknown")
		set_calls = [
			c
			for c in self.frappe.db.set_value.call_args_list
			if c.args and c.args[0] == "Omnichannel Message"
		]
		self.assertTrue(set_calls)
		values = set_calls[0].args[2]
		self.assertEqual(values["delivery_status"], "Unknown")

	def test_connector_error_marks_failed(self):
		self.adapter.send_text.side_effect = ConnectorError("page not allowed")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"])
		self.assertFalse(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Failed")

	def test_http_5xx_marks_unknown_not_failed(self):
		"""Ambiguous Connector 5xx — do not claim definitive non-delivery."""
		self.adapter.send_text.side_effect = ConnectorServerError(
			"send_text: server error (HTTP 503)"
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"])
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Unknown")
		set_calls = [
			c
			for c in self.frappe.db.set_value.call_args_list
			if c.args and c.args[0] == "Omnichannel Message"
		]
		self.assertEqual(set_calls[0].args[2]["delivery_status"], "Unknown")

	def test_auth_error_remains_definitive_failure(self):
		self.adapter.send_text.side_effect = ConnectorAuthError(
			"send_text: invalid or missing API key (HTTP 401)"
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		):
			out = self.outbound.send_facebook_text(
				conversation_name="CONV-1", text="hello", client_nonce="n-1"
			)
		self.assertFalse(out["ok"])
		self.assertFalse(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Failed")

	def test_is_duplicate_helper(self):
		self.assertTrue(
			self.outbound._is_duplicate_idempotency_error(
				self.frappe.UniqueValidationError("dup")
			)
		)
		self.assertFalse(
			self.outbound._is_duplicate_idempotency_error(RuntimeError("other"))
		)


class TestOmnichannelMessageDeliveryStatusSchema(unittest.TestCase):
	def test_unknown_is_allowed_select_option(self):
		import json

		data = json.loads(MESSAGE_JSON.read_text(encoding="utf-8"))
		field = next(f for f in data["fields"] if f.get("fieldname") == "delivery_status")
		options = [o.strip() for o in (field.get("options") or "").split("\n") if o.strip()]
		self.assertIn("Unknown", options)
		self.assertIn("Failed", options)
		self.assertIn("Queued", options)
		self.assertIn("Sent", options)


if __name__ == "__main__":
	unittest.main()
