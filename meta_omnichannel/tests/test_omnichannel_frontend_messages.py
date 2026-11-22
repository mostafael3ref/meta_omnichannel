"""Regression checks for Inbox message loading / Frappe response handling.

These are source-level and Node-free unit checks (no browser, no Bench).
Realtime soft-refresh behavior is covered by an in-file executable contract
that mirrors public/frontend/omnichannel.js (no separate Socket.IO client).
"""

from __future__ import annotations

import os
import re
import shutil
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from meta_omnichannel.tests._attachment_url_contract import (
	attachment_download_href,
	preferred_attachment_url,
	safe_attachment_url,
	safe_https_url,
)

ROOT = Path(__file__).resolve().parents[2]
PLAIN_JS = ROOT / "meta_omnichannel" / "public" / "frontend" / "omnichannel.js"
PLAIN_CSS = ROOT / "meta_omnichannel" / "public" / "frontend" / "omnichannel.css"
VUE_API = ROOT / "frontend" / "src" / "api.js"
VUE_APP = ROOT / "frontend" / "src" / "App.vue"
VUE_CSS = ROOT / "frontend" / "src" / "styles.css"
WWW = ROOT / "meta_omnichannel" / "www" / "omnichannel.html"

REALTIME_EVENT = "omnichannel_conversation_updated"
REALTIME_DEBOUNCE_MS = 400


def validate_conversation_hint(payload: Any) -> Optional[str]:
	"""Event payload is a hint only — nonempty conversation string required."""
	if not isinstance(payload, dict):
		return None
	name = payload.get("conversation")
	if not isinstance(name, str):
		return None
	trimmed = name.strip()
	return trimmed or None


def is_permission_error(err: Any) -> bool:
	msg = str(getattr(err, "message", None) or err or "").lower()
	return any(
		token in msg
		for token in (
			"not permitted",
			"permissionerror",
			"permission denied",
			"forbidden",
			"403",
		)
	)


@dataclass
class UiChrome:
	scroll_top: float = 0.0
	near_bottom: bool = True
	focus_composer: bool = False
	caret_start: Optional[int] = None
	caret_end: Optional[int] = None
	draft: str = ""


def capture_ui_chrome(
	*,
	scroll_top: float,
	scroll_height: float,
	client_height: float,
	focus_composer: bool,
	caret_start: Optional[int],
	caret_end: Optional[int],
	draft: str,
) -> UiChrome:
	near_bottom = scroll_height - scroll_top - client_height < 80
	return UiChrome(
		scroll_top=scroll_top,
		near_bottom=near_bottom,
		focus_composer=focus_composer,
		caret_start=caret_start,
		caret_end=caret_end,
		draft=draft,
	)


def restore_scroll(
	chrome: UiChrome, *, scroll_height: float, force_to_latest: bool = False
) -> float:
	"""Mirror restoreUiChrome message scroll rules in omnichannel.js."""
	if force_to_latest:
		return scroll_height
	if chrome.near_bottom:
		return scroll_height
	return chrome.scroll_top


@dataclass
class SoftRefreshInbox:
	"""Minimal state machine matching soft list/message refresh semantics."""

	selected: Optional[str] = None
	draft: str = ""
	messages: List[Dict[str, Any]] = field(default_factory=list)
	panel: Optional[Dict[str, Any]] = field(default_factory=dict)
	conversations: List[Dict[str, Any]] = field(default_factory=list)
	list_calls: int = 0
	message_calls: List[str] = field(default_factory=list)
	panel_calls: int = 0
	list_gen: int = 0
	messages_gen: int = 0
	subscribed: bool = False
	pending: Set[str] = field(default_factory=set)
	reconnect_pending: bool = False
	debounce_scheduled: bool = False
	permission_cleared: bool = False
	list_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None
	messages_provider: Optional[Callable[[str], List[Dict[str, Any]]]] = None
	messages_error: Optional[BaseException] = None

	def subscribe_once(self, realtime_available: bool = True) -> bool:
		if self.subscribed:
			return False
		if not realtime_available:
			return False
		self.subscribed = True
		return True

	def on_event(self, payload: Any) -> None:
		name = validate_conversation_hint(payload)
		if not name:
			return
		self.pending.add(name)
		self.debounce_scheduled = True

	def on_reconnect(self) -> None:
		self.reconnect_pending = True
		if self.selected:
			self.pending.add(self.selected)
		self.debounce_scheduled = True

	def flush(self) -> None:
		hints = set(self.pending)
		self.pending.clear()
		do_reconnect = self.reconnect_pending
		self.reconnect_pending = False
		self.debounce_scheduled = False
		selected_before = self.selected

		self._soft_load_list()
		refresh_messages = do_reconnect or (
			selected_before is not None and selected_before in hints
		)
		if refresh_messages and self.selected and (
			selected_before is None or self.selected == selected_before
		):
			self._soft_refresh_messages(self.selected)

	def _soft_load_list(self) -> None:
		gen = self.list_gen + 1
		self.list_gen = gen
		self.list_calls += 1
		items = self.list_provider() if self.list_provider else list(self.conversations)
		if gen != self.list_gen:
			return
		self.conversations = items

	def _soft_refresh_messages(self, conversation: str) -> None:
		if not self.selected or self.selected != conversation:
			return
		gen = self.messages_gen + 1
		self.messages_gen = gen
		self.message_calls.append(conversation)
		try:
			if self.messages_error is not None:
				raise self.messages_error
			items = (
				self.messages_provider(conversation)
				if self.messages_provider
				else list(self.messages)
			)
		except BaseException as err:
			if gen != self.messages_gen:
				return
			if self.selected != conversation:
				return
			if is_permission_error(err):
				self.clear_inaccessible()
				return
			raise
		if gen != self.messages_gen:
			return
		if self.selected != conversation:
			return
		self.messages = items

	def open_conversation(self, name: str) -> None:
		self.selected = name
		self.messages_gen += 1
		self.panel_calls += 1

	def clear_inaccessible(self) -> None:
		self.selected = None
		self.messages = []
		self.panel = None
		self.draft = ""
		self.messages_gen += 1
		self.permission_cleared = True


