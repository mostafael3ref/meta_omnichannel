"""Facebook connector message attachment mapping (offline / mocked frappe)."""

from __future__ import annotations

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock


# Realistic Facebook CDN URL with signed query params — exceeds Data's 140-char default.
_LONG_FB_CDN_URL = (
	"https://scontent.xx.fbcdn.net/v/t1.15752-9/123456789_987654321098765_"
	"1234567890123456789_n.jpg?_nc_cat=111&ccb=1-7&_nc_sid=9f807c&_nc_ohc="
	"AbCdEfGhIjKlMnOpQrStUvWxYz0123456789&_nc_ht=scontent.xx.fbcdn.net&oh="
	"00abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&oe=65ABCDEF"
)


class TestAttachmentUrlSchema(unittest.TestCase):
	"""DocType must store full CDN URLs (not Data/varchar 140)."""

	def test_attachment_url_is_small_text(self):
		path = (
			Path(__file__).resolve().parents[1]
			/ "meta_omnichannel"
			/ "doctype"
			/ "omnichannel_message"
			/ "omnichannel_message.json"
		)
		meta = json.loads(path.read_text(encoding="utf-8"))
		field = next(f for f in meta["fields"] if f["fieldname"] == "attachment_url")
		self.assertEqual(field["fieldtype"], "Small Text")
		self.assertGreater(len(_LONG_FB_CDN_URL), 140)


class TestAttachmentHelpersPure(unittest.TestCase):
	"""Pure helpers — no Frappe required."""

	@classmethod
	def setUpClass(cls):
		frappe = MagicMock()
		sys.modules["frappe"] = frappe
		utils = types.ModuleType("frappe.utils")
		utils.cint = int  # type: ignore[attr-defined]
		utils.get_datetime = lambda v: v  # type: ignore[attr-defined]
		utils.now_datetime = MagicMock(return_value="2026-11-18 12:00:00")  # type: ignore[attr-defined]
		sys.modules["frappe.utils"] = utils
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def test_four_supported_attachment_types(self):
		cases = [
			("image", "Image", "https://cdn.example/a.jpg"),
			("audio", "Audio", "https://cdn.example/a.ogg"),
			("video", "Video", "https://cdn.example/a.mp4"),
			("file", "File", "https://cdn.example/a.pdf"),
		]
		for raw_type, expected, url in cases:
			atts = self.sync.extract_connector_attachments(
				{"attachments": [{"type": raw_type, "url": url}]}
			)
			msg_type, att_url, mime = self.sync.resolve_message_type_and_attachment(
				text="", attachments=atts
			)
			self.assertEqual(msg_type, expected, raw_type)
			self.assertEqual(att_url, url)
			self.assertIsNone(mime)

	def test_text_plus_attachment(self):
		atts = self.sync.extract_connector_attachments(
			{
				"text": "see this",
				"attachments": [{"type": "image", "url": "https://cdn.example/x.png"}],
			}
		)
		msg_type, url, _ = self.sync.resolve_message_type_and_attachment(
			text="see this", attachments=atts
		)
		self.assertEqual(msg_type, "Image")
		self.assertEqual(url, "https://cdn.example/x.png")

	def test_multiple_attachments_do_not_contaminate_customer_text(self):
		atts = self.sync.extract_connector_attachments(
			{
				"attachments": [
					{"type": "image", "url": "https://cdn.example/1.jpg"},
					{"type": "file", "url": "https://cdn.example/2.pdf"},
					{"type": "audio", "url": "https://cdn.example/3.ogg"},
				]
			}
		)
		msg_type, url, _ = self.sync.resolve_message_type_and_attachment(
			text="hello customer", attachments=atts
		)
		self.assertEqual(msg_type, "Image")
		self.assertEqual(url, "https://cdn.example/1.jpg")
		# Helper must not invent a note; customer text stays caller-owned.
		self.assertEqual(len(atts), 3)

	def test_malformed_attachment_payloads(self):
		self.assertEqual(self.sync.extract_connector_attachments({}), [])
		self.assertEqual(self.sync.extract_connector_attachments({"attachments": None}), [])
		self.assertEqual(self.sync.extract_connector_attachments({"attachments": "bad"}), [])
		self.assertEqual(
			self.sync.extract_connector_attachments(
				{"attachments": [None, "x", {"type": "image"}, {"url": ""}]}
			),
			[],
		)

	def test_text_only_fingerprint_unchanged_from_legacy_shape(self):
		legacy = self.sync._payload_hash(
			{"c": "CONV", "t": "hi", "s": "100", "d": "Inbound"}
		)
		current = self.sync.build_message_dedup_fingerprint(
			conversation="CONV",
			text="hi",
			sent_at="100",
			direction="Inbound",
			attachments=[],
		)
		self.assertEqual(legacy, current)

	def test_different_attachment_urls_do_not_collide(self):
		a = self.sync.build_message_dedup_fingerprint(
			conversation="CONV",
			text="",
			sent_at="100",
			direction="Inbound",
			attachments=[{"type": "image", "url": "https://cdn.example/a.jpg", "mime": None}],
		)
		b = self.sync.build_message_dedup_fingerprint(
			conversation="CONV",
			text="",
			sent_at="100",
			direction="Inbound",
			attachments=[{"type": "audio", "url": "https://cdn.example/b.ogg", "mime": None}],
		)
		self.assertNotEqual(a, b)

	def test_attachment_fingerprint_not_equal_to_legacy_empty_text(self):
		"""Do not treat legacy empty-text hash as a match for attachment-only media."""
		legacy_empty = self.sync._payload_hash(
			{"c": "CONV", "t": "", "s": "100", "d": "Inbound"}
		)
		with_attachment = self.sync.build_message_dedup_fingerprint(
			conversation="CONV",
			text="",
			sent_at="100",
			direction="Inbound",
			attachments=[{"type": "image", "url": "https://cdn.example/a.jpg", "mime": None}],
		)
		self.assertNotEqual(legacy_empty, with_attachment)


