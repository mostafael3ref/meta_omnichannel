"""Offline tests for persist_message_attachment (mocked frappe + adapter)."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from meta_omnichannel.facebook_messenger.adapter import AttachmentPayload
from meta_omnichannel.meta_connectors.errors import ConnectorError


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.throw = MagicMock(
		side_effect=lambda *a, **k: (_ for _ in ()).throw(
			frappe.ValidationError(a[0] if a else "throw")
		)
	)
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock(return_value=[])
	frappe.delete_doc = MagicMock()
	frappe.utils = MagicMock()
	frappe.cache = MagicMock()
	sys.modules["frappe"] = frappe

	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda v: int(v or 0)  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils

	fm = types.ModuleType("frappe.utils.file_manager")
	fm.save_file = MagicMock()  # type: ignore[attr-defined]
	fm.get_file_path = MagicMock(return_value="/tmp/private/files/secret.bin")  # type: ignore[attr-defined]
	sys.modules["frappe.utils.file_manager"] = fm

	return frappe


class _Child(SimpleNamespace):
	pass


class _MessageDoc:
	def __init__(self, **kwargs):
		self.name = kwargs.get("name", "MSG-1")
		self.provider = kwargs.get("provider", "Facebook")
		self.conversation = kwargs.get("conversation", "CONV-1")
		self.external_message_id = kwargs.get("external_message_id", "m_ext_1")
		self.attachment_url = kwargs.get("attachment_url", "https://cdn.example/x.jpg")
		self.attachment_mime = kwargs.get("attachment_mime", "image/jpeg")
		self.attachments = list(kwargs.get("attachments") or [])
		self.flags = SimpleNamespace(ignore_permissions=False)
		self._saved = 0

	def get(self, key, default=None):
		return getattr(self, key, default)

	def save(self, ignore_permissions=False):
		self._saved += 1
		self.flags.ignore_permissions = ignore_permissions


class _ChildDoc:
	"""Fake child Document returned by frappe.get_doc({...})."""

	_seq = 0

	def __init__(self, values):
		_ChildDoc._seq += 1
		self.name = f"ATT-{_ChildDoc._seq}"
		self.doctype = values.get("doctype")
		for k, v in values.items():
			setattr(self, k, v)

	def insert(self, ignore_permissions=False):
		return self


class TestPersistMessageAttachment(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		import meta_omnichannel.facebook_messenger.media as media_mod

		importlib.reload(media_mod)
		cls.media = media_mod
		cls.save_file = sys.modules["frappe.utils.file_manager"].save_file
		cls.get_file_path = sys.modules["frappe.utils.file_manager"].get_file_path

	def setUp(self):
		self.frappe.db.reset_mock(side_effect=True, return_value=True)
		self.frappe.db.set_value.side_effect = None
		self.frappe.db.commit = MagicMock()
		self.frappe.db.rollback = MagicMock()
		self.frappe.get_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.get_all.reset_mock(side_effect=True, return_value=True)
		self.frappe.delete_doc.reset_mock(side_effect=True, return_value=True)
		self.frappe.throw.reset_mock(side_effect=True)
		self.frappe.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(
			self.frappe.ValidationError(a[0] if a else "throw")
		)
		self.save_file.reset_mock(side_effect=True, return_value=True)
		self.get_file_path.reset_mock(side_effect=True, return_value=True)
		self.get_file_path.return_value = "/tmp/private/files/secret.bin"
		self.frappe.get_all.return_value = []
		_ChildDoc._seq = 0

		self._lock_token: str | None = None
		self.cache = MagicMock()
		self.cache.make_key = MagicMock(side_effect=lambda k: f"site|{k}".encode())

		def _cache_set(key, token, **kwargs):
			self._lock_token = token
			return True

		def _cache_get(key):
			return self._lock_token

		def _cache_eval(script, numkeys, *args):
			# renew / release succeed only while this worker's token still owns the key
			token = args[1] if len(args) > 1 else None
			if self._lock_token is None:
				return 0  # TTL expired / key gone
			if token is not None and token != self._lock_token:
				return 0
			return 1

		self.cache.set = MagicMock(side_effect=_cache_set)
		self.cache.get = MagicMock(side_effect=_cache_get)
		self.cache.eval = MagicMock(side_effect=_cache_eval)
		self.frappe.cache = MagicMock(return_value=self.cache)

		self.msg = _MessageDoc()
		self.frappe.db.exists.return_value = True
		self.frappe.db.get_value.side_effect = self._db_get_value
		self.frappe.get_doc.side_effect = self._get_doc
		self._file_meta = {
			"FILE-1": SimpleNamespace(
				name="FILE-1",
				is_private=1,
				owner="Administrator",
				file_url="/private/files/secret.bin",
				attached_to_doctype="Omnichannel Message",
				attached_to_name="MSG-1",
			)
		}
		self._child_file_by_name: dict[str, str | None] = {}
		self._child_parent_by_name: dict[str, str] = {}
		self._child_rows_by_index: dict[int, SimpleNamespace] = {}
		self._disk_exists = True

		file_doc = SimpleNamespace(
			name="FILE-1",
			is_private=1,
			owner="Administrator",
			file_url="/private/files/secret.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="MSG-1",
		)
		self.save_file.return_value = file_doc

		self._os_exists_patcher = patch.object(self.media.os.path, "exists", side_effect=self._os_exists)
		self._os_exists_patcher.start()

	def tearDown(self):
		self._os_exists_patcher.stop()

	def _os_exists(self, path):
		return bool(self._disk_exists and path)

	def _get_doc(self, *args, **kwargs):
		if args and isinstance(args[0], dict):
			return _ChildDoc(args[0])
		return self.msg

	def _db_get_value(self, doctype, name=None, fieldname=None, *args, **kwargs):
		as_dict = kwargs.get("as_dict")
		if doctype == "File":
			meta = self._file_meta.get(name)
			if meta is None:
				return None
			if as_dict or isinstance(fieldname, (list, tuple)):
				return meta
			return name
		if doctype == "Omnichannel Conversation":
			return SimpleNamespace(page_id="PAGE-1", provider="Facebook")
		if doctype == "Omnichannel Message Attachment":
			if fieldname == "file":
				return self._child_file_by_name.get(name)
			if fieldname == "parent":
				return self._child_parent_by_name.get(name, "MSG-1")
			return None
		return None

	def _configure_get_all(self):
		def get_all(doctype, *a, **k):
			filters = k.get("filters") or {}
			if doctype == "Omnichannel Message Attachment":
				if "attachment_index" in filters:
					row = self._child_rows_by_index.get(filters["attachment_index"])
					return [row] if row else []
				if filters.get("status") == "Ready" and "content_hash" in filters:
					return []
				if "idx" in (k.get("fields") or []) or (k.get("order_by") or "").startswith("idx"):
					return []
			return []

		self.frappe.get_all.side_effect = get_all

	def _patch_adapter(self, payload: AttachmentPayload | None = None, exc: Exception | None = None):
		adapter = MagicMock()
		if exc:
			adapter.get_message_attachment.side_effect = exc
		else:
			adapter.get_message_attachment.return_value = payload or AttachmentPayload(
				content=b"\xff\xd8\xffJPEG",
				content_type="image/jpeg",
			)
		return patch.object(self.media, "_get_adapter", return_value=adapter), adapter

	def _set_value_calls(self, doctype: str):
		calls = []
		for c in self.frappe.db.set_value.call_args_list:
			args = c.args
			if args and args[0] == doctype:
				calls.append(c)
		return calls

	def test_lock_key_uniqueness(self):
		a = self.media.attachment_persist_lock_key("MSG-1", 0)
		b = self.media.attachment_persist_lock_key("MSG-1", 1)
		c = self.media.attachment_persist_lock_key("MSG-2", 0)
		self.assertNotEqual(a, b)
		self.assertNotEqual(a, c)
		self.assertIn("MSG-1", a)
		self.assertTrue(a.endswith(":0"))

	def test_lock_acquired_before_download(self):
		order: list[str] = []

		def set_lock(key, token, **k):
			order.append("lock")
			self._lock_token = token
			return True

		def download(*a, **k):
			order.append("download")
			return AttachmentPayload(content=b"abc", content_type="image/jpeg")

		self.cache.set.side_effect = set_lock
		adapter = MagicMock()
		adapter.get_message_attachment.side_effect = download
		with patch.object(self.media, "_get_adapter", return_value=adapter):
			self.media.persist_message_attachment("MSG-1", 0)
		self.assertEqual(order[0], "lock")
		self.assertIn("download", order)
		self.assertLess(order.index("lock"), order.index("download"))

	def test_lock_released_on_success(self):
		p, _ = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(result["ok"])
		self.cache.set.assert_called()
		self.cache.eval.assert_called()
		kwargs = self.cache.set.call_args
		self.assertTrue(kwargs.kwargs.get("nx") or (len(kwargs.args) > 3 and False) or kwargs.kwargs.get("nx") is True)
		self.assertEqual(kwargs.kwargs.get("ex"), self.media.ATTACHMENT_LOCK_TTL_SEC)

	def test_lock_released_on_failure(self):
		p, _ = self._patch_adapter(exc=ConnectorError("get_message_attachment: unexpected HTTP 404"))
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertFalse(result["ok"])
		self.assertEqual(result["status"], "Failed")
		self.cache.eval.assert_called()

	def test_contention_does_not_download(self):
		self.cache.set.side_effect = None
		self.cache.set.return_value = False
		p, adapter = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertFalse(result["ok"])
		self.assertTrue(result["retryable"])
		self.assertFalse(result["downloaded"])
		self.assertNotEqual(result["status"], "Failed")
		adapter.get_message_attachment.assert_not_called()
		self.save_file.assert_not_called()
		self.cache.eval.assert_not_called()

	def test_contention_does_not_mark_failed(self):
		self.cache.set.side_effect = None
		self.cache.set.return_value = False
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		failed = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if isinstance(c.args[2], dict) and c.args[2].get("status") == "Failed"
		]
		self.assertEqual(failed, [])

	def test_no_long_database_for_update_lock(self):
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		for c in self.frappe.get_doc.call_args_list:
			self.assertNotEqual(c.kwargs.get("for_update"), True)
			# Positional for_update is not used by our call pattern.
			if len(c.args) >= 3:
				self.assertIsNot(c.args[2], True)

	def test_no_commit_or_rollback_inside_persist(self):
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		self.frappe.db.commit.assert_not_called()
		self.frappe.db.rollback.assert_not_called()

	def test_successful_private_save(self):
		p, adapter = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "Ready")
		self.assertEqual(result["file"], "FILE-1")
		self.assertTrue(result["downloaded"])
		self.assertFalse(result["retryable"])
		self.assertNotIn("file_url", result)
		self.save_file.assert_called_once()
		self.assertEqual(self.save_file.call_args.kwargs.get("is_private"), 1)
		adapter.get_message_attachment.assert_called_once_with(
			page_id="PAGE-1", message_id="m_ext_1", attachment_index=0
		)

	def test_repeated_call_skips_download(self):
		ready = SimpleNamespace(
			name="ATT-READY",
			attachment_index=0,
			status="Ready",
			file="FILE-1",
			content_hash="abc",
			mime_type="image/jpeg",
			attachment_type="image",
			error_summary=None,
		)
		self._child_rows_by_index[0] = ready
		self._configure_get_all()
		p, adapter = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(result["ok"])
		self.assertFalse(result["downloaded"])
		adapter.get_message_attachment.assert_not_called()
		self.save_file.assert_not_called()

	def test_invalid_index(self):
		for bad in (-1, True, False, 1.5, "0"):
			with self.assertRaises(self.frappe.ValidationError):
				self.media.persist_message_attachment("MSG-1", bad)  # type: ignore[arg-type]

	def test_missing_message(self):
		self.frappe.db.exists.return_value = False
		with self.assertRaises(self.frappe.ValidationError):
			self.media.persist_message_attachment("MSG-GONE", 0)

	def test_wrong_channel(self):
		self.msg.provider = "Instagram"
		with self.assertRaises(self.frappe.ValidationError):
			self.media.persist_message_attachment("MSG-1", 0)

	def test_ready_row_with_missing_file_redownloads(self):
		ready = SimpleNamespace(
			name="ATT-1",
			attachment_index=0,
			status="Ready",
			file="FILE-MISSING",
			content_hash="old",
			mime_type="image/jpeg",
			attachment_type="image",
			error_summary=None,
		)
		self._child_rows_by_index[0] = ready
		self._configure_get_all()
		self._child_file_by_name["ATT-1"] = "FILE-MISSING"
		self._child_parent_by_name["ATT-1"] = "MSG-1"
		p, adapter = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(result["ok"])
		self.assertTrue(result["downloaded"])
		adapter.get_message_attachment.assert_called_once()

	def test_failed_download_safe_error(self):
		exc = ConnectorError("get_message_attachment: unexpected HTTP 404")
		p, _ = self._patch_adapter(exc=exc)
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertFalse(result["ok"])
		self.assertEqual(result["status"], "Failed")
		self.assertFalse(result["retryable"])
		self.assertNotIn("https://", (result["error"] or "").lower())
		self.save_file.assert_not_called()

	def test_duplicate_child_row_avoidance(self):
		existing = SimpleNamespace(
			name="ATT-1",
			attachment_index=0,
			status="Failed",
			file=None,
			content_hash=None,
			mime_type=None,
			attachment_type=None,
			error_summary="old",
		)
		self._child_rows_by_index[0] = existing
		self._configure_get_all()
		self._child_file_by_name["ATT-1"] = None
		self._child_parent_by_name["ATT-1"] = "MSG-1"
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		insert_dicts = [
			c.args[0]
			for c in self.frappe.get_doc.call_args_list
			if c.args and isinstance(c.args[0], dict)
		]
		self.assertEqual(insert_dicts, [])
		pending = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if c.args[1] == "ATT-1" and c.args[2].get("status") == "Pending"
		]
		self.assertTrue(pending)

	def test_system_ownership(self):
		file_doc = SimpleNamespace(
			name="FILE-1",
			is_private=1,
			owner="agent@example.com",
			file_url="/private/files/x.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="MSG-1",
		)
		self.save_file.return_value = file_doc
		self._file_meta["FILE-1"] = SimpleNamespace(
			name="FILE-1",
			is_private=1,
			owner="Administrator",
			file_url="/private/files/x.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="MSG-1",
		)
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		self.frappe.db.set_value.assert_any_call(
			"File",
			"FILE-1",
			{"owner": "Administrator"},
			update_modified=False,
		)
		for c in self._set_value_calls("File"):
			vals = c.args[2]
			if isinstance(vals, dict):
				self.assertNotIn("is_private", vals)

	def test_rejects_public_file_without_db_privacy_flip(self):
		"""Invalid File after save is infrastructure validation — must not become Failed."""
		file_doc = SimpleNamespace(
			name="FILE-PUB",
			is_private=0,
			owner="Administrator",
			file_url="/files/public.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="MSG-1",
		)
		self.save_file.return_value = file_doc
		self._file_meta["FILE-PUB"] = SimpleNamespace(
			name="FILE-PUB",
			is_private=0,
			owner="Administrator",
			file_url="/files/public.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="MSG-1",
		)
		p, _ = self._patch_adapter()
		with p:
			with self.assertRaises(self.frappe.ValidationError):
				self.media.persist_message_attachment("MSG-1", 0)
		for c in self._set_value_calls("File"):
			vals = c.args[2]
			if isinstance(vals, dict):
				self.assertNotIn("is_private", vals)
		failed = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if isinstance(c.args[2], dict) and c.args[2].get("status") == "Failed"
		]
		self.assertEqual(failed, [])
		# Lock still released.
		self.cache.eval.assert_called()

	def test_no_public_file_creation(self):
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		self.assertEqual(self.save_file.call_args.kwargs.get("is_private"), 1)

	def test_legacy_fields_unchanged_and_not_restored(self):
		legacy_url = self.msg.attachment_url
		legacy_mime = self.msg.attachment_mime
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		self.assertEqual(self.msg.attachment_url, legacy_url)
		self.assertEqual(self.msg.attachment_mime, legacy_mime)
		for c in self._set_value_calls("Omnichannel Message"):
			vals = c.args[2]
			if isinstance(vals, dict):
				self.assertNotIn("attachment_url", vals)
				self.assertNotIn("attachment_mime", vals)

	def test_safe_filename_deterministic(self):
		name = self.media._safe_filename("MSG/../evil name", 2, "application/pdf")
		self.assertTrue(name.startswith("omni_"))
		self.assertIn("_2.", name)
		self.assertTrue(name.endswith(".pdf"))
		self.assertNotIn("..", name)

	def test_invalid_file_fails_ownership_or_attachment_target(self):
		file_doc = SimpleNamespace(
			name="FILE-1",
			is_private=1,
			owner="Administrator",
			file_url="/private/files/secret.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="OTHER-MSG",
		)
		self.save_file.return_value = file_doc
		self._file_meta["FILE-1"] = SimpleNamespace(
			name="FILE-1",
			is_private=1,
			owner="Administrator",
			file_url="/private/files/secret.bin",
			attached_to_doctype="Omnichannel Message",
			attached_to_name="OTHER-MSG",
		)
		p, _ = self._patch_adapter()
		with p:
			with self.assertRaises(self.frappe.ValidationError):
				self.media.persist_message_attachment("MSG-1", 0)
		failed = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if isinstance(c.args[2], dict) and c.args[2].get("status") == "Failed"
		]
		self.assertEqual(failed, [])

	def test_missing_physical_file_rejected(self):
		self._disk_exists = False
		p, _ = self._patch_adapter()
		with p:
			with self.assertRaises(self.frappe.ValidationError):
				self.media.persist_message_attachment("MSG-1", 0)
		failed = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if isinstance(c.args[2], dict) and c.args[2].get("status") == "Failed"
		]
		self.assertEqual(failed, [])

	def test_failure_status_update_error_not_swallowed(self):
		exc = ConnectorError("get_message_attachment: unexpected HTTP 500")
		p, _ = self._patch_adapter(exc=exc)

		def boom(*a, **k):
			raise RuntimeError("status write failed")

		self.frappe.db.set_value.side_effect = boom
		with p:
			with self.assertRaises(RuntimeError):
				self.media.persist_message_attachment("MSG-1", 0)
		# Lock must still be released.
		self.cache.eval.assert_called()

	def test_retry_after_failed_succeeds(self):
		failed = SimpleNamespace(
			name="ATT-1",
			attachment_index=0,
			status="Failed",
			file=None,
			content_hash=None,
			mime_type=None,
			attachment_type=None,
			error_summary="ConnectorError: prior",
		)
		self._child_rows_by_index[0] = failed
		self._configure_get_all()
		self._child_file_by_name["ATT-1"] = None
		self._child_parent_by_name["ATT-1"] = "MSG-1"
		p, adapter = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "Ready")
		adapter.get_message_attachment.assert_called_once()

	def test_does_not_save_full_message_document(self):
		p, _ = self._patch_adapter()
		with p:
			self.media.persist_message_attachment("MSG-1", 0)
		self.assertEqual(self.msg._saved, 0)

	def test_lock_ownership_loss_before_finalize(self):
		"""If Redis ownership is lost after download, do not mark Ready/Failed."""

		def steal_after_download(*a, **k):
			self._lock_token = "stolen-by-other-worker"
			return AttachmentPayload(content=b"abc", content_type="image/jpeg")

		adapter = MagicMock()
		adapter.get_message_attachment.side_effect = steal_after_download
		with patch.object(self.media, "_get_adapter", return_value=adapter):
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertFalse(result["ok"])
		self.assertTrue(result["retryable"])
		self.assertTrue(result["lock_lost"])
		self.assertNotEqual(result["status"], "Failed")
		self.assertNotEqual(result["status"], "Ready")
		self.save_file.assert_not_called()
		failed = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if isinstance(c.args[2], dict) and c.args[2].get("status") == "Failed"
		]
		self.assertEqual(failed, [])

	def test_lock_expiry_during_download_returns_lock_lost(self):
		"""TTL/ownership lost mid-Connector call → renew after download fails closed."""

		def expire_during_download(*a, **k):
			self._lock_token = None  # simulate Redis TTL expiry
			return AttachmentPayload(content=b"abc", content_type="image/jpeg")

		adapter = MagicMock()
		adapter.get_message_attachment.side_effect = expire_during_download
		with patch.object(self.media, "_get_adapter", return_value=adapter):
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(self.media.is_lock_lost_result(result))
		self.assertTrue(result["retryable"])
		self.assertNotEqual(result["status"], "Failed")
		self.save_file.assert_not_called()

	def test_core_lock_lost_result_is_retryable(self):
		out = self.media._lock_lost_result("MSG-1", 0, downloaded=True)
		self.assertTrue(self.media.is_lock_lost_result(out))
		self.assertTrue(out["retryable"])
		self.assertTrue(out["lock_lost"])
		self.assertFalse(out["ok"])
		self.assertEqual(out["status"], "Pending")

	def test_db_exception_propagates_not_failed(self):
		"""Unexpected DB errors must not become committed Failed."""

		def boom_set_value(*a, **k):
			raise RuntimeError("deadlock detected")

		self.frappe.db.set_value.side_effect = boom_set_value
		p, _ = self._patch_adapter()
		with p:
			with self.assertRaises(RuntimeError):
				self.media.persist_message_attachment("MSG-1", 0)
		# No Failed child write succeeded (set_value always boom).
		self.cache.eval.assert_called()  # lock released

	def test_ordinary_connector_failure_marks_failed(self):
		p, _ = self._patch_adapter(exc=ConnectorError("get_message_attachment: unexpected HTTP 502"))
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertEqual(result["status"], "Failed")
		self.assertFalse(result.get("lock_lost"))
		self.assertFalse(result["retryable"])
		failed = [
			c
			for c in self._set_value_calls("Omnichannel Message Attachment")
			if isinstance(c.args[2], dict) and c.args[2].get("status") == "Failed"
		]
		self.assertTrue(failed)

	def test_sequential_retries_no_duplicate_child_rows(self):
		"""Failed then Ready reuse the same child; no second insert."""
		existing = SimpleNamespace(
			name="ATT-1",
			attachment_index=0,
			status="Failed",
			file=None,
			content_hash=None,
			mime_type=None,
			attachment_type=None,
			error_summary="prior",
		)
		self._child_rows_by_index[0] = existing
		self._configure_get_all()
		self._child_file_by_name["ATT-1"] = None
		self._child_parent_by_name["ATT-1"] = "MSG-1"

		p, _ = self._patch_adapter()
		with p:
			r1 = self.media.persist_message_attachment("MSG-1", 0)
			# Simulate Ready child for second call skip path is separate;
			# call again while still Pending→Ready path with same row.
			self._child_rows_by_index[0] = SimpleNamespace(
				name="ATT-1",
				attachment_index=0,
				status="Failed",
				file=None,
				content_hash=None,
				mime_type=None,
				attachment_type=None,
				error_summary="prior",
			)
			r2 = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(r1["ok"])
		self.assertTrue(r2["ok"])
		insert_dicts = [
			c.args[0]
			for c in self.frappe.get_doc.call_args_list
			if c.args and isinstance(c.args[0], dict)
		]
		self.assertEqual(insert_dicts, [])

	def test_private_file_validation_remains_intact(self):
		p, _ = self._patch_adapter()
		with p:
			result = self.media.persist_message_attachment("MSG-1", 0)
		self.assertTrue(result["ok"])
		self.assertEqual(self.save_file.call_args.kwargs.get("is_private"), 1)
		self.assertTrue(
			self.media._file_is_valid_private("FILE-1", "MSG-1")
		)


if __name__ == "__main__":
	unittest.main()