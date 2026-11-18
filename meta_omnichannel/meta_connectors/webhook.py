"""Webhook verification helpers (stubs).

Signature validation against Meta ``X-Hub-Signature-256`` (or equivalent)
will be implemented here. No verification logic is present yet.
"""

from __future__ import annotations


def verify_webhook_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:  # noqa: ARG001
	"""Return whether a webhook payload signature is valid.

	Raises:
		NotImplementedError: Always, until crypto verification is implemented.
	"""
	raise NotImplementedError(
		"verify_webhook_signature is not implemented in the app skeleton"
	)
