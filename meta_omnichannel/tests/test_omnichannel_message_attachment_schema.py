"""Schema-only checks for Omnichannel Message Attachment child table."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

DOCTYPE_ROOT = (
	Path(__file__).resolve().parents[1] / "meta_omnichannel" / "doctype"
)
CHILD_JSON = (
	DOCTYPE_ROOT
	/ "omnichannel_message_attachment"
	/ "omnichannel_message_attachment.json"
)
PARENT_JSON = DOCTYPE_ROOT / "omnichannel_message" / "omnichannel_message.json"


class TestOmnichannelMessageAttachmentSchema(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.child = json.loads(CHILD_JSON.read_text(encoding="utf-8"))
		cls.parent = json.loads(PARENT_JSON.read_text(encoding="utf-8"))
		cls.child_fields = {f["fieldname"]: f for f in cls.child["fields"]}
		cls.parent_fields = {f["fieldname"]: f for f in cls.parent["fields"]}

	def test_child_is_table_without_permissions(self):
		self.assertEqual(self.child["name"], "Omnichannel Message Attachment")
		self.assertEqual(self.child.get("istable"), 1)
		self.assertEqual(self.child.get("editable_grid"), 1)
		self.assertEqual(self.child.get("module"), "Meta Omnichannel")
		self.assertEqual(self.child.get("permissions"), [])
		# Child tables are not independently named/autonamed for user lists.
		self.assertNotIn("autoname", self.child)

	def test_required_child_fields(self):
		expected = {
			"attachment_index": ("Int", True),
			"attachment_type": ("Data", False),
			"mime_type": ("Data", False),
			"file": ("Link", False),
			"content_hash": ("Data", False),
			"status": ("Select", True),
			"error_summary": ("Small Text", False),
		}
		for name, (ftype, reqd) in expected.items():
			self.assertIn(name, self.child_fields, name)
			field = self.child_fields[name]
			self.assertEqual(field["fieldtype"], ftype, name)
			self.assertEqual(bool(field.get("reqd")), reqd, name)

		self.assertEqual(self.child_fields["file"]["options"], "File")
		self.assertEqual(
			self.child_fields["status"]["options"],
			"Pending\nReady\nFailed",
		)
		self.assertEqual(self.child_fields["status"].get("default"), "Pending")

	def test_attachment_index_not_marked_unique(self):
		"""Uniqueness is persistence-layer idempotency -- not a DB unique on the field."""
		field = self.child_fields["attachment_index"]
		self.assertFalse(bool(field.get("unique")))

	def test_parent_has_attachments_table(self):
		field = self.parent_fields["attachments"]
		self.assertEqual(field["fieldtype"], "Table")
		self.assertEqual(field["options"], "Omnichannel Message Attachment")
		self.assertIn("attachments", self.parent["field_order"])

	def test_legacy_attachment_fields_preserved(self):
		"""Parent still exposes legacy single-attachment and identity fields."""
		legacy = (
			"attachment_url",
			"attachment_mime",
			"external_message_id",
			"message_type",
		)
		for name in legacy:
			self.assertIn(name, self.parent_fields, f"missing legacy field: {name}")
			self.assertTrue(
				self.parent_fields[name].get("fieldtype"),
				f"legacy field {name} has no fieldtype",
			)
		# Full CDN URLs require Small Text (not Data/varchar 140).
		self.assertEqual(
			self.parent_fields["attachment_url"]["fieldtype"],
			"Small Text",
		)
		self.assertEqual(self.parent_fields["attachment_mime"]["fieldtype"], "Data")
		self.assertEqual(self.parent_fields["external_message_id"]["fieldtype"], "Data")
		self.assertEqual(self.parent_fields["message_type"]["fieldtype"], "Select")

	def test_field_order_matches_fields(self):
		self.assertEqual(
			set(self.child["field_order"]),
			{f["fieldname"] for f in self.child["fields"]},
		)
		self.assertEqual(
			set(self.parent["field_order"]),
			{f["fieldname"] for f in self.parent["fields"]},
		)

	def test_json_is_valid_and_loadable(self):
		# Round-trip ensures no trailing commas / encoding issues for migrate.
		json.dumps(self.child)
		json.dumps(self.parent)
		self.assertTrue(CHILD_JSON.is_file())
		py = CHILD_JSON.with_name("omnichannel_message_attachment.py")
		self.assertTrue(py.is_file())
		self.assertIn("OmnichannelMessageAttachment", py.read_text(encoding="utf-8"))


if __name__ == "__main__":
	unittest.main()
