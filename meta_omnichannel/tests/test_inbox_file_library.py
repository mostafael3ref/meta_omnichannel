"""Security / regression tests for ERPNext File Library inbox APIs.

Covers ``list_selectable_outbound_files``, ``send_image_from_file``, and
``send_document_from_file`` (agent gate, File + attached-doc ACL, type/size
filters, bounded disk read, outbound reuse, source File preservation).
"""

from __future__ import annotations

import builtins
import importlib
import io
import sys
import types
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

from meta_omnichannel.facebook_messenger.adapter import (
	MAX_OUTBOUND_DOCUMENT_BYTES,
	MAX_OUTBOUND_IMAGE_BYTES,
)

_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _png(n: int = 64) -> bytes:
	return b"\x89PNG\r\n\x1a\n" + b"\x00" * n


def _jpeg(n: int = 64) -> bytes:
	return b"\xff\xd8\xff\xe0" + b"\x00" * n


def _gif(n: int = 64) -> bytes:
	return b"GIF89a" + b"\x00" * n


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


def _install_fake_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_list = MagicMock(return_value=[])
	frappe.get_all = MagicMock(return_value=[])
	frappe.has_permission = MagicMock(return_value=True)
	frappe.session = SimpleNamespace(user="agent@example.com")
	frappe._ = lambda x: x
	frappe.throw = MagicMock(
		side_effect=lambda *a, **k: (_ for _ in ()).throw(Exception(a[0] if a else "throw"))
	)
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	frappe.request = MagicMock()
	frappe.request.files = {}
	utils = types.ModuleType("frappe.utils")
	utils.now_datetime = MagicMock(return_value="2026-11-24 12:00:00")  # type: ignore[attr-defined]
	utils.cint = int  # type: ignore[attr-defined]
	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = utils
	frappe.utils = utils
	return frappe


def _file_doc(
	*,
	name: str = "FILE-1",
	file_name: str = "photo.png",
	file_size: int = 128,
	is_private: int = 1,
	is_folder: int = 0,
	attached_to_doctype: str | None = "Customer",
	attached_to_name: str | None = "CUST-1",
	path: str = "/tmp/private/files/photo.png",
	content: bytes | None = None,
	readable: bool = True,
):
	doc = SimpleNamespace(
		name=name,
		file_name=file_name,
		file_size=file_size,
		is_private=is_private,
		is_folder=is_folder,
		attached_to_doctype=attached_to_doctype,
		attached_to_name=attached_to_name,
		file_url="/private/files/photo.png",
		content=content,
	)
	doc.has_permission = MagicMock(return_value=readable)
	doc.get_full_path = MagicMock(return_value=path)
	return doc


class _InboxLibraryTestCase(unittest.TestCase):
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
		self.frappe.get_list = MagicMock(return_value=[])
		self.frappe.has_permission = MagicMock(return_value=True)
		self.frappe.get_doc = MagicMock(
			return_value=SimpleNamespace(
				name="CONV-1",
				provider="Facebook",
				channel="C",
				page_id="P",
				participant_id="S",
			)
		)
		sys.modules["frappe"].get_doc = self.frappe.get_doc
		sys.modules["frappe"].get_list = self.frappe.get_list
		sys.modules["frappe"].has_permission = self.frappe.has_permission
		sys.modules["frappe"].throw = self.frappe.throw


