"""Outbound Facebook image send: idempotency, private File, API gate, delivery states."""

from __future__ import annotations

import hashlib
import importlib
import io
import sys
import types
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from meta_omnichannel.facebook_messenger.adapter import MAX_OUTBOUND_DOCUMENT_BYTES
from meta_omnichannel.meta_connectors.errors import (
	ConnectorError,
	ConnectorForbiddenError,
	ConnectorServerError,
	ConnectorUnreachableError,
)
from meta_omnichannel.utils.idempotency import (
	build_outbound_idempotency_key,
	build_outbound_image_idempotency_key,
	outbound_image_nonce_key_prefix,
)

_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _png(n: int = 64) -> bytes:
	return b"\x89PNG\r\n\x1a\n" + b"\x00" * n


def _jpeg(n: int = 64) -> bytes:
	return b"\xff\xd8\xff\xe0" + b"\x00" * n


def _pdf() -> bytes:
	return (
		b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
		b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
		b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
	)


def _docx() -> bytes:
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w") as zf:
		zf.writestr(
			"[Content_Types].xml",
			'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
			'<Override PartName="/word/document.xml" '
			'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
			"</Types>",
		)
		zf.writestr(
			"word/document.xml",
			'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
			"<w:body><w:p/></w:body></w:document>",
		)
	return buf.getvalue()


class TestOutboundImageIdempotencyHelpers(unittest.TestCase):
	def test_stable_image_key_is_nonce_scoped(self):
		sha = hashlib.sha256(_png()).hexdigest()
		a = build_outbound_image_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha
		)
		b = build_outbound_image_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha
		)
		self.assertEqual(a, b)
		self.assertTrue(a.startswith("oc-img:"))
		self.assertEqual(
			a,
			outbound_image_nonce_key_prefix(
				conversation_name="CONV-1", client_nonce="n1"
			),
		)

	def test_same_nonce_same_key_regardless_of_image_hash(self):
		"""DB unique fence is nonce-scoped; content hash is stored separately."""
		sha1 = hashlib.sha256(_png(10)).hexdigest()
		sha2 = hashlib.sha256(_png(11)).hexdigest()
		a = build_outbound_image_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha1
		)
		b = build_outbound_image_idempotency_key(
			conversation_name="CONV-1", client_nonce="n1", content_sha256=sha2
		)
		self.assertEqual(a, b)
		self.assertNotEqual(sha1, sha2)

	def test_text_key_algorithm_unchanged(self):
		a = build_outbound_idempotency_key(
			conversation_name="CONV-1", text="hi", client_nonce="n"
		)
		b = build_outbound_idempotency_key(
			conversation_name="CONV-1", text="hi", client_nonce="n"
		)
		self.assertEqual(a, b)
		self.assertTrue(a.startswith("oc:"))
		self.assertFalse(a.startswith("oc-img:"))


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock(return_value=[])
	frappe._ = lambda x: x
	frappe.throw = MagicMock(
		side_effect=lambda *a, **k: (_ for _ in ()).throw(Exception(a[0] if a else "throw"))
	)
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.UniqueValidationError = type("UniqueValidationError", (Exception,), {})
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	frappe.cache = MagicMock()
	cache = MagicMock()
	cache.make_key = lambda key, *a, **k: f"site|{key}"
	cache.set = MagicMock(return_value=True)
	cache.get = MagicMock(return_value=None)
	cache.delete = MagicMock()
	cache.eval = MagicMock(return_value=1)
	frappe.cache.return_value = cache
	frappe.request = MagicMock()
	frappe.request.files = {}
	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = MagicMock(return_value="2026-11-23 12:00:00")  # type: ignore[attr-defined]
	utils.cint = int  # type: ignore[attr-defined]
	fm = types.ModuleType("frappe.utils.file_manager")
	fm.save_file = MagicMock()  # type: ignore[attr-defined]
	fm.get_file_path = MagicMock(return_value="/tmp/private/files/x.bin")  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	sys.modules["frappe.utils.file_manager"] = fm
	frappe.utils = utils
	return frappe


