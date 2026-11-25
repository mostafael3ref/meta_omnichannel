"""Instagram message persistence/upsert regression tests (frappe mocked)."""

from __future__ import annotations

import hashlib
import importlib
import sys
import types
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _AfterCommit:
	"""Minimal stand-in for Frappe v15 ``frappe.db.after_commit``."""

	def __init__(self):
		self.callbacks: list = []

	def add(self, fn):
		self.callbacks.append(fn)

	def run(self):
		for fn in list(self.callbacks):
			fn()
		self.callbacks.clear()

	def clear(self):
		self.callbacks.clear()


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.db.after_commit = _AfterCommit()
	frappe.get_doc = MagicMock()
	frappe.enqueue = MagicMock(return_value=SimpleNamespace(id="job-1"))
	sys.modules["frappe"] = frappe

	utils = types.ModuleType("frappe.utils")
	utils.cint = int  # type: ignore[attr-defined]
	utils.get_datetime = lambda v: v  # type: ignore[attr-defined]
	utils.now_datetime = MagicMock(return_value="2026-11-18 12:00:00")  # type: ignore[attr-defined]
	utils.get_system_timezone = MagicMock(return_value="UTC")  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils
	return frappe


def _conv(
	*,
	name="CONV-IG",
	channel="CH-IG",
	provider="Instagram",
	page_id="IG_ACCT",
	participant_id="IGSID_1",
):
	return SimpleNamespace(
		name=name,
		channel=channel,
		provider=provider,
		page_id=page_id,
		participant_id=participant_id,
	)


def _channel(
	*,
	name="CH-IG",
	provider="Instagram",
	external_account_id="IG_ACCT",
):
	return SimpleNamespace(
		name=name,
		provider=provider,
		external_account_id=external_account_id,
	)


def _message_row(**overrides):
	row = {
		"direction": "inbound",
		"text": "hello",
		"attachments": [],
		"timestamp": "2026-11-18T12:00:00Z",
		"message_id": "ig-mid-1",
	}
	row.update(overrides)
	return row


