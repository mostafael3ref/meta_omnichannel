"""HMAC authentication for Connector → Frappe inbound notifications.

Separate from Meta ``X-Hub-Signature-256`` (see ``webhook.py`` stub). This module
authenticates the multi-tenant connector calling a customer Frappe site.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Final

SIGNATURE_HEADER = "X-Omnichannel-Signature"
TIMESTAMP_HEADER = "X-Omnichannel-Timestamp"
SIGNATURE_PREFIX = "v1="
# Reject notifications whose timestamp skew exceeds this window.
DEFAULT_MAX_SKEW_SEC: Final[int] = 300


def build_string_to_sign(*, timestamp: str, body: bytes) -> bytes:
	"""Return the exact bytes signed by the Connector (timestamp + '.' + body)."""
	ts = (timestamp or "").strip().encode("utf-8")
	return ts + b"." + (body or b"")


def compute_signature_hex(*, secret: str, timestamp: str, body: bytes) -> str:
	"""HMAC-SHA256 hex digest over ``timestamp.body``."""
	mac = hmac.new(
		(secret or "").encode("utf-8"),
		build_string_to_sign(timestamp=timestamp, body=body),
		hashlib.sha256,
	)
	return mac.hexdigest()


def format_signature_header(hex_digest: str) -> str:
	return f"{SIGNATURE_PREFIX}{(hex_digest or '').strip().lower()}"


def parse_signature_header(header_value: str | None) -> str | None:
	"""Extract hex digest from ``v1=<hex>``. Returns None when malformed."""
	raw = (header_value or "").strip()
	if not raw.lower().startswith(SIGNATURE_PREFIX):
		return None
	digest = raw[len(SIGNATURE_PREFIX) :].strip().lower()
	if not digest or any(c not in "0123456789abcdef" for c in digest):
		return None
	if len(digest) != 64:
		return None
	return digest


def signatures_match(expected_hex: str, provided_hex: str) -> bool:
	"""Constant-time compare of hex digests."""
	a = (expected_hex or "").strip().lower().encode("utf-8")
	b = (provided_hex or "").strip().lower().encode("utf-8")
	if len(a) != len(b):
		return False
	return hmac.compare_digest(a, b)


def timestamp_is_fresh(
	timestamp: str | int | float | None,
	*,
	now: int | None = None,
	max_skew_sec: int = DEFAULT_MAX_SKEW_SEC,
) -> bool:
	"""Return whether ``timestamp`` is within ``max_skew_sec`` of ``now`` (UTC unix)."""
	if timestamp is None or timestamp == "":
		return False
	try:
		ts = int(timestamp)
	except (TypeError, ValueError):
		return False
	# Reject non-positive / absurd far-future values before skew math.
	if ts <= 0:
		return False
	current = int(now if now is not None else time.time())
	return abs(current - ts) <= max(1, int(max_skew_sec))


def verify_request_signature(
	*,
	secret: str,
	timestamp: str | None,
	signature_header: str | None,
	body: bytes,
	now: int | None = None,
	max_skew_sec: int = DEFAULT_MAX_SKEW_SEC,
) -> tuple[bool, str]:
	"""Validate freshness + HMAC. Returns ``(ok, reason_code)``.

	``reason_code`` is a stable machine token for logs/tests (never includes the secret).
	"""
	if not (secret or "").strip():
		return False, "secret_missing"
	if not timestamp_is_fresh(timestamp, now=now, max_skew_sec=max_skew_sec):
		return False, "timestamp_stale_or_invalid"
	provided = parse_signature_header(signature_header)
	if not provided:
		return False, "signature_malformed"
	expected = compute_signature_hex(
		secret=secret, timestamp=str(timestamp).strip(), body=body or b""
	)
	if not signatures_match(expected, provided):
		return False, "signature_mismatch"
	return True, "ok"
