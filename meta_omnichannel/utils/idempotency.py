"""Stable idempotency key helpers for outbound connector sends."""

from __future__ import annotations

import hashlib
import re


_SAFE = re.compile(r"[^a-zA-Z0-9._:-]+")


def build_outbound_idempotency_key(
	*,
	conversation_name: str,
	text: str,
	client_nonce: str,
) -> str:
	"""Build a stable idempotency key from conversation + content + client nonce.

	The client nonce should be generated once per user send action and reused on retry.
	"""
	conversation_name = (conversation_name or "").strip()
	client_nonce = (client_nonce or "").strip()
	if not conversation_name or not client_nonce:
		raise ValueError("conversation_name and client_nonce are required")

	digest = hashlib.sha256(f"{conversation_name}\n{text}\n{client_nonce}".encode()).hexdigest()[:32]
	raw = f"oc:{conversation_name}:{client_nonce}:{digest}"
	return _SAFE.sub("-", raw)[:140]


def build_outbound_image_idempotency_key(
	*,
	conversation_name: str,
	client_nonce: str,
	content_sha256: str | None = None,
) -> str:
	"""DB reservation key for one outbound image nonce (conversation + nonce only).

	Content SHA-256 is intentionally **not** part of the unique key. The message row
	stores the hash in ``raw_payload_hash`` so same-nonce / different-image conflicts
	are detected after a unique-key collision, including when Redis is unavailable.

	``content_sha256`` is accepted for call-site compatibility and ignored for keying.
	"""
	conversation_name = (conversation_name or "").strip()
	client_nonce = (client_nonce or "").strip()
	if not conversation_name or not client_nonce:
		raise ValueError("conversation_name and client_nonce are required")
	# content_sha256 reserved for callers; must not affect uniqueness.
	_ = content_sha256
	raw = f"oc-img:{conversation_name}:{client_nonce}"
	return _SAFE.sub("-", raw)[:140]


def outbound_image_nonce_key_prefix(*, conversation_name: str, client_nonce: str) -> str:
	"""Exact nonce-scoped key prefix (equals the reservation key for current format)."""
	return build_outbound_image_idempotency_key(
		conversation_name=conversation_name,
		client_nonce=client_nonce,
	)