class TestUpsertMessageAttachments(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe = MagicMock()
		frappe.db = MagicMock()
		frappe.get_doc = MagicMock()
		sys.modules["frappe"] = frappe
		utils = types.ModuleType("frappe.utils")
		utils.cint = int  # type: ignore[attr-defined]
		utils.get_datetime = lambda v: v  # type: ignore[attr-defined]
		utils.now_datetime = MagicMock(return_value="2026-11-18 12:00:00")  # type: ignore[attr-defined]
		sys.modules["frappe.utils"] = utils
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod
		cls.frappe = frappe

	def setUp(self):
		self.frappe.db.get_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		doc = MagicMock()
		doc.name = "MSG-NEW"
		self.frappe.get_doc.return_value = doc

	def _inserted_fields(self):
		return self.frappe.get_doc.call_args.args[0]

	def test_text_plus_attachment_preserves_customer_text_only(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-text-att",
				"text": "caption only",
				"attachments": [{"type": "image", "url": "https://cdn.example/x.png"}],
			},
		)
		fields = self._inserted_fields()
		self.assertEqual(fields["text_content"], "caption only")
		self.assertEqual(fields["content"], "caption only")
		self.assertEqual(fields["attachment_url"], "https://cdn.example/x.png")
		self.assertEqual(fields["message_type"], "Image")
		self.assertNotIn("Additional attachments", fields["text_content"])
		self.assertNotIn("cdn.example/x.png", fields["content"])

	def test_long_facebook_cdn_url_preserved_verbatim(self):
		"""Regression: CharacterLengthExceededError when attachment_url was Data/140."""
		self.assertGreater(len(_LONG_FB_CDN_URL), 140)
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-long-url",
				"text": "",
				"attachments": [{"type": "image", "url": _LONG_FB_CDN_URL}],
			},
		)
		fields = self._inserted_fields()
		self.assertEqual(fields["attachment_url"], _LONG_FB_CDN_URL)
		self.assertEqual(len(fields["attachment_url"]), len(_LONG_FB_CDN_URL))
		self.assertIn("?_nc_cat=", fields["attachment_url"])
		self.assertIn("&oe=", fields["attachment_url"])
		self.assertEqual(fields["message_type"], "Image")
		# Must not truncate, strip query params, or push URL into Text Editor content.
		self.assertNotIn("fbcdn.net", fields["content"])
		self.assertEqual(fields["content"], "")
		self.assertEqual(fields["text_content"], "")

	def test_untrusted_html_like_urls_never_enter_content(self):
		evil = 'https://evil.example/"/><script>alert(1)</script>'
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-evil",
				"text": "safe body",
				"attachments": [
					{"type": "image", "url": evil},
					{"type": "file", "url": "https://cdn.example/2.pdf"},
				],
			},
		)
		fields = self._inserted_fields()
		self.assertEqual(fields["content"], "safe body")
		self.assertEqual(fields["text_content"], "safe body")
		self.assertNotIn("<script>", fields["content"])
		self.assertNotIn("cdn.example/2.pdf", fields["content"])
		self.assertEqual(fields["attachment_url"], evil)

	def test_multiple_attachments_primary_only_in_fields(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"message_id": "m-multi",
				"text": "",
				"attachments": [
					{"type": "image", "url": "https://cdn.example/1.jpg"},
					{"type": "file", "url": "https://cdn.example/2.pdf"},
				],
			},
		)
		fields = self._inserted_fields()
		self.assertEqual(fields["message_type"], "Image")
		self.assertEqual(fields["attachment_url"], "https://cdn.example/1.jpg")
		self.assertEqual(fields["text_content"], "")
		self.assertEqual(fields["content"], "")
		self.assertNotIn("2.pdf", fields["text_content"])

	def test_duplicate_external_id_skips_insert(self):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if isinstance(filters, dict) and filters.get("external_message_id") == "dup-1":
				return "MSG-EXISTING"
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{"message_id": "dup-1", "text": "x", "direction": "inbound"},
		)
		self.assertEqual(name, "MSG-EXISTING")
		self.frappe.get_doc.assert_not_called()

	def test_same_timestamp_different_urls_do_not_collide(self):
		seen_hashes: list[str] = []

		def get_value(doctype, filters=None, fieldname=None, **kwargs):
			if isinstance(filters, dict) and "raw_payload_hash" in filters:
				h = filters["raw_payload_hash"]
				if h in seen_hashes:
					return "MSG-DUP"
				seen_hashes.append(h)
				return None
			return None

		self.frappe.db.get_value.side_effect = get_value
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"timestamp": "100",
				"attachments": [{"type": "image", "url": "https://cdn.example/a.jpg"}],
			},
		)
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"timestamp": "100",
				"attachments": [{"type": "audio", "url": "https://cdn.example/b.ogg"}],
			},
		)
		self.assertEqual(len(seen_hashes), 2)
		self.assertNotEqual(seen_hashes[0], seen_hashes[1])

	def test_legacy_empty_fingerprint_not_used_as_attachment_match(self):
		"""Conservative: attachment-only must not claim the legacy empty-text hash."""
		legacy_empty = self.sync._payload_hash(
			{"c": "CONV", "t": "", "s": "100", "d": "Inbound"}
		)
		looked_up: list[str] = []

		def get_value(doctype, filters=None, fieldname=None, **kwargs):
			if isinstance(filters, dict) and "raw_payload_hash" in filters:
				h = filters["raw_payload_hash"]
				looked_up.append(h)
				# Simulate a legacy Unknown attachment-only row under empty hash.
				if h == legacy_empty:
					return "MSG-LEGACY-EMPTY"
				return None
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{
				"direction": "inbound",
				"timestamp": "100",
				"attachments": [{"type": "image", "url": "https://cdn.example/a.jpg"}],
			},
		)
		self.assertEqual(name, "MSG-NEW")
		self.assertEqual(len(looked_up), 1)
		self.assertNotEqual(looked_up[0], legacy_empty)
		self.frappe.get_doc.assert_called_once()


if __name__ == "__main__":
	unittest.main()