class TestInstagramMessageUpsert(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as fb_sync
		import meta_omnichannel.instagram.sync as ig_sync
		import meta_omnichannel.utils.datetimes as dt_mod

		importlib.reload(dt_mod)
		importlib.reload(fb_sync)
		importlib.reload(ig_sync)
		cls.ig = ig_sync
		cls.fb = fb_sync

	def setUp(self):
		self.frappe.db.get_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.set_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.sql.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.frappe.db.after_commit = _AfterCommit()
		self.frappe.enqueue.reset_mock()
		doc = MagicMock()
		doc.name = "MSG-IG-NEW"
		self.frappe.get_doc.return_value = doc

	def _ready_identity_lookups(self, *, then=None):
		"""Conversation + channel identity OK; optional further get_value returns."""
		conv = _conv()
		channel = _channel()
		tail = list(then or [])

		def _get_value(doctype, *args, **kwargs):
			filters = args[0] if args else kwargs.get("filters")
			as_dict = kwargs.get("as_dict")
			if doctype == "Omnichannel Conversation" and as_dict:
				if isinstance(filters, dict) and "external_thread_key" in filters:
					return conv
			if doctype == "Omnichannel Channel" and as_dict:
				return channel
			if tail:
				return tail.pop(0)
			return None

		self.frappe.db.get_value.side_effect = _get_value
		return conv, channel

	def test_inbound_insert(self):
		self._ready_identity_lookups()
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(direction="inbound", text="hi"),
		)
		self.assertEqual(result["name"], "MSG-IG-NEW")
		self.assertEqual(result["status"], "inserted")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(payload["provider"], "Instagram")
		self.assertEqual(payload["direction"], "Inbound")
		self.assertEqual(payload["text_content"], "hi")
		self.assertEqual(payload["external_message_id"], "ig-mid-1")
		self.assertEqual(payload["delivery_status"], "Delivered")
		self.assertEqual(payload["sender_external_id"], "IGSID_1")
		self.assertEqual(payload["conversation"], "CONV-IG")
		self.assertEqual(payload["channel"], "CH-IG")

	def test_outbound_insert(self):
		self._ready_identity_lookups()
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(direction="outbound", text="reply", message_id="ig-out-1"),
		)
		self.assertEqual(result["name"], "MSG-IG-NEW")
		self.assertEqual(result["status"], "inserted")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(payload["direction"], "Outbound")
		self.assertEqual(payload["delivery_status"], "Sent")
		self.assertEqual(payload["sender_external_id"], "IG_ACCT")
		self.frappe.db.sql.assert_not_called()

	def test_duplicate_external_id_same_conversation_no_unread(self):
		existing = SimpleNamespace(name="MSG-EXIST", sent_at=datetime(2026, 11, 18, 12, 0, 0))
		self._ready_identity_lookups(then=[existing])
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(),
			count_unread=True,
			unread_watermark=datetime(2020, 1, 1),
		)
		self.assertEqual(result["name"], "MSG-EXIST")
		self.assertEqual(result["status"], "duplicate")
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.sql.assert_not_called()

	def test_missing_message_id_uses_fingerprint_dedup(self):
		fp_existing = "MSG-FP"
		# Identity → then fingerprint lookup hits
		conv = _conv()
		channel = _channel()

		def _get_value(doctype, *args, **kwargs):
			as_dict = kwargs.get("as_dict")
			filters = args[0] if args else kwargs.get("filters")
			if doctype == "Omnichannel Conversation" and as_dict:
				return conv
			if doctype == "Omnichannel Channel" and as_dict:
				return channel
			if doctype == "Omnichannel Message" and isinstance(filters, dict):
				if "raw_payload_hash" in filters:
					return fp_existing
			return None

		self.frappe.db.get_value.side_effect = _get_value
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(message_id=None, text="no-id"),
			count_unread=True,
			unread_watermark=datetime(2020, 1, 1),
		)
		self.assertEqual(result["name"], "MSG-FP")
		self.assertEqual(result["status"], "duplicate")
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.sql.assert_not_called()

	def test_null_message_id_inserts_with_fingerprint(self):
		self._ready_identity_lookups()
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(message_id=None, text="anon"),
		)
		self.assertEqual(result["name"], "MSG-IG-NEW")
		self.assertEqual(result["status"], "inserted")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertIsNone(payload["external_message_id"])
		self.assertTrue(payload["raw_payload_hash"])

	def test_attachment_metadata_only_no_facebook_jobs(self):
		self._ready_identity_lookups()
		with patch(
			"meta_omnichannel.jobs.sync_jobs.enqueue_facebook_attachment_persist"
		) as enqueue:
			result = self.ig.upsert_instagram_message_from_connector(
				account_id="IG_ACCT",
				sender_id="IGSID_1",
				row=_message_row(
					text="",
					attachments=[
						{
							"type": "image",
							"url": "https://cdn.example/ig.png",
							"mime_type": "image/png",
						}
					],
					message_id="ig-att-1",
				),
			)
		self.assertEqual(result["name"], "MSG-IG-NEW")
		self.assertEqual(result["status"], "inserted")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(payload["message_type"], "Image")
		self.assertEqual(payload["attachment_url"], "https://cdn.example/ig.png")
		self.assertEqual(payload["attachment_mime"], "image/png")
		self.assertEqual(payload["text_content"], "")
		enqueue.assert_not_called()
		self.assertEqual(self.frappe.db.after_commit.callbacks, [])

	def test_identity_mismatch_missing_conversation(self):
		self.frappe.db.get_value.return_value = None
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(),
		)
		self.assertIsNone(result)
		self.frappe.get_doc.assert_not_called()

	def test_identity_mismatch_wrong_provider(self):
		def _get_value(doctype, *args, **kwargs):
			as_dict = kwargs.get("as_dict")
			if doctype == "Omnichannel Conversation" and as_dict:
				return _conv(provider="Facebook")
			return None

		self.frappe.db.get_value.side_effect = _get_value
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(),
		)
		self.assertIsNone(result)
		self.frappe.get_doc.assert_not_called()

	def test_identity_mismatch_channel_account(self):
		def _get_value(doctype, *args, **kwargs):
			as_dict = kwargs.get("as_dict")
			if doctype == "Omnichannel Conversation" and as_dict:
				return _conv()
			if doctype == "Omnichannel Channel" and as_dict:
				return _channel(external_account_id="OTHER")
			return None

		self.frappe.db.get_value.side_effect = _get_value
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(),
		)
		self.assertIsNone(result)
		self.frappe.get_doc.assert_not_called()

	def test_cross_provider_external_id_collision(self):
		"""Global unique id owned by a Facebook row: no second insert / no unread."""
		self._ready_identity_lookups(then=[None, "MSG-FB-ELSEWHERE"])
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(message_id="shared-mid"),
			count_unread=True,
			unread_watermark=datetime(2020, 1, 1),
		)
		self.assertEqual(result["name"], "MSG-FB-ELSEWHERE")
		self.assertEqual(result["status"], "duplicate")
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.sql.assert_not_called()

	def test_persistence_error_propagates(self):
		self._ready_identity_lookups()
		doc = MagicMock()
		doc.name = "MSG-FAIL"
		doc.insert.side_effect = RuntimeError("db down")
		self.frappe.get_doc.return_value = doc
		with self.assertRaises(RuntimeError):
			self.ig.upsert_instagram_message_from_connector(
				account_id="IG_ACCT",
				sender_id="IGSID_1",
				row=_message_row(message_id="ig-fail"),
			)

	def test_unknown_direction_fail_closed(self):
		self._ready_identity_lookups()
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(direction="sideways"),
		)
		self.assertIsNone(result)
		self.frappe.get_doc.assert_not_called()

	def test_duplicate_does_not_increment_unread(self):
		existing = SimpleNamespace(name="MSG-DUP", sent_at=None)
		self._ready_identity_lookups(then=[existing])
		with patch.object(self.ig, "_increment_unread_count") as bump:
			self.ig.upsert_instagram_message_from_connector(
				account_id="IG_ACCT",
				sender_id="IGSID_1",
				row=_message_row(),
				count_unread=True,
				unread_watermark=datetime(2020, 1, 1),
			)
		bump.assert_not_called()

	def test_new_inbound_can_increment_unread_with_watermark(self):
		self._ready_identity_lookups()
		with patch.object(self.ig, "_increment_unread_count") as bump, patch.object(
			self.ig, "_advance_last_inbound_at"
		) as advance:
			self.ig.upsert_instagram_message_from_connector(
				account_id="IG_ACCT",
				sender_id="IGSID_1",
				row=_message_row(
					timestamp="2026-11-18T15:00:00Z",
					message_id="ig-new-unread",
				),
				count_unread=True,
				unread_watermark=datetime(2026, 11, 18, 12, 0, 0),
			)
		advance.assert_called_once()
		bump.assert_called_once_with("CONV-IG")

	def test_facebook_upsert_still_schedules_attachment_persist(self):
		"""Facebook behavior unchanged: attachment messages still enqueue after commit."""
		doc = MagicMock()
		doc.name = "MSG-FB-NEW"
		self.frappe.get_doc.return_value = doc
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.frappe.db.after_commit = _AfterCommit()

		with patch.object(self.fb, "_enqueue_facebook_attachment_persist_safe") as safe:
			name = self.fb.upsert_message_from_connector(
				"CONV-FB",
				"CH-FB",
				{
					"direction": "inbound",
					"message_id": "fb-att-1",
					"text": "cap",
					"attachments": [
						{"type": "image", "url": "https://cdn.example/fb.png"}
					],
				},
			)
			self.assertEqual(name, "MSG-FB-NEW")
			self.assertEqual(len(self.frappe.db.after_commit.callbacks), 1)
			self.frappe.db.after_commit.run()
			safe.assert_called_once_with("MSG-FB-NEW")

	def test_facebook_long_message_id_not_normalized(self):
		"""Facebook upsert must not apply Instagram ig: SHA-256 mapping."""
		long_id = "F" * 141
		doc = MagicMock()
		doc.name = "MSG-FB-LONG"
		self.frappe.get_doc.return_value = doc
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.frappe.db.after_commit = _AfterCommit()

		self.fb.upsert_message_from_connector(
			"CONV-FB",
			"CH-FB",
			{"direction": "inbound", "message_id": long_id, "text": "x", "attachments": []},
		)
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(payload["external_message_id"], long_id)
		self.assertFalse(str(payload["external_message_id"]).startswith("ig:"))


