"""Focused tests: list_messages exposes Ready private File URLs only."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _install_inbox_frappe() -> MagicMock:
	frappe = MagicMock()
	frappe._ = lambda x: x
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.throw = MagicMock(side_effect=Exception("throw"))
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock(return_value=[])
	frappe.whitelist = lambda *a, **k: (lambda fn: fn)
	sys.modules["frappe"] = frappe
	utils = types.ModuleType("frappe.utils")
	utils.__path__ = []  # type: ignore[attr-defined]  # allow frappe.utils.* stubs
	utils.cint = lambda v: int(v or 0)  # type: ignore[attr-defined]
	sys.modules["frappe.utils"] = utils
	return frappe


class TestListMessagesAttachmentFileUrl(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.frappe = _install_inbox_frappe()
		perm = types.ModuleType("meta_omnichannel.permissions")
		perm.ensure_omnichannel_agent = MagicMock()  # type: ignore[attr-defined]
		perm.has_conversation_permission = MagicMock(return_value=True)  # type: ignore[attr-defined]
		perm.user_is_omnichannel_admin = MagicMock(return_value=True)  # type: ignore[attr-defined]
		sys.modules["meta_omnichannel.permissions"] = perm
		# file_manager must exist before media import (frappe.utils is a stub module).
		fm = types.ModuleType("frappe.utils.file_manager")
		fm.save_file = MagicMock()  # type: ignore[attr-defined]
		fm.get_file_path = MagicMock(  # type: ignore[attr-defined]
			return_value="/tmp/private/files/x.bin"
		)
		sys.modules["frappe.utils.file_manager"] = fm
		# Ensure media is importable before inbox lazy-imports it.
		import meta_omnichannel.facebook_messenger.media as media_mod
		import meta_omnichannel.api.inbox as inbox_mod

		importlib.reload(media_mod)
		importlib.reload(inbox_mod)
		cls.media = media_mod
		cls.inbox = inbox_mod
		cls.perm = perm

	def setUp(self):
		self.frappe.db.reset_mock()
		self.frappe.get_doc.reset_mock()
		self.frappe.get_all.reset_mock()
		self.frappe.throw.reset_mock(side_effect=True)
		self.frappe.throw.side_effect = Exception("throw")
		self.perm.ensure_omnichannel_agent.reset_mock()
		self.perm.has_conversation_permission.reset_mock()
		self.perm.has_conversation_permission.return_value = True
		self.conv = SimpleNamespace(
			name="CONV-1",
			provider="Facebook",
			channel="CH",
			participant_name="A",
			participant_id="S1",
			status="Open",
			assigned_to=None,
			unread_count=1,
			last_message_at=None,
			last_message_preview="",
			last_inbound_at=None,
			crm_contact=None,
			crm_lead=None,
			page_id="P1",
		)
		self.frappe.get_doc.return_value = self.conv
		self.frappe.db.count.return_value = 1

	def test_ready_local_url_attached(self):
		msg = SimpleNamespace(
			name="MSG-1",
			direction="Inbound",
			message_type="Image",
			text_content="",
			delivery_status="Delivered",
			sent_at=None,
			attachment_url="https://cdn.example/x.jpg",
			attachment_mime="image/jpeg",
			is_internal_note=0,
			error_summary=None,
			external_message_id="m1",
		)
		self.frappe.get_all.return_value = [msg]
		with patch.object(
			self.media,
			"map_ready_attachment_file_urls",
			return_value={"MSG-1": "/private/files/omni_msg1_0.jpg"},
		) as mapped:
			result = self.inbox.list_messages("CONV-1")
		mapped.assert_called_once()
		item = result["items"][0]
		self.assertEqual(item["attachment_url"], "https://cdn.example/x.jpg")
		self.assertEqual(item["attachment_file_url"], "/private/files/omni_msg1_0.jpg")
		self.perm.has_conversation_permission.assert_called()

	def test_pending_or_invalid_yields_null_local_url(self):
		msg = SimpleNamespace(
			name="MSG-2",
			direction="Inbound",
			message_type="Image",
			text_content="",
			delivery_status="Delivered",
			sent_at=None,
			attachment_url="https://cdn.example/y.jpg",
			attachment_mime="image/jpeg",
			is_internal_note=0,
			error_summary=None,
			external_message_id="m2",
		)
		self.frappe.get_all.return_value = [msg]
		with patch.object(
			self.media, "map_ready_attachment_file_urls", return_value={}
		):
			result = self.inbox.list_messages("CONV-1")
		self.assertIsNone(result["items"][0]["attachment_file_url"])
		self.assertEqual(result["items"][0]["attachment_url"], "https://cdn.example/y.jpg")

	def test_unauthorized_conversation_does_not_map_files(self):
		self.perm.has_conversation_permission.return_value = False
		with patch.object(self.media, "map_ready_attachment_file_urls") as mapped:
			with self.assertRaises(Exception):
				self.inbox.list_messages("CONV-1")
		mapped.assert_not_called()


class TestReadyAttachmentFileUrlHelper(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe = MagicMock()
		frappe._ = lambda x: x
		frappe.ValidationError = type("ValidationError", (Exception,), {})
		frappe.throw = MagicMock(
			side_effect=lambda *a, **k: (_ for _ in ()).throw(
				frappe.ValidationError(a[0] if a else "throw")
			)
		)
		frappe.db = MagicMock()
		frappe.get_all = MagicMock(return_value=[])
		frappe.cache = MagicMock()
		sys.modules["frappe"] = frappe
		utils = types.ModuleType("frappe.utils")
		utils.cint = lambda v: int(v or 0)  # type: ignore[attr-defined]
		sys.modules["frappe.utils"] = utils
		fm = types.ModuleType("frappe.utils.file_manager")
		fm.save_file = MagicMock()  # type: ignore[attr-defined]
		fm.get_file_path = MagicMock(  # type: ignore[attr-defined]
			return_value="/tmp/private/files/x.bin"
		)
		sys.modules["frappe.utils.file_manager"] = fm
		import meta_omnichannel.facebook_messenger.media as media_mod

		importlib.reload(media_mod)
		cls.media = media_mod
		cls.frappe = frappe

	def setUp(self):
		self.frappe.get_all.return_value = []
		self.frappe.db.get_value.side_effect = None
		self.frappe.db.get_value.return_value = None

	def test_rejects_non_ready_status(self):
		self.frappe.get_all.return_value = [
			SimpleNamespace(
				name="ATT-1",
				attachment_index=0,
				status="Pending",
				file="FILE-1",
				content_hash="abc",
				mime_type="image/jpeg",
				attachment_type="image",
				error_summary=None,
			)
		]
		self.assertIsNone(self.media.get_ready_attachment_file_url("MSG-1", 0))

	def test_rejects_invalid_private_file(self):
		self.frappe.get_all.return_value = [
			SimpleNamespace(
				name="ATT-1",
				attachment_index=0,
				status="Ready",
				file="FILE-1",
				content_hash="abc",
				mime_type="image/jpeg",
				attachment_type="image",
				error_summary=None,
			)
		]
		with patch.object(self.media, "_file_is_valid_private", return_value=False):
			self.assertIsNone(self.media.get_ready_attachment_file_url("MSG-1", 0))

	def test_returns_safe_private_url(self):
		self.frappe.get_all.return_value = [
			SimpleNamespace(
				name="ATT-1",
				attachment_index=0,
				status="Ready",
				file="FILE-1",
				content_hash="abc",
				mime_type="image/jpeg",
				attachment_type="image",
				error_summary=None,
			)
		]
		self.frappe.db.get_value.return_value = "/private/files/omni_ok.jpg"
		with patch.object(self.media, "_file_is_valid_private", return_value=True):
			url = self.media.get_ready_attachment_file_url("MSG-1", 0)
		self.assertEqual(url, "/private/files/omni_ok.jpg")

	def test_safe_private_file_url_rejects_bad_paths(self):
		self.assertIsNone(self.media._safe_private_file_url("/files/public.jpg"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/../etc/passwd"))
		self.assertIsNone(self.media._safe_private_file_url("//evil.example/x"))
		self.assertIsNone(self.media._safe_private_file_url("C:\\Windows\\x"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/.._evil.pdf"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/foo?bar=1"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/foo%2f../etc/passwd"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/%2e%2e/%2e%2e/etc/passwd"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/%252e%252e/secret"))
		self.assertIsNone(self.media._safe_private_file_url("/private/files/a\tb.pdf"))
		self.assertEqual(
			self.media._safe_private_file_url("/private/files/ok.jpg"),
			"/private/files/ok.jpg",
		)

	def test_safe_private_file_url_accepts_unicode_and_spaces(self):
		"""Historical Arabic / spaced PDF File URLs must open (no rename)."""
		arabic = "/private/files/تقرير المبيعات.pdf"
		spaced = "/private/files/my report.pdf"
		hash_name = "/private/files/Invoice #1.pdf"
		self.assertEqual(self.media._safe_private_file_url(arabic), arabic)
		self.assertEqual(self.media._safe_private_file_url(spaced), spaced)
		self.assertEqual(self.media._safe_private_file_url(hash_name), hash_name)
		# Percent-encoded legitimate basename normalizes once for download_file.
		self.assertEqual(
			self.media._safe_private_file_url("/private/files/my%20report.pdf"),
			"/private/files/my report.pdf",
		)

	def test_map_ready_does_not_truncate_when_extra_ready_rows(self):
		"""Duplicate Ready children must not drop another parent's URL (mixed pages)."""
		rows = [
			SimpleNamespace(
				parent="MSG-IMG",
				file="FILE-IMG",
				content_hash="h1",
				status="Ready",
			),
			SimpleNamespace(
				parent="MSG-IMG",
				file="FILE-IMG-DUP",
				content_hash="h1b",
				status="Ready",
			),
			SimpleNamespace(
				parent="MSG-PDF",
				file="FILE-PDF",
				content_hash="h2",
				status="Ready",
			),
		]
		self.frappe.get_all.return_value = rows

		def _file_url(doctype, name, field=None):
			return {
				"FILE-IMG": "/private/files/omni_MSG-IMG_0.png",
				"FILE-IMG-DUP": "/private/files/omni_MSG-IMG_dup.png",
				"FILE-PDF": "/private/files/omni_MSG-PDF_0.pdf",
			}.get(name)

		self.frappe.db.get_value.side_effect = _file_url
		with patch.object(self.media, "_file_is_valid_private", return_value=True):
			mapped = self.media.map_ready_attachment_file_urls(["MSG-IMG", "MSG-PDF"])
		kwargs = self.frappe.get_all.call_args.kwargs
		self.assertEqual(kwargs.get("limit_page_length"), 0)
		self.assertEqual(mapped["MSG-IMG"], "/private/files/omni_MSG-IMG_0.png")
		self.assertEqual(mapped["MSG-PDF"], "/private/files/omni_MSG-PDF_0.pdf")


