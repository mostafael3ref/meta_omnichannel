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
