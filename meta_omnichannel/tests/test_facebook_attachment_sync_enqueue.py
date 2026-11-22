"""Focused tests: sync schedules attachment persist after DB commit."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _AfterCommit:
	"""Minimal stand-in for Frappe v15 ``frappe.db.after_commit`` CallbackManager."""

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


class TestFacebookAttachmentSyncEnqueue(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.sync as sync_mod

		importlib.reload(sync_mod)
		cls.sync = sync_mod

	def setUp(self):
		self.frappe.db.get_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.set_value.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None
		self.frappe.db.after_commit = _AfterCommit()
		self.frappe.enqueue.reset_mock()
		doc = MagicMock()
		doc.name = "MSG-NEW"
		self.frappe.get_doc.return_value = doc

	def _attachment_row(self, **extra):
		row = {
			"direction": "inbound",
			"message_id": "m-att-1",
			"text": "caption",
			"attachments": [{"type": "image", "url": "https://cdn.example/x.png"}],
		}
		row.update(extra)
		return row

	def test_successful_enqueue_after_commit(self):
		"""New attachment message registers after_commit; enqueue runs only then."""
		with patch.object(
			self.sync, "_enqueue_facebook_attachment_persist_safe"
		) as safe_enqueue:
			name = self.sync.upsert_message_from_connector("CONV", "CH", self._attachment_row())
			self.assertEqual(name, "MSG-NEW")
			safe_enqueue.assert_not_called()
			self.assertEqual(len(self.frappe.db.after_commit.callbacks), 1)
			self.frappe.db.after_commit.run()
			safe_enqueue.assert_called_once_with("MSG-NEW")

	def test_enqueue_helper_reused_on_commit(self):
		"""Callback must call jobs.sync_jobs.enqueue_facebook_attachment_persist."""
		with patch(
			"meta_omnichannel.jobs.sync_jobs.enqueue_facebook_attachment_persist"
		) as enqueue:
			self.sync.upsert_message_from_connector("CONV", "CH", self._attachment_row())
			enqueue.assert_not_called()
			self.frappe.db.after_commit.run()
			enqueue.assert_called_once_with("MSG-NEW", 0)

	def test_text_only_never_schedules(self):
		self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{"direction": "inbound", "message_id": "m-text", "text": "hello only"},
		)
		self.assertEqual(self.frappe.db.after_commit.callbacks, [])

	def test_duplicate_existing_with_attachment_schedules(self):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if isinstance(filters, dict) and filters.get("external_message_id") == "dup-att":
				if kwargs.get("as_dict") or isinstance(fieldname, (list, tuple)):
					return SimpleNamespace(name="MSG-EXISTING", sent_at="2026-01-01 12:00:00")
				return "MSG-EXISTING"
			# Stored metadata lookup for schedule decision
			if filters == "MSG-EXISTING" and fieldname == "attachment_url":
				return "https://cdn.example/stored.png"
			return None

		self.frappe.db.get_value.side_effect = get_value
		with patch.object(
			self.sync, "_enqueue_facebook_attachment_persist_safe"
		) as safe_enqueue:
			name = self.sync.upsert_message_from_connector(
				"CONV",
				"CH",
				self._attachment_row(message_id="dup-att"),
			)
			self.assertEqual(name, "MSG-EXISTING")
			self.frappe.get_doc.assert_not_called()
			self.assertEqual(len(self.frappe.db.after_commit.callbacks), 1)
			self.frappe.db.after_commit.run()
			safe_enqueue.assert_called_once_with("MSG-EXISTING")

	def test_duplicate_text_only_does_not_schedule(self):
		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			if isinstance(filters, dict) and filters.get("external_message_id") == "dup-text":
				if kwargs.get("as_dict") or isinstance(fieldname, (list, tuple)):
					return SimpleNamespace(name="MSG-TEXT", sent_at="2026-01-01 12:00:00")
				return "MSG-TEXT"
			if filters == "MSG-TEXT" and fieldname == "attachment_url":
				return None
			return None

		self.frappe.db.get_value.side_effect = get_value
		name = self.sync.upsert_message_from_connector(
			"CONV",
			"CH",
			{"direction": "inbound", "message_id": "dup-text", "text": "hi"},
		)
		self.assertEqual(name, "MSG-TEXT")
		self.assertEqual(self.frappe.db.after_commit.callbacks, [])

	def test_cross_conversation_external_id_does_not_schedule(self):
		"""Same external_message_id in another conversation: dedupe return, no enqueue."""

		def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
			# Same-conversation lookup misses.
			if (
				isinstance(filters, dict)
				and filters.get("external_message_id") == "cross-mid"
				and filters.get("conversation") == "CONV-B"
			):
				return None
			# Cross-conversation unique-id hit.
			if (
				isinstance(filters, dict)
				and filters.get("external_message_id") == "cross-mid"
				and "conversation" not in filters
			):
				return "MSG-OTHER-CONV"
			# Must not be consulted for scheduling on this path.
			if filters == "MSG-OTHER-CONV":
				self.fail("must not look up attachment metadata for cross-conversation hit")
			return None

		self.frappe.db.get_value.side_effect = get_value
		with patch.object(
			self.sync, "_enqueue_facebook_attachment_persist_safe"
		) as safe_enqueue:
			with patch.object(
				self.sync, "schedule_facebook_attachment_persist_after_commit"
			) as schedule:
				name = self.sync.upsert_message_from_connector(
					"CONV-B",
					"CH",
					self._attachment_row(message_id="cross-mid"),
				)
		self.assertEqual(name, "MSG-OTHER-CONV")
		self.frappe.get_doc.assert_not_called()
		self.assertEqual(self.frappe.db.after_commit.callbacks, [])
		schedule.assert_not_called()
		safe_enqueue.assert_not_called()

	def test_already_ready_skips_job_after_commit(self):
		"""after_commit still fires; enqueue helper skips Ready attachments."""
		with patch(
			"meta_omnichannel.jobs.sync_jobs.enqueue_facebook_attachment_persist",
			return_value=None,
		) as enqueue:
			self.sync.upsert_message_from_connector("CONV", "CH", self._attachment_row())
			self.frappe.db.after_commit.run()
			enqueue.assert_called_once_with("MSG-NEW", 0)

	def test_failed_enqueue_does_not_break_sync(self):
		with patch(
			"meta_omnichannel.jobs.sync_jobs.enqueue_facebook_attachment_persist",
			side_effect=RuntimeError("redis down"),
		):
			name = self.sync.upsert_message_from_connector(
				"CONV", "CH", self._attachment_row()
			)
			self.assertEqual(name, "MSG-NEW")
			# Callback must not raise into the caller that runs after_commit.
			self.frappe.db.after_commit.run()

	def test_schedule_failure_does_not_break_upsert(self):
		self.frappe.db.after_commit.add = MagicMock(side_effect=RuntimeError("hook broken"))
		name = self.sync.upsert_message_from_connector(
			"CONV", "CH", self._attachment_row()
		)
		self.assertEqual(name, "MSG-NEW")

	def test_rollback_does_not_enqueue(self):
		"""Callbacks registered on after_commit are not run when txn rolls back."""
		with patch.object(
			self.sync, "_enqueue_facebook_attachment_persist_safe"
		) as safe_enqueue:
			self.sync.upsert_message_from_connector("CONV", "CH", self._attachment_row())
			self.assertEqual(len(self.frappe.db.after_commit.callbacks), 1)
			# Simulate rollback: discard hooks without running them.
			self.frappe.db.after_commit.clear()
			safe_enqueue.assert_not_called()

	def test_unread_and_insert_behavior_preserved(self):
		"""Scheduling must not skip insert or invent multi-attachment indexes."""
		with patch.object(self.sync, "schedule_facebook_attachment_persist_after_commit"):
			self.sync.upsert_message_from_connector("CONV", "CH", self._attachment_row())
		fields = self.frappe.get_doc.call_args.args[0]
		self.assertEqual(fields["attachment_url"], "https://cdn.example/x.png")
		self.assertEqual(fields["message_type"], "Image")
		self.assertEqual(fields["text_content"], "caption")


if __name__ == "__main__":
	unittest.main()