class TestListSelectableOutboundFiles(_InboxLibraryTestCase):
	def test_requires_agent(self):
		self.inbox.list_selectable_outbound_files()
		self.perm.ensure_omnichannel_agent.assert_called()

	def test_filters_extensions_size_and_omits_urls(self):
		allowed = _file_doc(
			name="FILE-OK",
			file_name="ok.png",
			file_size=100,
			content=_png(),
		)
		denied_acl = _file_doc(
			name="FILE-DENY",
			file_name="secret.png",
			file_size=100,
			readable=False,
		)
		wrong_ext = _file_doc(
			name="FILE-TXT",
			file_name="notes.txt",
			file_size=100,
		)
		too_big = _file_doc(
			name="FILE-BIG",
			file_name="big.png",
			file_size=MAX_OUTBOUND_IMAGE_BYTES + 1,
		)

		self.frappe.get_list.return_value = [
			SimpleNamespace(name="FILE-OK"),
			SimpleNamespace(name="FILE-DENY"),
			SimpleNamespace(name="FILE-TXT"),
			SimpleNamespace(name="FILE-BIG"),
		]

		def _get_doc(dt, name=None, **kwargs):
			key = name if name is not None else dt
			if dt == "File" or (isinstance(dt, str) and dt.startswith("FILE-")):
				mapping = {
					"FILE-OK": allowed,
					"FILE-DENY": denied_acl,
					"FILE-TXT": wrong_ext,
					"FILE-BIG": too_big,
				}
				docname = name if dt == "File" else key
				return mapping[docname]
			if dt == "Customer":
				ref = SimpleNamespace(name=name or "CUST-1")
				ref.has_permission = MagicMock(return_value=True)
				return ref
			raise AssertionError(f"unexpected get_doc {dt!r} {name!r}")

		self.frappe.get_doc.side_effect = _get_doc
		# Attached Customer read allowed for FILE-OK.
		self.frappe.has_permission.side_effect = (
			lambda doctype, ptype="read", doc=None, **k: True
		)

		out = self.inbox.list_selectable_outbound_files(limit=10, offset=0)
		self.frappe.get_list.assert_called()
		kwargs = self.frappe.get_list.call_args.kwargs
		self.assertEqual(kwargs["limit_page_length"], 10)
		self.assertEqual(kwargs["limit_start"], 0)
		filters = kwargs["filters"]
		self.assertIn(["is_folder", "=", 0], filters)
		self.assertIn(["file_size", "<=", 8 * 1024 * 1024], filters)
		or_filters = kwargs["or_filters"]
		joined = str(or_filters)
		self.assertIn(".png", joined)
		self.assertIn(".pdf", joined)
		self.assertIn(".docx", joined)

		self.assertEqual(len(out["items"]), 1)
		item = out["items"][0]
		self.assertEqual(item["name"], "FILE-OK")
		self.assertEqual(item["file_name"], "ok.png")
		self.assertEqual(item["kind"], "image")
		self.assertEqual(item["content_type"], "image/png")
		self.assertNotIn("file_url", item)
		self.assertNotIn("content", item)
		self.assertTrue(item["is_private"])

	def test_search_strips_like_wildcards(self):
		self.frappe.get_list.return_value = []
		self.inbox.list_selectable_outbound_files(search="  a%b_c\\d  ")
		filters = self.frappe.get_list.call_args.kwargs["filters"]
		like = [f for f in filters if f[0] == "file_name" and f[1] == "like"]
		self.assertEqual(len(like), 1)
		self.assertEqual(like[0][2], "%abcd%")

	def test_attached_document_denied_excludes_row(self):
		file_doc = _file_doc(name="FILE-1", file_name="x.pdf", file_size=50)
		self.frappe.get_list.return_value = [SimpleNamespace(name="FILE-1")]

		def _get_doc(dt, name=None, **kwargs):
			if dt == "File":
				return file_doc
			if dt == "Customer":
				ref = SimpleNamespace(name=name)
				ref.has_permission = MagicMock(return_value=False)
				return ref
			raise AssertionError(dt)

		self.frappe.get_doc.side_effect = _get_doc
		self.frappe.has_permission.side_effect = (
			lambda doctype, ptype="read", doc=None, **k: doctype != "Customer"
		)
		out = self.inbox.list_selectable_outbound_files()
		self.assertEqual(out["items"], [])


