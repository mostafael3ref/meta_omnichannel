"""Pure Python mirror of Inbox attachment URL gates (offline unit tests)."""

from __future__ import annotations

from urllib.parse import urlparse


def safe_https_url(raw: object) -> str | None:
	"""Accept https URLs only — never javascript:/data:/http: or malformed."""
	s = str(raw or "").strip()
	if not s:
		return None
	try:
		parsed = urlparse(s)
	except Exception:
		return None
	if parsed.scheme.lower() != "https":
		return None
	if not parsed.netloc:
		return None
	# Preserve the original string when it already parses as https (match URL.href intent).
	return s


def safe_private_file_path(raw: object) -> str | None:
	"""Trusted same-origin ``/private/files/...`` only."""
	s = str(raw or "").strip()
	if not s.startswith("/private/files/"):
		return None
	if s.startswith("//") or ".." in s or "\\" in s or "\0" in s:
		return None
	if any(ch.isspace() for ch in s):
		return None
	return s


def safe_attachment_url(raw: object) -> str | None:
	"""Private File path or https CDN — reject untrusted schemes/paths."""
	s = str(raw or "").strip()
	if not s:
		return None
	if s.startswith("/"):
		return safe_private_file_path(s)
	return safe_https_url(s)


def preferred_attachment_url(
	*, attachment_file_url: object = None, attachment_url: object = None
) -> str | None:
	"""Prefer Ready local File URL; fall back to Meta HTTPS URL."""
	return safe_attachment_url(attachment_file_url) or safe_attachment_url(attachment_url)


def attachment_download_href(url: object) -> str | None:
	"""Mirror Inbox download link builder.

	Private Files use Frappe ``frappe.handler.download_file`` (authorized,
	``Content-Disposition: attachment``). External HTTPS URLs stay as-is —
	cross-origin ``download`` is not guaranteed.
	"""
	from urllib.parse import quote

	private = safe_private_file_path(url)
	if private:
		return f"/api/method/frappe.handler.download_file?file_url={quote(private, safe='')}"
	return safe_https_url(url)