class TestDeployedFrontendIsPlainBundle(unittest.TestCase):
	def test_www_loads_public_frontend_bundle(self):
		"""Deployed /omnichannel serves public/frontend (not Vue src directly)."""
		html = WWW.read_text(encoding="utf-8")
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn("/assets/meta_omnichannel/frontend/omnichannel.js", html)
		self.assertIn("/assets/meta_omnichannel/frontend/omnichannel.css", html)
		self.assertNotIn("frontend/src/App.vue", html)
		self.assertIn("no-build fallback", js)
		self.assertNotIn("createApp", js)

	def test_plain_js_facebook_page_name_layout(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		css = PLAIN_CSS.read_text(encoding="utf-8")
		self.assertIn("function conversationPrimaryTitle", js)
		self.assertIn("function buildChatHeaderTitle", js)
		self.assertIn('provider || "") === "Facebook"', js)
		self.assertIn("conversationPrimaryTitle(c)", js)
		self.assertIn("conversationPrimaryTitle(selected)", js)
		self.assertIn("page_id_label", js)
		self.assertIn("oc-chat-page-id", js)
		self.assertIn("oc-chat-title-primary", js)
		# Page name is the primary title — not a second label.
		self.assertNotIn("oc-chat-page-name", js)
		self.assertNotIn(".oc-chat-page-name", css)
		self.assertIn("patchPreservedChatHeader", js)
		self.assertIn("fresh.page_name", js)
		self.assertIn(".oc-chat-page-id", css)
		# Header must not also paint participant_name when page_name is used.
		header_fn = js[
			js.index("function buildChatHeaderTitle") : js.index("async function loadList")
		]
		self.assertNotIn("selected.participant_name", header_fn)
		self.assertIn("conversationPrimaryTitle(selected)", header_fn)
		# List uses the same primary-title helper (one title row).
		self.assertIn("conversationPrimaryTitle(c)", js)

	def test_plain_js_customer_panel_uses_primary_title(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn("oc-customer-title", js)
		self.assertIn("conversationPrimaryTitle(state.selected)", js)
		self.assertIn("function patchPreservedCustomerTitle", js)
		# Secondary line still shows provider / participant_id.
		self.assertIn(
			"`${state.selected.provider} / ${state.selected.participant_id}`",
			js,
		)
		# Old bold participant_name HTML injection must be gone.
		self.assertNotIn(
			"html: `<strong>${state.selected.participant_name || \"\"}</strong>`",
			js,
		)


class TestFrappeCallDoesNotDropMessageOnServerMessages(unittest.TestCase):
	def test_plain_js_does_not_treat_server_messages_alone_as_failure(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertNotRegex(
			js,
			r"if\s*\(\s*data\.exc\s*\|\|\s*data\._server_messages\s*\)",
			"call() must not throw solely because _server_messages is present",
		)
		self.assertIn("!response.ok || data.exc", js)

	def test_vue_unwrap_helper_keeps_message_when_server_messages_present(self):
		# Execute the pure helper logic mirrored in api.js via a tiny eval of the
		# exported behavior by re-implementing the contract under test in Python.
		from meta_omnichannel.tests._frappe_response_contract import unwrap_frappe_message

		payload = {
			"message": {"items": [{"name": "m1"}], "total": 1},
			"_server_messages": '["{\\"message\\":\\"FYI\\"}"]',
		}
		result = unwrap_frappe_message(payload, ok=True)
		self.assertEqual(result["items"][0]["name"], "m1")

		with self.assertRaises(ValueError):
			unwrap_frappe_message({"exc": "Traceback", "message": {"items": []}}, ok=True)


class TestOpenConversationDoesNotCouplePanelToMessages(unittest.TestCase):
	def test_plain_js_loads_messages_before_panel(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		# Guard against regressing to Promise.all([list_messages, get_customer_panel])
		self.assertIsNone(
			re.search(
				r"Promise\.all\(\[\s*call\(\s*[\"']meta_omnichannel\.api\.inbox\.list_messages",
				js,
			)
		)
		self.assertIn("list_messages", js)
		self.assertIn("get_customer_panel", js)
		self.assertIn("state.messages = (msgData && msgData.items) || []", js)

	def test_vue_app_loads_messages_independently(self):
		vue = VUE_APP.read_text(encoding="utf-8")
		self.assertIsNone(
			re.search(
				r"Promise\.all\(\[\s*call\(\s*[\"']meta_omnichannel\.api\.inbox\.list_messages",
				vue,
			)
		)
		self.assertIn("messages.value = (msgData && msgData.items) || []", vue)


class TestPendingChannelDoesNotHideFacebookMessages(unittest.TestCase):
	def test_pending_placeholder_only_for_non_facebook(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		# Composer/pending gate is Facebook-only; messages still render for Facebook.
		self.assertIn(
			'if (state.selected && state.selected.provider !== "Facebook")',
			js,
		)
		msg_idx = js.index('className: "oc-messages"')
		pending_render_idx = js.index("t(\"pending_channel\")")
		self.assertLess(msg_idx, pending_render_idx)


class TestSafeHttpsUrlContract(unittest.TestCase):
	def test_accepts_https_only(self):
		ok = "https://scontent.xx.fbcdn.net/v/t1/a.jpg?oe=1&oh=2"
		self.assertEqual(safe_https_url(ok), ok)
		self.assertIsNone(safe_https_url("http://cdn.example/a.jpg"))
		self.assertIsNone(safe_https_url("javascript:alert(1)"))
		self.assertIsNone(safe_https_url("data:text/html,<script>"))
		self.assertIsNone(safe_https_url(""))
		self.assertIsNone(safe_https_url(None))
		self.assertIsNone(safe_https_url("not a url"))

	def test_safe_attachment_url_accepts_private_and_https(self):
		self.assertEqual(
			safe_attachment_url("/private/files/omni_x.jpg"),
			"/private/files/omni_x.jpg",
		)
		self.assertEqual(
			preferred_attachment_url(
				attachment_file_url="/private/files/a.jpg",
				attachment_url="https://cdn.example/a.jpg",
			),
			"/private/files/a.jpg",
		)
		self.assertIsNone(safe_attachment_url("//cdn.example/x"))
		self.assertIsNone(safe_attachment_url("/files/public.jpg"))
		self.assertIsNone(safe_attachment_url("javascript:alert(1)"))

	def test_private_download_uses_frappe_handler(self):
		href = attachment_download_href("/private/files/omni_x.jpg")
		self.assertIsNotNone(href)
		self.assertTrue(href.startswith("/api/method/frappe.handler.download_file?"))
		self.assertIn("file_url=", href)
		self.assertIn("omni_x.jpg", href)
		# External Meta: plain https fallback (no forced download claim).
		self.assertEqual(
			attachment_download_href("https://cdn.example/a.jpg"),
			"https://cdn.example/a.jpg",
		)

	def test_plain_js_and_vue_enforce_https(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		vue = VUE_APP.read_text(encoding="utf-8")
		self.assertIn("function safeHttpsUrl", js)
		self.assertIn('u.protocol !== "https:"', js)
		self.assertIn("function safeAttachmentUrl", js)
		self.assertIn("function safePrivateFilePath", js)
		self.assertIn("preferredAttachmentUrl", js)
		self.assertIn("/private/files/", js)
		self.assertIn("frappe.handler.download_file", js)
		self.assertIn("function getLightboxFocusables", js)
		self.assertIn("lightboxTrigger", js)
		self.assertIn("function safeHttpsUrl", vue)
		self.assertIn('u.protocol !== "https:"', vue)


class TestAttachmentRenderingSource(unittest.TestCase):
	def test_plain_js_renders_image_audio_video_and_file(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn('el("img"', js)
		self.assertIn('el("audio"', js)
		self.assertIn('el("video"', js)
		self.assertIn("renderSafeAttachmentLink", js)
		self.assertIn('type === "Image"', js)
		self.assertIn('type === "Audio"', js)
		self.assertIn('type === "Video"', js)
		self.assertIn("loading", js)
		self.assertIn("controls", js)
		self.assertIn('rel: "noopener noreferrer"', js)
		self.assertIn("onerror: onMediaError", js)
		self.assertIn("replaceWith(renderSafeAttachmentLink", js)
		# Must not fall back to label-only for attachments with URLs.
		self.assertNotIn("el(\"div\", { text: m.text_content || m.message_type })", js)
		self.assertIn("renderMessageBody(m)", js)
		# Captions preserved separately from media.
		self.assertIn('className: "oc-bubble-text"', js)
		self.assertIn("attachment_unavailable", js)
		self.assertIn("preferredAttachmentUrl(m)", js)
		self.assertIn("openImageLightbox", js)
		self.assertIn("Escape", js)
		self.assertIn("download_attachment", js)
		self.assertIn("attachmentDownloadHref", js)
		self.assertIn("renderDownloadAction", js)
		self.assertIn('ev.key !== "Tab"', js)
		self.assertIn("trigger.focus", js)

	def test_plain_js_never_injects_attachment_via_innerhtml(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		# Attachment path must use setAttribute/textContent helpers, not html:.
		attach_fn = js[js.index("function renderAttachmentBlock") : js.index("function renderMessageBody")]
		self.assertNotIn("innerHTML", attach_fn)
		self.assertNotIn("html:", attach_fn)

	def test_vue_renders_image_audio_video_file_and_fallbacks(self):
		vue = VUE_APP.read_text(encoding="utf-8")
		self.assertIn('v-if="m.message_type === \'Image\'"', vue)
		self.assertIn('v-else-if="m.message_type === \'Audio\'"', vue)
		self.assertIn('v-else-if="m.message_type === \'Video\'"', vue)
		self.assertIn('rel="noopener noreferrer"', vue)
		self.assertIn('@error="markMediaFailed(m.name)"', vue)
		self.assertIn("attachment_unavailable", vue)
		self.assertIn("messageCaption(m)", vue)
		self.assertIn("safeHttpsUrl(m.attachment_url)", vue)
		# Old unsafe bare href without https gate must be gone.
		self.assertNotIn('<a :href="m.attachment_url" target="_blank" rel="noopener">Attachment</a>', vue)

	def test_css_constrains_media(self):
		plain = PLAIN_CSS.read_text(encoding="utf-8")
		self.assertIn(".oc-attach-media", plain)
		self.assertIn("max-width: 100%", plain)
		self.assertIn("max-height: 320px", plain)
		self.assertIn("min-width: 0", plain)
		self.assertIn("object-fit: contain", plain)
		self.assertIn(".oc-lightbox", plain)
		# Vue CSS unchanged in this step — keep prior media constraints.
		vue_css = VUE_CSS.read_text(encoding="utf-8")
		self.assertIn(".oc-attach-media", vue_css)
		self.assertIn("max-width: 100%", vue_css)
		self.assertIn("max-height: 320px", vue_css)

	def test_css_viewport_bounded_independent_scroll(self):
		"""Inbox shell stays viewport-bound; messages + list scroll inside columns."""
		css = PLAIN_CSS.read_text(encoding="utf-8")
		# Page shell must not grow with message history.
		self.assertIn("overflow: hidden", css)
		self.assertRegex(css, r"\.oc-shell\s*\{[^}]*height:\s*100%")
		self.assertRegex(css, r"\.oc-shell\s*\{[^}]*overflow:\s*hidden")
		self.assertRegex(
			css,
			r"\.oc-side,\s*\.oc-list,\s*\.oc-chat,\s*\.oc-customer\s*\{[^}]*min-height:\s*0",
		)
		# Chat column: header | scrollable messages | composer.
		self.assertIn("grid-template-rows: auto minmax(0, 1fr) auto", css)
		msgs_block = css[css.index(".oc-messages") : css.index(".oc-bubble")]
		self.assertIn("min-height: 0", msgs_block)
		self.assertIn("overflow-y: auto", msgs_block)
		# Conversation list scrolls independently of the toolbar.
		self.assertIn(".oc-list-items", css)
		list_items = css[css.index(".oc-list-items") : css.index(".oc-list-toolbar input")]
		self.assertIn("min-height: 0", list_items)
		self.assertIn("overflow-y: auto", list_items)
		# Mobile still allows page flow while chat keeps a bounded height.
		mobile = css[css.index("@media (max-width: 760px)") :]
		self.assertIn("overflow: auto", mobile)
		self.assertIn("height: min(70vh, 640px)", mobile)


class TestRealtimeSoftRefreshContract(unittest.TestCase):
	"""Executable soft-refresh behavior (no browser). Wiring checks are separate."""

	def test_invalid_payload_ignored(self):
		self.assertIsNone(validate_conversation_hint(None))
		self.assertIsNone(validate_conversation_hint("CONV-1"))
		self.assertIsNone(validate_conversation_hint({"conversation": ""}))
		self.assertIsNone(validate_conversation_hint({"conversation": "   "}))
		self.assertIsNone(validate_conversation_hint({"conversation": 12}))
		self.assertIsNone(validate_conversation_hint({"conversation": None}))
		self.assertEqual(validate_conversation_hint({"conversation": " CONV-1 "}), "CONV-1")

		inbox = SoftRefreshInbox(selected="CONV-1")
		inbox.on_event({"conversation": ""})
		inbox.on_event({"other": "CONV-1"})
		inbox.on_event(None)
		self.assertEqual(inbox.pending, set())
		self.assertFalse(inbox.debounce_scheduled)

	def test_burst_events_coalesced_to_one_list_refresh(self):
		inbox = SoftRefreshInbox(selected="CONV-A", conversations=[{"name": "CONV-A"}])
		inbox.list_provider = lambda: [{"name": "CONV-A"}, {"name": "CONV-B"}]
		inbox.messages_provider = lambda _c: [{"name": "m1"}]
		inbox.on_event({"conversation": "CONV-A"})
		inbox.on_event({"conversation": "CONV-A"})
		inbox.on_event({"conversation": "CONV-B"})
		self.assertEqual(inbox.pending, {"CONV-A", "CONV-B"})
		inbox.flush()
		self.assertEqual(inbox.list_calls, 1)
		self.assertEqual(inbox.message_calls, ["CONV-A"])
		self.assertEqual(inbox.pending, set())

	def test_only_active_conversation_refreshes_messages(self):
		inbox = SoftRefreshInbox(selected="CONV-A")
		inbox.messages_provider = lambda c: [{"name": f"msg-{c}"}]
		inbox.on_event({"conversation": "CONV-B"})
		inbox.flush()
		self.assertEqual(inbox.list_calls, 1)
		self.assertEqual(inbox.message_calls, [])

		inbox.on_event({"conversation": "CONV-A"})
		inbox.flush()
		self.assertEqual(inbox.message_calls, ["CONV-A"])
		self.assertEqual(inbox.panel_calls, 0)

	def test_soft_refresh_never_reloads_customer_panel(self):
		inbox = SoftRefreshInbox(selected="CONV-1", panel={"lead": {"name": "L1"}})
		inbox.messages_provider = lambda _c: [{"name": "m2"}]
		panel_before = inbox.panel
		inbox.on_event({"conversation": "CONV-1"})
		inbox.flush()
		self.assertIs(inbox.panel, panel_before)
		self.assertEqual(inbox.panel_calls, 0)

	def test_draft_and_scroll_chrome_preserved(self):
		chrome = capture_ui_chrome(
			scroll_top=120,
			scroll_height=500,
			client_height=200,
			focus_composer=True,
			caret_start=3,
			caret_end=3,
			draft="hello draft",
		)
		self.assertFalse(chrome.near_bottom)
		self.assertEqual(restore_scroll(chrome, scroll_height=600), 120)
		self.assertEqual(chrome.draft, "hello draft")
		self.assertTrue(chrome.focus_composer)
		self.assertEqual(chrome.caret_start, 3)

		near = capture_ui_chrome(
			scroll_top=410,
			scroll_height=500,
			client_height=200,
			focus_composer=False,
			caret_start=None,
			caret_end=None,
			draft="x",
		)
		self.assertTrue(near.near_bottom)
		self.assertEqual(restore_scroll(near, scroll_height=700), 700)

		# Opening a different conversation always jumps to latest, even if chrome
		# still remembers a mid-thread reading position from the prior thread.
		self.assertEqual(
			restore_scroll(chrome, scroll_height=900, force_to_latest=True),
			900,
		)

		inbox = SoftRefreshInbox(selected="CONV-1", draft="keep me")
		inbox.messages_provider = lambda _c: [{"name": "m"}]
		inbox.on_event({"conversation": "CONV-1"})
		inbox.flush()
		self.assertEqual(inbox.draft, "keep me")

	def test_stale_response_does_not_overwrite_new_selection(self):
		inbox = SoftRefreshInbox(selected="CONV-OLD", messages=[{"name": "old"}])

		def slow_messages(conversation: str):
			inbox.open_conversation("CONV-NEW")
			return [{"name": f"stale-{conversation}"}]

		inbox.messages_provider = slow_messages
		inbox.on_event({"conversation": "CONV-OLD"})
		inbox.flush()
		self.assertEqual(inbox.selected, "CONV-NEW")
		self.assertEqual(inbox.messages, [{"name": "old"}])
		self.assertEqual(inbox.message_calls, ["CONV-OLD"])

	def test_permission_error_clears_private_data(self):
		inbox = SoftRefreshInbox(
			selected="PRIVATE-1",
			draft="secret draft",
			messages=[{"name": "private-msg"}],
			panel={"lead": {"name": "L"}},
		)
		inbox.messages_error = PermissionError("Not permitted")
		inbox.on_event({"conversation": "PRIVATE-1"})
		inbox.flush()
		self.assertTrue(inbox.permission_cleared)
		self.assertIsNone(inbox.selected)
		self.assertEqual(inbox.messages, [])
		self.assertIsNone(inbox.panel)
		self.assertEqual(inbox.draft, "")

	def test_reconnect_schedules_one_soft_refresh(self):
		inbox = SoftRefreshInbox(selected="CONV-1")
		inbox.messages_provider = lambda _c: [{"name": "after-reconnect"}]
		inbox.on_reconnect()
		self.assertTrue(inbox.debounce_scheduled)
		self.assertTrue(inbox.reconnect_pending)
		inbox.flush()
		self.assertEqual(inbox.list_calls, 1)
		self.assertEqual(inbox.message_calls, ["CONV-1"])
		self.assertFalse(inbox.reconnect_pending)

	def test_subscribe_exactly_once_and_works_without_realtime(self):
		inbox = SoftRefreshInbox()
		self.assertTrue(inbox.subscribe_once(True))
		self.assertFalse(inbox.subscribe_once(True))
		self.assertTrue(inbox.subscribed)

		offline = SoftRefreshInbox()
		self.assertFalse(offline.subscribe_once(False))
		self.assertFalse(offline.subscribed)
		offline.list_provider = lambda: [{"name": "C1"}]
		offline._soft_load_list()
		self.assertEqual(len(offline.conversations), 1)


class TestRealtimeSoftRefreshWiring(unittest.TestCase):
	"""Source wiring for published omnichannel.js — does not prove browser runtime."""

	def test_subscribes_once_to_permission_scoped_event(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn('const REALTIME_EVENT = "omnichannel_conversation_updated"', js)
		self.assertIn(REALTIME_EVENT, js)
		self.assertIn("frappe.realtime", js)
		self.assertIn("rt.on(REALTIME_EVENT, onConversationUpdated)", js)
		self.assertIn("realtimeSubscribed && realtimeEventSocket === rt.socket", js)
		self.assertEqual(js.count("subscribeRealtimeOnce()"), 3)  # def + retry + boot
		self.assertIn("rt.connect()", js)
		self.assertIn("ensureRealtimeConnected", js)
		self.assertIn("scheduleRealtimeSubscribeRetry", js)
		self.assertIn("clearRealtimeSubscribeRetry", js)
		self.assertIn('socket.on("connect", onRealtimeSocketConnect)', js)
		self.assertIn("realtimeBoundSocket === socket", js)
		# Must not mark subscribed before socket exists (Frappe on() no-ops).
		self.assertIn("!rt.socket", js)
		# Prefer Frappe lifecycle connect over raw socket.connect for lazy_connect.
		self.assertNotRegex(js, r"socket\.disconnected.*socket\.connect\(\)")

	def test_soft_message_refresh_skips_open_conversation_and_panel(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		start = js.index("async function softRefreshMessages")
		end = js.index("async function openConversation")
		soft = js[start:end]
		self.assertIn("list_messages", soft)
		self.assertNotIn("get_customer_panel", soft)
		self.assertNotIn("openConversation(", soft)
		self.assertNotIn("mark_conversation_read", soft)
		self.assertNotIn("unread_count = 0", soft)
		self.assertIn("isPermissionError", soft)
		self.assertIn("clearInaccessibleConversation", soft)
		self.assertIn("pendingSoftMessageRefresh", soft)

		flush_start = js.index("async function flushRealtimeRefresh")
		flush_end = js.index("function scheduleRealtimeRefresh")
		flush = js[flush_start:flush_end]
		self.assertIn("softLoadList()", flush)
		self.assertIn("softRefreshMessages(", flush)
		self.assertNotIn("get_customer_panel", flush)
		self.assertNotIn("openConversation(", flush)

	def test_render_preserves_composer_search_and_note_chrome(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn("function captureUiChrome", js)
		self.assertIn("function restoreUiChrome", js)
		self.assertIn("const chrome = captureUiChrome()", js)
		self.assertIn("restoreUiChrome(chrome)", js)
		self.assertIn("captureFocusedField", js)
		self.assertIn(".oc-composer textarea", js)
		self.assertIn(".oc-list-toolbar input[type='search']", js)
		self.assertIn(".oc-customer textarea", js)
		self.assertIn("setSelectionRange", js)
		self.assertIn("chrome.nearBottom", js)
		self.assertIn("preserveChat: true", js)
		self.assertIn("openConversationSeq", js)
		# Open → latest; soft refresh reuses nearBottom / scrollTop (no duplicate scrollers).
		self.assertIn("pendingScrollToLatest", js)
		self.assertIn("pendingScrollToLatest = true", js)
		self.assertIn('className: "oc-list-items"', js)
		restore_fn = js[
			js.index("function restoreUiChrome") : js.index("function el(")
		]
		self.assertIn("pendingScrollToLatest && !state.loadingChat", restore_fn)
		self.assertIn("chrome.nearBottom", restore_fn)
		self.assertIn("chrome.scrollTop", restore_fn)

	def test_debounce_window_is_within_required_band(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn(f"const REALTIME_DEBOUNCE_MS = {REALTIME_DEBOUNCE_MS}", js)
		self.assertGreaterEqual(REALTIME_DEBOUNCE_MS, 350)
		self.assertLessEqual(REALTIME_DEBOUNCE_MS, 500)

	def test_graceful_when_realtime_missing(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertIn("!rt || typeof rt.on !== \"function\" || !rt.socket", js)
		self.assertIn("scheduleRealtimeSubscribeRetry", js)
		self.assertIn("subscribeRealtimeOnce();", js)
		self.assertIn("loadList();", js)

	def test_production_surface_has_no_test_only_mutators(self):
		js = PLAIN_JS.read_text(encoding="utf-8")
		self.assertNotIn("bumpMessagesRefreshGen", js)
		self.assertNotIn("setSearch:", js)
		self.assertNotIn("setFilter:", js)
		self.assertIn("window.__omnichannelRealtime", js)
		self.assertIn("getState:", js)


def _find_node_executable() -> Optional[str]:
	candidates = [
		Path(os.environ.get("LOCALAPPDATA", ""))
		/ "Programs"
		/ "cursor"
		/ "resources"
		/ "app"
		/ "resources"
		/ "helpers"
		/ "node.exe",
		Path(os.environ.get("ProgramFiles", "")) / "nodejs" / "node.exe",
		Path("/usr/bin/node"),
		Path("/usr/local/bin/node"),
	]
	which = shutil.which("node")
	if which:
		candidates.insert(0, Path(which))
	for path in candidates:
		if path and path.is_file():
			return str(path)
	return None


NODE_HARNESS = r"""
"use strict";
const fs = require("fs");
const assert = require("assert");
const vm = require("vm");
const jsPath = process.argv[2];
const scenario = process.argv[3];

function createEl(tag) {
  const kids = [];
  const listeners = {};
  const attrs = {};
  const node = {
    tagName: String(tag).toUpperCase(),
    className: "",
    style: {},
    children: kids,
    childNodes: kids,
    parentNode: null,
    value: "",
    textContent: "",
    selectionStart: 0,
    selectionEnd: 0,
    scrollTop: 0,
    scrollHeight: 200,
    clientHeight: 100,
    disabled: false,
    checked: false,
    selected: false,
    type: "",
    href: "",
    src: "",
    alt: "",
    rel: "",
    target: "",
    rows: "",
    placeholder: "",
    dataset: {},
    _innerHTML: "",
    get innerHTML() { return node._innerHTML; },
    set innerHTML(v) {
      node._innerHTML = String(v);
      if (String(v) === "") {
        while (kids.length) {
          const c = kids.pop();
          c.parentNode = null;
        }
      }
    },
    setAttribute(k, v) {
      attrs[k] = v;
      if (k === "value") node.value = v;
      if (k === "type") node.type = String(v);
      if (k === "class") node.className = v;
    },
    getAttribute(k) { return attrs[k]; },
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    removeEventListener() {},
    appendChild(c) {
      if (!c) return c;
      c.parentNode = node;
      kids.push(c);
      return c;
    },
    removeChild(c) {
      const i = kids.indexOf(c);
      if (i >= 0) kids.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    remove() {
      if (node.parentNode) node.parentNode.removeChild(node);
    },
    replaceWith(n) {
      const p = node.parentNode;
      if (!p) return;
      const i = p.children.indexOf(node);
      if (i >= 0) {
        p.children[i] = n;
        n.parentNode = p;
        node.parentNode = null;
      }
    },
    focus() { sandbox.document.activeElement = node; },
    blur() {
      if (sandbox.document.activeElement === node) sandbox.document.activeElement = null;
    },
    setSelectionRange(a, b) { node.selectionStart = a; node.selectionEnd = b; },
    querySelector(sel) { return queryOne(node, sel); },
    querySelectorAll(sel) { return queryAll(node, sel); },
    _listeners: listeners,
  };
  return node;
}

function matchSel(node, sel) {
  if (!sel) return false;
  if (sel.startsWith(".")) {
    const cls = sel.slice(1).split(/[\s>\[]/)[0];
    return String(node.className || "").split(/\s+/).includes(cls);
  }
  if (sel.includes("[")) {
    const base = sel.replace(/\[.*$/, "");
    if (base && !matchSel(node, base)) return false;
    if (/type=['"]search['"]/.test(sel)) return node.type === "search";
    return true;
  }
  if (sel.includes(".")) {
    const [tag, cls] = sel.split(".");
    const tagOk = !tag || node.tagName === tag.toUpperCase();
    const clsOk = String(node.className || "").split(/\s+/).includes(cls.split(/[\s>\[]/)[0]);
    return tagOk && clsOk;
  }
  return node.tagName === sel.toUpperCase();
}

function walk(node, out) {
  out.push(node);
  (node.children || []).forEach((c) => walk(c, out));
}
function queryAll(rootNode, sel) {
  const parts = sel.trim().split(/\s+/);
  let set = [rootNode];
  for (const part of parts) {
    const next = [];
    for (const n of set) {
      const all = [];
      walk(n, all);
      for (const cand of all) {
        if (cand !== n && matchSel(cand, part)) next.push(cand);
      }
    }
    set = next;
  }
  set.forEach = Array.prototype.forEach;
  return set;
}
function queryOne(rootNode, sel) {
  const all = queryAll(rootNode, sel);
  return all[0] || null;
}

const root = createEl("div");
root.id = "omnichannel-app";
root.querySelector = (sel) => queryOne(root, sel);

const gate = { list: [], messages: [], panel: [] };
function defer() {
  let settled = false;
  let resolve, reject;
  const p = new Promise((res, rej) => { resolve = res; reject = rej; });
  p.resolve = (v) => {
    if (settled) return;
    settled = true;
    resolve(v);
  };
  p.reject = (e) => {
    if (settled) return;
    settled = true;
    reject(e);
  };
  p.isSettled = () => settled;
  return p;
}

// Mirrors Frappe v15 RealTimeClient: on() no-ops without socket; connect() clears lazy_connect.
function createMockSocket() {
  const handlers = {};
  return {
    connected: false,
    disconnected: true,
    handlers,
    connect() {
      // Establish the transport; tests fire "connect" explicitly for reconnect cases
      // so boot does not race a debounced soft list refresh.
      this.connected = true;
      this.disconnected = false;
    },
    on(event, fn) {
      (handlers[event] = handlers[event] || []).push(fn);
    },
    off(event, fn) {
      const list = handlers[event] || [];
      handlers[event] = list.filter((x) => x !== fn);
    },
    listenerCount(event) {
      return (handlers[event] || []).length;
    },
    emit(event, payload) {
      (handlers[event] || []).forEach((fn) => fn(payload));
    },
  };
}

function createFrappeRealtime(opts) {
  const options = opts || {};
  const rt = {
    lazy_connect: options.lazy_connect !== false,
    socket: options.withSocket === false ? null : createMockSocket(),
    connectCalls: 0,
    onCalls: [],
    connect() {
      this.connectCalls += 1;
      if (this.lazy_connect && this.socket) {
        this.socket.connect();
        this.lazy_connect = false;
      }
    },
    on(event, callback) {
      this.onCalls.push(event);
      // Exact Frappe behavior: silent no-op until socket exists.
      if (this.socket) {
        this.connect();
        this.socket.on(event, callback);
      }
    },
    attachSocket() {
      this.socket = createMockSocket();
      return this.socket;
    },
    replaceSocket() {
      this.socket = createMockSocket();
      this.lazy_connect = true;
      return this.socket;
    },
  };
  if (rt.socket && options.autoConnect) {
    rt.lazy_connect = false;
    rt.socket.connected = true;
    rt.socket.disconnected = false;
  }
  return rt;
}

const startWithoutSocket =
  scenario.indexOf("lazy_") === 0 ||
  scenario === "subscribe_waits_for_socket" ||
  scenario === "registration_error_keeps_retrying";
const realtime = createFrappeRealtime({
  lazy_connect: true,
  withSocket: !startWithoutSocket,
  autoConnect: false,
});

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  URL,
  URLSearchParams,
  Object,
  Array,
  String,
  Number,
  Boolean,
  JSON,
  Error,
  Promise,
  Math,
  Date,
  parseInt,
  isNaN,
  csrf_token: "t",
  omnichannelBoot: { lang: "en", user: "agent@example.com", csrf_token: "t" },
  frappe: { realtime: realtime },
  document: {
    activeElement: null,
    getElementById(id) { return id === "omnichannel-app" ? root : null; },
    createElement: createEl,
    querySelector(sel) { return queryOne(root, sel); },
  },
  fetch: async (url, opts) => {
    const params = new URLSearchParams(opts.body);
    const path = String(url || "");
    if (path.indexOf("list_conversations") !== -1) {
      const entry = { search: params.get("search") || "", d: defer() };
      gate.list.push(entry);
      const items = await entry.d;
      return { ok: true, json: async () => ({ message: { items } }) };
    }
    if (path.indexOf("list_messages") !== -1) {
      const entry = { conversation: params.get("conversation"), d: defer() };
      gate.messages.push(entry);
      const items = await entry.d;
      return { ok: true, json: async () => ({ message: { items } }) };
    }
    if (path.indexOf("get_customer_panel") !== -1) {
      const entry = { conversation: params.get("conversation"), d: defer() };
      gate.panel.push(entry);
      const panel = await entry.d;
      return { ok: true, json: async () => ({ message: panel }) };
    }
    return { ok: true, json: async () => ({ message: { ok: true } }) };
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

vm.createContext(sandbox);
// Load published source, then inject test-only mutators in-memory only (never shipped).
let publishedSrc = fs.readFileSync(jsPath, "utf8");
assert.ok(
  publishedSrc.indexOf("bumpMessagesRefreshGen") === -1,
  "production bundle must not expose bumpMessagesRefreshGen"
);
assert.ok(
  publishedSrc.indexOf("setSearch:") === -1 && publishedSrc.indexOf("setFilter:") === -1,
  "production bundle must not expose setSearch/setFilter mutators"
);
const harnessOnlyMutators =
  "bumpMessagesRefreshGen: function () { messagesRefreshGen += 1; return messagesRefreshGen; },\n" +
  "    setSearch: function (value) { state.search = value || \"\"; },\n" +
  "    setFilter: function (value) { state.filter = value || \"all\"; },\n" +
  "    ";
const instrumentedSrc = publishedSrc.replace(
  "window.__omnichannelRealtime = {",
  "window.__omnichannelRealtime = {\n    " + harnessOnlyMutators
);
assert.notStrictEqual(instrumentedSrc, publishedSrc, "failed to inject harness-only mutators");
vm.runInContext(instrumentedSrc, sandbox, { filename: "omnichannel.js" });
const api = sandbox.__omnichannelRealtime;
assert.ok(api, "realtime test surface missing");
assert.strictEqual(typeof api.bumpMessagesRefreshGen, "function");
assert.strictEqual(typeof api.setSearch, "function");

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
async function waitUntil(fn, label, tries = 100) {
  for (let i = 0; i < tries; i++) {
    if (fn()) return;
    await sleep(10);
  }
  throw new Error("timeout: " + label + " list=" + gate.list.length + " messages=" + gate.messages.length);
}

const bootItems = [
  { name: "CONV-A", participant_name: "A", provider: "Facebook", status: "Open", unread_count: 0 },
];

async function settleOpenListCalls(items) {
  for (const entry of gate.list) {
    if (!entry.d.isSettled()) entry.d.resolve(items || bootItems);
  }
}

(async () => {
  await waitUntil(() => gate.list.length >= 1, "boot list");
  gate.list[0].d.resolve(bootItems);

  if (scenario === "subscribe_waits_for_socket" || scenario === "lazy_connect_registers_listener") {
    assert.strictEqual(api.isSubscribed(), false, "must not claim subscribed before socket exists");
    assert.ok(!realtime.socket, "socket should be absent at boot for lazy scenario");
    const sock = realtime.attachSocket();
    await waitUntil(() => api.isSubscribed() === true, "retry subscribe after socket");
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(realtime.lazy_connect, false, "Frappe connect() must clear lazy_connect");
    assert.ok(realtime.connectCalls >= 1);
    assert.strictEqual(api.isConnectHandlerBound(), true);
    // Duplicate subscribe must not add another conversation listener.
    assert.strictEqual(api.subscribeRealtimeOnce(), false);
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(sock.listenerCount("connect"), 1);
    console.log("OK " + scenario);
    return;
  }

  if (scenario === "lazy_reconnect_no_duplicate_listeners") {
    assert.strictEqual(api.isSubscribed(), false);
    const sock = realtime.attachSocket();
    await waitUntil(() => api.isSubscribed() === true, "subscribed");
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(sock.listenerCount("connect"), 1);
    // Simulate transport reconnect callbacks on the same socket (Frappe keeps the socket).
    sock.emit("connect");
    sock.emit("connect");
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(sock.listenerCount("connect"), 1);
    assert.strictEqual(api.subscribeRealtimeOnce(), false);
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 1);
    // Reconnect soft refresh is debounced — settle without hanging the process.
    await sleep((api.debounceMs || 400) + 80);
    await settleOpenListCalls(bootItems);
    console.log("OK lazy_reconnect_no_duplicate_listeners");
    return;
  }

  if (scenario === "socket_replacement_rebinds_listeners") {
    const sock1 = realtime.socket || realtime.attachSocket();
    if (!api.isSubscribed()) {
      assert.strictEqual(api.subscribeRealtimeOnce(), true);
    }
    await waitUntil(() => api.isSubscribed() === true, "subscribed on sock1");
    assert.strictEqual(sock1.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(sock1.listenerCount("connect"), 1);
    assert.strictEqual(api.getBoundEventSocket(), sock1);
    assert.strictEqual(api.getBoundConnectSocket(), sock1);

    const sock2 = realtime.replaceSocket();
    assert.notStrictEqual(sock1, sock2);
    // Same-socket guard must not block rebind after Frappe replaces the socket object.
    assert.strictEqual(api.subscribeRealtimeOnce(), true);
    assert.strictEqual(sock2.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(sock2.listenerCount("connect"), 1);
    assert.strictEqual(api.getBoundEventSocket(), sock2);
    assert.strictEqual(api.getBoundConnectSocket(), sock2);
    // Old socket should not keep handlers after rebind cleanup.
    assert.strictEqual(sock1.listenerCount("connect"), 0);
    assert.strictEqual(sock1.listenerCount("omnichannel_conversation_updated"), 0);
    // Second call on the new socket must not duplicate.
    assert.strictEqual(api.subscribeRealtimeOnce(), false);
    assert.strictEqual(sock2.listenerCount("omnichannel_conversation_updated"), 1);
    assert.strictEqual(sock2.listenerCount("connect"), 1);
    console.log("OK socket_replacement_rebinds_listeners");
    return;
  }

  if (scenario === "registration_error_keeps_retrying") {
    assert.strictEqual(api.isSubscribed(), false);
    let blows = 1;
    const originalOn = realtime.on.bind(realtime);
    realtime.on = function (event, cb) {
      if (blows > 0) {
        blows -= 1;
        throw new Error("simulated registration failure");
      }
      return originalOn(event, cb);
    };
    const sock = realtime.attachSocket();
    assert.strictEqual(api.subscribeRealtimeOnce(), false);
    assert.strictEqual(api.isSubscribed(), false);
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 0);
    await waitUntil(() => api.isSubscribed() === true, "retry after registration error");
    assert.strictEqual(sock.listenerCount("omnichannel_conversation_updated"), 1);
    console.log("OK registration_error_keeps_retrying");
    return;
  }

  if (scenario === "soft_list_newest_wins") {
    const base = gate.list.length;
    const p1 = api.softLoadList();
    await waitUntil(() => gate.list.length >= base + 1, "soft1");
    api.setSearch("alpha");
    const p2 = api.softLoadList();
    assert.strictEqual(gate.list[base].search, "");
    api.setSearch("beta");
    const p3 = api.softLoadList();
    gate.list[base].d.resolve([
      { name: "STALE", participant_name: "stale", provider: "Facebook", status: "Open", unread_count: 0 },
    ]);
    await waitUntil(() => gate.list.length >= base + 2, "soft rerun");
    assert.strictEqual(gate.list[gate.list.length - 1].search, "beta");
    gate.list[gate.list.length - 1].d.resolve([
      { name: "CONV-BETA", participant_name: "beta", provider: "Facebook", status: "Open", unread_count: 0 },
    ]);
    await Promise.all([p1, p2, p3]);
    assert.strictEqual(api.getState().conversationCount, 1);
    console.log("OK soft_list_newest_wins calls=" + gate.list.length);
    return;
  }

  if (scenario === "queue_during_open") {
    const row = {
      name: "CONV-A",
      participant_name: "A",
      provider: "Facebook",
      status: "Open",
      unread_count: 1,
    };
    const openP = api.openConversation(row);
    await waitUntil(() => gate.messages.length >= 1, "open messages");
    await api.softRefreshMessages("CONV-A");
    assert.strictEqual(api.getPendingSoftMessageRefresh(), "CONV-A");
    assert.strictEqual(gate.messages.length, 1, "soft refresh must not call while loading");
    gate.messages[0].d.resolve([
      {
        name: "m-open",
        direction: "Inbound",
        message_type: "Text",
        text_content: "hi",
        sent_at: "2020-01-01",
      },
    ]);
    await waitUntil(() => gate.panel.length >= 1, "panel");
    gate.panel[0].d.resolve({ lead: null, contact: null, notes: [] });
    await waitUntil(() => gate.messages.length >= 2, "drained soft messages");
    gate.messages[1].d.resolve([
      {
        name: "m-open",
        direction: "Inbound",
        message_type: "Text",
        text_content: "hi",
        sent_at: "2020-01-01",
      },
      {
        name: "m-rt",
        direction: "Inbound",
        message_type: "Text",
        text_content: "new",
        sent_at: "2020-01-02",
      },
    ]);
    await openP;
    assert.strictEqual(api.getPendingSoftMessageRefresh(), null);
    assert.strictEqual(api.getState().messageCount, 2);
    assert.strictEqual(api.getState().loadingChat, false);
    console.log("OK queue_during_open");
    return;
  }

  if (scenario === "stale_open_finalization") {
    const row = {
      name: "CONV-A",
      participant_name: "A",
      provider: "Facebook",
      status: "Open",
      unread_count: 0,
    };
    const first = api.openConversation(row);
    await waitUntil(() => gate.messages.length >= 1, "first open");
    const second = api.openConversation(Object.assign({}, row, { participant_name: "A2" }));
    await waitUntil(() => gate.messages.length >= 2, "second open");
    gate.messages[0].d.resolve([
      {
        name: "stale",
        direction: "Inbound",
        message_type: "Text",
        text_content: "stale",
        sent_at: "1",
      },
    ]);
    await sleep(30);
    assert.strictEqual(api.getState().loadingChat, true);
    gate.messages[1].d.resolve([
      {
        name: "fresh",
        direction: "Inbound",
        message_type: "Text",
        text_content: "fresh",
        sent_at: "2",
      },
    ]);
    await waitUntil(() => gate.panel.length >= 1, "panel for second");
    gate.panel[gate.panel.length - 1].d.resolve({ lead: null, notes: [] });
    await second;
    await first;
    assert.strictEqual(api.getState().loadingChat, false);
    assert.strictEqual(api.getState().messageCount, 1);
    assert.strictEqual(api.getState().selected, "CONV-A");
    console.log("OK stale_open_finalization");
    return;
  }

  if (scenario === "preserve_search_note_and_media") {
    const row = {
      name: "CONV-A",
      participant_name: "A",
      provider: "Facebook",
      status: "Open",
      unread_count: 0,
    };
    const openP = api.openConversation(row);
    await waitUntil(() => gate.messages.length >= 1, "open");
    gate.messages[0].d.resolve([
      {
        name: "vid1",
        direction: "Inbound",
        message_type: "Video",
        attachment_url: "https://cdn.example/v.mp4",
        text_content: "",
        sent_at: "1",
      },
    ]);
    await waitUntil(() => gate.panel.length >= 1, "panel");
    gate.panel[0].d.resolve({ lead: null, notes: [] });
    await openP;
    const video = root.querySelector("video");
    assert.ok(video, "video mounted");
    video._mark = "keep-me";
    const note = root.querySelector(".oc-customer textarea");
    assert.ok(note, "note textarea");
    note.value = "internal";
    note.selectionStart = 2;
    note.selectionEnd = 2;
    note.focus();
    const listBase = gate.list.length;
    const softP = api.softLoadList();
    await waitUntil(() => gate.list.length >= listBase + 1, "soft list");
    gate.list[gate.list.length - 1].d.resolve([
      { name: "CONV-A", participant_name: "A", provider: "Facebook", status: "Open", unread_count: 0 },
      { name: "CONV-B", participant_name: "B", provider: "Facebook", status: "Open", unread_count: 0 },
    ]);
    await softP;
    const video2 = root.querySelector("video");
    assert.ok(video2);
    assert.strictEqual(video2._mark, "keep-me", "list-only refresh must reuse media element");
    const note2 = root.querySelector(".oc-customer textarea");
    assert.strictEqual(note2, note, "note textarea node reused");
    api.setSearch("kept");
    const search = root.querySelector(".oc-list-toolbar input");
    search.focus();
    search.selectionStart = 2;
    search.selectionEnd = 2;
    const listBase2 = gate.list.length;
    const softP2 = api.softLoadList();
    await waitUntil(() => gate.list.length >= listBase2 + 1, "soft list 2");
    gate.list[gate.list.length - 1].d.resolve([
      { name: "CONV-A", participant_name: "A", provider: "Facebook", status: "Open", unread_count: 0 },
    ]);
    await softP2;
    const search3 = root.querySelector(".oc-list-toolbar input");
    assert.strictEqual(search3.value, "kept");
    console.log("OK preserve_search_note_and_media");
    return;
  }

  if (scenario === "scroll_open_and_soft_refresh") {
    const row = {
      name: "CONV-A",
      participant_name: "A",
      provider: "Facebook",
      status: "Open",
      unread_count: 0,
    };
    const openP = api.openConversation(row);
    await waitUntil(() => gate.messages.length >= 1, "open messages");
    gate.messages[0].d.resolve([
      { name: "m1", direction: "Inbound", message_type: "Text", text_content: "one", sent_at: "1" },
      { name: "m2", direction: "Inbound", message_type: "Text", text_content: "two", sent_at: "2" },
    ]);
    await waitUntil(() => gate.panel.length >= 1, "panel");
    gate.panel[0].d.resolve({ lead: null, notes: [] });
    await openP;
    assert.ok(root.querySelector(".oc-list-items"), "conversation list scroll container");
    assert.ok(root.querySelector(".oc-chat-header"), "chat header present");
    assert.ok(root.querySelector(".oc-composer"), "composer present");
    let msgs = root.querySelector(".oc-messages");
    assert.ok(msgs, "messages pane");
    assert.strictEqual(
      msgs.scrollTop,
      msgs.scrollHeight,
      "open conversation scrolls to latest"
    );

    // Reading older messages: soft refresh must preserve scrollTop.
    msgs.scrollHeight = 800;
    msgs.clientHeight = 200;
    msgs.scrollTop = 40;
    const softAway = api.softRefreshMessages("CONV-A");
    await waitUntil(() => gate.messages.length >= 2, "soft away from bottom");
    gate.messages[gate.messages.length - 1].d.resolve([
      { name: "m1", direction: "Inbound", message_type: "Text", text_content: "one", sent_at: "1" },
      { name: "m2", direction: "Inbound", message_type: "Text", text_content: "two", sent_at: "2" },
      { name: "m3", direction: "Inbound", message_type: "Text", text_content: "three", sent_at: "3" },
    ]);
    await softAway;
    msgs = root.querySelector(".oc-messages");
    assert.strictEqual(msgs.scrollTop, 40, "soft refresh preserves reading position");

    // Near bottom: soft refresh should follow the tip.
    msgs.scrollHeight = 800;
    msgs.clientHeight = 200;
    msgs.scrollTop = 740;
    const softNear = api.softRefreshMessages("CONV-A");
    await waitUntil(() => gate.messages.length >= 3, "soft near bottom");
    gate.messages[gate.messages.length - 1].d.resolve([
      { name: "m1", direction: "Inbound", message_type: "Text", text_content: "one", sent_at: "1" },
      { name: "m2", direction: "Inbound", message_type: "Text", text_content: "two", sent_at: "2" },
      { name: "m3", direction: "Inbound", message_type: "Text", text_content: "three", sent_at: "3" },
      { name: "m4", direction: "Inbound", message_type: "Text", text_content: "four", sent_at: "4" },
    ]);
    await softNear;
    msgs = root.querySelector(".oc-messages");
    assert.strictEqual(
      msgs.scrollTop,
      msgs.scrollHeight,
      "near-bottom soft refresh follows latest"
    );

    // Switching conversations must jump to latest even if prior thread was scrolled up.
    msgs.scrollHeight = 800;
    msgs.clientHeight = 200;
    msgs.scrollTop = 50;
    const rowB = {
      name: "CONV-B",
      participant_name: "B",
      provider: "Facebook",
      status: "Open",
      unread_count: 0,
    };
    const openB = api.openConversation(rowB);
    await waitUntil(() => gate.messages.length >= 4, "open B messages");
    gate.messages[gate.messages.length - 1].d.resolve([
      { name: "b1", direction: "Inbound", message_type: "Text", text_content: "b", sent_at: "1" },
    ]);
    await waitUntil(() => gate.panel.length >= 2, "panel B");
    gate.panel[gate.panel.length - 1].d.resolve({ lead: null, notes: [] });
    await openB;
    msgs = root.querySelector(".oc-messages");
    assert.strictEqual(
      msgs.scrollTop,
      msgs.scrollHeight,
      "opening another conversation scrolls to latest"
    );
    console.log("OK scroll_open_and_soft_refresh");
    return;
  }

  if (scenario === "open_gen_invalidate_clears_loading") {
    const row = {
      name: "CONV-A",
      participant_name: "A",
      provider: "Facebook",
      status: "Open",
      unread_count: 0,
    };
    const openP = api.openConversation(row);
    await waitUntil(() => gate.messages.length >= 1, "open messages pending");
    assert.strictEqual(api.getState().loadingChat, true);
    // Invalidate messagesRefreshGen without starting a newer openConversationSeq.
    // Previously this early-returned from the list_messages try/catch and skipped
    // the panel try/finally, leaving loadingChat stuck true.
    api.bumpMessagesRefreshGen();
    await api.softRefreshMessages("CONV-A");
    assert.strictEqual(api.getPendingSoftMessageRefresh(), "CONV-A");
    gate.messages[0].d.resolve([
      {
        name: "stale-should-not-apply",
        direction: "Inbound",
        message_type: "Text",
        text_content: "stale",
        sent_at: "1",
      },
    ]);
    // Finalize clears loading before drain's soft refresh completes — poll, do not
    // deadlock on await openP (open's finally awaits the drained list_messages).
    await waitUntil(() => api.getState().loadingChat === false, "loading cleared by outer finally");
    assert.strictEqual(api.getState().selected, "CONV-A");
    assert.strictEqual(api.getState().messageCount, 0, "stale list_messages must not apply");
    await waitUntil(() => gate.messages.length >= 2, "drained soft after finalize");
    gate.messages[1].d.resolve([
      {
        name: "m-after",
        direction: "Inbound",
        message_type: "Text",
        text_content: "after",
        sent_at: "2",
      },
    ]);
    await openP;
    assert.strictEqual(api.getState().loadingChat, false, "must not remain stuck loading");
    assert.strictEqual(api.getState().messageCount, 1);
    assert.strictEqual(api.getPendingSoftMessageRefresh(), null);
    console.log("OK open_gen_invalidate_clears_loading");
    return;
  }

  throw new Error("unknown scenario " + scenario);
})().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
"""

class TestRealtimeConcurrencyNodeHarness(unittest.TestCase):
	"""Execute published omnichannel.js under Node with a minimal DOM/fetch mock."""

	@classmethod
	def setUpClass(cls):
		cls.node = _find_node_executable()
		if not cls.node:
			raise unittest.SkipTest("node executable not available")

	def _run_scenario(self, scenario: str) -> str:
		import subprocess
		import tempfile

		with tempfile.TemporaryDirectory() as tmp:
			harness = Path(tmp) / "harness.js"
			harness.write_text(NODE_HARNESS, encoding="utf-8")
			proc = subprocess.run(
				[self.node, str(harness), str(PLAIN_JS), scenario],
				capture_output=True,
				text=True,
				timeout=30,
			)
			if proc.returncode != 0:
				self.fail(
					f"scenario {scenario} failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
				)
			return proc.stdout

	def test_soft_load_list_newest_query_wins(self):
		out = self._run_scenario("soft_list_newest_wins")
		self.assertIn("OK soft_list_newest_wins", out)

	def test_realtime_message_refresh_queued_during_open(self):
		out = self._run_scenario("queue_during_open")
		self.assertIn("OK queue_during_open", out)

	def test_stale_open_conversation_finalization_ignored(self):
		out = self._run_scenario("stale_open_finalization")
		self.assertIn("OK stale_open_finalization", out)

	def test_list_only_refresh_preserves_media_and_search_value(self):
		out = self._run_scenario("preserve_search_note_and_media")
		self.assertIn("OK preserve_search_note_and_media", out)

	def test_messages_gen_invalidate_during_open_clears_loading(self):
		out = self._run_scenario("open_gen_invalidate_clears_loading")
		self.assertIn("OK open_gen_invalidate_clears_loading", out)

	def test_scroll_open_and_soft_refresh(self):
		out = self._run_scenario("scroll_open_and_soft_refresh")
		self.assertIn("OK scroll_open_and_soft_refresh", out)

	def test_subscribe_waits_for_lazy_socket(self):
		out = self._run_scenario("subscribe_waits_for_socket")
		self.assertIn("OK subscribe_waits_for_socket", out)

	def test_lazy_connect_registers_single_listener(self):
		out = self._run_scenario("lazy_connect_registers_listener")
		self.assertIn("OK lazy_connect_registers_listener", out)

	def test_lazy_reconnect_does_not_duplicate_listeners(self):
		out = self._run_scenario("lazy_reconnect_no_duplicate_listeners")
		self.assertIn("OK lazy_reconnect_no_duplicate_listeners", out)

	def test_socket_replacement_rebinds_listeners(self):
		out = self._run_scenario("socket_replacement_rebinds_listeners")
		self.assertIn("OK socket_replacement_rebinds_listeners", out)

	def test_registration_error_keeps_retrying(self):
		out = self._run_scenario("registration_error_keeps_retrying")
		self.assertIn("OK registration_error_keeps_retrying", out)


if __name__ == "__main__":
	unittest.main()