class TestNormalizeInstagramExternalMessageId(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		_install_fake_frappe()
		import meta_omnichannel.instagram.sync as ig_sync

		importlib.reload(ig_sync)
		cls.ig = ig_sync

	def test_boundary_140_unchanged(self):
		raw = "a" * 140
		self.assertEqual(self.ig.normalize_instagram_external_message_id(raw), raw)

	def test_boundary_141_hashed(self):
		raw = "b" * 141
		stored = self.ig.normalize_instagram_external_message_id(raw)
		expected = "ig:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
		self.assertEqual(stored, expected)
		self.assertEqual(len(stored), 67)
		self.assertLessEqual(len(stored), 140)

	def test_same_long_id_same_stored(self):
		raw = "c" * 200
		a = self.ig.normalize_instagram_external_message_id(raw)
		b = self.ig.normalize_instagram_external_message_id(raw)
		self.assertEqual(a, b)

	def test_different_long_ids_differ(self):
		a = self.ig.normalize_instagram_external_message_id("x" * 150)
		b = self.ig.normalize_instagram_external_message_id("y" * 150)
		self.assertNotEqual(a, b)

	def test_never_hash_twice(self):
		raw = "z" * 180
		once = self.ig.normalize_instagram_external_message_id(raw)
		twice = self.ig.normalize_instagram_external_message_id(once)
		self.assertEqual(once, twice)
		self.assertTrue(once.startswith("ig:"))

	def test_empty_and_nullish(self):
		self.assertEqual(self.ig.normalize_instagram_external_message_id(""), "")
		self.assertEqual(self.ig.normalize_instagram_external_message_id("   "), "")


class TestInstagramLongMessageIdUpsert(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as fb_sync
		import meta_omnichannel.instagram.sync as ig_sync
		import meta_omnichannel.utils.datetimes as dt_mod

		importlib.reload(dt_mod)
		importlib.reload(fb_sync)
		importlib.reload(ig_sync)
		cls.ig = ig_sync

	def setUp(self):
		self.frappe.db.get_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.sql.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		doc = MagicMock()
		doc.name = "MSG-IG-LONG"
		self.frappe.get_doc.return_value = doc

	def _ready_identity(self, *, then=None):
		conv = _conv()
		channel = _channel()
		tail = list(then or [])

		def _get_value(doctype, *args, **kwargs):
			filters = args[0] if args else kwargs.get("filters")
			as_dict = kwargs.get("as_dict")
			if doctype == "Omnichannel Conversation" and as_dict:
				if isinstance(filters, dict) and "external_thread_key" in filters:
					return conv
			if doctype == "Omnichannel Channel" and as_dict:
				return channel
			if tail:
				return tail.pop(0)
			return None

		self.frappe.db.get_value.side_effect = _get_value
		return conv

	def test_long_id_stored_normalized(self):
		raw = "L" * 141
		expected = self.ig.normalize_instagram_external_message_id(raw)
		self._ready_identity()
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(message_id=raw),
		)
		self.assertEqual(result["status"], "inserted")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(payload["external_message_id"], expected)
		self.assertEqual(len(payload["external_message_id"]), 67)

	def test_long_id_duplicate_uses_normalized_lookup(self):
		raw = "D" * 160
		normalized = self.ig.normalize_instagram_external_message_id(raw)
		existing = SimpleNamespace(name="MSG-EXIST-LONG", sent_at=datetime(2026, 1, 1))
		seen_filters: list = []

		conv = _conv()
		channel = _channel()

		def _get_value(doctype, *args, **kwargs):
			filters = args[0] if args else kwargs.get("filters")
			as_dict = kwargs.get("as_dict")
			if doctype == "Omnichannel Conversation" and as_dict:
				return conv
			if doctype == "Omnichannel Channel" and as_dict:
				return channel
			if doctype == "Omnichannel Message" and isinstance(filters, dict):
				seen_filters.append(dict(filters))
				if filters.get("external_message_id") == normalized and filters.get(
					"conversation"
				):
					return existing
			return None

		self.frappe.db.get_value.side_effect = _get_value
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(message_id=raw),
			count_unread=True,
			unread_watermark=datetime(2020, 1, 1),
		)
		self.assertEqual(result["status"], "duplicate")
		self.assertEqual(result["name"], "MSG-EXIST-LONG")
		self.assertTrue(
			any(f.get("external_message_id") == normalized for f in seen_filters)
		)
		self.frappe.get_doc.assert_not_called()
		self.frappe.db.sql.assert_not_called()

	def test_null_id_fingerprint_path(self):
		self._ready_identity()
		result = self.ig.upsert_instagram_message_from_connector(
			account_id="IG_ACCT",
			sender_id="IGSID_1",
			row=_message_row(message_id=None, text="anon-fp"),
		)
		self.assertEqual(result["status"], "inserted")
		payload = self.frappe.get_doc.call_args.args[0]
		self.assertIsNone(payload["external_message_id"])
		self.assertTrue(payload["raw_payload_hash"])

	def test_long_id_unread_still_increments_on_new_insert(self):
		raw = "U" * 150
		self._ready_identity()
		with patch.object(self.ig, "_increment_unread_count") as bump, patch.object(
			self.ig, "_advance_last_inbound_at"
		):
			self.ig.upsert_instagram_message_from_connector(
				account_id="IG_ACCT",
				sender_id="IGSID_1",
				row=_message_row(
					message_id=raw,
					timestamp="2026-11-18T15:00:00Z",
				),
				count_unread=True,
				unread_watermark=datetime(2026, 11, 18, 12, 0, 0),
			)
		bump.assert_called_once_with("CONV-IG")

	def test_long_id_duplicate_unread_unchanged(self):
		raw = "V" * 150
		normalized = self.ig.normalize_instagram_external_message_id(raw)
		existing = SimpleNamespace(name="MSG-DUP-LONG", sent_at=datetime(2026, 1, 1))
		conv = _conv()
		channel = _channel()

		def _get_value(doctype, *args, **kwargs):
			filters = args[0] if args else kwargs.get("filters")
			as_dict = kwargs.get("as_dict")
			if doctype == "Omnichannel Conversation" and as_dict:
				return conv
			if doctype == "Omnichannel Channel" and as_dict:
				return channel
			if doctype == "Omnichannel Message" and isinstance(filters, dict):
				if filters.get("external_message_id") == normalized:
					return existing if as_dict else existing.name
			return None

		self.frappe.db.get_value.side_effect = _get_value
		with patch.object(self.ig, "_increment_unread_count") as bump:
			self.ig.upsert_instagram_message_from_connector(
				account_id="IG_ACCT",
				sender_id="IGSID_1",
				row=_message_row(message_id=raw),
				count_unread=True,
				unread_watermark=datetime(2020, 1, 1),
			)
		bump.assert_not_called()


if __name__ == "__main__":
	unittest.main()