class TestSendFacebookImage(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		for name in list(sys.modules):
			if name.startswith("meta_omnichannel.facebook_messenger.outbound") or name.startswith(
				"meta_omnichannel.facebook_messenger.media"
			):
				del sys.modules[name]
		cls.outbound = importlib.import_module("meta_omnichannel.facebook_messenger.outbound")
		cls.media = importlib.import_module("meta_omnichannel.facebook_messenger.media")

	def setUp(self):
		f = self.frappe
		f.db.reset_mock()
		f.get_doc.reset_mock()
		f.get_all.reset_mock()
		f.get_all.return_value = []
		f.throw.reset_mock()
		f.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(
			Exception(str(a[0]) if a else "throw")
		)
		f.db.commit = MagicMock()
		f.db.rollback = MagicMock()
		f.db.set_value = MagicMock()
		f.db.exists = MagicMock(return_value=False)
		f.db.get_value = MagicMock(return_value=None)
		f.cache.return_value.set.return_value = True

		self.conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CHAN-1",
			page_id="PAGE-1",
			participant_id="PSID-1",
		)
		f.get_doc.side_effect = self._get_doc
		self.adapter = MagicMock()
		self.adapter.send_image.return_value = {
			"status": "sent",
			"message_id": "mid-1",
			"duplicate": False,
		}

	def _get_doc(self, arg, name=None):
		if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
			doc = MagicMock()
			doc.name = "MSG-IMG-1"
			doc.insert = MagicMock()
			for k, v in arg.items():
				setattr(doc, k, v)
			return doc
		return self.conv

	def test_rejects_invalid_bytes(self):
		with self.assertRaises(Exception) as ctx:
			self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=b"not-an-image",
				filename="x.png",
				content_type="image/png",
			)
		self.assertIn("PNG", str(ctx.exception).upper())

	def test_rejects_mime_mismatch(self):
		with self.assertRaises(Exception) as ctx:
			self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="x.jpg",
				content_type="image/jpeg",
			)
		self.assertIn("MIME", str(ctx.exception))

	def test_rejects_oversized(self):
		from meta_omnichannel.facebook_messenger.adapter import MAX_OUTBOUND_IMAGE_BYTES

		huge = b"\xff\xd8\xff" + b"\x00" * MAX_OUTBOUND_IMAGE_BYTES
		with self.assertRaises(Exception) as ctx:
			self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=huge,
				filename="big.jpg",
				content_type="image/jpeg",
			)
		self.assertIn("8 MiB", str(ctx.exception))

	def test_success_persists_then_sends(self):
		attach = MagicMock(
			return_value={"ok": True, "status": "Ready", "file": "FILE-1", "content_hash": "abc"}
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch.dict(
			sys.modules,
			{"meta_omnichannel.facebook_messenger.media": MagicMock(attach_outbound_image_file=attach)},
		):
			# Re-import path uses the patched module name inside function — patch the attribute on media after import.
			pass

		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file",
			attach,
		):
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["delivery_status"], "Sent")
		attach.assert_called_once()
		self.adapter.send_image.assert_called_once()
		self.assertEqual(self.adapter.send_image.call_args.kwargs["content_type"], "image/png")

	def test_same_nonce_same_image_dedupes_without_connector(self):
		sha = hashlib.sha256(_png()).hexdigest()
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OLD",
			delivery_status="Sent",
			error_summary=None,
			raw_payload_hash=sha,
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out["deduplicated"])
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_same_nonce_different_image_conflict(self):
		png = _png(8)
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OTHER",
			delivery_status="Sent",
			error_summary=None,
			raw_payload_hash=hashlib.sha256(_png(9)).hexdigest(),
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=png,
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out.get("conflict"))
		self.assertFalse(out["ok"])
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_lock_busy_without_row_is_uncertain_no_send(self):
		self.frappe.cache.return_value.set.return_value = False
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["uncertain"])
		self.assertFalse(out["ok"])
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_redis_down_same_image_unique_insert_dedupes(self):
		"""When Redis fails, unique nonce key still prevents a second Connector call."""
		png = _png()
		sha = hashlib.sha256(png).hexdigest()
		self.frappe.cache.return_value.set.side_effect = RuntimeError("redis down")

		def _get_value(*_a, **_k):
			# First check miss, after unique race return winner.
			if getattr(self, "_after_dup", False):
				return SimpleNamespace(
					name="MSG-WIN",
					delivery_status="Queued",
					error_summary=None,
					raw_payload_hash=sha,
				)
			return None

		self.frappe.db.get_value.side_effect = _get_value

		def _boom_insert(*_a, **_k):
			self._after_dup = True
			raise self.frappe.UniqueValidationError("Duplicate entry for idempotency_key")

		doc = MagicMock()
		doc.name = "MSG-LOSE"
		doc.insert.side_effect = _boom_insert

		def _get_doc(arg, name=None):
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
				for k, v in arg.items():
					setattr(doc, k, v)
				return doc
			return self.conv

		self.frappe.get_doc.side_effect = _get_doc
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=png,
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["uncertain"] or out.get("deduplicated"))
		self.assertEqual(out["message"], "MSG-WIN")
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()
		self.frappe.db.rollback.assert_called()

	def test_redis_down_different_image_unique_conflict(self):
		png = _png(8)
		self.frappe.cache.return_value.set.side_effect = RuntimeError("redis down")
		self._after_dup = False

		def _get_value(*_a, **_k):
			if getattr(self, "_after_dup", False):
				return SimpleNamespace(
					name="MSG-WIN",
					delivery_status="Sent",
					error_summary=None,
					raw_payload_hash=hashlib.sha256(_png(9)).hexdigest(),
				)
			return None

		self.frappe.db.get_value.side_effect = _get_value

		def _boom_insert(*_a, **_k):
			self._after_dup = True
			raise self.frappe.UniqueValidationError("Duplicate entry for idempotency_key")

		doc = MagicMock()
		doc.name = "MSG-LOSE"
		doc.insert.side_effect = _boom_insert

		def _get_doc(arg, name=None):
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
				return doc
			return self.conv

		self.frappe.get_doc.side_effect = _get_doc
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=png,
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out.get("conflict"))
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_redis_down_first_request_can_send(self):
		"""Redis outage must not block the first DB reservation holder."""
		self.frappe.cache.return_value.set.side_effect = RuntimeError("redis down")
		attach = MagicMock(
			return_value={"ok": True, "status": "Ready", "file": "FILE-1", "content_hash": "abc"}
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file",
			attach,
		):
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["delivery_status"], "Sent")
		self.adapter.send_image.assert_called_once()
		# Reservation stores content hash on the message row.
		created = None
		for call in self.frappe.get_doc.call_args_list:
			arg = call.args[0] if call.args else None
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
				created = arg
		self.assertIsNotNone(created)
		self.assertEqual(created.get("raw_payload_hash"), hashlib.sha256(_png()).hexdigest())

	def test_duplicate_while_queued_does_not_send(self):
		sha = hashlib.sha256(_png()).hexdigest()
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-Q",
			delivery_status="Queued",
			error_summary=None,
			raw_payload_hash=sha,
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Queued")
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_duplicate_after_unknown_does_not_resend(self):
		sha = hashlib.sha256(_png()).hexdigest()
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-U",
			delivery_status="Unknown",
			error_summary="timeout",
			raw_payload_hash=sha,
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Unknown")
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_duplicate_after_failed_returns_failed_without_resend(self):
		sha = hashlib.sha256(_png()).hexdigest()
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-F",
			delivery_status="Failed",
			error_summary="forbidden",
			raw_payload_hash=sha,
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file"
		) as attach:
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertFalse(out["ok"])
		self.assertFalse(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Failed")
		self.adapter.send_image.assert_not_called()
		attach.assert_not_called()

	def test_attach_failure_marks_failed_without_connector(self):
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file",
			side_effect=RuntimeError("disk full"),
		):
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertFalse(out["ok"])
		self.assertEqual(out["delivery_status"], "Failed")
		self.adapter.send_image.assert_not_called()
		failed = [
			c
			for c in self.frappe.db.set_value.call_args_list
			if c.args and c.args[0] == "Omnichannel Message" and isinstance(c.args[2], dict)
		]
		self.assertTrue(any(c.args[2].get("delivery_status") == "Failed" for c in failed))

	def test_timeout_marks_unknown_no_retry(self):
		self.adapter.send_image.side_effect = ConnectorUnreachableError("timed out")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Unknown")
		self.assertEqual(self.adapter.send_image.call_count, 1)

	def test_http_502_marks_unknown(self):
		self.adapter.send_image.side_effect = ConnectorServerError("HTTP 502")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertEqual(out["delivery_status"], "Unknown")

	def test_forbidden_marks_failed(self):
		self.adapter.send_image.side_effect = ConnectorForbiddenError("HTTP 403")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_image_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)
		self.assertEqual(out["delivery_status"], "Failed")
		self.assertFalse(out["uncertain"])

	def test_non_facebook_rejected(self):
		self.conv.provider = "Instagram"
		with self.assertRaises(Exception):
			self.outbound.send_facebook_image(
				conversation_name="CONV-1",
				client_nonce="n1",
				image_bytes=_png(),
				filename="a.png",
				content_type="image/png",
			)