class TestPreferredAttachmentUrlContract(unittest.TestCase):
	def test_prefers_local_over_meta(self):
		from meta_omnichannel.tests._attachment_url_contract import preferred_attachment_url

		self.assertEqual(
			preferred_attachment_url(
				attachment_file_url="/private/files/a.jpg",
				attachment_url="https://cdn.example/a.jpg",
			),
			"/private/files/a.jpg",
		)

	def test_falls_back_to_https(self):
		from meta_omnichannel.tests._attachment_url_contract import preferred_attachment_url

		self.assertEqual(
			preferred_attachment_url(
				attachment_file_url=None,
				attachment_url="https://cdn.example/a.jpg",
			),
			"https://cdn.example/a.jpg",
		)

	def test_rejects_untrusted(self):
		from meta_omnichannel.tests._attachment_url_contract import safe_attachment_url

		self.assertIsNone(safe_attachment_url("javascript:alert(1)"))
		self.assertIsNone(safe_attachment_url("data:text/html,x"))
		self.assertIsNone(safe_attachment_url("//cdn.example/x"))
		self.assertIsNone(safe_attachment_url("/public/files/x"))
		self.assertIsNone(safe_attachment_url("../private/files/x"))
		self.assertIsNone(safe_attachment_url("http://cdn.example/x"))

	def test_download_href_uses_frappe_api_for_private(self):
		from meta_omnichannel.tests._attachment_url_contract import attachment_download_href
		from urllib.parse import unquote

		href = attachment_download_href("/private/files/omni_ok.jpg")
		self.assertIn("frappe.handler.download_file", href)
		self.assertIn("file_url=", href)
		self.assertEqual(
			attachment_download_href("https://cdn.example/a.jpg"),
			"https://cdn.example/a.jpg",
		)

	def test_download_href_encodes_unicode_and_spaces(self):
		from meta_omnichannel.tests._attachment_url_contract import (
			attachment_download_href,
			safe_private_file_path,
		)
		from urllib.parse import unquote

		arabic = "/private/files/تقرير المبيعات.pdf"
		self.assertEqual(safe_private_file_path(arabic), arabic)
		href = attachment_download_href(arabic)
		self.assertIsNotNone(href)
		self.assertTrue(href.startswith("/api/method/frappe.handler.download_file?"))
		raw_q = href.split("file_url=", 1)[1]
		# Wire form must be percent-encoded (no raw space); round-trip restores path.
		self.assertNotIn(" ", raw_q)
		self.assertEqual(unquote(raw_q), arabic)
		spaced = attachment_download_href("/private/files/my report.pdf")
		self.assertIn("my%20report.pdf", spaced)

	def test_contract_rejects_encoded_traversal(self):
		from meta_omnichannel.tests._attachment_url_contract import safe_private_file_path

		self.assertIsNone(safe_private_file_path("/private/files/%2e%2e/etc/passwd"))
		self.assertIsNone(safe_private_file_path("/private/files/foo%2f..%2fbar"))
		self.assertIsNone(safe_private_file_path("/private/files/%252e%252e/x"))
		self.assertIsNone(safe_private_file_path("https://cdn.example/x.pdf"))


if __name__ == "__main__":
	unittest.main()