class TestSendFromLibraryFile(_InboxLibraryTestCase):
	def _conv(self, provider: str = "Facebook"):
		return SimpleNamespace(
			name="CONV-1",
			provider=provider,
			channel="C",
			page_id="P",
			participant_id="S",
		)

	def _wire_file_and_conv(self, file_doc, *, provider: str = "Facebook"):
		conv = self._conv(provider)

		def _get_doc(dt, name=None, **kwargs):
			if dt == "Omnichannel Conversation":
				return conv
			if dt == "File":
				return file_doc
			if dt == "Customer":
				ref = SimpleNamespace(name=name or "CUST-1")
				ref.has_permission = MagicMock(return_value=True)
				return ref
			raise AssertionError(f"unexpected get_doc {dt!r}")

		self.frappe.get_doc.side_effect = _get_doc
		self.frappe.has_permission.return_value = True
		return conv

	def test_send_image_from_file_success(self):
		payload = _png(32)
		file_doc = _file_doc(
			name="FILE-IMG",
			file_name="shot.png",
			file_size=len(payload),
			path="/tmp/private/files/shot.png",
		)
		self._wire_file_and_conv(file_doc)
		with patch.object(builtins, "open", mock_open(read_data=payload)) as opened, patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_image",
			return_value={"ok": True, "delivery_status": "Sent", "message": "MSG-1"},
		) as send:
			out = self.inbox.send_image_from_file("CONV-1", "FILE-IMG", "nonce-img")
		self.perm.ensure_omnichannel_agent.assert_called()
		self.perm.has_conversation_permission.assert_called()
		self.assertTrue(out["ok"])
		self.assertEqual(out["delivery_status"], "Sent")
		send.assert_called_once()
		kwargs = send.call_args.kwargs
		self.assertEqual(kwargs["conversation_name"], "CONV-1")
		self.assertEqual(kwargs["client_nonce"], "nonce-img")
		self.assertEqual(kwargs["image_bytes"], payload)
		self.assertEqual(kwargs["filename"], "shot.png")
		self.assertEqual(kwargs["content_type"], "image/png")
		opened.assert_called()
		# Source File must not be deleted or renamed by the inbox layer.
		self.assertEqual(file_doc.file_name, "shot.png")
		self.assertEqual(file_doc.name, "FILE-IMG")

	def test_send_document_pdf_and_docx(self):
		for data, fname, mime in (
			(_pdf(), "quote.pdf", _PDF_MIME),
			(_docx(), "spec.docx", _DOCX_MIME),
		):
			file_doc = _file_doc(
				name="FILE-DOC",
				file_name=fname,
				file_size=len(data),
				path=f"/tmp/private/files/{fname}",
			)
			self._wire_file_and_conv(file_doc)
			with patch.object(builtins, "open", mock_open(read_data=data)), patch(
				"meta_omnichannel.facebook_messenger.outbound.send_facebook_document",
				return_value={"ok": True, "delivery_status": "Sent"},
			) as send:
				out = self.inbox.send_document_from_file("CONV-1", "FILE-DOC", "nonce-doc")
			self.assertTrue(out["ok"])
			kwargs = send.call_args.kwargs
			self.assertEqual(kwargs["document_bytes"], data)
			self.assertEqual(kwargs["filename"], fname)
			self.assertEqual(kwargs["content_type"], mime)

	def test_permission_denied_conversation(self):
		file_doc = _file_doc(file_name="a.png", file_size=10)
		self._wire_file_and_conv(file_doc)
		self.perm.has_conversation_permission.return_value = False
		with self.assertRaises(Exception):
			self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")

	def test_permission_denied_file(self):
		file_doc = _file_doc(file_name="a.png", file_size=10, readable=False)
		self._wire_file_and_conv(file_doc)
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")
		self.assertIn("Not permitted", str(ctx.exception))

	def test_attached_document_denied_on_send(self):
		file_doc = _file_doc(file_name="a.png", file_size=10)
		self._wire_file_and_conv(file_doc)
		self.frappe.has_permission.side_effect = (
			lambda doctype, ptype="read", doc=None, **k: doctype != "Customer"
		)

		def _get_doc(dt, name=None, **kwargs):
			if dt == "Omnichannel Conversation":
				return self._conv()
			if dt == "File":
				return file_doc
			if dt == "Customer":
				ref = SimpleNamespace(name=name)
				ref.has_permission = MagicMock(return_value=False)
				return ref
			raise AssertionError(dt)

		self.frappe.get_doc.side_effect = _get_doc
		with self.assertRaises(Exception):
			self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")

	def test_instagram_rejected(self):
		file_doc = _file_doc(file_name="a.png", file_size=10)
		self._wire_file_and_conv(file_doc, provider="Instagram")
		with self.assertRaises(Exception):
			self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")

	def test_image_endpoint_rejects_pdf(self):
		file_doc = _file_doc(file_name="a.pdf", file_size=20)
		self._wire_file_and_conv(file_doc)
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")
		self.assertIn("image", str(ctx.exception).lower())

	def test_document_endpoint_rejects_png(self):
		file_doc = _file_doc(file_name="a.png", file_size=20)
		self._wire_file_and_conv(file_doc)
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_document_from_file("CONV-1", "FILE-1", "n1")
		self.assertIn("document", str(ctx.exception).lower())

	def test_missing_nonce(self):
		file_doc = _file_doc(file_name="a.png", file_size=10)
		self._wire_file_and_conv(file_doc)
		with self.assertRaises(Exception) as ctx:
			self.inbox.send_image_from_file("CONV-1", "FILE-1", "  ")
		self.assertIn("client_nonce", str(ctx.exception))

	def test_bounded_oversized_disk_read(self):
		file_doc = _file_doc(
			name="FILE-1",
			file_name="big.png",
			# Claimed size is within limit so ACL/size gate passes; disk bytes overflow.
			file_size=MAX_OUTBOUND_IMAGE_BYTES,
			path="/tmp/private/files/big.png",
		)
		self._wire_file_and_conv(file_doc)
		huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_OUTBOUND_IMAGE_BYTES
		with patch.object(builtins, "open", mock_open(read_data=huge)) as opened:
			with self.assertRaises(Exception) as ctx:
				self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")
		self.assertIn("8 MiB", str(ctx.exception))
		# Bounded read: open().read(max+1)
		handle = opened.return_value.__enter__.return_value
		handle.read.assert_called_with(MAX_OUTBOUND_IMAGE_BYTES + 1)

	def test_unknown_passthrough_no_retry(self):
		payload = _pdf()
		file_doc = _file_doc(
			file_name="a.pdf",
			file_size=len(payload),
			path="/tmp/private/files/a.pdf",
		)
		self._wire_file_and_conv(file_doc)
		with patch.object(builtins, "open", mock_open(read_data=payload)), patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_document",
			return_value={
				"ok": False,
				"uncertain": True,
				"delivery_status": "Unknown",
				"message": "MSG-U",
			},
		) as send:
			out = self.inbox.send_document_from_file("CONV-1", "FILE-1", "nonce-u")
		self.assertEqual(out["delivery_status"], "Unknown")
		self.assertFalse(out["ok"])
		send.assert_called_once()

	def test_failed_passthrough_no_retry(self):
		payload = _jpeg(16)
		file_doc = _file_doc(
			file_name="a.jpg",
			file_size=len(payload),
			path="/tmp/private/files/a.jpg",
		)
		self._wire_file_and_conv(file_doc)
		with patch.object(builtins, "open", mock_open(read_data=payload)), patch(
			"meta_omnichannel.facebook_messenger.outbound.send_facebook_image",
			return_value={"ok": False, "delivery_status": "Failed", "error": "page"},
		) as send:
			out = self.inbox.send_image_from_file("CONV-1", "FILE-1", "nonce-f")
		self.assertEqual(out["delivery_status"], "Failed")
		send.assert_called_once()

	def test_recheck_acl_before_read(self):
		"""Permission must be enforced again at read time, not only at resolve."""
		payload = _png()
		file_doc = _file_doc(
			file_name="a.png",
			file_size=len(payload),
			path="/tmp/private/files/a.png",
			readable=True,
		)
		self._wire_file_and_conv(file_doc)
		calls = {"n": 0}

		def _flip_read(_ptype="read"):
			calls["n"] += 1
			# First checks (open/select) succeed; fail on the pre-read recheck.
			return calls["n"] < 2

		file_doc.has_permission.side_effect = _flip_read
		with patch.object(builtins, "open", mock_open(read_data=payload)) as opened:
			with self.assertRaises(Exception):
				self.inbox.send_image_from_file("CONV-1", "FILE-1", "n1")
		opened.assert_not_called()


class TestLibraryHelpers(_InboxLibraryTestCase):
	def test_extension_mapping(self):
		self.assertEqual(
			self.inbox._library_type_for_filename("X.PNG"),
			("image", "image/png"),
		)
		self.assertEqual(
			self.inbox._library_type_for_filename("a.JPEG"),
			("image", "image/jpeg"),
		)
		self.assertEqual(
			self.inbox._library_type_for_filename("a.gif"),
			("image", "image/gif"),
		)
		self.assertEqual(
			self.inbox._library_type_for_filename("a.PDF"),
			("document", _PDF_MIME),
		)
		self.assertEqual(
			self.inbox._library_type_for_filename("a.DOCX"),
			("document", _DOCX_MIME),
		)
		self.assertIsNone(self.inbox._library_type_for_filename("a.webp"))
		self.assertIsNone(self.inbox._library_type_for_filename("a.doc"))


if __name__ == "__main__":
	unittest.main()