class TestSendFacebookDocument(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		for name in list(sys.modules):
			if name.startswith("meta_omnichannel.facebook_messenger.outbound") or name.startswith(
				"meta_omnichannel.facebook_messenger.media"
			):
				del sys.modules[name]
		cls.outbound = importlib.import_module("meta_omnichannel.facebook_messenger.outbound")

	def setUp(self):
		f = self.frappe
		f.db.reset_mock()
		f.get_doc.reset_mock()
		f.get_all.reset_mock()
		f.get_all.return_value = []
		f.throw.reset_mock()
		f.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(
			Exception(str(a[0]) if a else "throw")
		)
		f.db.commit = MagicMock()
		f.db.rollback = MagicMock()
		f.db.set_value = MagicMock()
		f.db.exists = MagicMock(return_value=False)
		f.db.get_value = MagicMock(return_value=None)
		f.cache.return_value.set.return_value = True
		f.cache.return_value.set.side_effect = None

		self.conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CHAN-1",
			page_id="PAGE-1",
			participant_id="PSID-1",
		)
		f.get_doc.side_effect = self._get_doc
		self.adapter = MagicMock()
		self.adapter.send_document.return_value = {
			"status": "sent",
			"message_id": "mid-doc-1",
			"duplicate": False,
		}
		self._after_dup = False

	def _get_doc(self, arg, name=None):
		if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
			doc = MagicMock()
			doc.name = "MSG-DOC-1"
			doc.insert = MagicMock()
			for k, v in arg.items():
				setattr(doc, k, v)
			return doc
		return self.conv

	def test_pdf_success_persists_then_sends(self):
		attach = MagicMock(
			return_value={"ok": True, "status": "Ready", "file": "FILE-DOC", "content_hash": "h"}
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			attach,
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["delivery_status"], "Sent")
		attach.assert_called_once()
		self.assertEqual(attach.call_args.kwargs.get("filename"), "report.pdf")
		self.adapter.send_document.assert_called_once()
		kwargs = self.adapter.send_document.call_args.kwargs
		self.assertEqual(kwargs["content_type"], _PDF_MIME)
		self.assertTrue(kwargs["idempotency_key"].startswith("oc-doc:"))
		created = None
		for call in self.frappe.get_doc.call_args_list:
			arg = call.args[0] if call.args else None
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
				created = arg
		self.assertEqual(created.get("message_type"), "File")
		self.assertEqual(created.get("raw_payload_hash"), hashlib.sha256(_pdf()).hexdigest())

	def test_docx_success_persists_then_sends(self):
		attach = MagicMock(
			return_value={"ok": True, "status": "Ready", "file": "FILE-DOCX", "content_hash": "h"}
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			attach,
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n2",
				document_bytes=_docx(),
				filename="notes.docx",
				content_type=_DOCX_MIME,
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["delivery_status"], "Sent")
		self.adapter.send_document.assert_called_once()
		self.assertEqual(
			self.adapter.send_document.call_args.kwargs["content_type"], _DOCX_MIME
		)

	def test_same_nonce_same_bytes_dedupes_without_connector(self):
		sha = hashlib.sha256(_pdf()).hexdigest()
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OLD",
			delivery_status="Sent",
			error_summary=None,
			raw_payload_hash=sha,
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file"
		) as attach:
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertTrue(out["ok"])
		self.assertTrue(out["deduplicated"])
		self.adapter.send_document.assert_not_called()
		attach.assert_not_called()

	def test_same_nonce_different_bytes_conflict(self):
		self.frappe.db.get_value.return_value = SimpleNamespace(
			name="MSG-OTHER",
			delivery_status="Sent",
			error_summary=None,
			raw_payload_hash=hashlib.sha256(_docx()).hexdigest(),
		)
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file"
		) as attach:
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertTrue(out.get("conflict"))
		self.assertFalse(out["ok"])
		self.adapter.send_document.assert_not_called()
		attach.assert_not_called()

	def test_redis_down_unique_insert_dedupes(self):
		payload = _pdf()
		sha = hashlib.sha256(payload).hexdigest()
		self.frappe.cache.return_value.set.side_effect = RuntimeError("redis down")

		def _get_value(*_a, **_k):
			if getattr(self, "_after_dup", False):
				return SimpleNamespace(
					name="MSG-WIN",
					delivery_status="Queued",
					error_summary=None,
					raw_payload_hash=sha,
				)
			return None

		self.frappe.db.get_value.side_effect = _get_value

		def _boom_insert(*_a, **_k):
			self._after_dup = True
			raise self.frappe.UniqueValidationError("Duplicate entry for idempotency_key")

		doc = MagicMock()
		doc.name = "MSG-LOSE"
		doc.insert.side_effect = _boom_insert

		def _get_doc(arg, name=None):
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message":
				return doc
			return self.conv

		self.frappe.get_doc.side_effect = _get_doc
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file"
		) as attach:
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=payload,
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertTrue(out["uncertain"] or out.get("deduplicated"))
		self.assertEqual(out["message"], "MSG-WIN")
		self.adapter.send_document.assert_not_called()
		attach.assert_not_called()
		self.frappe.db.rollback.assert_called()

	def test_attach_failure_marks_failed_without_connector(self):
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			side_effect=RuntimeError("disk full"),
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertFalse(out["ok"])
		self.assertEqual(out["delivery_status"], "Failed")
		self.adapter.send_document.assert_not_called()

	def test_timeout_marks_unknown_no_retry(self):
		self.adapter.send_document.side_effect = ConnectorUnreachableError("timed out")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertTrue(out["uncertain"])
		self.assertEqual(out["delivery_status"], "Unknown")
		self.assertEqual(self.adapter.send_document.call_count, 1)

	def test_http_502_marks_unknown(self):
		self.adapter.send_document.side_effect = ConnectorServerError("HTTP 502")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertEqual(out["delivery_status"], "Unknown")

	def test_forbidden_marks_failed(self):
		self.adapter.send_document.side_effect = ConnectorForbiddenError("HTTP 403")
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertEqual(out["delivery_status"], "Failed")
		self.assertFalse(out["uncertain"])

	def test_missing_connector_message_id_is_unknown(self):
		self.adapter.send_document.return_value = {"status": "sent", "duplicate": False}
		with patch.object(
			self.outbound, "FacebookConnectorAdapter", return_value=self.adapter
		), patch(
			"meta_omnichannel.facebook_messenger.media.attach_outbound_document_file",
			return_value={"ok": True, "file": "F1", "content_hash": "h", "status": "Ready"},
		):
			out = self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=_pdf(),
				filename="report.pdf",
				content_type=_PDF_MIME,
			)
		self.assertEqual(out["delivery_status"], "Unknown")
		self.assertTrue(out["uncertain"])
		self.assertFalse(out["ok"])

	def test_rejects_invalid_bytes(self):
		with self.assertRaises(Exception):
			self.outbound.send_facebook_document(
				conversation_name="CONV-1",
				client_nonce="n1",
				document_bytes=b"not-a-document",
				filename="x.pdf",
				content_type=_PDF_MIME,
			)


