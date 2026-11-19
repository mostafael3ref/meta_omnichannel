"""Pure Python mirror of Inbox ``safeHttpsUrl`` (offline unit tests)."""

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