class TestAttachOutboundImageFile(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		for name in list(sys.modules):
			if name.startswith("meta_omnichannel.facebook_messenger.media"):
				del sys.modules[name]
		cls.media = importlib.import_module("meta_omnichannel.facebook_messenger.media")

	def setUp(self):
		f = self.frappe
		f.db.reset_mock()
		f.get_doc.reset_mock()
		f.get_all.return_value = []
		f.db.exists.return_value = True
		f.db.get_value.return_value = None
		self.save_file = sys.modules["frappe.utils.file_manager"].save_file
		self.save_file.reset_mock()
		self.save_file.side_effect = None
		file_doc = MagicMock()
		file_doc.name = "FILE-NEW"
		file_doc.is_private = 1
		file_doc.file_url = "/private/files/x.png"
		file_doc.attached_to_doctype = "Omnichannel Message"
		file_doc.attached_to_name = "MSG-1"
		file_doc.owner = "Administrator"
		self.save_file.return_value = file_doc

	def test_saves_private_ready_child(self):
		msg = MagicMock()
		msg.name = "MSG-1"
		msg.provider = "Facebook"
		self.frappe.db.exists.return_value = True

		child = MagicMock()
		child.name = "CHILD-1"
		child.insert = MagicMock()

		def _get_doc(arg, name=None):
			if arg == "Omnichannel Message" or (
				isinstance(arg, str) and arg == "Omnichannel Message"
			):
				return msg
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message Attachment":
				return child
			return msg

		def _get_value(doctype, name, fields=None, as_dict=False, **kwargs):
			if doctype == "File" and name == "FILE-NEW":
				meta = SimpleNamespace(
					name="FILE-NEW",
					is_private=1,
					owner="Administrator",
					file_url="/private/files/x.png",
					attached_to_doctype="Omnichannel Message",
					attached_to_name="MSG-1",
				)
				return meta if as_dict else meta
			return None

		self.frappe.get_doc.side_effect = _get_doc
		self.frappe.db.get_value.side_effect = _get_value

		with patch.object(self.media, "_physical_file_exists", return_value=True), patch.object(
			self.media, "_get_child_row", return_value=None
		), patch.object(self.media, "_find_existing_file_for_hash", return_value=None), patch.object(
			self.media, "_next_child_idx", return_value=1
		):
			out = self.media.attach_outbound_image_file("MSG-1", _png(), "image/png")
		self.assertTrue(out["ok"])
		self.assertEqual(out["status"], "Ready")
		self.assertEqual(self.save_file.call_args.kwargs.get("is_private"), 1)
		self.frappe.db.set_value.assert_called()

	def test_image_duplicate_ready_reuses_without_save(self):
		ready = SimpleNamespace(
			name="CHILD-READY",
			status="Ready",
			content_hash=hashlib.sha256(_png()).hexdigest(),
			file="FILE-EXISTING",
		)
		with patch.object(self.media, "_load_message"), patch.object(
			self.media, "_get_child_row", return_value=ready
		), patch.object(
			self.media, "_file_is_valid_private", return_value=True
		), patch.object(self.media, "_find_existing_file_for_hash", return_value="FILE-EXISTING"):
			out = self.media.attach_outbound_image_file("MSG-1", _png(), "image/png")
		self.assertTrue(out["ok"])
		self.assertEqual(out["file"], "FILE-EXISTING")
		self.save_file.assert_not_called()


class TestAttachOutboundDocumentFile(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		for name in list(sys.modules):
			if name.startswith("meta_omnichannel.facebook_messenger.media"):
				del sys.modules[name]
		cls.media = importlib.import_module("meta_omnichannel.facebook_messenger.media")

	def setUp(self):
		f = self.frappe
		f.db.reset_mock()
		f.get_doc.reset_mock()
		f.get_all.return_value = []
		f.db.exists.return_value = True
		f.db.get_value.return_value = None
		self.save_file = sys.modules["frappe.utils.file_manager"].save_file
		self.save_file.reset_mock()
		self.save_file.side_effect = None
		file_doc = MagicMock()
		file_doc.name = "FILE-DOC"
		file_doc.is_private = 1
		file_doc.file_url = "/private/files/doc.pdf"
		file_doc.attached_to_doctype = "Omnichannel Message"
		file_doc.attached_to_name = "MSG-1"
		file_doc.owner = "Administrator"
		self.save_file.return_value = file_doc

	def _wire_message_and_child(self, *, file_url="/private/files/doc.pdf"):
		msg = MagicMock()
		msg.name = "MSG-1"
		msg.provider = "Facebook"
		child = MagicMock()
		child.name = "CHILD-DOC"
		child.insert = MagicMock()

		def _get_doc(arg, name=None):
			if arg == "Omnichannel Message" or (
				isinstance(arg, str) and arg == "Omnichannel Message"
			):
				return msg
			if isinstance(arg, dict) and arg.get("doctype") == "Omnichannel Message Attachment":
				return child
			return msg

		def _get_value(doctype, name, fields=None, as_dict=False, **kwargs):
			if doctype == "File" and name == "FILE-DOC":
				meta = SimpleNamespace(
					name="FILE-DOC",
					is_private=1,
					owner="Administrator",
					file_url=file_url,
					attached_to_doctype="Omnichannel Message",
					attached_to_name="MSG-1",
				)
				return meta if as_dict else meta
			return None

		self.frappe.get_doc.side_effect = _get_doc
		self.frappe.db.get_value.side_effect = _get_value
		return child

	def test_pdf_saves_private_ready_child(self):
		self._wire_message_and_child()
		with patch.object(self.media, "_physical_file_exists", return_value=True), patch.object(
			self.media, "_get_child_row", return_value=None
		), patch.object(self.media, "_find_existing_file_for_hash", return_value=None), patch.object(
			self.media, "_next_child_idx", return_value=1
		):
			out = self.media.attach_outbound_document_file(
				"MSG-1", _pdf(), _PDF_MIME, filename="report.pdf"
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["status"], "Ready")
		self.assertEqual(out["file"], "FILE-DOC")
		self.assertEqual(out["content_hash"], hashlib.sha256(_pdf()).hexdigest())
		self.assertEqual(self.save_file.call_args.args[0], "report.pdf")
		self.assertEqual(self.save_file.call_args.kwargs.get("is_private"), 1)
		ready_calls = [
			c
			for c in self.frappe.db.set_value.call_args_list
			if c.args and c.args[0] == "Omnichannel Message Attachment"
		]
		self.assertTrue(ready_calls)
		values = ready_calls[-1].args[2]
		self.assertEqual(values["status"], "Ready")
		self.assertEqual(values["mime_type"], _PDF_MIME)
		self.assertEqual(values["attachment_type"], "file")

	def test_docx_saves_private_ready_child(self):
		self._wire_message_and_child(file_url="/private/files/notes.docx")
		with patch.object(self.media, "_physical_file_exists", return_value=True), patch.object(
			self.media, "_get_child_row", return_value=None
		), patch.object(self.media, "_find_existing_file_for_hash", return_value=None), patch.object(
			self.media, "_next_child_idx", return_value=1
		):
			out = self.media.attach_outbound_document_file(
				"MSG-1", _docx(), _DOCX_MIME, filename="notes.docx"
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["status"], "Ready")
		self.assertEqual(self.save_file.call_args.args[0], "notes.docx")
		self.assertEqual(self.save_file.call_args.kwargs.get("is_private"), 1)

	def test_sanitizes_filename_path_separators(self):
		self._wire_message_and_child()
		with patch.object(self.media, "_physical_file_exists", return_value=True), patch.object(
			self.media, "_get_child_row", return_value=None
		), patch.object(self.media, "_find_existing_file_for_hash", return_value=None), patch.object(
			self.media, "_next_child_idx", return_value=1
		):
			self.media.attach_outbound_document_file(
				"MSG-1",
				_pdf(),
				_PDF_MIME,
				filename=r"..\..\evil/report.pdf",
			)
		self.assertEqual(self.save_file.call_args.args[0], ".._.._evil_report.pdf")

	def test_duplicate_ready_reuses_without_save(self):
		payload = _pdf()
		ready = SimpleNamespace(
			name="CHILD-READY",
			status="Ready",
			content_hash=hashlib.sha256(payload).hexdigest(),
			file="FILE-EXISTING",
		)
		with patch.object(self.media, "_load_message"), patch.object(
			self.media, "_get_child_row", return_value=ready
		), patch.object(
			self.media, "_file_is_valid_private", return_value=True
		), patch.object(self.media, "_find_existing_file_for_hash", return_value="FILE-EXISTING"):
			out = self.media.attach_outbound_document_file(
				"MSG-1", payload, _PDF_MIME, filename="report.pdf"
			)
		self.assertTrue(out["ok"])
		self.assertEqual(out["file"], "FILE-EXISTING")
		self.save_file.assert_not_called()

	def test_persistence_failure_marks_failed_and_reraises(self):
		self._wire_message_and_child()
		self.save_file.side_effect = RuntimeError("disk full")
		with patch.object(self.media, "_physical_file_exists", return_value=True), patch.object(
			self.media, "_get_child_row", return_value=None
		), patch.object(self.media, "_find_existing_file_for_hash", return_value=None), patch.object(
			self.media, "_next_child_idx", return_value=1
		), patch.object(self.media, "_delete_new_file_best_effort") as delete_mock:
			with self.assertRaises(RuntimeError):
				self.media.attach_outbound_document_file(
					"MSG-1", _pdf(), _PDF_MIME, filename="report.pdf"
				)
		failed_calls = [
			c
			for c in self.frappe.db.set_value.call_args_list
			if c.args
			and c.args[0] == "Omnichannel Message Attachment"
			and isinstance(c.args[2], dict)
			and c.args[2].get("status") == "Failed"
		]
		self.assertTrue(failed_calls)
		delete_mock.assert_called()

	def test_rejects_unsupported_mime(self):
		with self.assertRaises(Exception):
			self.media.attach_outbound_document_file(
				"MSG-1", b"hello", "text/plain", filename="x.txt"
			)
		self.save_file.assert_not_called()

	def test_rejects_oversized_document(self):
		huge = b"%PDF-" + b"\x00" * MAX_OUTBOUND_DOCUMENT_BYTES
		with self.assertRaises(Exception):
			self.media.attach_outbound_document_file(
				"MSG-1", huge, _PDF_MIME, filename="big.pdf"
			)
		self.save_file.assert_not_called()


class TestSendImageMessageApi(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		# permissions module imports
		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()
		perm.has_conversation_permission = MagicMock(return_value=True)
		perm.user_is_omnichannel_admin = MagicMock(return_value=False)
		sys.modules["meta_omnichannel.permissions"] = perm
		cls.perm = perm
		# datetimes helper used at inbox import
		for name in list(sys.modules):
			if name == "meta_omnichannel.api.inbox" or name.endswith(".api.inbox"):
				del sys.modules[name]
		cls.inbox = importlib.import_module("meta_omnichannel.api.inbox")

	def setUp(self):
		self.perm.ensure_omnichannel_agent.reset_mock()
		self.perm.has_conversation_permission.reset_mock()
		self.perm.has_conversation_permission.return_value = True
		self.frappe.throw.reset_mock()
		self.frappe.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(
			Exception(str(a[0]) if a else "throw")
		)
		self.frappe.get_doc.side_effect = None
		self.frappe.get_doc.return_value = SimpleNamespace(
			name="CONV-1", provider="Facebook", channel="C", page_id="P", participant_id="S"
		)
		self.frappe.request.files = {}
		# Ensure inbox sees this request/files object.
		sys.modules["frappe"].request = self.frappe.request
		sys.modules["frappe"].throw = self.frappe.throw
		sys.modules["frappe"].get_doc = self.frappe.get_doc

	def test_requires_agent_and_write(self):
		upload = MagicMock()
		upload.filename = "a.png"
		upload.content_type = "image/png"
		upload.stream = MagicMock()
		upload.stream.read = MagicMock(return_value=_png())
		self.frappe.request.files = {"image": upload}
		with patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_image",
			return_value={"ok": True, "delivery_status": "Sent"},
		) as send:
			out = self.inbox.send_image_message("CONV-1", "nonce-1")
		self.perm.ensure_omnichannel_agent.assert_called()
		self.perm.has_conversation_permission.assert_called()
		self.assertTrue(out["ok"])
		send.assert_called_once()
		self.assertEqual(send.call_args.kwargs["client_nonce"], "nonce-1")

	def test_permission_denied(self):
		self.perm.has_conversation_permission.return_value = False
		with self.assertRaises(Exception):
			self.inbox.send_image_message("CONV-1", "n1")

	def test_instagram_rejected(self):
		self.frappe.get_doc.return_value = SimpleNamespace(
			name="CONV-1", provider="Instagram", channel="C", page_id="P", participant_id="S"
		)
		upload = MagicMock()
		upload.filename = "a.png"
		upload.content_type = "image/png"
		upload.stream = MagicMock()
		upload.stream.read = MagicMock(return_value=_png())
		self.frappe.request.files = {"image": upload}
		with self.assertRaises(Exception):
			self.inbox.send_image_message("CONV-1", "n1")

	def test_missing_upload(self):
		self.frappe.request.files = {}
		with self.assertRaises(Exception):
			self.inbox.send_image_message("CONV-1", "n1")

	def test_bounded_oversized_read(self):
		from meta_omnichannel.facebook_messenger.adapter import MAX_OUTBOUND_IMAGE_BYTES

		upload = MagicMock()
		upload.filename = "big.jpg"
		upload.content_type = "image/jpeg"
		upload.stream = MagicMock()
		upload.stream.read = MagicMock(
			return_value=b"\xff\xd8\xff" + b"\x00" * MAX_OUTBOUND_IMAGE_BYTES
		)
		self.frappe.request.files = {"image": upload}
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_image_message("CONV-1", "n1")
		self.assertIn("8 MiB", str(ctx.exception))
		upload.stream.read.assert_called_with(MAX_OUTBOUND_IMAGE_BYTES + 1)


class TestSendDocumentMessageApi(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_fake_frappe()
		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()
		perm.has_conversation_permission = MagicMock(return_value=True)
		perm.user_is_omnichannel_admin = MagicMock(return_value=False)
		sys.modules["meta_omnichannel.permissions"] = perm
		cls.perm = perm
		for name in list(sys.modules):
			if name == "meta_omnichannel.api.inbox" or name.endswith(".api.inbox"):
				del sys.modules[name]
		cls.inbox = importlib.import_module("meta_omnichannel.api.inbox")

	def setUp(self):
		self.perm.ensure_omnichannel_agent.reset_mock()
		self.perm.has_conversation_permission.reset_mock()
		self.perm.has_conversation_permission.return_value = True
		self.frappe.throw.reset_mock()
		self.frappe.throw.side_effect = lambda *a, **k: (_ for _ in ()).throw(
			Exception(str(a[0]) if a else "throw")
		)
		self.frappe.get_doc.side_effect = None
		self.frappe.get_doc.return_value = SimpleNamespace(
			name="CONV-1", provider="Facebook", channel="C", page_id="P", participant_id="S"
		)
		self.frappe.request.files = {}
		sys.modules["frappe"].request = self.frappe.request
		sys.modules["frappe"].throw = self.frappe.throw
		sys.modules["frappe"].get_doc = self.frappe.get_doc

	def _upload(self, *, data: bytes, filename: str, content_type: str):
		upload = MagicMock()
		upload.filename = filename
		upload.content_type = content_type
		upload.stream = MagicMock()
		upload.stream.read = MagicMock(return_value=data)
		return upload

	def test_pdf_success_calls_outbound(self):
		upload = self._upload(
			data=_pdf(), filename="quote.pdf", content_type=_PDF_MIME
		)
		self.frappe.request.files = {"document": upload}
		with patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_document",
			return_value={"ok": True, "delivery_status": "Sent", "message": "MSG-1"},
		) as send:
			out = self.inbox.send_document_message("CONV-1", "nonce-pdf")
		self.perm.ensure_omnichannel_agent.assert_called()
		self.perm.has_conversation_permission.assert_called()
		self.assertTrue(out["ok"])
		self.assertEqual(out["delivery_status"], "Sent")
		send.assert_called_once()
		kwargs = send.call_args.kwargs
		self.assertEqual(kwargs["conversation_name"], "CONV-1")
		self.assertEqual(kwargs["client_nonce"], "nonce-pdf")
		self.assertEqual(kwargs["document_bytes"], _pdf())
		self.assertEqual(kwargs["filename"], "quote.pdf")
		self.assertEqual(kwargs["content_type"], _PDF_MIME)
		upload.stream.read.assert_called_with(MAX_OUTBOUND_DOCUMENT_BYTES + 1)

	def test_docx_success_preserves_filename_override(self):
		upload = self._upload(
			data=_docx(), filename="upload.docx", content_type=_DOCX_MIME
		)
		self.frappe.request.files = {"document": upload}
		with patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_document",
			return_value={"ok": True, "delivery_status": "Sent"},
		) as send:
			out = self.inbox.send_document_message(
				"CONV-1", "nonce-docx", filename="Sanitized Report.docx"
			)
		self.assertTrue(out["ok"])
		kwargs = send.call_args.kwargs
		self.assertEqual(kwargs["document_bytes"], _docx())
		self.assertEqual(kwargs["filename"], "Sanitized Report.docx")
		self.assertEqual(kwargs["content_type"], _DOCX_MIME)

	def test_missing_upload(self):
		self.frappe.request.files = {}
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_document_message("CONV-1", "n1")
		self.assertIn("Document upload is required", str(ctx.exception))

	def test_empty_upload(self):
		upload = self._upload(data=b"", filename="empty.pdf", content_type=_PDF_MIME)
		self.frappe.request.files = {"document": upload}
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_document_message("CONV-1", "n1")
		self.assertIn("Document upload is required", str(ctx.exception))

	def test_oversized_upload(self):
		upload = self._upload(
			data=b"%PDF" + b"\x00" * MAX_OUTBOUND_DOCUMENT_BYTES,
			filename="big.pdf",
			content_type=_PDF_MIME,
		)
		self.frappe.request.files = {"document": upload}
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_document_message("CONV-1", "n1")
		self.assertIn("8 MiB", str(ctx.exception))
		upload.stream.read.assert_called_with(MAX_OUTBOUND_DOCUMENT_BYTES + 1)

	def test_invalid_nonce(self):
		upload = self._upload(
			data=_pdf(), filename="a.pdf", content_type=_PDF_MIME
		)
		self.frappe.request.files = {"document": upload}
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_document_message("CONV-1", "   ")
		self.assertIn("client_nonce", str(ctx.exception))

	def test_permission_denied(self):
		self.perm.has_conversation_permission.return_value = False
		with self.assertRaises(Exception):
			self.inbox.send_document_message("CONV-1", "n1")

	def test_instagram_rejected(self):
		self.frappe.get_doc.return_value = SimpleNamespace(
			name="CONV-1", provider="Instagram", channel="C", page_id="P", participant_id="S"
		)
		upload = self._upload(
			data=_pdf(), filename="a.pdf", content_type=_PDF_MIME
		)
		self.frappe.request.files = {"document": upload}
		with self.assertRaises(Exception):
			self.inbox.send_document_message("CONV-1", "n1")

	def test_unknown_response_passthrough_no_retry(self):
		upload = self._upload(
			data=_pdf(), filename="a.pdf", content_type=_PDF_MIME
		)
		self.frappe.request.files = {"document": upload}
		with patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_document",
			return_value={"ok": False, "delivery_status": "Unknown", "message": "MSG-U"},
		) as send:
			out = self.inbox.send_document_message("CONV-1", "nonce-u")
		self.assertEqual(out["delivery_status"], "Unknown")
		self.assertFalse(out["ok"])
		send.assert_called_once()


if __name__ == "__main__":
	unittest.main()
